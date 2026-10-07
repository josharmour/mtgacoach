"""LLM call discipline: who may call the shared model server, when, and how often.

Evidence (2026-10-06 standalone.log, 1.8 h of play on one vLLM server that the
user also codes against): about half of the server's slot time went to
background calls — 139 game-plan calls, 19 deck-playbook runs for 4-5 decks,
31 win-in-N calls of which none was ever read. With the autopilot driving the
coach still made 14 threat-alert calls and re-ran plan_actions on 15
fall-throughs. During the 18:49-18:54 outage (bug_20261006_185403) every
trigger kept paying for failing calls, the coach stalled 16-21 s per advice
call, and the empty-advice counter restarted the backend into more failures.
"""

from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_deck_strategy import deck_case

from arenamcp import game_plan as game_plan_module
from arenamcp import standalone
from arenamcp.backend_health import LOCAL_FALLBACK_PREFIX, BackendHealth, HealthState
from arenamcp.coach import CoachEngine
from arenamcp.conversation import TURN_ADVICE, ConversationController
from arenamcp.deck_strategy import deck_identity
from arenamcp.game_plan import (
    STRATEGIC_LANE,
    GamePlanManager,
    accepted_call_kwargs,
    is_skipped_call_text,
    llm_available,
)
from arenamcp.standalone_tempo import _TempoTracker
from arenamcp.voice_session import VoiceSession

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_health_and_lane():
    # get_advice starts background plan refreshes; let any still running finish.
    assert STRATEGIC_LANE.wait_idle(5), f"{STRATEGIC_LANE.holder} still holds the strategic lane"
    BackendHealth.reset_instance()
    yield
    BackendHealth.reset_instance()
    assert STRATEGIC_LANE.wait_idle(5), f"a test leaked the strategic lane to {STRATEGIC_LANE.holder}"


class BreakerBackend:
    """A backend with a circuit breaker the test controls."""

    model = "fake-glm"

    def __init__(self, responses=(), *, up: bool = True) -> None:
        self.responses = list(responses)
        self.up = up
        self.calls: list[tuple[str, str, tuple, dict]] = []

    def available(self) -> bool:
        return self.up

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user, args, kwargs))
        return self.responses.pop(0) if self.responses else "Pass."


def _plan_json(path: str = "race for lethal") -> str:
    return json.dumps({"win_conditions": ["beatdown"], "path": path, "develop_next": "a 2-drop"})


def _game(turn: int = 3, *, active: int = 1, phase: str = "Phase_Main1", **extra) -> dict:
    state = {
        "match_id": "m1",
        "local_seat_id": 1,
        "turn": {"turn_number": turn, "active_player": active, "priority_player": active, "phase": phase},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "hand_size": 5},
            {"seat_id": 2, "is_local": False, "life_total": 20, "hand_size": 5},
        ],
        "battlefield": [],
        "hand": [],
        "stack": [],
        "legal_actions": [],
    }
    state.update(extra)
    return state


# ---------------------------------------------------------------------------
# accepted_call_kwargs / breaker probes
# ---------------------------------------------------------------------------


def test_new_keywords_reach_only_backends_that_accept_them():
    class Legacy:
        def complete(self, system, user, max_tokens, temperature=0.3, request_timeout_s=None):
            return ""

    class Modern:
        def complete(self, system, user, *args, call_class=None, priority=None, cancel_event=None):
            return ""

    class Flexible:
        def complete(self, system, user, *args, **kwargs):
            return ""

    labels = {"call_class": "background.game_plan", "priority": 10, "cancel_event": None}
    assert accepted_call_kwargs(Legacy(), **labels) == {}
    assert accepted_call_kwargs(Modern(), **labels) == {"call_class": "background.game_plan", "priority": 10}
    assert accepted_call_kwargs(Flexible(), **labels) == {
        "call_class": "background.game_plan",
        "priority": 10,
    }


def test_a_mock_backend_never_reads_as_breaker_open():
    assert llm_available(Mock()) is True
    assert llm_available(BreakerBackend(up=False)) is False


def test_proxy_skip_and_cancel_sentinels_are_not_failures():
    assert is_skipped_call_text("[BACKEND ERROR] model server unavailable (circuit open; retry in 9s)")
    assert is_skipped_call_text(
        "[BACKEND ERROR] model server unavailable (backend down; background call skipped)"
    )
    assert is_skipped_call_text("[BACKEND ERROR] request cancelled (superseded)")
    assert not is_skipped_call_text("[BACKEND ERROR] Connection error")


# ---------------------------------------------------------------------------
# Coaching loop: no coach model calls while the autopilot drives
# ---------------------------------------------------------------------------


class StubUI:
    def __init__(self) -> None:
        self.logs: list[str] = []
        self.advice_calls: list[tuple[str, str]] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def status(self, key: str, value: str) -> None:
        pass

    def advice(self, text: str, seat_info: str) -> None:
        self.advice_calls.append((text, seat_info))

    def error(self, message: str) -> None:
        self.logs.append(f"ERROR: {message}")


class FakeMcp:
    def __init__(self, state: dict) -> None:
        self.state = state

    def poll_log(self) -> None:
        pass

    def get_game_state(self) -> dict:
        return deepcopy(self.state)

    def get_draft_pack(self) -> dict:
        return {"is_active": False}

    def clear_pending_combat_steps(self) -> None:
        pass


class FakeTrigger:
    def __init__(self, triggers: list[str], threat: dict | None = None) -> None:
        self.triggers = list(triggers)
        if threat is not None:
            self._last_threat = threat

    def check_triggers(self, prev_state, curr_state):
        fired, self.triggers = self.triggers, []
        return fired

    def _has_castable_instants(self, state) -> bool:
        return False


class FakeBridgePoller:
    connected = False

    def poll(self):
        return None

    def reset(self) -> None:
        pass

    def enrich_snapshot(self, snapshot) -> None:
        pass


class FakeSettings:
    def __init__(self, data: dict | None = None) -> None:
        self.data = dict(data or {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value, save: bool = True) -> None:
        self.data[key] = value


class RecordingCoach:
    """Records every model-backed coach entry point."""

    def __init__(self, advice: str = "Attack with everything.") -> None:
        self.model_calls: list[str] = []
        self.advice = advice
        self._deck_strategy = None
        self.on_advice = None

    def get_advice(self, game_state, trigger=None, style=None, **kwargs) -> str:
        self.model_calls.append(f"get_advice:{trigger}")
        if self.on_advice is not None:
            self.on_advice()
        return self.advice

    def generate_win_probability(self, game_state, opp_cards) -> str:
        self.model_calls.append("win_probability")
        return "You are about 10% to win."

    def deterministic_advice(self, game_state, *, trigger=None, threat=None) -> str:
        if threat:
            return f"{threat['name']} is the key threat."
        return "Attack: Yuriko -> Jace (1 damage, their counterattack 0)."

    def clear_deck_strategy(self) -> None:
        pass


class FallThroughAutopilot:
    """An engine that hands every window back to the player."""

    requires_desktop_poll = False

    def __init__(self) -> None:
        self.triggers: list[str] = []
        self._planner = SimpleNamespace(plan_actions=Mock(side_effect=AssertionError("no re-plan")))

    def is_window_given_up(self, state) -> bool:
        return False

    def process_trigger(self, state, trigger) -> bool:
        self.triggers.append(trigger)
        return False

    def get_reusable_advice(self, state):
        return None

    def _get_legal_actions(self, state):
        return []


def _combat_state() -> dict:
    return _game(5, phase="Phase_Combat", step="Step_DeclareAttack")


def make_loop(state: dict, triggers: list[str], *, autopilot: bool, engine=None, threat=None):
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = StubUI()
    coach._mcp = FakeMcp(state)
    coach._normalize_turn_snapshot = lambda s: s
    coach._voice_output = None
    coach._running = True
    coach.draft_mode = False
    coach.set_code = None
    coach._trigger = FakeTrigger(triggers, threat=threat)
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
    coach._autopilot_enabled = autopilot
    coach._autopilot = engine
    coach._auto_deck_strategy = False
    coach._deck_analyzed = False
    coach.advice_style = "quick"
    coach.settings = FakeSettings()
    coach.advice_frequency = "every_priority"
    coach._coach = RecordingCoach()
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
    coach.spoken: list[str] = []
    coach.speak_advice = lambda text, blocking=True: coach.spoken.append(text)
    coach._record_advice = lambda *args, **kwargs: None
    coach._inject_library_summary_if_needed = lambda s: None
    coach._has_actionable_priority_window = lambda s: False
    coach._is_meaningful_advice_window = lambda *a, **k: True
    coach._get_match_context = lambda: {}
    coach._run_win_plan_worker = Mock()
    coach.last_match_id = None
    coach.voice_session = VoiceSession(None)
    coach.conversation = ConversationController(coach, emit_event=None, snapshot_fn=lambda: dict(state))
    coach.conversation.set_mode(TURN_ADVICE, persist=False)
    return coach


def run_loop(monkeypatch, coach, iterations: int = 1) -> None:
    """Run the loop for ``iterations`` trailing poll sleeps (delay buffers don't count)."""
    polls = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        if seconds >= 0.5 or seconds == 0:
            polls["n"] += 1
            if polls["n"] >= iterations:
                coach._running = False

    monkeypatch.setattr(standalone.time, "sleep", fake_sleep)
    monkeypatch.setattr(standalone.StandaloneCoach, "_get_poll_interval", lambda self, state: 1.0)
    coach._coaching_loop()


THREAT = {"name": "Sheoldred, the Apocalypse", "warning": "drains 2 on every draw"}


def test_threat_alert_makes_no_model_call_while_the_autopilot_drives(monkeypatch):
    coach = make_loop(
        _game(5), ["threat_detected"], autopilot=True, engine=FallThroughAutopilot(), threat=THREAT
    )
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == []
    assert coach.spoken == []
    assert any("[THREAT]" in line and "Sheoldred" in line for line in coach.ui.logs)


def test_losing_badly_makes_no_win_probability_call_while_the_autopilot_drives(monkeypatch):
    coach = make_loop(_game(5), ["losing_badly"], autopilot=True, engine=FallThroughAutopilot())
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == []


def test_autopilot_fall_through_gives_local_advice_once_and_never_replans(monkeypatch):
    engine = FallThroughAutopilot()
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=True, engine=engine)
    run_loop(monkeypatch, coach)
    coach._trigger.triggers = ["combat_attackers"]
    coach._running = True
    run_loop(monkeypatch, coach)
    assert engine.triggers == ["combat_attackers", "combat_attackers"]
    engine._planner.plan_actions.assert_not_called()
    assert coach._coach.model_calls == []
    # The same window and advice is spoken once, not on every re-trigger.
    assert coach.spoken == [
        f"{LOCAL_FALLBACK_PREFIX} Attack: Yuriko -> Jace (1 damage, their counterattack 0)."
    ]


def test_fall_through_reuses_the_autopilots_own_advice_first(monkeypatch):
    engine = FallThroughAutopilot()
    engine.get_reusable_advice = lambda state: "Attack with Yuriko."
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=True, engine=engine)
    run_loop(monkeypatch, coach)
    assert coach.spoken == ["Attack with Yuriko."]
    assert coach._coach.model_calls == []


def test_autopilot_setting_on_before_its_engine_starts_leaves_the_coach_advising(monkeypatch):
    state = _game(1, pending_decision="Mulligan", decision_context={"type": "mulligan"})
    coach = make_loop(state, ["decision_required"], autopilot=True, engine=None)
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == ["get_advice:decision_required"]


class _AdvisingPlanner:
    def __init__(self) -> None:
        self.plan_actions = Mock(
            return_value=SimpleNamespace(spoken_actions=lambda: "Cast Seam Rip on their Sworn Guardian.")
        )


class PausedAutopilot(FallThroughAutopilot):
    state = SimpleNamespace(value="paused")


class StandingByAutopilot(FallThroughAutopilot):
    def in_manual_play_cooldown(self) -> bool:
        return True


class BridgelessAutopilot(FallThroughAutopilot):
    _gre_bridge = SimpleNamespace(connected=False)


class GaveUpAutopilot(FallThroughAutopilot):
    def is_window_given_up(self, state) -> bool:
        return True


@pytest.mark.parametrize(
    "engine_cls", [PausedAutopilot, StandingByAutopilot, BridgelessAutopilot, GaveUpAutopilot]
)
def test_when_the_autopilot_hands_the_window_back_the_coach_advises_with_the_model(monkeypatch, engine_cls):
    """2026-10-07 review: the coach went silent (or board-math only) exactly
    when the player had to act: PAUSED / MANUAL REQUIRED, advise-only standby
    after a manual play, and the whole match with the bridge offline. Under
    the old code 537 of 798 such fall-throughs got model advice."""
    engine = engine_cls()
    engine._planner = _AdvisingPlanner()
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=True, engine=engine)
    run_loop(monkeypatch, coach)
    engine._planner.plan_actions.assert_called_once()
    assert coach.spoken == ["Cast Seam Rip on their Sworn Guardian."]

    threat_engine = engine_cls()
    coach = make_loop(_game(5), ["threat_detected"], autopilot=True, engine=threat_engine, threat=THREAT)
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == ["get_advice:threat_detected"]


def test_an_acting_autopilot_with_its_bridge_up_still_blocks_coach_calls(monkeypatch):
    engine = FallThroughAutopilot()
    engine._gre_bridge = SimpleNamespace(connected=True)
    engine.state = SimpleNamespace(value="idle")
    engine.in_manual_play_cooldown = lambda: False
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=True, engine=engine)
    run_loop(monkeypatch, coach)
    engine._planner.plan_actions.assert_not_called()
    assert coach._coach.model_calls == []


def test_without_the_autopilot_the_coach_still_advises(monkeypatch):
    coach = make_loop(_game(5), ["threat_detected"], autopilot=False, threat=THREAT)
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == ["get_advice:threat_detected"]
    coach = make_loop(_game(5), ["losing_badly"], autopilot=False)
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == ["win_probability"]
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=False)
    run_loop(monkeypatch, coach)
    assert coach._coach.model_calls == ["get_advice:combat_attackers"]


def test_win_in_n_worker_is_never_spawned_while_the_autopilot_drives(monkeypatch):
    coach = make_loop(_game(5), ["new_turn"], autopilot=True, engine=FallThroughAutopilot())
    coach.settings = FakeSettings({"auto_win_plan": True})
    run_loop(monkeypatch, coach)
    coach._run_win_plan_worker.assert_not_called()


def test_win_in_n_check_is_on_by_default_and_board_gated(monkeypatch):
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach._autopilot_enabled = False
    coach._coach = SimpleNamespace(_backend=BreakerBackend())
    coach.settings = FakeSettings({"auto_win_plan": False})
    state = _game(5)
    alive = SimpleNamespace(opp_lethal_on_board=False, their_clock=3, our_clock=2, facts_line=lambda: "")
    monkeypatch.setattr("arenamcp.board_assessment.assess", lambda s: alive)
    assert not coach._auto_win_plan_allowed(state)  # the setting turns it off
    # On by default for coaching (2026-10-07 review: opt-in with no UI for the
    # setting removed the feature for every user).
    coach.settings = FakeSettings()
    assert coach._auto_win_plan_allowed(state)
    # Both "VIABLE" answers on 2026-10-06 came while the opponent had lethal.
    dying = SimpleNamespace(opp_lethal_on_board=True, their_clock=1, our_clock=None, facts_line=lambda: "")
    monkeypatch.setattr("arenamcp.board_assessment.assess", lambda s: dying)
    assert not coach._auto_win_plan_allowed(state)
    monkeypatch.setattr("arenamcp.board_assessment.assess", lambda s: alive)
    coach._coach._backend.up = False
    assert not coach._auto_win_plan_allowed(state)
    coach._coach._backend.up = True
    coach._autopilot_enabled = True
    assert not coach._auto_win_plan_allowed(state)


def test_win_in_n_worker_waits_its_turn_in_the_strategic_lane():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach._win_plan_worker = Mock()
    token = STRATEGIC_LANE.try_acquire("game_plan")
    try:
        coach._run_win_plan_worker({})
        coach._win_plan_worker.assert_not_called()
    finally:
        STRATEGIC_LANE.release(token)
    coach._run_win_plan_worker({})
    coach._win_plan_worker.assert_called_once()
    assert STRATEGIC_LANE.holder is None


# ---------------------------------------------------------------------------
# Circuit breaker: advice, restarts, banners, startup probe
# ---------------------------------------------------------------------------


def _coach_with(backend) -> CoachEngine:
    coach = CoachEngine(backend=backend)
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *a, **k: [])
    coach._format_game_context = lambda state, **kwargs: "CTX"
    # No background plan refresh racing the advice call for scripted replies.
    coach._ensure_game_plan_mgr = lambda: None
    return coach


def test_open_breaker_skips_the_model_and_says_offline_once():
    backend = BreakerBackend(up=False)
    coach = _coach_with(backend)
    first = coach.get_advice(_game(5), trigger="new_turn")
    second = coach.get_advice(_game(5), trigger="threat_detected", threat=THREAT)
    third = coach.get_advice(_game(5), trigger="new_turn")
    assert backend.calls == []
    assert first.startswith(LOCAL_FALLBACK_PREFIX) and "offline" in first
    assert "offline" not in second and "Sheoldred, the Apocalypse" in second
    assert third == ""
    assert "offline" in coach.get_advice(_game(5), question="Should I attack?")
    # Back online: the model is asked again, and a later outage is announced again.
    backend.up = True
    coach.get_advice(_game(5), trigger="new_turn")
    assert len(backend.calls) == 1
    backend.up = False
    assert "offline" in coach.get_advice(_game(5), trigger="new_turn")


def test_advice_calls_are_labelled_and_bounded_to_a_first_token():
    backend = BreakerBackend(["Attack with everything."])
    _coach_with(backend).get_advice(_game(5), trigger="new_turn")
    (kwargs,) = [call[3] for call in backend.calls]
    assert kwargs["call_class"] == "coach.advice"
    # 12 s, not 8: 51 of 3638 good calls had a first token after 8 s at busy
    # times, and advice has no deterministic move to fall back on.
    assert kwargs["first_token_timeout_s"] == CoachEngine._ADVICE_FIRST_TOKEN_TIMEOUT_S == 12.0
    assert kwargs["first_token_timeout_s"] <= kwargs.get("request_timeout_s", 17.0) - 2.0


def test_conversation_mode_renders_are_labelled_as_questions():
    backend = BreakerBackend(["What a turn."])
    _coach_with(backend).get_advice(_game(5), trigger="new_turn", conversational=True)
    assert backend.calls[0][3]["call_class"] == "coach.question"


def test_breaker_opening_mid_call_falls_back_instead_of_showing_an_error():
    backend = BreakerBackend(["[BACKEND ERROR] model server unavailable (circuit open; retry in 12s)"])
    advice = _coach_with(backend).get_advice(_game(5), trigger="new_turn")
    assert advice.startswith(LOCAL_FALLBACK_PREFIX) and "offline" in advice


def test_threat_fallback_follows_the_block_solver_and_survives_the_legal_filter(monkeypatch):
    """18:51:49: the legal-action replacement turned the threat fallback into
    "Block with: Diviner of Victory" while the solver said no blocks."""
    monkeypatch.setattr(
        "arenamcp.combat_strategy.safe_default_blocks",
        lambda state, pending=None: ({}, "no blocks: 2 damage at 20 life"),
    )
    state = _game(
        9,
        active=2,
        phase="Phase_Combat",
        pending_decision="Declare Blockers",
        decision_context={"type": "declare_blockers"},
        legal_actions=["Block with: Diviner of Victory"],
    )
    advice = _coach_with(BreakerBackend(["Error: LLM timed out"])).get_advice(
        state, trigger="threat_detected", threat={"name": "Pia", "warning": "grows"}
    )
    assert "No blocks: 2 damage at 20 life" in advice
    assert "Block with" not in advice


def test_empty_advice_counts_toward_a_restart_only_after_a_model_failure(monkeypatch):
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=False)
    coach._coach.advice = ""
    run_loop(monkeypatch, coach)
    assert getattr(coach, "_consecutive_errors", 0) == 0  # deterministic empty: not counted

    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=False)
    coach._coach.advice = ""
    coach._coach.on_advice = lambda: BackendHealth.instance().record_failure("Connection error")
    run_loop(monkeypatch, coach)
    assert coach._consecutive_errors == 1

    # Breaker open: never counted, so never a backend restart into the outage.
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=False)
    coach._coach.advice = ""
    coach._coach._backend = BreakerBackend(up=False)
    coach._coach.on_advice = lambda: BackendHealth.instance().record_failure("Connection error")
    coach._consecutive_errors = 2
    coach._reinit_coach = Mock(side_effect=AssertionError("no restart while the breaker is open"))
    run_loop(monkeypatch, coach)
    assert coach._consecutive_errors == 2


def test_a_fast_failing_outage_never_restarts_the_coach_before_the_breaker_opens(monkeypatch):
    """2026-10-07 review: an instant 502 / connection refused failed in about
    0.6 s, so three empties re-forced every 0.5 s hit the restart threshold
    (3) at ~2.7 s, before the breaker's 5th failure, and _reinit_coach threw
    the CoachEngine (playbook, deck strategy, game plan) away."""
    from tests.test_backend_circuit_breaker import FakeClient, litellm_500

    from arenamcp.backends import health
    from arenamcp.backends.proxy import ProxyBackend

    health.reset_circuit_breakers()
    monkeypatch.setattr(ProxyBackend, "_local_warmup", lambda self: None)
    backend = ProxyBackend(model="glm-5.3-flash", base_url="http://restart.invalid/v1")
    backend._client = FakeClient([litellm_500() for _ in range(5)])
    coach = make_loop(_combat_state(), ["combat_attackers"], autopilot=False)
    coach._coach.advice = ""
    coach._coach._backend = backend
    coach._coach.on_advice = lambda: backend.complete("s", "u", call_class="coach.advice")
    coach._reinit_coach = Mock(side_effect=AssertionError("no restart for an outage the breaker tracks"))
    try:
        for _ in range(3):
            coach._trigger.triggers = ["combat_attackers"]
            coach._running = True
            run_loop(monkeypatch, coach)
        assert len(backend._client.calls) == 3
        assert backend.available(), "three fast failures: the breaker has not opened yet"
        assert getattr(coach, "_consecutive_errors", 0) == 0
        coach._reinit_coach.assert_not_called()
    finally:
        health.reset_circuit_breakers()


def test_breaker_open_and_close_each_show_one_banner():
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = Mock()
    backend = BreakerBackend()
    coach._coach = SimpleNamespace(_backend=backend)
    coach._poll_model_circuit()
    coach.ui.emit_backend_health.assert_not_called()
    backend.up = False
    coach._poll_model_circuit()
    coach._poll_model_circuit()
    assert coach.ui.emit_backend_health.call_count == 1
    assert coach.ui.emit_backend_health.call_args.args[0]["state"] == "down"
    assert "circuit open" in coach.ui.emit_backend_health.call_args.args[0]["detail"]
    backend.up = True
    coach._poll_model_circuit()
    assert coach.ui.emit_backend_health.call_count == 2
    assert coach.ui.emit_backend_health.call_args.args[0]["state"] == "ok"


@pytest.mark.parametrize("records_itself", [True, False])
def test_startup_check_uses_an_inference_probe_not_the_model_list(monkeypatch, records_itself):
    """18:50:46 and 18:51:28: GET /models said OK while vLLM was dead."""

    class ProbeBackend:
        _base_url = "https://gateway.test/v1"
        model = "fake-glm"

        def probe_inference(self, timeout: float = 8.0):
            if records_itself:
                BackendHealth.instance().record_failure("Connection error")
            return False, "fake-glm: inference probe failed after 5000ms: Connection error"

    monkeypatch.setattr(
        "arenamcp.standalone.check_gateway_health",
        Mock(side_effect=AssertionError("model list is not a probe")),
    )
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = Mock()
    coach._coach = SimpleNamespace(_backend=ProbeBackend())
    coach._startup_finished = False
    coach._probe_backend_health_at_startup()
    assert "Connection error" in coach._startup_connection_error
    snapshot = BackendHealth.instance().snapshot()
    assert snapshot["total_failures"] == 1  # recorded exactly once
    assert BackendHealth.instance().state is not HealthState.OK


# ---------------------------------------------------------------------------
# Game plan cadence, supersede, breaker and lane
# ---------------------------------------------------------------------------


class GatedPlanBackend(BreakerBackend):
    """Blocks inside complete() until released; records the call keywords."""

    def __init__(self, responses=()) -> None:
        super().__init__(responses)
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user, args, kwargs))
        self.entered.set()
        assert self.release.wait(3)
        return self.responses.pop(0) if self.responses else _plan_json()


@pytest.fixture
def plan_threads(monkeypatch):
    started: list[threading.Thread] = []
    real_thread = threading.Thread

    def make(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        started.append(thread)
        return thread

    monkeypatch.setattr("arenamcp.game_plan.threading.Thread", make)
    yield started
    for thread in started:
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_one_plan_per_turn_side_and_card_churn_never_reforms():
    backend = BreakerBackend([_plan_json("ours"), _plan_json("for turn 5")])
    mgr = GamePlanManager(backend)
    mgr.maybe_reform(_game(3))
    state = _game(
        3, hand=[{"instance_id": 1, "name": "Drawn Card"}], graveyard=[{"instance_id": 2, "name": "Spent"}]
    )
    mgr.maybe_reform(state)
    assert len(backend.calls) == 1
    mgr.maybe_reform(_game(4, active=2))
    mgr.maybe_reform(_game(4, active=2, hand=[{"instance_id": 3, "name": "Another"}]))
    assert len(backend.calls) == 2
    mgr.maybe_reform(_game(5))  # our turn, already planned during theirs
    assert len(backend.calls) == 2


def test_plan_calls_are_labelled_background_and_cancellable(plan_threads):
    backend = BreakerBackend([_plan_json()])
    assert GamePlanManager(backend).request_reform(_game(3))
    plan_threads[-1].join(timeout=3)
    kwargs = backend.calls[0][3]
    assert kwargs["call_class"] == "background.game_plan"
    assert kwargs["priority"] == game_plan_module.BACKGROUND_PRIORITY
    assert isinstance(kwargs["cancel_event"], threading.Event)


def test_a_plan_for_a_turn_that_has_passed_is_cancelled_and_discarded(plan_threads):
    backend = GatedPlanBackend([_plan_json("stale line")])
    mgr = GamePlanManager(backend)
    assert mgr.request_reform(_game(4, active=2))  # their turn 4: a plan for our turn 5
    assert backend.entered.wait(3)
    cancel = backend.calls[0][3]["cancel_event"]
    assert not mgr.request_reform(_game(5))  # our turn 5: still the turn it plans for
    assert not cancel.is_set()
    assert not mgr.request_reform(_game(6, active=2))  # turn 5 has passed
    assert cancel.is_set()
    backend.release.set()
    plan_threads[-1].join(timeout=3)
    assert mgr.current is None
    assert STRATEGIC_LANE.holder is None


def test_a_superseded_plan_frees_the_manager_and_the_lane_at_once(monkeypatch, plan_threads):
    """2026-10-07 review: a superseded request queued on a busy server held
    the manager's in-flight slot and the strategic lane until its 75 s budget
    ran out, so the plan for the new turn could not start."""
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    stuck_entered, stuck_release = threading.Event(), threading.Event()

    class StuckThenFast(BreakerBackend):
        def complete(self, system, user, *args, **kwargs):
            self.calls.append((system, user, args, kwargs))
            if len(self.calls) == 1:
                stuck_entered.set()
                assert stuck_release.wait(5)  # ignores its cancel_event
                return _plan_json("stale line")
            return _plan_json("fresh line")

    backend = StuckThenFast()
    mgr = GamePlanManager(backend)
    try:
        assert mgr.request_reform(_game(4, active=2))  # their turn 4: a plan for our turn 5
        assert stuck_entered.wait(3)
        now[0] += 60
        # Turn 5 has passed: the stuck request is cancelled, and the plan for
        # our turn 7 starts in the same call.
        assert mgr.request_reform(_game(6, active=2))
        assert backend.calls[0][3]["cancel_event"].is_set()
        plan_threads[-1].join(timeout=3)
        assert mgr.current is not None and mgr.current.path == "fresh line"
        assert STRATEGIC_LANE.holder is None
    finally:
        stuck_release.set()
    plan_threads[0].join(timeout=3)
    assert mgr.current.path == "fresh line", "the superseded answer is discarded"
    assert not mgr._inflight
    assert STRATEGIC_LANE.holder is None


def test_a_new_match_frees_the_lane_held_by_the_old_matchs_plan(plan_threads):
    backend = GatedPlanBackend([_plan_json("old game")])
    mgr = GamePlanManager(backend)
    assert mgr.request_reform(_game(4, active=2))
    assert backend.entered.wait(3)
    assert STRATEGIC_LANE.holder == "game_plan"
    mgr.reset()
    assert backend.calls[0][3]["cancel_event"].is_set()
    assert STRATEGIC_LANE.holder is None
    assert not mgr._inflight
    backend.release.set()
    plan_threads[-1].join(timeout=3)
    assert mgr.current is None


def test_no_plan_request_while_the_breaker_is_open_then_one_after_it_closes(plan_threads):
    backend = BreakerBackend([_plan_json()], up=False)
    mgr = GamePlanManager(backend)
    assert not mgr.request_reform(_game(3))
    assert plan_threads == []
    backend.up = True
    assert mgr.request_reform(_game(3))
    plan_threads[-1].join(timeout=3)
    assert mgr.current is not None


def test_routine_reform_waits_for_a_moment_with_no_decision_pending(monkeypatch, plan_threads):
    now = [100.0]
    monkeypatch.setattr("arenamcp.game_plan.time.monotonic", lambda: now[0])
    backend = BreakerBackend([_plan_json("first"), _plan_json("their turn")])
    mgr = GamePlanManager(backend)
    assert mgr.request_reform(_game(3))  # first plan: urgent, decisions or not
    plan_threads[-1].join(timeout=3)
    now[0] += 60
    blockers = _game(4, active=2, pending_decision="Declare Blockers")
    assert not mgr.request_reform(blockers)
    assert mgr.request_reform(_game(4, active=2))
    plan_threads[-1].join(timeout=3)
    assert mgr.current.path == "their turn"


def test_only_one_background_strategy_job_at_a_time(plan_threads):
    backend = BreakerBackend([_plan_json()])
    mgr = GamePlanManager(backend)
    token = STRATEGIC_LANE.try_acquire("deck_playbook")
    try:
        assert not mgr.request_reform(_game(3))
    finally:
        STRATEGIC_LANE.release(token)
    assert mgr.request_reform(_game(3))
    plan_threads[-1].join(timeout=3)
    assert STRATEGIC_LANE.holder is None


def test_coaching_loop_starts_due_reforms_only_with_no_decision_pending():
    mgr = Mock()
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach._autopilot_enabled = True
    coach._autopilot = None
    coach.draft_mode = False
    coach._coach = SimpleNamespace(_ensure_game_plan_mgr=lambda: mgr, _deck_strategy="playbook")
    coach._maybe_refresh_game_plan(_game(4, active=2, pending_decision="Declare Blockers"))
    mgr.request_reform.assert_not_called()
    coach._maybe_refresh_game_plan(_game(4, active=2))
    mgr.request_reform.assert_called_once()
    coach._autopilot = SimpleNamespace(_config=SimpleNamespace(afk_mode=True, land_drop_mode=False))
    coach._maybe_refresh_game_plan(_game(4, active=2))
    mgr.request_reform.assert_called_once()  # AFK autopilot never plans


# ---------------------------------------------------------------------------
# Deck playbook: one analysis per deck, reused across matches and restarts
# ---------------------------------------------------------------------------


@pytest.fixture
def deck(monkeypatch):
    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    return state, catalog, response


def _playbook_backend(response: dict, *, notes: str = "Discovery notes") -> Mock:
    backend = Mock()
    backend.complete.side_effect = [notes, json.dumps(response)]
    return backend


def test_same_deck_next_match_reuses_the_playbook_without_a_model_call(deck):
    state, _, response = deck
    backend = _playbook_backend(response)
    coach = CoachEngine(backend)
    assert coach.analyze_deck(state)
    assert backend.complete.call_count == 2
    # Match boundary: the coach clears per-match strategy, then re-analyses.
    coach.clear_deck_strategy()
    next_match = dict(deepcopy(state), match_id="next-match")
    assert coach.analyze_deck(next_match)
    assert backend.complete.call_count == 2
    assert coach._deck_playbook is not None and not coach._deck_strategy_pending


def test_restart_reuses_the_playbook_from_disk(deck):
    state, _, response = deck
    first = CoachEngine(_playbook_backend(response))
    assert first.analyze_deck(state)
    restarted_backend = Mock()
    restarted = CoachEngine(restarted_backend)
    assert restarted.analyze_deck(deepcopy(state)) == first._deck_strategy
    restarted_backend.complete.assert_not_called()


def test_disk_playbook_from_older_prompts_is_rebuilt(deck, monkeypatch):
    state, _, response = deck
    assert CoachEngine(_playbook_backend(response)).analyze_deck(state)
    monkeypatch.setattr("arenamcp.coach._playbook_prompt_fingerprint", lambda: "changed-prompt")
    backend = _playbook_backend(response)
    assert CoachEngine(backend).analyze_deck(deepcopy(state))
    assert backend.complete.call_count == 2


def test_new_match_with_the_same_deck_does_not_abort_a_running_analysis(deck):
    """Startup analysed the previous match id, then "New match detected" came 2 s later."""
    state, _, response = deck
    entered, release = threading.Event(), threading.Event()
    calls = []

    def complete(system, user, *args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            return "Discovery notes"
        return json.dumps(response)

    coach = CoachEngine(Mock(complete=complete))
    first = threading.Thread(target=coach.analyze_deck, args=(state,))
    first.start()
    assert entered.wait(3)
    # The match boundary, then the worker for the same deck in the new match.
    coach.clear_deck_strategy()
    generation = coach.begin_deck_analysis(deck_identity(state))
    results: list = []
    second = threading.Thread(
        target=lambda: results.append(coach.analyze_deck(state, analysis_generation=generation))
    )
    second.start()
    time.sleep(0.05)
    release.set()
    first.join(3)
    second.join(3)
    assert len(calls) == 2  # one discovery + one compile, not a second analysis
    assert results and results[0]
    assert coach._deck_playbook is not None and not coach._deck_strategy_pending


def test_deck_calls_are_labelled_background_work_and_discovery_keeps_full_thinking(deck):
    """Discovery is the pass meant to reason about the deck, and its result is
    cached for 30 days: it keeps the template-default thinking it always had
    (2026-10-07 review); only the JSON compile pass runs at low effort."""
    state, _, response = deck
    backend = _playbook_backend(response)
    assert CoachEngine(backend).analyze_deck(state)
    discovery, compile_pass = backend.complete.call_args_list
    assert discovery.kwargs["enable_thinking"] is True
    assert "reasoning_effort" not in discovery.kwargs
    assert compile_pass.kwargs["enable_thinking"] is False
    assert compile_pass.kwargs["reasoning_effort"] == "low"
    for call in (discovery, compile_pass):
        assert call.kwargs["call_class"] == "background.deck_playbook"
        assert call.kwargs["priority"] == game_plan_module.BACKGROUND_PRIORITY


def test_changing_a_passs_model_settings_rebuilds_cached_playbooks(monkeypatch):
    from arenamcp import coach as coach_module

    before = coach_module._playbook_prompt_fingerprint()
    monkeypatch.setitem(
        coach_module._PLAYBOOK_PASS_SETTINGS,
        "discovery",
        {"enable_thinking": False, "reasoning_effort": "low"},
    )
    assert coach_module._playbook_prompt_fingerprint() != before


def _sleep_on_this_thread(monkeypatch, on_sleep) -> None:
    """Fake time.sleep for the calling (test) thread only.

    ``arenamcp.coach.time`` is the global time module, so patching its sleep
    also catches every other live thread's sleeps; counting those made the
    exact-count assertion below order-dependent (2026-10-07 review).
    """
    real_sleep = time.sleep
    test_thread = threading.current_thread()

    def fake_sleep(seconds):
        if threading.current_thread() is test_thread:
            on_sleep(seconds)
        else:
            real_sleep(seconds)

    monkeypatch.setattr("arenamcp.coach.time.sleep", fake_sleep)


def test_deck_analysis_waits_for_the_model_server_instead_of_failing(deck, monkeypatch):
    state, _, response = deck
    backend = BreakerBackend(["Discovery notes", json.dumps(response)], up=False)
    waits = []

    def on_sleep(seconds):
        waits.append(seconds)
        backend.up = len(waits) >= 2

    _sleep_on_this_thread(monkeypatch, on_sleep)
    # Another live thread sleeping meanwhile must not count as a wait.
    stop = threading.Event()

    def other_thread():
        while not stop.is_set():
            time.sleep(0.001)

    sleeper = threading.Thread(target=other_thread, daemon=True)
    sleeper.start()
    try:
        coach = CoachEngine(backend)
        assert coach.analyze_deck(state)
    finally:
        stop.set()
        sleeper.join(2)
    assert len(waits) == 2 and len(backend.calls) == 2


def test_deck_analysis_retries_a_call_the_proxy_skipped(deck, monkeypatch):
    state, _, response = deck
    skipped = "[BACKEND ERROR] model server unavailable (backend down; background call skipped)"
    backend = BreakerBackend([skipped, "Discovery notes", json.dumps(response)])
    _sleep_on_this_thread(monkeypatch, lambda seconds: None)
    assert CoachEngine(backend).analyze_deck(state)
    assert len(backend.calls) == 3


def test_placeholder_spoken_summary_is_never_spoken(deck):
    """18:48:59 spoke "(Summary omitted in final field placement)"."""
    state, _, response = deck
    response["spoken_summary"] = "(Summary omitted in final field placement)"
    coach = CoachEngine(_playbook_backend(response))
    assert coach.analyze_deck(state)
    summary = coach._deck_playbook.data["spoken_summary"]
    assert "omitted" not in summary
    assert summary.startswith(response["archetype"])


def test_switching_decks_cancels_the_old_decks_model_call(deck):
    state, _, _ = deck
    entered, release = threading.Event(), threading.Event()
    seen = {}

    def complete(system, user, *args, **kwargs):
        seen["cancel"] = kwargs["cancel_event"]
        entered.set()
        assert release.wait(3)
        return "[BACKEND ERROR] request cancelled (cancelled)"

    coach = CoachEngine(Mock(complete=complete))
    worker = threading.Thread(target=coach.analyze_deck, args=(state,))
    worker.start()
    assert entered.wait(3)
    assert not seen["cancel"].is_set()
    coach.begin_deck_analysis("a-different-deck")
    assert seen["cancel"].is_set()  # the proxy closes the stream on this
    release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert coach._deck_playbook is None and coach._deck_analysis_identity == "a-different-deck"
