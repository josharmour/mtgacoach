"""The standalone coaching loop's match boundaries and autopilot re-polling.

A bare StandaloneCoach (``__new__`` plus the attributes the loop reads) runs
``_coaching_loop`` for a fixed number of iterations against fake log, trigger
and bridge sources. No Qt, no subprocess, no network.

The loop detects three boundaries: the parser's game-end event, a match id
change and a turn-number drop. A new match or game bumps ``_match_number``,
clears the advice history and the deck strategy; a game end alone stages the
post-match analysis and leaves them for the next boundary.
"""

from __future__ import annotations

import threading

import pytest

from arenamcp import standalone
from arenamcp.standalone_tempo import _TempoTracker


class StubUI:
    """UI adapter without auto-created attributes, so the loop's emit_* pipe hooks stay inert."""

    def __init__(self) -> None:
        self.logs: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        self.advice_calls: list[tuple[str, str]] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def status(self, key: str, value: str) -> None:
        self.statuses.append((key, value))

    def advice(self, text: str, seat_info: str) -> None:
        self.advice_calls.append((text, seat_info))

    def error(self, message: str) -> None:
        self.logs.append(f"ERROR: {message}")


class FakeMcp:
    def __init__(self, state: dict) -> None:
        self.state = state
        self.poll_hooks: list = []

    def poll_log(self) -> None:
        for hook in list(self.poll_hooks):
            hook()

    def get_game_state(self) -> dict:
        return dict(self.state)

    def get_draft_pack(self) -> dict:
        return {"is_active": False}

    def clear_pending_combat_steps(self) -> None:
        pass


class FakeTrigger:
    def __init__(self, triggers: list[str], repeat: bool = True) -> None:
        self._triggers = list(triggers)
        self._repeat = repeat

    def check_triggers(self, prev_state: dict, curr_state: dict) -> list[str]:
        fired = list(self._triggers)
        if not self._repeat:
            self._triggers = []
        return fired

    def _has_castable_instants(self, state: dict) -> bool:
        return False


class FakeBridgePoller:
    connected = False

    def poll(self):
        return None

    def reset(self) -> None:
        pass

    def enrich_snapshot(self, snapshot: dict) -> None:
        pass


class FakeCoachEngine:
    def __init__(self) -> None:
        self.calls: list = []
        self._deck_strategy = "Go wide, then pump."
        self.deck_strategy_clears = 0

    def get_advice(self, game_state, trigger=None, style=None, **kwargs) -> str:
        self.calls.append(trigger)
        return "Cast your Lightning Bolt."

    def clear_deck_strategy(self) -> None:
        self.deck_strategy_clears += 1
        self._deck_strategy = None


class FakeSettings:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, save: bool = True) -> None:
        self.data[key] = value


class FakeServerGameState:
    """Stand-in for arenamcp.server.game_state: no game-end evidence unless the event is set."""

    def __init__(self) -> None:
        self.game_ended_event = threading.Event()
        self.last_game_result = None
        self._pre_reset_snapshot = None
        self.consumed = 0

    def consume_game_end(self):
        self.consumed += 1
        self.game_ended_event.clear()
        return "loss", None


@pytest.fixture
def server_game_state(monkeypatch):
    import arenamcp.server as server_mod

    fake = FakeServerGameState()
    monkeypatch.setattr(server_mod, "game_state", fake)
    yield fake
    fake.game_ended_event.clear()


def make_state(match_id="m1", turn=5, pending=None, decision_type=None) -> dict:
    state = {
        "match_id": match_id,
        "turn": {
            "turn_number": turn,
            "phase": "Phase_Main1",
            "step": "",
            "active_player": 1,
            "priority_player": 1,
        },
        "players": [],
        "battlefield": [],
        "hand": [],
        "stack": [],
        "legal_actions": [],
    }
    if pending is not None:
        state["pending_decision"] = pending
        state["decision_context"] = {"type": decision_type or "target_selection"}
    return state


def make_loop_coach(
    state: dict | None = None,
    triggers: list[str] | None = None,
    repeat_triggers: bool = True,
) -> standalone.StandaloneCoach:
    """Bare StandaloneCoach via __new__ plus the attributes the coaching loop reads."""
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = StubUI()
    coach._mcp = FakeMcp(state or make_state())
    coach._normalize_turn_snapshot = lambda s: s
    coach._voice_output = None
    coach._running = True
    coach.draft_mode = False
    coach.set_code = None
    coach._trigger = FakeTrigger(triggers or [], repeat=repeat_triggers)
    coach._bridge_poller = FakeBridgePoller()
    coach._last_bridge_ui_status = False
    coach._match_number = 0
    coach._advice_history = []
    coach._game_end_handled = False
    coach._match_boundary_ts = 0.0
    coach._recent_gre_log = []
    coach._recent_gre_log_max = 30
    coach._vlm_card_cache = {}
    coach._vlm_card_failures = set()
    coach._missed_decisions = []
    coach._tempo_tracker = _TempoTracker()
    coach._autopilot_enabled = False
    coach._autopilot = None
    coach._auto_deck_strategy = False
    coach._deck_analyzed = False
    coach.advice_style = "quick"
    coach.settings = FakeSettings()
    coach.advice_frequency = "every_priority"
    coach._coach = FakeCoachEngine()
    coach._last_advised_decision_sig = None
    coach._last_forced_decision_sig = None
    coach._last_forced_decision_ts = 0.0
    coach._win_plan_turn = 0
    coach._pending_win_plan = None
    coach._pending_win_plan_turn = 0
    coach._pending_win_plan_turns = 0
    coach._last_backend_status = ""
    coach._backend_failed = False
    coach._backend_name = "online"
    coach._original_backend = None
    coach._original_model = None
    coach.speak_advice_calls: list[str] = []
    coach.speak_advice = lambda text, blocking=True: coach.speak_advice_calls.append(text)  # type: ignore[method-assign]
    coach._record_advice = lambda *args, **kwargs: None  # type: ignore[method-assign]
    coach._inject_library_summary_if_needed = lambda s: None  # type: ignore[method-assign]
    coach._has_actionable_priority_window = lambda s: False  # type: ignore[method-assign]
    coach._is_meaningful_advice_window = lambda *a, **k: True  # type: ignore[method-assign]
    coach.staged_analyses: list[dict] = []
    coach._stage_post_match_analysis = lambda **kwargs: coach.staged_analyses.append(kwargs) or True  # type: ignore[method-assign]
    coach._get_latest_replay_path = lambda: None  # type: ignore[method-assign]
    return coach


def run_loop(monkeypatch, coach, iterations: int) -> None:
    """Run _coaching_loop for exactly `iterations` iterations (the trailing sleep ends each one)."""
    sleeps = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] >= iterations:
            coach._running = False

    monkeypatch.setattr(standalone.time, "sleep", fake_sleep)
    coach._coaching_loop()


def on_poll(coach, n: int, change) -> None:
    """Apply `change` to the fake log state from the n-th poll on (the top of iteration n)."""
    polls = {"n": 0}

    def hook():
        polls["n"] += 1
        if polls["n"] >= n:
            change(coach._mcp.state)

    coach._mcp.poll_hooks.append(hook)


def test_game_end_event_stages_the_match_but_does_not_start_a_new_one(monkeypatch, server_game_state):
    coach = make_loop_coach(triggers=[])
    coach._advice_history = [{"advice": "earlier advice"}]

    # Set the event on iteration 2, after iteration 1 saw the match id: on the
    # first iteration a set event is a stale one from the log catch-up.
    drafts = {"n": 0}
    original_draft = coach._mcp.get_draft_pack

    def draft_hook():
        drafts["n"] += 1
        if drafts["n"] >= 2:
            server_game_state.game_ended_event.set()
        return original_draft()

    coach._mcp.get_draft_pack = draft_hook  # type: ignore[method-assign]

    run_loop(monkeypatch, coach, iterations=2)

    assert server_game_state.consumed == 1
    assert coach._game_end_handled is True
    assert [(s["match_result"], s["reason"]) for s in coach.staged_analyses] == [("loss", "event-signal")]
    # The next match id or turn drop starts the new match.
    assert coach._match_number == 0
    assert coach._advice_history == [{"advice": "earlier advice"}]
    assert coach._coach.deck_strategy_clears == 0


def test_match_id_change_starts_a_new_match(monkeypatch, server_game_state):
    coach = make_loop_coach(triggers=[])
    coach._advice_history = [{"advice": "m1 advice"}]
    on_poll(coach, 2, lambda state: state.update(match_id="m2"))

    run_loop(monkeypatch, coach, iterations=2)

    # None -> m1 (the first match the loop sees) is not counted; m1 -> m2 is.
    assert coach._match_number == 1
    assert coach._advice_history == []
    assert coach._coach.deck_strategy_clears == 1
    # The game end was missed, so the boundary stages m1's analysis itself.
    assert [s["reason"] for s in coach.staged_analyses] == ["match-boundary"]
    assert coach._game_end_handled is False


def test_turn_drop_starts_a_new_game_without_claiming_the_last_one_ended(monkeypatch, server_game_state):
    coach = make_loop_coach(triggers=["land_played"], repeat_triggers=False)
    coach._advice_history = [{"advice": "game 1 advice"}]
    on_poll(coach, 2, lambda state: state["turn"].update(turn_number=3))

    run_loop(monkeypatch, coach, iterations=2)

    # Iteration 1 advised on turn 5; iteration 2's turn 3 is the drop.
    assert coach.speak_advice_calls == ["Cast your Lightning Bolt."]
    assert coach._match_number == 1
    assert coach._advice_history == []
    assert coach._coach.deck_strategy_clears == 1
    # A turn drop alone can be a relaunch or resync: no post-match analysis.
    assert coach.staged_analyses == []


def test_autopilot_repolls_after_accept_before_handling_search(monkeypatch, server_game_state):
    state = make_state(pending="Optional Action", decision_type="optional")
    state["local_seat_id"] = 1
    coach = make_loop_coach(state=state, triggers=["decision_required", "stack_spell_yours"])
    calls = []

    class Autopilot:
        requires_desktop_poll = False

        def is_window_given_up(self, _state):
            return False

        def process_trigger(self, snapshot, trigger):
            calls.append((snapshot["pending_decision"], trigger))
            # Accepting Worldwagon's optional ETB opens a new search request.
            # The other trigger in this batch still refers to the old prompt.
            coach._mcp.state = {
                **coach._mcp.state,
                "pending_decision": "Search Library",
                "decision_context": {"type": "search", "min": 0, "max": 1},
            }
            return True

    coach._autopilot_enabled = True
    coach._autopilot = Autopilot()

    run_loop(monkeypatch, coach, iterations=2)

    assert calls == [
        ("Optional Action", "decision_required"),
        ("Search Library", "decision_required"),
    ]
    assert coach._coach.calls == []
