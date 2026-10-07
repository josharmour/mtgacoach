"""The autopilot's phase/step checks on bridge-named and log-named turn states.

The COMBAT STEP GUARD in ``AutopilotEngine.process_trigger`` compared the step
with the exact log names "Step_DeclareBlock"/"Step_DeclareAttack". The bridge's
turn overlay (``server._normalize_bridge_turn`` over the Windows plugin's or
``mac_game_state``'s ``CurrentStep.ToString()``) says "DeclareBlock", so on
every bridge-supplied snapshot the guard was skipped: no combat context was
stashed, and a combat trigger without a decision context went to the LLM.
In standalone.log the guard fired only with the bridge offline (2026-09-28 to
2026-10-01, log names).

The states are the recorded 2026-10-06 18:54:03 board (bug_20261006_185403:
bridge names Combat/DeclareBlock, a declare_attackers context) and the same
board renamed to log form.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, _canonical_turn
from arenamcp.autopilot_models import ClickResult

FIXTURE = Path(__file__).parent / "fixtures" / "bug_20261006_185403_game_state.json"
LOCAL, OPPONENT = 2, 1

NAMES = pytest.mark.parametrize("log_names", [False, True], ids=["bridge-names", "log-names"])


def _state(step: str, *, log_names: bool, active: int = LOCAL, context: bool = False) -> dict[str, Any]:
    """The recorded board in combat at ``step`` ("DeclareBlock"), bridge or log names."""
    state = json.loads(FIXTURE.read_text())
    assert state["turn"]["phase"] == "Combat" and state["turn"]["step"] == "DeclareBlock"
    assert state["local_seat_id"] == LOCAL
    phase = "Combat"
    if log_names:
        phase, step = f"Phase_{phase}", f"Step_{step}"
    state["turn"].update(phase=phase, step=step, active_player=active, priority_player=active)
    if not context:
        state["decision_context"] = None
        state["pending_decision"] = None
    return state


class _Planned(Exception):
    """The trigger reached LLM planning."""


class _Planner:
    """plan_actions records the decision context, then returns ``plan`` or stops the trigger."""

    _timeout = 15.0
    _backend = None

    def __init__(self, plan: ActionPlan | None = None):
        self.plan = plan
        self.contexts: list[Any] = []

    def plan_actions(self, game_state, trigger, legal_actions, decision_context):
        self.contexts.append(copy.deepcopy(decision_context))
        if self.plan is None:
            raise _Planned
        return copy.deepcopy(self.plan)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Bridge:
    """Offline when ``pending`` is None; otherwise connected with that request pending."""

    def __init__(self, pending: dict[str, Any] | None = None):
        self.pending = pending
        self.connected = pending is not None

    def connect(self) -> bool:
        return self.connected

    def get_pending_actions(self) -> dict[str, Any]:
        return self.pending or {"has_pending": False}

    def auto_respond(self) -> bool:
        pytest.fail("a combat request must never be auto-responded")


def _engine(
    monkeypatch, state: dict[str, Any], *, bridge: _Bridge | None = None, plan: ActionPlan | None = None
):
    bridge = bridge or _Bridge()
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    planner = _Planner(plan)
    engine = AutopilotEngine(
        planner=planner,
        get_game_state=lambda: copy.deepcopy(state),
        config=AutopilotConfig(dry_run=False, verify_after_action=False, post_action_delay=0.0),
    )
    engine._game_plan_mgr = None
    engine._MAX_CONTINUATION_DEPTH = 0
    monkeypatch.setattr(engine, "_get_game_state", lambda: copy.deepcopy(state))
    monkeypatch.setattr(engine, "_try_typed_decision_path", lambda *args: None)
    monkeypatch.setattr(engine, "_get_legal_actions", lambda game: list(game.get("legal_actions") or []))
    executed: list[GameAction] = []

    def execute(action, game):
        executed.append(action)
        return ClickResult(True)

    monkeypatch.setattr(engine, "_execute_action", execute)
    return engine, planner, executed


def _run(engine: AutopilotEngine, state: dict[str, Any], trigger: str) -> bool | None:
    """process_trigger's result, or None when the trigger reached LLM planning."""
    try:
        return engine.process_trigger(copy.deepcopy(state), trigger)
    except _Planned:
        return None


def _bridge_pending(request_class: str) -> dict[str, Any]:
    request_type = request_class.removesuffix("Request")
    return {
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": request_type,
        "_bridge_request_class": request_class,
    }


@pytest.mark.parametrize(
    ("turn", "expected"),
    [
        ({"phase": "Combat", "step": "DeclareBlock"}, ("Phase_Combat", "Step_DeclareBlock")),
        ({"phase": "Phase_Combat", "step": "Step_DeclareAttack"}, ("Phase_Combat", "Step_DeclareAttack")),
        ({"phase": "Main1", "step": "None"}, ("Phase_Main1", "")),
        ({"phase": "Phase_Main1", "step": ""}, ("Phase_Main1", "")),
        ({}, ("", "")),
        (None, ("", "")),
    ],
)
def test_canonical_turn_puts_both_sources_in_log_form(turn, expected):
    assert _canonical_turn(turn) == expected


@NAMES
def test_declare_context_is_stashed_on_both_names(monkeypatch, log_names):
    # The recorded state as captured: declare_attackers context, bridge names.
    state = _state("DeclareBlock", log_names=log_names, context=True)
    engine, planner, executed = _engine(monkeypatch, state)

    assert _run(engine, state, "decision_required") is None
    assert engine._last_combat_context == state["decision_context"]
    assert engine._last_combat_context["type"] == "declare_attackers"
    assert planner.contexts == [state["decision_context"]]  # the LLM still plans the declaration
    assert executed == []


@NAMES
@pytest.mark.parametrize(
    ("trigger", "step", "active", "action_type"),
    [
        ("combat_attackers", "DeclareAttack", LOCAL, ActionType.DECLARE_ATTACKERS),
        ("combat_blockers", "DeclareBlock", OPPONENT, ActionType.DECLARE_BLOCKERS),
    ],
)
def test_combat_trigger_without_context_declares_nothing_on_both_names(
    monkeypatch, log_names, trigger, step, active, action_type
):
    state = _state(step, log_names=log_names, active=active)
    engine, planner, executed = _engine(monkeypatch, state)

    assert _run(engine, state, trigger) is True
    assert planner.contexts == []
    assert [a.action_type for a in executed] == [action_type]
    assert not executed[0].attacker_names and not executed[0].blocker_assignments


@NAMES
def test_bridge_declaration_pending_without_context_declares_nothing(monkeypatch, log_names):
    state = _state("DeclareBlock", log_names=log_names, active=OPPONENT)
    state.update(_bridge_pending("DeclareBlockersRequest"))
    bridge = _Bridge({"has_pending": True, "request_type": "DeclareBlockers"})
    engine, planner, executed = _engine(monkeypatch, state, bridge=bridge)

    assert _run(engine, state, "combat_blockers") is True
    assert planner.contexts == []
    assert [a.action_type for a in executed] == [ActionType.DECLARE_BLOCKERS]


@NAMES
def test_fresh_stashed_context_is_restored_on_both_names(monkeypatch, log_names):
    stashed = json.loads(FIXTURE.read_text())["decision_context"]
    state = _state("DeclareAttack", log_names=log_names)
    state.update(_bridge_pending("DeclareAttackersRequest"))
    bridge = _Bridge({"has_pending": True, "request_type": "DeclareAttackers"})
    engine, planner, executed = _engine(monkeypatch, state, bridge=bridge)
    engine._last_combat_context = dict(stashed)
    engine._last_combat_context_time = time.time()
    engine._last_combat_context_turn = state["turn"]["turn_number"]

    assert _run(engine, state, "combat_attackers") is None
    assert planner.contexts == [stashed]
    assert engine._last_combat_context is None
    assert executed == []


@NAMES
def test_other_bridge_request_is_planned_not_answered_with_a_declaration(monkeypatch, log_names):
    # A combat trigger while the bridge shows another request: neither an
    # empty declaration nor a stashed one answers it.
    stashed = json.loads(FIXTURE.read_text())["decision_context"]
    state = _state("DeclareAttack", log_names=log_names)
    state.update(_bridge_pending("SelectReplacementRequest"))
    bridge = _Bridge({"has_pending": True, "request_type": "SelectReplacement"})
    engine, planner, executed = _engine(monkeypatch, state, bridge=bridge)
    engine._last_combat_context = dict(stashed)
    engine._last_combat_context_time = time.time()
    engine._last_combat_context_turn = state["turn"]["turn_number"]

    assert _run(engine, state, "combat_attackers") is None
    assert planner.contexts == [None]
    assert engine._last_combat_context == stashed  # kept for its own window
    assert executed == []


@NAMES
@pytest.mark.parametrize("step", ["CombatDamage", "EndCombat"])
def test_guard_stays_out_of_other_combat_steps(monkeypatch, log_names, step):
    state = _state(step, log_names=log_names, active=OPPONENT)
    engine, planner, executed = _engine(monkeypatch, state)

    assert _run(engine, state, "combat_blockers") is None
    assert planner.contexts == [None]
    assert engine._last_combat_context is None
    assert executed == []


def _cast_plan() -> ActionPlan:
    return ActionPlan(actions=[GameAction(action_type=ActionType.CAST_SPELL, card_name="Living Library")])


@pytest.mark.parametrize("fresh_log_names", [False, True], ids=["fresh-bridge", "fresh-log"])
def test_staleness_check_ignores_a_naming_change(monkeypatch, fresh_log_names):
    """The re-polled state may come from the other source; the phase did not change."""
    pre = _state("DeclareBlock", log_names=not fresh_log_names, active=OPPONENT)
    fresh = _state("DeclareBlock", log_names=fresh_log_names, active=OPPONENT)
    engine, planner, executed = _engine(monkeypatch, fresh, plan=_cast_plan())

    engine.process_trigger(copy.deepcopy(pre), "decision_required")

    assert len(planner.contexts) == 1
    # A real phase change into combat discards a sorcery-speed plan; a mere
    # "Phase_Combat" vs "Combat" did too.
    assert [a.action_type for a in executed] == [ActionType.CAST_SPELL]


def test_staleness_check_still_discards_a_sorcery_plan_overtaken_by_combat(monkeypatch):
    pre = _state("DeclareBlock", log_names=False, active=OPPONENT)
    pre["turn"].update(phase="Main1", step="None")
    fresh = _state("DeclareBlock", log_names=True, active=OPPONENT)
    engine, _, executed = _engine(monkeypatch, fresh, plan=_cast_plan())

    engine.process_trigger(copy.deepcopy(pre), "decision_required")

    assert executed == []


def _verify(monkeypatch, pre: dict[str, Any], post: dict[str, Any]) -> bool:
    engine, _, _ = _engine(monkeypatch, post)
    engine._config.verification_timeout = 0.2
    return engine._verify_action(GameAction(action_type=ActionType.PASS_PRIORITY), copy.deepcopy(pre))


@NAMES
def test_verification_does_not_count_a_naming_change_as_progress(monkeypatch, log_names):
    pre = _state("DeclareBlock", log_names=log_names, active=OPPONENT)
    post = _state("DeclareBlock", log_names=not log_names, active=OPPONENT)
    assert _verify(monkeypatch, pre, post) is False


def test_verification_still_sees_a_real_step_change(monkeypatch):
    pre = _state("DeclareBlock", log_names=False, active=OPPONENT)
    post = _state("CombatDamage", log_names=True, active=OPPONENT)
    assert _verify(monkeypatch, pre, post) is True
