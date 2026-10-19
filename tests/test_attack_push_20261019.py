"""The line search's attack pushed over a declaration that ends worse (ledger #178, 2026-10-19).

Match c0af235f T12 (15:21:36-15:21:41; Player.log gsid 306, the board in
``tests.valkyrie_boards_20261019``): twenty able attackers (Leonardo 27/3,
five Hare Apparents, five vigilant Warriors, nine Rabbits) against seven
untapped blockers, we at 25, they at 32. The board facts said holding is dead
on T15 and attacking the only survivor; the low-effort declaration was empty
("Stabilize behind the token wall and close with later swarm swings") and it
stood: every combat guard (losing attack, crackback, the #152 attack line
guard) can only hold attackers back. Their T13 fliers took 14.

On master the same board (20 attackers) goes to the combat solver without a
model call (#162), and the solver holds all twenty ("hold all attackers").
Master's search, with Righteous Valkyrie's +2/+2 switching off below 32 life
(#177), attacks with the thirteen tokens (they go to 26, the anthem ends) and
survives at 20, 15, 13; holding only wins on T16, a distant win the posture
ranks as alive at a lower value. At 15 life holding is dead on T15 outright.

The push is shadow by default (``ARENAMCP_ATTACK_PUSH``): it logs
"attack_push [shadow]: would attack with ...".
"""

from __future__ import annotations

import json
import logging

import pytest
from tests import valkyrie_boards_20261019 as vb

from arenamcp import action_planner as ap
from arenamcp import combat_strategy
from arenamcp.action_planner import (
    FALLBACK_ATTACK_PUSH,
    FALLBACK_BIG_BOARD,
    ActionPlan,
    ActionPlanner,
    ActionType,
    GameAction,
)
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.board_assessment import assess
from arenamcp.combat_strategy import combat_choice
from arenamcp.line_guard import attack_push, attack_push_mode

# The lines these tests pin must not depend on a loaded machine (the hard cap is a clock).
pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


# The thirteen tokens the best line sends at T12: the Warriors and the able Rabbits.
TOKENS = [600, 602, 603, 607, 609, 610, 611, 615, 617, 618, 619, 620, 625]
MODEL_STRATEGY = "Stabilize behind the token wall and close with later swarm swings."


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv("ARENAMCP_ATTACK_PUSH", raising=False)
    monkeypatch.delenv("ARENAMCP_ATTACK_LINE_GUARD", raising=False)


def _no_attack() -> ActionPlan:
    """The recorded T12 answer (15:21:41): Done, no attackers."""
    return ActionPlan(
        actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")], overall_strategy=MODEL_STRATEGY
    )


def _planner() -> ActionPlanner:
    return ActionPlanner(None, timeout=1, land_drop_first=False)


def _model_planner(answer: dict) -> ActionPlanner:
    class Backend:
        def complete(self, system, user, *args, **kwargs):
            return json.dumps(answer)

    return ActionPlanner(Backend(), timeout=1, land_drop_first=False)


def _run_guards(plan: ActionPlan, state: dict) -> ActionPlan:
    """The planner's declare-attackers guards, in plan_actions' order."""
    planner = _planner()
    context = state["decision_context"]
    for guard in (
        planner._follow_line_attack,
        planner._check_losing_attacks,
        planner._ensure_free_attacks,
        planner._check_attack_crackback,
        planner._check_attack_line,
    ):
        guard(plan, state, context)
    return plan


def _at_life(life: int) -> dict:
    state = vb.t12_declare_attackers()
    state["players"][0]["life_total"] = life
    return state


# --- the board ------------------------------------------------------------------------------


def test_the_t12_board_holding_ranks_below_attacking_and_the_solver_holds():
    state = vb.t12_declare_attackers()
    facts = assess(state)
    result = facts.line_search
    assert not facts.lethal_now and not facts.all_in and result.posture == "attack"
    assert result.best.outcome_text() == "survives (life 20, 15, 13; opponent at 26)"
    assert sorted(result.best.steps[0].plays[2]) == TOKENS
    hold = result.first_action[("noattack",)]
    assert hold.outcome_text() == "lethal on T16" and hold.ranked < result.best.ranked
    # The big-board path's declaration: the solver keeps all twenty home.
    choice = combat_choice(state)
    assert choice is not None and choice.assignments == {}
    assert choice.explanation.startswith("hold all attackers")


def test_the_verdict_on_the_recorded_empty_declaration():
    state = vb.t12_declare_attackers()
    facts = assess(state)
    verdict = attack_push(
        facts.line_search, [], state, eligible=list(vb.T12_ATTACKERS), lethal_now=False, all_in=False
    )
    assert verdict is not None
    assert verdict.attack_ids == TOKENS
    assert set(verdict.attack_names) == {"Warrior", "Rabbit"}
    assert verdict.declared_text == "lethal on T16 (ranks below: value 40.3 vs 53.9)"
    assert verdict.attack_text == "survives (life 20, 15, 13; opponent at 26)"
    assert verdict.robust and verdict.applies is False and verdict.setting == "shadow"
    assert verdict.as_trace()["attack_ids"] == TOKENS
    # The caller's lethal and all-in verdicts stand the push down; so does a declaration of the line.
    for kwargs in ({"lethal_now": True, "all_in": False}, {"lethal_now": False, "all_in": True}):
        assert attack_push(facts.line_search, [], state, **kwargs) is None
    assert attack_push(facts.line_search, TOKENS, state, lethal_now=False, all_in=False) is None
    # An attacker the request does not offer is never pushed.
    assert (
        attack_push(facts.line_search, [], state, eligible=TOKENS[:-1], lethal_now=False, all_in=False)
        is None
    )


def test_holding_dead_sooner_is_pushed_too():
    # The board at 15 life: holding is dead on T15, the tokens' attack survives.
    state = _at_life(15)
    verdict = attack_push(assess(state).line_search, [], state, lethal_now=False, all_in=False)
    assert verdict is not None and verdict.attack_ids == TOKENS
    assert verdict.reason.startswith("hold dead T15, attack survives (life 13, 11, 9; opponent at 26)")
    # At 13 both die, attacking with all twenty a turn later (T15 against T13).
    state = _at_life(13)
    verdict = attack_push(assess(state).line_search, [], state, lethal_now=False, all_in=False)
    assert verdict is not None and verdict.attack_ids == sorted(vb.T12_ATTACKERS)
    assert verdict.reason.startswith("hold dead T13, attack dead T15")


# --- the model's declaration ----------------------------------------------------------------


def test_shadow_by_default_logs_the_push_and_keeps_the_empty_declaration(caplog):
    assert attack_push_mode() == "shadow"
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        plan = _run_guards(_no_attack(), vb.t12_declare_attackers())
    assert [a.action_type for a in plan.actions] == [ActionType.CLICK_BUTTON]
    assert plan.fallback_reason == ""
    assert (
        "attack_push [shadow]: would attack with Warrior, Rabbit, Rabbit, Warrior, Rabbit, Rabbit, Rabbit, "
        "Warrior, Rabbit, Rabbit, Rabbit, Rabbit, Warrior (hold lethal on T16 (ranks below: value 40.3 vs "
        "53.9), attack survives (life 20, 15, 13; opponent at 26)" in caplog.text
    )
    assert f"[model planned: {MODEL_STRATEGY}]" in caplog.text


def test_on_the_empty_declaration_becomes_the_lines_attack(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "on")
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = _run_guards(_no_attack(), vb.t12_declare_attackers())
    (action,) = plan.actions
    assert action.action_type == ActionType.DECLARE_ATTACKERS and action.line_push
    assert action.attacker_instance_ids == TOKENS and len(action.attacker_names) == 13
    assert set(action.attacker_targets.values()) == {"Opponent"}
    assert action.model_reasoning == MODEL_STRATEGY
    assert plan.fallback_reason == FALLBACK_ATTACK_PUSH
    assert plan.overall_strategy == (
        "Attack with Warrior and Rabbit: by the board math, holding ends lethal on T16 (ranks below: value "
        "40.3 vs 53.9); this attack survives (life 20, 15, 13; opponent at 26)."
    )
    assert "attack_push: attacking with Warrior, Rabbit" in caplog.text


def test_off_does_nothing(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "off")
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        plan = _run_guards(_no_attack(), vb.t12_declare_attackers())
    assert [a.action_type for a in plan.actions] == [ActionType.CLICK_BUTTON]
    assert "attack_push" not in caplog.text


def test_the_model_path_end_to_end(monkeypatch):
    # Below the big-board size the model answers; its empty declaration meets the push last.
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "on")
    monkeypatch.setattr(ap, "big_board_declaration", lambda *_a, **_k: "")
    planner = _model_planner(
        {
            "actions": [{"action_type": "click_button", "card_name": "done"}],
            "overall_strategy": MODEL_STRATEGY,
        }
    )
    state = vb.t12_declare_attackers()
    plan = planner.plan_actions(state, "decision_required", vb.t12_menu(), state["decision_context"])
    assert plan.fallback_reason == FALLBACK_ATTACK_PUSH
    assert plan.actions[0].attacker_instance_ids == TOKENS and plan.actions[0].line_push


# --- the big-board path ---------------------------------------------------------------------


def test_the_big_board_solver_declaration_is_judged_too(monkeypatch, caplog):
    state = vb.t12_declare_attackers()
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        plan = _planner().plan_actions(state, "decision_required", vb.t12_menu(), state["decision_context"])
    assert plan.fallback_reason == FALLBACK_BIG_BOARD and plan.actions == []
    assert "attack_push [shadow]: would attack with Warrior, Rabbit" in caplog.text
    assert "[solver planned: combat solver: hold all attackers" in caplog.text
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "on")
    plan = _planner().plan_actions(state, "decision_required", vb.t12_menu(), state["decision_context"])
    assert plan.fallback_reason == FALLBACK_ATTACK_PUSH
    (action,) = plan.actions
    assert action.attacker_instance_ids == TOKENS and action.line_push


# --- composition: nothing thins a pushed attack ---------------------------------------------


def test_a_pushed_attack_is_not_thinned_by_the_losing_attack_guard(monkeypatch):
    # At 13 life the line attacks with all twenty. Whatever the losing-attack guard says of the
    # Hares (here: each dies to a block for nothing), neither its planner pass, which runs before
    # the push, nor the bridge's pass at submission thins the pushed declaration.
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "on")
    hares = sorted(vb.HARES)

    def guard(state, attackers, *args, **kwargs):
        return {i: "dies to a block for nothing" for i in attackers if i in hares}

    monkeypatch.setattr(combat_strategy, "losing_attackers", guard)
    state = _at_life(13)
    everyone = sorted(vb.T12_ATTACKERS)
    sent = GameAction(
        ActionType.DECLARE_ATTACKERS,
        attacker_names=["Hare Apparent"] * len(hares),
        attacker_instance_ids=list(hares),
        attacker_targets={"Hare Apparent": "Opponent"},
    )
    plan = _run_guards(ActionPlan(actions=[sent], overall_strategy="Send the Hares."), state)
    (action,) = plan.actions
    assert action.line_push and action.attacker_instance_ids == everyone
    assert plan.fallback_reason == FALLBACK_ATTACK_PUSH
    # The bridge's own pass of the guard leaves it whole as well.
    face = {"type": "DamageRecType_Player", "playerSystemSeatId": vb.OPPONENT}
    entries = [{"attackerInstanceId": i, "damageRecipient": face} for i in everyone]
    pending = {"attackers": state["decision_context"]["raw_attackers"]}
    _submitted, kept = _BridgeSubmitMixin()._prepare_attack_submission(
        action, list(action.attacker_names), entries, state, pending
    )
    assert [entry["attackerInstanceId"] for entry in kept] == everyone
    # Without the flag the same declaration loses its Hares.
    action.line_push = False
    _submitted, kept = _BridgeSubmitMixin()._prepare_attack_submission(
        action, list(action.attacker_names), entries, state, pending
    )
    assert not set(hares) & {entry["attackerInstanceId"] for entry in kept}


def test_the_hold_guard_still_holds_what_the_line_keeps_home():
    # The 633f56bd T11 board (#152): the best line holds, so there is nothing to push.
    from tests.test_attack_line_guard_20261019 import _both, _t11

    state = _t11()
    facts = assess(state)
    assert attack_push(facts.line_search, [336, 349], state, lethal_now=False, all_in=False) is None
    assert attack_push(facts.line_search, [], state, lethal_now=False, all_in=False) is None
    plan = _run_guards(_both(), state)
    assert plan.fallback_reason != FALLBACK_ATTACK_PUSH
