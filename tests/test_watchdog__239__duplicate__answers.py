"""BUG-239: one OptionalAction must not be answered three times in six seconds.

Evidence (2026-10-24 10:42-10:43, match 18975f2b-2685-43e3-9026-40200c6be761,
``~/.arenamcp/standalone.log`` lines 716400-716560):

    10:42:20.356 Bridge detected decision: Optional Action (type=OptionalAction actions=0)
    10:42:31.712 Autopilot manual required: OptionalAction no safe automatic choice
    10:43:12.341 [WATCHDOG] Optional Action pending 52.0s ... 20s left ... submitting a safe default
    10:43:12.383 GRE bridge autorespond OptionalActionMessageRequest
    10:43:12.472 Bridge detected decision cleared
    10:43:12.533 Bridge detected decision Optional Action again
    10:43:17.043 [WATCHDOG] Optional Action pending 4.5s ... 15s left ... submitting a safe default
    10:43:18.368 [WATCHDOG] Optional Action pending 1.1s ... 14s left ... submitting a safe default
    10:43:20.253 [gre-aware] typed-decision OptionalAction Accept the optional effect ...
    10:43:20.371 Bridge detected decision cleared

Arena re-offers an unanswered prompt as a new GRE message, so ``msg_id`` (and
usually ``game_state_id``) change while the question does not. ``request_key``
therefore saw three new requests, and the urgent-clock branch answered each
one; none of the three AutoResponds resolved the prompt, and the model's own
answer at 10:43:20 is what cleared it.

These are deterministic reproductions of that mechanism on current master, not
live-service latency measurements.
"""

from __future__ import annotations

import logging
import time

from tests.test_autopilot_search_stall import kodama_search
from tests.test_decision_watchdog import Clock, YieldingAutopilot
from tests.test_standalone_coaching_loop import make_loop_coach, make_state, run_loop

from arenamcp.decision_watchdog import DecisionWatchdog, prompt_key, request_key
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response

SEARCH = kodama_search(revealed=False)


def optional_action(msg_id: int, game_state_id: int = 165) -> dict:
    """The OptionalAction of 10:42:20, re-issued by Arena with a fresh message id."""
    return {
        "ok": True,
        "has_pending": True,
        "request_type": "OptionalAction",
        "request_class": "OptionalActionMessageRequest",
        "game_state_id": game_state_id,
        "msg_id": msg_id,
        "can_pass": False,
        "source_instance_id": 14612,
        "prompt": {"promptId": 144, "parameters": [{"parameterName": "CardId", "numberValue": 654}]},
    }


# --- the identity Arena's re-issue churns ----------------------------------------


def test_the_reissued_prompt_keeps_its_identity_while_the_message_ids_change():
    first, second = optional_action(222), optional_action(223, game_state_id=166)
    assert request_key(first) != request_key(second)
    assert prompt_key(first) == prompt_key(second)


def test_a_different_question_is_a_different_prompt():
    assert prompt_key(optional_action(222)) != prompt_key({**SEARCH, "msg_id": 222})
    assert prompt_key(kodama_search(revealed=True)) != prompt_key(kodama_search(revealed=False))
    # A second OptionalAction from another card is a new question, not our answer coming back.
    other_card = {**optional_action(222), "source_instance_id": 14613}
    other_card["prompt"] = {"promptId": 144, "parameters": [{"parameterName": "CardId", "numberValue": 655}]}
    assert prompt_key(other_card) != prompt_key(optional_action(222))


def test_a_second_optional_action_from_another_card_is_still_answered():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(optional_action(222), clock_remaining_s=20.0).action == "safe_default"
    watchdog.note_safe_default(True)
    clock.now += 1.0
    other_card = {**optional_action(223), "source_instance_id": 14613}
    other_card["prompt"] = {"promptId": 144, "parameters": [{"parameterName": "CardId", "numberValue": 655}]}
    assert watchdog.step(other_card, clock_remaining_s=19.0).action == "safe_default"


# --- the watchdog answers once ---------------------------------------------------


def test_the_reissued_optional_action_is_not_answered_three_times():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    # 10:43:12.341 - 52 s unanswered, 20 s left on the rope.
    assert watchdog.step(optional_action(222), clock_remaining_s=20.0).action == "safe_default"
    watchdog.note_safe_default(True)
    # 10:43:12.533 - Arena re-offers it as msgId 223; our AutoRespond did not take.
    clock.now += 0.2
    assert watchdog.step(optional_action(223), clock_remaining_s=19.5) is None
    # 10:43:17.043 - still unanswered by us: say so instead of answering again.
    clock.now += 4.8
    assert watchdog.step(optional_action(224), clock_remaining_s=15.0).action == "alert"


def test_the_answer_record_survives_the_request_clearing():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(optional_action(222), clock_remaining_s=20.0).action == "safe_default"
    watchdog.note_safe_default(True)
    # 10:43:12.472 - Arena clears the prompt for one poll before re-raising it.
    assert watchdog.step({"ok": True, "has_pending": False}) is None
    clock.now += 0.2
    assert watchdog.step(optional_action(223), clock_remaining_s=19.5) is None


def test_a_failed_answer_record_survives_the_request_clearing():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(optional_action(222), clock_remaining_s=20.0).action == "safe_default"
    watchdog.note_safe_default(False)  # nothing legal to submit
    assert watchdog.step({"ok": True, "has_pending": False}) is None
    clock.now += 0.2
    assert watchdog.step(optional_action(223), clock_remaining_s=19.5).action == "alert"


def test_the_guard_expires_so_a_stuck_prompt_still_gets_a_bounded_retry():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(optional_action(222), clock_remaining_s=20.0).action == "safe_default"
    watchdog.note_safe_default(True)
    clock.now += DecisionWatchdog.SAME_PROMPT_GUARD_S + 1
    assert watchdog.step(optional_action(230), clock_remaining_s=9.0).action == "safe_default"


def test_a_different_question_still_gets_its_safe_default():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(optional_action(222), clock_remaining_s=20.0).action == "safe_default"
    watchdog.note_safe_default(True)
    clock.now += 1.0
    search = {**SEARCH, "game_state_id": 166, "msg_id": 224}
    assert watchdog.step(search, clock_remaining_s=19.0).action == "safe_default"


def test_a_new_request_is_still_replanned_and_defaulted():
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    assert watchdog.step(SEARCH, clock_remaining_s=60.0) is None
    clock.now += 12.0
    assert watchdog.step(SEARCH, clock_remaining_s=48.0).action == "replan"
    clock.now += 18.0
    assert watchdog.step(SEARCH, clock_remaining_s=30.0).action == "safe_default"


# --- the coaching loop submits one AutoRespond -----------------------------------


class BouncingOptionalActionPoller:
    """The bridge during the recurrence: connected, the OptionalAction pending."""

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
                "request_type": "OptionalAction",
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


class BouncingOptionalAction(YieldingAutopilot):
    """Arena re-offers the same OptionalAction with a fresh msgId after our AutoRespond."""

    def __init__(self, coach) -> None:
        super().__init__(coach)
        self.msg_id = 222

    def watchdog_safe_default(self, game_state, reason=""):
        self.safe_defaults.append(reason)
        self.msg_id += 1
        self.coach._bridge_poller.resp = optional_action(self.msg_id)
        return True, "auto respond"


def bouncing_coach(clock: Clock, seconds: float):
    state = make_state(match_id="18975f2b", turn=7)
    state["players"] = [{"seat_id": 1, "is_local": True}, {"seat_id": 2}]
    coach = make_loop_coach(state=state, triggers=["decision_required"], repeat_triggers=False)
    coach._bridge_poller = BouncingOptionalActionPoller(optional_action(222))
    coach._autopilot_enabled = True
    coach._autopilot = BouncingOptionalAction(coach)
    coach._decision_watchdog = DecisionWatchdog(monotonic=clock)

    def rope_runs_low():
        # Arena's TimerStateMessage arrives just after the request: 20 s left.
        if coach._bridge_poller.polls >= 1 and "timer_state" not in coach._mcp.state:
            coach._mcp.state["timer_state"] = {
                1: {"time_remaining_ms": 20000, "is_ticking": True, "received_at": time.time()}
            }

    coach._mcp.poll_hooks.append(rope_runs_low)
    stop_at = clock.now + seconds

    def tick():
        clock.now += 2.5  # the loop's slowest poll interval
        if clock.now > stop_at:
            coach._running = False

    coach._mcp.poll_hooks.append(tick)
    return coach


def test_the_coaching_loop_submits_one_answer_and_then_alerts(monkeypatch, caplog):
    clock = Clock()
    coach = bouncing_coach(clock, seconds=20)
    with caplog.at_level(logging.INFO):
        run_loop(monkeypatch, coach, iterations=10**6)  # the clock hook ends it

    autopilot = coach._autopilot
    assert len(autopilot.safe_defaults) == 1
    assert "[WATCHDOG] safe default sent for Optional Action" in caplog.text
    assert caplog.text.count("submitting a safe default") == 1
    assert any("MANUAL REQUIRED" in text and "Optional Action" in text for text in coach.speak_advice_calls)
