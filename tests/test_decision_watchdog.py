"""The decision watchdog: autoplay never lets Arena's clock run out silently.

Evidence (2026-10-18, match f37313e7, Historic Brawl, our seat 1): the Search
raised at 18:35:41 was never answered; Player.log shows our rope running
(TimerStateMessage 18:35:43.437: ActivePlayer 140 s, 13.2 s elapsed;
Inactivity 150 s), the rope's TimeoutMessage at 18:37:50 and the game lost
at 18:38:20 with ResultReason_Timeout. The coach logged nothing in between.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from tests.fakes import TypedBridge, make_engine
from tests.test_autopilot_search_stall import FOREST, kodama_search
from tests.test_standalone_coaching_loop import make_loop_coach, make_state, run_loop

from arenamcp.decision_clock import seconds_remaining
from arenamcp.decision_watchdog import DecisionWatchdog
from arenamcp.decisions import build_pending_decision, search_safe_default
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response

# Verbatim from Player_1792357682.log (lines 123222 and 123233).
TIMER_18_35_43 = {
    "timestamp": "1792373743437",
    "greToClientEvent": {
        "greToClientMessages": [
            {
                "type": "GREMessageType_TimerStateMessage",
                "systemSeatIds": [1, 2],
                "msgId": 222,
                "gameStateId": 165,
                "timerStateMessage": {
                    "seatId": 1,
                    "timers": [
                        {
                            "timerId": 1,
                            "type": "TimerType_Prologue",
                            "durationSec": 120,
                            "behavior": "TimerBehavior_TakeControl",
                            "warningThresholdSec": 30,
                        },
                        {
                            "timerId": 2,
                            "type": "TimerType_Epilogue",
                            "durationSec": 145,
                            "behavior": "TimerBehavior_TakeControl",
                            "warningThresholdSec": 30,
                        },
                        {
                            "timerId": 3,
                            "type": "TimerType_ActivePlayer",
                            "durationSec": 140,
                            "elapsedSec": 13,
                            "running": True,
                            "behavior": "TimerBehavior_TakeControl",
                            "warningThresholdSec": 30,
                            "elapsedMs": 13205,
                        },
                        {
                            "timerId": 4,
                            "type": "TimerType_NonActivePlayer",
                            "durationSec": 45,
                            "behavior": "TimerBehavior_TakeControl",
                            "warningThresholdSec": 30,
                        },
                        {
                            "timerId": 5,
                            "type": "TimerType_Delay",
                            "durationSec": 2,
                            "behavior": "TimerBehavior_StartDelayedTimer",
                        },
                        {
                            "timerId": 6,
                            "type": "TimerType_Inactivity",
                            "durationSec": 150,
                            "running": True,
                            "behavior": "TimerBehavior_Timeout",
                            "warningThresholdSec": 30,
                        },
                    ],
                },
            }
        ]
    },
}
TIMEOUT_18_37_50 = {
    "timestamp": "1792373870232",
    "greToClientEvent": {
        "greToClientMessages": [
            {
                "type": "GREMessageType_TimeoutMessage",
                "systemSeatIds": [1, 2],
                "timeoutMessage": {
                    "seatId": 1,
                    "timer": {
                        "timerId": 3,
                        "type": "TimerType_ActivePlayer",
                        "durationSec": 170,
                        "elapsedSec": 140,
                        "running": True,
                        "behavior": "TimerBehavior_TakeControl",
                        "warningThresholdSec": 30,
                        "elapsedMs": 140000,
                    },
                },
            }
        ]
    },
}
RECEIVED_18_35_43 = 1792373743.437


# --- Arena's clock, read from Player.log ----------------------------------------


def parsed(*events) -> GameState:
    state = GameState()
    handler = create_game_state_handler(state)
    for event in events:
        handler(event)
    return state


def test_the_real_timer_message_reads_our_seats_rope():
    state = parsed(TIMER_18_35_43)
    entry = state.timer_state[1]
    assert entry["timer_type"] == "TimerType_ActivePlayer"
    assert entry["is_ticking"] is True
    assert entry["time_remaining_ms"] == 126795  # 140 s - 13.205 s
    assert entry["received_at"] == pytest.approx(RECEIVED_18_35_43)
    # 18:37:30 -> about 7 s before the rope ran out at 18:37:50.
    assert seconds_remaining(state.timer_state, 1, now=RECEIVED_18_35_43 + 120) == pytest.approx(
        6.8, abs=0.01
    )


def test_a_reading_from_an_earlier_window_is_not_this_requests_clock():
    state = parsed(TIMER_18_35_43)
    assert (
        seconds_remaining(state.timer_state, 1, now=RECEIVED_18_35_43 + 60, not_before=RECEIVED_18_35_43 + 5)
        is None
    )
    assert (
        seconds_remaining(state.timer_state, 2, now=RECEIVED_18_35_43 + 1) is None
    )  # nothing for the opponent


def test_the_ropes_timeout_message_leaves_no_time():
    state = parsed(TIMER_18_35_43, TIMEOUT_18_37_50)
    assert state.timer_state[1]["timed_out"] is True
    assert seconds_remaining(state.timer_state, 1, now=1792373871.0) <= 0


# --- the escalation ladder (injected clock, no waits) ---------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


SEARCH = kodama_search(revealed=False)


def run_watchdog(watchdog, clock, seconds, *, step=1.0, **kwargs):
    """Step the watchdog every ``step`` seconds for ``seconds``; return [(t, action)]."""
    actions = []
    start = clock.now
    while clock.now - start <= seconds:
        result = watchdog.step(SEARCH, **kwargs)
        if result is not None:
            actions.append((round(clock.now - start, 1), result.action))
        clock.now += step
    return actions


def test_an_unanswered_request_is_replanned_then_given_a_safe_default():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert run_watchdog(watchdog, clock, 31) == [(12.0, "replan"), (30.0, "safe_default")]


def test_a_failed_safe_default_alerts_at_once_and_keeps_alerting():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    run_watchdog(watchdog, clock, 30)
    watchdog.note_safe_default(False)
    assert watchdog.step(SEARCH).action == "alert"
    assert run_watchdog(watchdog, clock, 31) == [(15.0, "alert"), (30.0, "alert")]


def test_a_safe_default_that_does_not_take_is_alerted():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    run_watchdog(watchdog, clock, 30)
    watchdog.note_safe_default(True)
    assert run_watchdog(watchdog, clock, 5) == [(3.0, "alert")]  # still the same request 4 s later


def test_a_normally_answered_request_never_wakes_the_watchdog():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert run_watchdog(watchdog, clock, 8) == []
    answered = {**SEARCH, "game_state_id": 166, "msg_id": 224}  # the next request
    for _ in range(9):
        assert watchdog.step(answered) is None
        clock.now += 1.0
    assert watchdog.step({"ok": True, "has_pending": False}) is None
    assert run_watchdog(watchdog, clock, 11) == []  # the count starts over


def test_our_own_answer_restarts_the_count():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert run_watchdog(watchdog, clock, 10) == []
    answered_at = clock.now
    assert run_watchdog(watchdog, clock, 11, last_answer_at=answered_at) == []
    assert run_watchdog(watchdog, clock, 2, last_answer_at=answered_at) == [(0.0, "replan")]


def test_a_nearly_expired_clock_skips_straight_to_the_safe_default():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(SEARCH, clock_remaining_s=60.0) is None
    clock.now += 2.0
    assert watchdog.step(SEARCH, clock_remaining_s=18.0).action == "safe_default"


def test_a_window_handed_to_the_player_waits_for_the_clock_margin():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert run_watchdog(watchdog, clock, 60, handed_to_user=True, clock_remaining_s=40.0) == []
    assert watchdog.step(SEARCH, handed_to_user=True, clock_remaining_s=19.0).action == "safe_default"


def test_a_handed_window_with_no_clock_is_answered_well_before_the_inactivity_loss():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    actions = run_watchdog(watchdog, clock, 91, handed_to_user=True)
    assert actions == [(90.0, "safe_default")]
    assert actions[0][0] < 150  # TimerType_Inactivity, TimerBehavior_Timeout


# --- safe defaults ---------------------------------------------------------------


@pytest.fixture
def forest_names(monkeypatch):
    monkeypatch.setattr(
        "arenamcp.server.get_card_info", lambda grp: {"name": "Forest"} if grp == FOREST else {}
    )


def test_search_safe_default_takes_an_offered_card(forest_names):
    assert search_safe_default(build_pending_decision(kodama_search(revealed=True))) == ["sel:314"]
    # Unreadable names still beat losing on time.
    assert search_safe_default(build_pending_decision(kodama_search(revealed=False))) == ["sel:314"]


def test_search_safe_default_prefers_distinct_known_cards_up_to_the_limit(monkeypatch):
    names = {1: "Forest", 2: "Island"}
    monkeypatch.setattr(
        "arenamcp.server.get_card_info", lambda grp: {"name": names[grp]} if grp in names else {}
    )
    poll = {
        **kodama_search(revealed=True),
        "select_n_max": 2,
        "search_candidates": [
            {"instanceId": 10, "grpId": 1},
            {"instanceId": 11, "grpId": 1},
            {"instanceId": 12, "grpId": 2},
        ],
    }
    assert search_safe_default(build_pending_decision(poll)) == ["sel:10", "sel:12"]
    assert search_safe_default(build_pending_decision({**poll, "search_candidates": []})) == []


def test_the_engine_answers_the_stalled_search_with_a_forest(forest_names):
    bridge = TypedBridge(kodama_search(revealed=True))
    engine = make_engine(bridge)
    submitted, detail = engine.watchdog_safe_default({"turn": {"turn_number": 7}}, reason="test")
    assert submitted is True
    assert detail == "chose Forest"
    assert bridge.submitted == [("selection", [314])]


def test_the_engine_passes_an_unanswered_priority_window():
    poll = {
        "ok": True,
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [],
    }
    bridge = TypedBridge(poll)
    engine = make_engine(bridge)
    assert engine.watchdog_safe_default({"turn": {"turn_number": 7}}) == (True, "passed priority")
    assert bridge.submitted == [("pass",)]


def test_the_engine_never_races_a_plan_in_progress():
    bridge = TypedBridge(kodama_search(revealed=True))
    engine = make_engine(bridge)
    holder = threading.Thread(target=engine._acquire_lock)
    holder.start()
    holder.join()
    assert engine.watchdog_safe_default({}) == (False, "autopilot busy")
    assert bridge.submitted == []


# --- in the coaching loop: all triggers quiet, the bridge shows the Search -------


class SearchPoller:
    """The bridge poller during the stall: connected, the Search pending, no change after the first poll."""

    connected = True
    client_runtime = "il2cpp-macos"

    def __init__(self, resp: dict) -> None:
        self.resp = resp
        self.polls = 0
        self._bridge = type("Bridge", (), {"last_submission_at": 0.0})()

    def poll(self):
        self.polls += 1
        if self.polls == 1:
            return {
                "trigger": "decision_required",
                "request_type": "Search",
                "has_pending": True,
                "actions": [],
            }
        return None

    def latest_pending(self):
        return self.resp

    def reset(self) -> None:
        pass

    def enrich_snapshot(self, snapshot: dict) -> None:
        enrich_snapshot_from_pending_response(snapshot, self.resp, bridge_connected=True)


class YieldingAutopilot:
    """Waits for "Arena to reveal the search choices" every time it is asked (the 18:35:41 behavior)."""

    requires_desktop_poll = False
    state = None

    def __init__(self, coach) -> None:
        self.coach = coach
        self.triggers: list[str] = []
        self.safe_defaults: list[str] = []
        self._config = type("Config", (), {"dry_run": False, "land_drop_mode": False})()

    def is_window_given_up(self, _state) -> bool:
        return False

    def in_manual_play_cooldown(self) -> bool:
        return False

    def process_trigger(self, snapshot, trigger) -> bool:
        self.triggers.append(trigger)
        return True

    def watchdog_safe_default(self, game_state, reason=""):
        self.safe_defaults.append(reason)
        self.coach._bridge_poller.resp = {"ok": True, "has_pending": False}
        return True, "chose Forest"


class NoBridge:
    """get_bridge() for the loop's concede/oops/auto-queue probes: no client, no keepalive thread."""

    connected = False
    concede_supported = False
    emote_supported = False

    def connect(self) -> bool:
        return False


@pytest.fixture
def server_game_state(monkeypatch):
    from tests.test_standalone_coaching_loop import FakeServerGameState

    import arenamcp.server as server_mod

    fake = FakeServerGameState()
    monkeypatch.setattr(server_mod, "game_state", fake)
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: NoBridge())
    yield fake
    fake.game_ended_event.clear()


def stalled_search_coach(clock: Clock, seconds: float):
    """The 18:35:41 stall: log pending None, one trigger, then quiet; the loop runs ``seconds`` of clock."""
    state = make_state(match_id="f37313e7", turn=7)
    state["players"] = [{"seat_id": 1, "is_local": True}, {"seat_id": 2}]
    coach = make_loop_coach(
        state=state, triggers=["decision_required", "stack_spell_yours"], repeat_triggers=False
    )
    coach._bridge_poller = SearchPoller(kodama_search(revealed=False))
    coach._autopilot_enabled = True
    coach._autopilot = YieldingAutopilot(coach)
    coach._decision_watchdog = DecisionWatchdog(monotonic=clock)
    stop_at = clock.now + seconds

    def tick():
        clock.now += 2.5  # the loop's slowest poll interval
        if clock.now > stop_at:
            coach._running = False

    coach._mcp.poll_hooks.append(tick)
    return coach


def run(monkeypatch, coach) -> None:
    run_loop(monkeypatch, coach, iterations=10**6)  # the clock hook ends it


def test_the_stalled_search_is_answered_with_every_trigger_quiet(monkeypatch, server_game_state, caplog):
    clock = Clock()
    coach = stalled_search_coach(clock, seconds=40)
    with caplog.at_level(logging.INFO):
        run(monkeypatch, coach)
    autopilot = coach._autopilot
    # The trigger of 18:35:41, then the backstop although the log shows no
    # pending decision, then the watchdog's forced re-plan at 12 s.
    assert autopilot.triggers[:3] == ["decision_required"] * 3
    assert "Autopilot backstop: forced decision_required for 'Search Library'" in caplog.text
    assert "[WATCHDOG] Search Library pending" in caplog.text and "forcing a re-plan" in caplog.text
    # ...and a safe default well before Arena's 150 s inactivity loss.
    assert len(autopilot.safe_defaults) == 1
    assert "safe default sent for Search Library: chose Forest" in caplog.text
    assert any("Search Library" in text for text in coach.speak_advice_calls)
    assert "Coaching loop error" not in caplog.text


def test_a_safe_default_lands_before_a_nearly_spent_clock(monkeypatch, server_game_state, caplog):
    clock = Clock()
    coach = stalled_search_coach(clock, seconds=8)  # long before the 30 s age limit

    def rope_runs_low():
        # Arena's TimerStateMessage arrives just after the request: 12 s left.
        if coach._bridge_poller.polls >= 1 and "timer_state" not in coach._mcp.state:
            coach._mcp.state["timer_state"] = {
                1: {"time_remaining_ms": 12000, "is_ticking": True, "received_at": time.time(), "running": []}
            }

    coach._mcp.poll_hooks.append(rope_runs_low)
    with caplog.at_level(logging.INFO):
        run(monkeypatch, coach)
    assert len(coach._autopilot.safe_defaults) == 1
    assert "left on Arena's clock" in coach._autopilot.safe_defaults[0]
    assert "Coaching loop error" not in caplog.text


def test_no_watchdog_action_when_the_autopilot_answers(monkeypatch, server_game_state, caplog):
    clock = Clock()
    coach = stalled_search_coach(clock, seconds=40)

    def answer(snapshot, trigger):
        coach._autopilot.triggers.append(trigger)
        coach._bridge_poller.resp = {"ok": True, "has_pending": False}
        return True

    coach._autopilot.process_trigger = answer
    with caplog.at_level(logging.INFO):
        run(monkeypatch, coach)
    assert coach._autopilot.triggers == ["decision_required"]
    assert coach._autopilot.safe_defaults == []
    assert "[WATCHDOG]" not in caplog.text


def test_no_watchdog_while_autoplay_is_off(monkeypatch, server_game_state, caplog):
    clock = Clock()
    coach = stalled_search_coach(clock, seconds=40)
    coach._autopilot_enabled = False
    with caplog.at_level(logging.INFO):
        run(monkeypatch, coach)
    assert coach._autopilot.safe_defaults == []
    assert "[WATCHDOG]" not in caplog.text


def test_no_safe_default_alerts_loudly(monkeypatch, server_game_state, caplog):
    clock = Clock()
    coach = stalled_search_coach(clock, seconds=40)
    coach._autopilot.watchdog_safe_default = lambda state, reason="": (
        False,
        "no safe default for this request",
    )
    with caplog.at_level(logging.INFO):
        run(monkeypatch, coach)
    spoken = [text for text in coach.speak_advice_calls if text.startswith("MANUAL REQUIRED")]
    assert spoken and "Search Library" in spoken[0]
    assert "essential" in coach.speech_categories
    assert any(text.startswith("MANUAL REQUIRED") for text, _ in coach.ui.advice_calls)


@pytest.mark.parametrize("kind", ["DeclareAttackers", "DeclareBlockers"])
def test_combat_request_id_churn_does_not_renew_unanswered_deadline(kind):
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    window = ("match", 1, 12, 2, "Combat", kind, ())
    actions = []
    for elapsed in range(32):
        poll = {
            "has_pending": True,
            "request_type": kind,
            "game_state_id": 320 + elapsed,
            "msg_id": 400 + elapsed,
        }
        if elapsed == 12:
            assert watchdog.may_act(poll, window=window)
        step = watchdog.step(poll, window=window)
        if step:
            actions.append((elapsed, step.action))
        clock.now += 1
    assert actions == [(12, "replan"), (30, "safe_default")]


def test_combat_watchdog_resets_for_a_new_turn_and_respects_manual_handoff():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    poll = {"has_pending": True, "request_type": "DeclareBlockers", "game_state_id": 320}
    window = ("match", 1, 12, 2, "Combat", "DeclareBlock", ())
    assert watchdog.step(poll, window=window) is None
    clock.now += 31
    assert watchdog.may_act(dict(poll, game_state_id=323), window=window)
    assert watchdog.step(poll, window=window, handed_to_user=True) is None
    new_window = ("match", 1, 14, 2, "Combat", "DeclareBlock", ())
    assert watchdog.step(poll, window=new_window) is None
    clock.now += 11
    assert watchdog.step(poll, window=new_window) is None


def test_combat_answer_still_restarts_watchdog_budget_despite_recapture():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    poll = {"has_pending": True, "request_type": "DeclareBlockers", "game_state_id": 320}
    window = ("match", 1, 12, 2, "Combat", "DeclareBlock", ())
    watchdog.step(poll, window=window)
    clock.now += 11
    answered = clock.now
    clock.now += 2
    assert watchdog.step(dict(poll, game_state_id=323), window=window, last_answer_at=answered) is None
