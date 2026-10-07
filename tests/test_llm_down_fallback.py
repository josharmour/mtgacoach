"""Deterministic play while the model server is down (bug_20261006_185403).

At 18:53 on 2026-10-06 the vLLM server was saturated. The planner waited out
30 s per combat window and typed decisions 12 s per priority window, then the
fallback played a Swamp and PASSED turn 10 with Living Library, Codie, Geist
of Saint Thalia and Theoretical Necromancer all payable (its rule was "play a
land, otherwise pass"). These tests pin the replacement:

- priority windows: land drop, then the board-math line's spells, never a
  pass while that line casts something (opponent's turn: still pass);
- attacks: ``combat_strategy.combat_choice`` with the all-in and
  losing-attack guards; blocks keep ``safe_default_blocks``;
- targets / modes keep their safe classifiers (never our own permanent);
- with the circuit breaker open no model call is made at all, and a model
  failure never escalates the planner budget.

The fixture is the board captured in that bug report (game_state only).
"""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import (
    DECLINE_DECISION,
    FALLBACK_AUTO_PICK,
    FALLBACK_LLM_UNAVAILABLE,
    ActionPlan,
    ActionPlanner,
    ActionType,
    board_math_legacy_plan,
    board_math_option_pick,
    is_llm_unavailable_error,
)
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.autopilot_models import ClickResult
from arenamcp.backend_health import BackendHealth
from arenamcp.backends.proxy import BackendError
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision

FIXTURE = Path(__file__).parent / "fixtures" / "bug_20261006_185403_game_state.json"
YURIKO, PIA, THEIR_JACE, SWAMP = 266, 280, 341, 346
GEIST, NECROMANCER = "Geist of Saint Thalia", "Theoretical Necromancer"


def _captured() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _main_phase(*, land_played: bool = True, active: int = 2) -> dict[str, Any]:
    """Turn 10 main phase 1 as at 18:53:41 (land_played=False) / 18:53:53 (True).

    The report was captured in combat a few seconds later; Yuriko was still
    a legal attacker at 18:53:53, so she is untapped here.
    """
    state = _captured()
    state["turn"].update(
        {"phase": "Phase_Main1", "step": "", "active_player": active, "priority_player": active}
    )
    state["pending_decision"] = "Action Required"
    state["decision_context"] = {}
    for card in state["battlefield"]:
        if card["instance_id"] == YURIKO:
            card.update(is_tapped=False, is_attacking=False)
    if not land_played:
        swamp = next(card for card in state["battlefield"] if card["instance_id"] == SWAMP)
        state["battlefield"].remove(swamp)
        state["hand"].append(swamp)
        next(p for p in state["players"] if p["seat_id"] == 2)["lands_played"] = 0
    return state


def _hand(state: dict[str, Any], name: str) -> dict[str, Any]:
    return next(card for card in state["hand"] if card["name"] == name)


def _priority_poll(state: dict[str, Any]) -> dict[str, Any]:
    """The bridge's ActionsAvailable request, shaped like the 18:53 captures."""
    actions = [
        {
            "actionType": "ActionType_Cast",
            "instanceId": _hand(state, name)["instance_id"],
            "grpId": _hand(state, name)["grp_id"],
            "hasAutoTap": True,
        }
        for name in ("Living Library", "Codie, Ravenous Codex", GEIST, NECROMANCER)
    ]
    land_drop = next(p for p in state["players"] if p["seat_id"] == 2)["lands_played"] == 0
    actions += [
        {"actionType": "ActionType_Play", "instanceId": card["instance_id"], "grpId": card["grp_id"]}
        for card in state["hand"]
        if land_drop and "Land" in (card.get("card_types") or [])
    ]
    actions += [
        {"actionType": "ActionType_Activate_Mana", "instanceId": 242},
        {"actionType": "ActionType_Pass"},
    ]
    return {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": actions}


def _decision(state: dict[str, Any]) -> PendingDecision:
    names = {card["grp_id"]: card["name"] for card in state["hand"] + state["battlefield"]}
    return build_pending_decision(_priority_poll(state), resolve_name=lambda grp: names.get(grp, ""))


def _picked_name(decision: PendingDecision, state: dict[str, Any], picked: list[str]) -> str:
    assert len(picked) == 1
    if picked[0] == "pass":
        return "pass"
    meta = decision.find(picked[0]).meta
    return next(
        card["name"]
        for card in state["hand"] + state["battlefield"]
        if card["instance_id"] == meta["instanceId"]
    )


class _Backend:
    """Records every complete() call; raises, sleeps, or answers as told."""

    def __init__(self, *, answer: str = "", error: Exception | None = None, available: bool | None = None):
        self.answer = answer
        self.error = error
        self._available = available
        self.calls: list[dict[str, Any]] = []

    def available(self) -> bool:
        return True if self._available is None else self._available

    def complete(
        self,
        system_prompt,
        user_message,
        max_tokens=4096,
        temperature=0.3,
        request_timeout_s=None,
        raise_on_error=False,
        *,
        call_class=None,
        first_token_timeout_s=None,
    ):
        self.calls.append(
            {
                "max_tokens": max_tokens,
                "request_timeout_s": request_timeout_s,
                "raise_on_error": raise_on_error,
                "call_class": call_class,
                "first_token_timeout_s": first_token_timeout_s,
            }
        )
        if self.error is not None:
            raise self.error
        return self.answer


def _planner(backend: Any, timeout: float = 15.0) -> ActionPlanner:
    return ActionPlanner(backend=backend, timeout=timeout)


# --- priority windows ----------------------------------------------------------------


def test_outage_turn10_casts_the_board_math_line_instead_of_passing():
    state = _main_phase(land_played=True)
    decision = _decision(state)
    # The rule that passed at 18:53:53 with four payable creatures.
    assert ActionPlanner.deterministic_option_pick(decision) == ["pass"]

    planner = _planner(_Backend(error=BackendError("Request timed out.")))
    picked = planner.plan_decision_options(decision, state)

    assert _picked_name(decision, state, picked) in {GEIST, NECROMANCER}
    assert planner.last_llm_failure == "unavailable"
    reasoning = planner.get_decision_reasoning(picked)
    assert reasoning == (
        "Model unavailable; the board-math line casts Geist of Saint Thalia and Theoretical Necromancer."
    )


def test_outage_turn10_land_first_then_the_spell_line():
    """18:53:41 played Swamp, then 18:53:53 passed: now land, then a cast."""
    before = _main_phase(land_played=False)
    decision = _decision(before)
    planner = _planner(_Backend(error=BackendError("Request timed out.")))
    first = _picked_name(decision, before, planner.plan_decision_options(decision, before))
    # A land drop first; the board math (and the model's own game plan that
    # turn) keeps the Swamp for later, the old rule took the first land.
    assert first == "Island"

    after = _main_phase(land_played=True)  # the board as it was after the Swamp
    decision = _decision(after)
    second = _picked_name(decision, after, planner.plan_decision_options(decision, after))
    assert second in {GEIST, NECROMANCER}

    # After Necromancer resolves, the re-plan continues the line with Geist.
    resolved = copy.deepcopy(after)
    necromancer = _hand(resolved, NECROMANCER)
    resolved["hand"].remove(necromancer)
    resolved["battlefield"].append({**necromancer, "controller_seat_id": 2, "turn_entered_battlefield": 10})
    for card in resolved["battlefield"]:
        if card["instance_id"] in (274, 249, 322):
            card["is_tapped"] = True
    poll = _priority_poll(after)
    poll["actions"] = [a for a in poll["actions"] if a.get("instanceId") != necromancer["instance_id"]]
    names = {card["grp_id"]: card["name"] for card in resolved["hand"] + resolved["battlefield"]}
    decision = build_pending_decision(poll, resolve_name=lambda grp: names.get(grp, ""))
    assert _picked_name(decision, resolved, planner.plan_decision_options(decision, resolved)) == GEIST


def test_opponent_turn_window_still_passes_without_the_model():
    state = _main_phase(land_played=True, active=1)
    decision = _decision(state)
    picked, why = board_math_option_pick(decision, state)
    assert picked == ["pass"]
    assert "outside our main phase" in why


def test_nothing_scheduled_holds_instead_of_casting_reactive_spells():
    state = _main_phase(land_played=True)
    counter = {
        "instance_id": 901,
        "grp_id": 9901,
        "name": "Essence Scatter",
        "type_line": "Instant",
        "card_types": ["Instant"],
        "mana_cost": "{1}{U}",
        "oracle_text": "Counter target creature spell.",
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    state["hand"] = [counter]
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [
                {"actionType": "Cast", "instanceId": 901, "grpId": 9901, "hasAutoTap": True},
                {"actionType": "Pass"},
            ],
        },
        resolve_name=lambda grp: "Essence Scatter",
    )
    assert board_math_option_pick(decision, state)[0] == ["pass"]


def test_unpayable_and_x_spells_are_never_picked():
    state = _main_phase(land_played=True)
    decision = _decision(state)
    unpayable = PendingDecision(
        decision.request_id,
        "ActionsAvailable",
        tuple(
            DecisionOption(
                o.option_id,
                o.label,
                payable=False if o.meta.get("actionType") == "ActionType_Cast" else o.payable,
                meta=o.meta,
            )
            for o in decision.options
        ),
        can_pass=True,
    )
    assert board_math_option_pick(unpayable, state)[0] == ["pass"]
    for card in state["hand"]:
        card["mana_cost"] = "{X}" + str(card.get("mana_cost") or "")
    assert board_math_option_pick(decision, state)[0] == ["pass"]


def test_circuit_open_decides_at_once_without_a_model_call():
    state = _main_phase(land_played=True)
    decision = _decision(state)
    backend = _Backend(error=AssertionError("the model must not be called"), available=False)
    planner = _planner(backend)
    started = time.perf_counter()
    picked = planner.plan_decision_options(decision, state)
    assert time.perf_counter() - started < 2.0
    assert backend.calls == []
    assert _picked_name(decision, state, picked) in {GEIST, NECROMANCER}
    assert planner.last_llm_failure == "unavailable"


def test_bad_answer_also_casts_the_line_but_is_not_called_an_outage():
    state = _main_phase(land_played=True)
    decision = _decision(state)
    planner = _planner(_Backend(answer="I think we should hold up mana."))
    picked = planner.plan_decision_options(decision, state)
    assert _picked_name(decision, state, picked) in {GEIST, NECROMANCER}
    assert planner.last_llm_failure == "bad_answer"
    assert planner.get_decision_reasoning(picked).startswith("No usable model answer;")


def test_targets_and_modes_keep_their_safe_classifiers_while_down():
    backend = _Backend(available=False)
    planner = _planner(backend)
    state = {
        "players": [{"is_local": True, "seat_id": 1}, {"seat_id": 2}],
        "battlefield": [
            {"instance_id": 607, "name": "Ours", "power": 1, "controller_seat_id": 1, "owner_seat_id": 1},
        ],
        "stack": [{"instance_id": 900, "name": "Murder", "oracle_text": "Destroy target creature."}],
    }
    only_ours = PendingDecision((1, 1), "SelectTargets", (DecisionOption("tgt:607", "Ours"),))
    assert planner.plan_decision_options(only_ours, state) == [DECLINE_DECISION]

    state["battlefield"].append(
        {"instance_id": 812, "name": "Theirs", "power": 5, "controller_seat_id": 2, "owner_seat_id": 2}
    )
    both = PendingDecision(
        (1, 1), "SelectTargets", (DecisionOption("tgt:607", "Ours"), DecisionOption("tgt:812", "Theirs"))
    )
    assert planner.plan_decision_options(both, state) == ["tgt:812"]

    modes = PendingDecision(
        (1, 1),
        "CastingTimeOptions",
        (DecisionOption("idx:0", "Mode one", meta={"choiceKind": "modal", "childIndex": 0}),),
        can_cancel=True,
    )
    assert planner.plan_decision_options(modes, {}) == [DECLINE_DECISION]
    assert backend.calls == []


_SIM_WURM = {
    "instance_id": 950,
    "grp_id": 99950,
    "name": "Sim Wurm",
    "type_line": "Creature - Wurm",
    "card_types": ["Creature"],
    "oracle_text": "Trample",
    "power": 6,
    "toughness": 6,
    "controller_seat_id": 1,
    "owner_seat_id": 1,
    "is_tapped": False,
}


def _removal_targets(oracle: str) -> tuple[dict[str, Any], PendingDecision]:
    """Our removal spell on the stack, choosing between their Pia (2/2) and a 6/6."""
    state = _main_phase(land_played=True)
    state["battlefield"].append(dict(_SIM_WURM))
    for card in state["battlefield"]:
        if card["instance_id"] == PIA:
            card.update(is_tapped=False, is_attacking=False)
    spell = {
        "instance_id": 901,
        "grp_id": 99901,
        "name": "Sim Removal",
        "type_line": "Sorcery",
        "card_types": ["Sorcery"],
        "mana_cost": "{1}{B}",
        "oracle_text": oracle,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
    }
    state["stack"] = [spell]
    state["decision_context"] = {"type": "target_selection", "source_id": 901}
    decision = PendingDecision(
        (5, 5),
        "SelectTargets",
        (DecisionOption(f"tgt:{PIA}", "Pia, Aether Ascetic"), DecisionOption("tgt:950", "Sim Wurm")),
        can_cancel=True,
    )
    return state, decision


@pytest.mark.parametrize(
    "oracle",
    ["Sim Removal deals 2 damage to target creature.", "Target creature gets -2/-2 until end of turn."],
)
def test_fallback_removal_targets_a_creature_it_kills(oracle):
    """2026-10-07 review: with the model down, board math cast 2-damage removal
    because it kills their 2/2, then the targeting fallback (biggest power
    first) aimed it at a 6/6 and the card was wasted."""
    state, decision = _removal_targets(oracle)
    planner = _planner(_Backend(available=False))
    assert planner.plan_decision_options(decision, state) == [f"tgt:{PIA}"]
    # The legacy bridge safe-default uses the same choice.
    assert ActionPlanner.__new__(ActionPlanner).targeting_fallback_choice(decision, state) == [f"tgt:{PIA}"]


def test_fallback_keeps_removal_that_kills_no_legal_target():
    state, decision = _removal_targets("Sim Removal deals 2 damage to target creature.")
    decision = PendingDecision(
        (5, 5), "SelectTargets", (DecisionOption("tgt:950", "Sim Wurm"),), can_cancel=True
    )
    assert _planner(_Backend(available=False)).plan_decision_options(decision, state) == [DECLINE_DECISION]


def test_fallback_destroy_removal_still_takes_the_biggest_threat():
    state, decision = _removal_targets("Destroy target creature.")
    assert _planner(_Backend(available=False)).plan_decision_options(decision, state) == ["tgt:950"]


# --- plan_actions -----------------------------------------------------------------------


_COMBAT_LEGAL = ["Attack with: Yuriko, Hope from the Shadows (1/1)", "Done (confirm attackers)"]


def _combat_state() -> dict[str, Any]:
    state = _captured()
    state["turn"].update({"phase": "Combat", "step": "DeclareAttack"})
    for card in state["battlefield"]:
        if card["instance_id"] == YURIKO:
            card.update(is_tapped=False, is_attacking=False)
    return state


def test_plan_actions_with_the_circuit_open_skips_the_model():
    backend = _Backend(error=AssertionError("the model must not be called"), available=False)
    plan = _planner(backend).plan_actions(_combat_state(), "decision_required", _COMBAT_LEGAL)
    assert plan.actions == []
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE
    assert backend.calls == []


@pytest.mark.parametrize(
    "backend",
    [
        _Backend(error=BackendError("Error code: 500 - Connection error.")),
        _Backend(answer="[BACKEND ERROR] model server unavailable (circuit open; retry in 10s)"),
    ],
)
def test_plan_actions_backend_failures_mean_llm_unavailable(backend):
    plan = _planner(backend).plan_actions(_combat_state(), "decision_required", _COMBAT_LEGAL)
    assert plan.actions == []
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE


def test_plan_actions_timeout_is_bounded_even_if_the_backend_ignores_it():
    release = threading.Event()

    class _Hung:
        def complete(self, *args, **kwargs):
            release.wait(5.0)
            return ""

    try:
        started = time.perf_counter()
        plan = _planner(_Hung(), timeout=0.2).plan_actions(
            _combat_state(), "decision_required", _COMBAT_LEGAL
        )
        assert time.perf_counter() - started < 2.0
    finally:
        release.set()
    assert plan.actions == []
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE


def test_failure_classification():
    assert is_llm_unavailable_error(BackendError("Request timed out."))
    assert is_llm_unavailable_error(TimeoutError())
    assert not is_llm_unavailable_error(ValueError("typed-decision response contained no JSON object"))


# --- call labels --------------------------------------------------------------------------


def test_decision_calls_carry_call_class_and_first_token_timeout():
    state = _main_phase(land_played=True)
    backend = _Backend(answer='{"option_ids": ["pass"], "reasoning": "hold"}')
    planner = _planner(backend)
    planner.plan_decision_options(_decision(state), state)
    planner.plan_pay_or_decline("Source", "You may pay {1}.", state)
    planner.plan_actions(_combat_state(), "decision_required", _COMBAT_LEGAL)
    classes = [call["call_class"] for call in backend.calls]
    assert classes[:3] == ["decision.typed", "decision.pay", "decision.plan"]
    typed, pay, plan = backend.calls[:3]
    assert typed["first_token_timeout_s"] == ActionPlanner._FIRST_TOKEN_TIMEOUT_S
    assert typed["request_timeout_s"] == 12.0
    assert pay["first_token_timeout_s"] is None
    # Plan prompts are the big ones: 12 s (two real plan calls that succeeded
    # had first tokens at 11.3 s and 11.7 s), 2 s inside the 15 s budget.
    assert plan["first_token_timeout_s"] == ActionPlanner._PLAN_FIRST_TOKEN_TIMEOUT_S == 12.0
    assert _planner(backend, timeout=9.0)._plan_first_token_timeout() == ActionPlanner._FIRST_TOKEN_TIMEOUT_S
    assert all(call["raise_on_error"] is True for call in backend.calls)


def test_backends_without_the_new_keywords_still_raise_on_error():
    calls = []

    class _Older:
        def complete(
            self,
            system_prompt,
            user_message,
            max_tokens=4096,
            temperature=0.3,
            request_timeout_s=None,
            raise_on_error=False,
        ):
            calls.append(raise_on_error)
            return '{"option_ids": ["pass"]}'

    state = _main_phase(land_played=True)
    _planner(_Older()).plan_decision_options(_decision(state), state)
    assert calls == [True]  # no TypeError retry that drops raise_on_error


# --- autopilot ---------------------------------------------------------------------------


class _Bridge:
    """GRE bridge double; records attack declarations."""

    def __init__(self, pending: dict[str, Any] | None = None, refuse_attackers: bool = False):
        self.connected = True
        self.pending = pending or {"has_pending": False}
        self.refuse_attackers = refuse_attackers
        self.attacker_calls: list[list[dict]] = []

    def connect(self) -> bool:
        return True

    def get_pending_actions(self) -> dict[str, Any]:
        return self.pending

    def submit_attackers_raw(self, entries, **kwargs):
        self.attacker_calls.append(entries)
        if self.refuse_attackers and entries:
            return {"ok": False}
        return {"ok": True}

    def auto_respond(self) -> bool:
        pytest.fail("combat requests must never be auto-responded")


def _engine(monkeypatch, planner: Any, bridge: _Bridge, state: dict[str, Any]) -> AutopilotEngine:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    engine = AutopilotEngine(
        planner=planner,
        get_game_state=lambda: copy.deepcopy(state),
        config=AutopilotConfig(dry_run=False, verify_after_action=False, post_action_delay=0.0),
    )
    engine._game_plan_mgr = None
    engine._MAX_CONTINUATION_DEPTH = 0
    monkeypatch.setattr(engine, "_get_game_state", lambda: copy.deepcopy(state))
    return engine


def test_engine_caps_the_planner_budget(monkeypatch):
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _Bridge())  # never the real pipe
    planner = ActionPlanner(backend=_Backend(), timeout=30.0)
    engine = AutopilotEngine(planner=planner, config=AutopilotConfig(planning_timeout=30.0))
    assert engine._effective_planning_timeout == 15.0
    assert planner._timeout == 15.0


class _PlannerStub:
    """plan_actions returns the scripted plan; everything else is inert."""

    _timeout = 15.0
    _backend = None

    def __init__(self, plan: ActionPlan):
        self.plan = plan
        self.calls = 0

    def plan_actions(self, *args, **kwargs) -> ActionPlan:
        self.calls += 1
        return copy.deepcopy(self.plan)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _legacy_priority_state() -> dict[str, Any]:
    state = _main_phase(land_played=True)
    state.update(_bridge_connected=True, _bridge_has_pending=True, _bridge_request_type="ActionsAvailable")
    state["legal_actions"] = [
        "Cast Living Library [OK]",
        "Cast Codie, Ravenous Codex [OK]",
        "Cast Geist of Saint Thalia [OK]",
        "Cast Theoretical Necromancer [OK]",
        "Activate Ability: Murmuring Volume [OK]",
        "Action: Activate_Mana",
        "Pass",
    ]
    return state


def _run_legacy(monkeypatch, plan: ActionPlan, *, health_failures: int = 0, failures_before: int = 0):
    state = _legacy_priority_state()
    planner = _PlannerStub(plan)
    engine = _engine(
        monkeypatch, planner, _Bridge({"has_pending": True, "request_type": "ActionsAvailable"}), state
    )
    health = BackendHealth()
    for _ in range(health_failures):
        health.record_failure("Request timed out.")
    monkeypatch.setattr(BackendHealth, "_instance", health)
    monkeypatch.setattr(engine, "_try_typed_decision_path", lambda *args: None)
    monkeypatch.setattr(engine, "_get_legal_actions", lambda game: list(game["legal_actions"]))
    executed: list = []

    def execute(action, game):
        executed.append(action)
        return ClickResult(True)

    monkeypatch.setattr(engine, "_execute_action", execute)
    engine._consecutive_plan_failures = failures_before
    engine.process_trigger(copy.deepcopy(state), "decision_required")
    return engine, executed


def test_llm_unavailable_plan_is_replaced_by_the_board_math_play_without_escalation(monkeypatch):
    engine, executed = _run_legacy(monkeypatch, ActionPlan(fallback_reason=FALLBACK_LLM_UNAVAILABLE))
    assert [a.action_type for a in executed] == [ActionType.CAST_SPELL]
    assert executed[0].card_name in {GEIST, NECROMANCER}
    assert engine._consecutive_plan_failures == 0
    assert engine._effective_planning_timeout == 15.0
    assert engine.last_plan_fallback_reason == FALLBACK_LLM_UNAVAILABLE


def test_empty_plans_do_not_escalate_the_budget_while_the_backend_is_failing(monkeypatch):
    engine, executed = _run_legacy(monkeypatch, ActionPlan(), health_failures=1, failures_before=1)
    assert engine._consecutive_plan_failures == 2
    assert engine._effective_planning_timeout == 15.0
    assert executed == []

    engine, _ = _run_legacy(monkeypatch, ActionPlan(), failures_before=1)
    assert engine._effective_planning_timeout == 22.5


def test_repeated_empty_plans_use_board_math_not_the_broken_modes_fallback(monkeypatch):
    engine, executed = _run_legacy(monkeypatch, ActionPlan(), failures_before=3)
    assert [a.card_name for a in executed] in ([GEIST], [NECROMANCER])


def test_legacy_plan_passes_on_the_opponents_turn():
    state = _legacy_priority_state()
    state["turn"]["active_player"] = 1
    plan = board_math_legacy_plan(state, state["legal_actions"], "decision_required")
    assert [a.action_type for a in plan.actions] == [ActionType.PASS_PRIORITY]
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE
    plan = board_math_legacy_plan(state, ["Pass"], "x", fallback_reason=FALLBACK_AUTO_PICK)
    assert plan.fallback_reason == FALLBACK_AUTO_PICK


def _attack_pending() -> dict[str, Any]:
    return {
        "has_pending": True,
        "request_type": "DeclareAttackers",
        "request_class": "DeclareAttackerRequest",
        "attackers": [
            {
                "attackerInstanceId": YURIKO,
                "legalDamageRecipients": [
                    {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                    {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": THEIR_JACE},
                ],
            }
        ],
    }


def _attack_state(*, pia_tapped: bool) -> dict[str, Any]:
    state = _combat_state()
    for card in state["battlefield"]:
        if card["instance_id"] == PIA:
            card["is_tapped"] = pia_tapped
    state.update(
        _bridge_request_type="DeclareAttackers",
        _bridge_request_class="DeclareAttackerRequest",
        _bridge_can_pass=False,
    )
    return state


def test_attack_safe_default_declares_the_solver_attack(monkeypatch):
    """Their Pia is tapped: Yuriko hits their Jace, as the model chose once it recovered."""
    state = _attack_state(pia_tapped=True)
    bridge = _Bridge(_attack_pending())
    engine = _engine(monkeypatch, _PlannerStub(ActionPlan()), bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert bridge.attacker_calls[0] == [
        {
            "attackerInstanceId": YURIKO,
            "damageRecipient": {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": THEIR_JACE},
        }
    ]


def test_attack_safe_default_holds_when_the_solver_holds(monkeypatch):
    state = _attack_state(pia_tapped=False)  # an untapped 2/2 eats the 1/1
    bridge = _Bridge(_attack_pending())
    engine = _engine(monkeypatch, _PlannerStub(ActionPlan()), bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert bridge.attacker_calls == [[]]


def test_failed_solver_attack_falls_back_to_no_attackers(monkeypatch):
    state = _attack_state(pia_tapped=True)
    bridge = _Bridge(_attack_pending(), refuse_attackers=True)
    engine = _engine(monkeypatch, _PlannerStub(ActionPlan()), bridge, state)
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert bridge.attacker_calls[-1] == []
    assert len(bridge.attacker_calls) == 2


def test_typed_priority_window_submits_a_cast_while_the_circuit_is_open(monkeypatch):
    state = _main_phase(land_played=True)
    poll = _priority_poll(state)
    submitted: list = []

    class _TypedBridge(_Bridge):
        def submit_action_by_index(self, index, expected=None):
            submitted.append(index)
            return True

        def submit_pass(self):
            submitted.append("pass")
            return True

    names = {card["grp_id"]: {"name": card["name"]} for card in state["hand"] + state["battlefield"]}
    monkeypatch.setattr("arenamcp.server.get_card_info", lambda grp: names.get(grp, {}))
    backend = _Backend(error=AssertionError("the model must not be called"), available=False)
    engine = _engine(monkeypatch, _planner(backend), _TypedBridge(poll), state)
    state.update(_bridge_connected=True, _bridge_request_type="ActionsAvailable")
    assert engine._try_typed_decision_path(copy.deepcopy(state), "decision_required") is True
    assert len(submitted) == 1 and submitted[0] != "pass"
    cast = poll["actions"][submitted[0]]
    assert cast["instanceId"] in {
        _hand(state, GEIST)["instance_id"],
        _hand(state, NECROMANCER)["instance_id"],
    }
    assert engine.last_plan_fallback_reason == FALLBACK_LLM_UNAVAILABLE
    assert backend.calls == []


def test_bridge_safe_default_aims_removal_at_a_creature_it_kills(monkeypatch):
    """The legacy bridge safe-default (_try_interactive_safe_default) used the
    biggest-power pick too; it now shares the planner's targeting fallback."""
    state, decision = _removal_targets("Sim Removal deals 2 damage to target creature.")
    state["_bridge_request_type"] = "SelectTargets"
    bridge = _Bridge({"has_pending": True, "request_type": "SelectTargets"})
    engine = _engine(monkeypatch, _planner(_Backend(available=False)), bridge, state)
    submitted = []
    monkeypatch.setattr("arenamcp.decisions.build_pending_decision", lambda pending: decision)
    monkeypatch.setattr(
        "arenamcp.decisions.submit_option",
        lambda bridge, dec, selected: submitted.append(list(selected)) or True,
    )
    assert engine._try_interactive_safe_default(state, "decision_required") is True
    assert submitted == [[f"tgt:{PIA}"]]
