"""Attack declarations see the line search's multi-turn verdict (ledger #152, 2026-10-19).

Match 633f56bd (Mono-Red Burn vs W lifegain), T11 13:59:49-13:59:54; the board is the match's
Player.log at the DeclareAttackers request (gsid 300): we are at 10 with two Slickshot Show-Offs
(1/2 flying haste, one cast this turn), two Mountains open and an empty hand; they are at 17
with Essence Channeler 4/3 and Leonin Vanguard 2/1 (lifelink, ward {2} from Sheltered by
Ghosts) both tapped from their T10 attack, one Plains open and one card in hand.

The model attacked with both ("No untapped blockers; free 4 flying damage." — it was 2;
"Chip with unblockable fliers while saving burn for the burst turn." — the hand was empty)
although the line search said holding survives (life 10, 6, 2) and attacking is dead on T16.
Their T12 attack took 8 (10 -> 2). The losing-attack, free-attack and crackback guards all let
it stand: the crackback guard looks one attack ahead (10 - 6 = 4, above its danger line).
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from unittest.mock import Mock

import pytest
from tests import race_boards_20261019 as rb

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.board_assessment import assess
from arenamcp.line_guard import attack_line_guard, attack_line_mode

PLAYER = {"type": "DamageRecType_Player", "playerSystemSeatId": rb.OPPONENT}
# Arena's card text (the match's card database), as the replayed snapshot carries it.
CARDS = {
    "Slickshot Show-Off": {
        "type_line": "Creature — Bird Wizard", "mana_cost": "{1}{R}", "power": 1, "toughness": 2,
        "keywords": ["flying", "haste"], "card_types": ["CardType_Creature"], "subtypes": ["Bird", "Wizard"],
        "oracle_text": "Flying\nHaste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end "
        "of turn.\nPlot {o1oR}",
    },
    "Essence Channeler": {
        "type_line": "Creature — Bat Cleric", "mana_cost": "{1}{W}", "power": 4, "toughness": 3,
        "card_types": ["CardType_Creature"], "subtypes": ["Bat", "Cleric"], "counters": {"P1P1": 2},
        "oracle_text": "As long as you've lost life this turn, this creature has flying and vigilance.\nWhenever "
        "you gain life, put a +1/+1 counter on this creature.\nWhen this creature dies, put its counters on "
        "target creature you control.",
    },
    "Leonin Vanguard": {
        "type_line": "Creature — Cat Soldier", "mana_cost": "{W}", "power": 2, "toughness": 1,
        "keywords": ["lifelink"], "card_types": ["CardType_Creature"], "subtypes": ["Cat", "Soldier"],
        "oracle_text": "At the beginning of combat on your turn, if you control three or more creatures, this "
        "creature gets +1/+1 until end of turn and you gain 1 life.",
    },
    "Sheltered by Ghosts": {
        "type_line": "Enchantment — Aura", "mana_cost": "{1}{W}", "card_types": ["CardType_Enchantment"],
        "subtypes": ["Aura"],
        "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an "
        "opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has "
        "lifelink and ward {o2}.",
    },
    "Stasis Snare": {
        "type_line": "Enchantment", "mana_cost": "{1}{W}{W}", "card_types": ["CardType_Enchantment"],
        "oracle_text": "Flash\nWhen this enchantment enters, exile target creature an opponent controls until "
        "this enchantment leaves the battlefield.",
    },
    "Leyline of Hope": {
        "type_line": "Enchantment", "mana_cost": "{2}{W}{W}", "card_types": ["CardType_Enchantment"],
        "oracle_text": "If this card is in your opening hand, you may begin the game with it on the "
        "battlefield.\nIf you would gain life, you gain that much life plus 1 instead.\nAs long as you have "
        "at least 7 life more than your starting life total, creatures you control get +2/+2.",
    },
    "Emberheart Challenger": deepcopy(rb.CARDS["Emberheart Challenger"]),
}  # fmt: skip
MODEL_REASON = "No untapped blockers; free 4 flying damage."
MODEL_STRATEGY = "Chip with unblockable fliers while saving burn for the burst turn."
MENU = [
    "Attack with: Slickshot Show-Off #1 (1/2)",
    "Attack with: Slickshot Show-Off #2 (1/2)",
    "Done (confirm attackers)",
]


def _card(iid: int, name: str, seat: int, **extra) -> dict:
    return {
        "instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
        **deepcopy(CARDS[name]), **extra,
    }  # fmt: skip


def _t11(*, their_life: int = 17, life: int = 10) -> dict:
    """633f56bd T11 at the DeclareAttackers request (gsid 300; local seat 1)."""
    state = rb.board(
        turn=11, life=life, their_life=their_life, lands_played=0, opponent_hand=1,
        ours=[(281, "Mountain", True, 1), (290, "Mountain", True, 3), (302, "Mountain", False, 5),
              (320, "Mountain", False, 7)],
        theirs=[(286, "Plains", True, 2), (295, "Plains", True, 4), (330, "Plains", True, 8),
                (340, "Plains", False, 10)],
        hand=[], phase="Phase_Combat", step="Step_DeclareAttack",
    )  # fmt: skip
    state["match_id"] = "633f56bd-2155-4233-a6b0-4369ce49f87e"
    state["battlefield"] += [
        _card(336, "Slickshot Show-Off", rb.LOCAL, is_tapped=False, turn_entered_battlefield=9),
        _card(349, "Slickshot Show-Off", rb.LOCAL, is_tapped=False, turn_entered_battlefield=11),
        _card(331, "Essence Channeler", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=8),
        _card(296, "Leonin Vanguard", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=4),
        _card(314, "Sheltered by Ghosts", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=6,
              attached_to_id=296),
        _card(341, "Stasis Snare", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=10),
        _card(280, "Leyline of Hope", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=1),
    ]  # fmt: skip
    state["exile"] = [
        _card(318, "Emberheart Challenger", rb.LOCAL),
        _card(346, "Emberheart Challenger", rb.LOCAL),
    ]
    state["decision_context"] = {
        "type": "declare_attackers",
        "legal_attackers": ["Slickshot Show-Off", "Slickshot Show-Off"],
        "legal_attacker_ids": [336, 349],
        "raw_attackers": [
            {"attackerInstanceId": 336, "legalDamageRecipients": [PLAYER]},
            {"attackerInstanceId": 349, "legalDamageRecipients": [PLAYER]},
        ],
    }
    return state


def _both(names=("Slickshot Show-Off #1", "Slickshot Show-Off #2"), ids=(336, 349)) -> ActionPlan:
    """The recorded declaration (13:59:54.57): both Slickshots at the opponent."""
    return ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=list(names),
                attacker_instance_ids=list(ids),
                attacker_targets={name: "Opponent" for name in names},
                reasoning=MODEL_REASON,
            )
        ],
        overall_strategy=MODEL_STRATEGY,
    )


def _model_planner(prompts: list[str]) -> ActionPlanner:
    """A planner whose model answers the recorded plan (attacker names as the menu numbers them)."""
    response = {
        "actions": [
            {
                "action_type": "declare_attackers",
                "attacker_names": ["Slickshot Show-Off #1", "Slickshot Show-Off #2"],
                "attacker_targets": {
                    "Slickshot Show-Off #1": "Opponent",
                    "Slickshot Show-Off #2": "Opponent",
                },
                "reasoning": MODEL_REASON,
            }
        ],
        "overall_strategy": MODEL_STRATEGY,
    }

    class Backend:
        def complete(self, system, user, *args, **kwargs):
            prompts.append(user)
            return json.dumps(response)

    return ActionPlanner(Backend(), timeout=1, land_drop_first=False)


def _run_guards(plan: ActionPlan, state: dict) -> ActionPlan:
    """The planner's declare-attackers guards, in plan_actions' order."""
    planner = ActionPlanner(None, timeout=1, land_drop_first=False)
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


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv("ARENAMCP_ATTACK_LINE_GUARD", raising=False)


# --- the recorded board ----------------------------------------------------------------------


def test_the_t11_board_reproduces_the_recorded_verdict():
    facts = assess(_t11())
    result = facts.line_search
    assert not facts.lethal_now and not facts.all_in
    assert result.posture == "hold"
    assert result.posture_reason == (
        "attack with Slickshot Show-Off, Slickshot Show-Off: dead on T16; hold: survives (life 10, 6, 2)"
    )
    assert result.best.summary() == "no play — survives (life 10, 6, 2)"


def test_the_one_turn_guards_let_the_all_in_attack_stand():
    """The gap #152 names: nothing before this change held the attack."""
    planner = ActionPlanner(None, timeout=1, land_drop_first=False)
    plan, state = _both(), _t11()
    for guard in (
        planner._follow_line_attack,
        planner._check_losing_attacks,
        planner._ensure_free_attacks,
        planner._check_attack_crackback,
    ):
        guard(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_instance_ids == [336, 349] and plan.fallback_reason == ""


# --- the prompt ---------------------------------------------------------------------------------


def test_the_declare_attackers_prompt_carries_the_attack_vs_hold_verdict():
    prompts: list[str] = []
    state = _t11()
    _model_planner(prompts).plan_actions(state, "decision_required", MENU, state["decision_context"])
    prompt = prompts[-1]
    trigger = prompt.index("TRIGGER: ")
    note = prompt.index("ATTACK vs HOLD (board-math line search")
    assert trigger < note < prompt.index("=== GAME ===")
    assert (
        "attack with Slickshot Show-Off, Slickshot Show-Off: dead on T16; hold: survives (life 10, 6, 2). "
        "Verdict: HOLD. Not lethal this turn." in prompt
    )
    assert "an attacker without vigilance stays tapped through their next turn and cannot block" in prompt


def test_no_verdict_line_outside_the_declaration_or_on_a_lethal_board():
    state = _t11()
    planner = ActionPlanner(None, timeout=1, land_drop_first=False)
    blockers = dict(state, decision_context={"type": "declare_blockers"})
    assert "ATTACK vs HOLD" not in planner._build_action_prompt(blockers, "combat_blockers", ["Done"])
    # They are at 2: the two fliers are lethal, and the lethal facts speak for themselves.
    lethal = _t11(their_life=2)
    assert assess(lethal).lethal_now
    assert "ATTACK vs HOLD" not in planner._build_action_prompt(
        lethal, "decision_required", MENU, lethal["decision_context"]
    )


# --- the guard ----------------------------------------------------------------------------------


def test_shadow_by_default_logs_the_hold_and_keeps_the_attack(caplog):
    assert attack_line_mode() == "shadow"
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        plan = _run_guards(_both(), _t11())
    action = plan.actions[0]
    assert action.attacker_instance_ids == [336, 349] and not action.guard_hold
    assert plan.fallback_reason == ""
    assert (
        "attack_line_guard [shadow]: would hold Slickshot Show-Off, Slickshot Show-Off (attack dead T16, "
        "hold survives (life 10, 6, 2); best line: no play — survives (life 10, 6, 2)" in caplog.text
    )
    assert f"[model planned: {MODEL_REASON} | {MODEL_STRATEGY}]" in caplog.text


def test_the_recorded_plan_end_to_end_in_shadow(caplog):
    state = _t11()
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        plan = _model_planner([]).plan_actions(state, "decision_required", MENU, state["decision_context"])
    assert plan.actions[0].attacker_instance_ids == [336, 349]
    assert "attack_line_guard [shadow]: would hold Slickshot Show-Off, Slickshot Show-Off" in caplog.text


def test_on_it_holds_both_and_the_hold_is_a_guard_hold(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_ATTACK_LINE_GUARD", "on")
    state = _t11()
    planner = _model_planner([])
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = planner.plan_actions(state, "decision_required", MENU, state["decision_context"])
    action = plan.actions[0]
    assert action.action_type == ActionType.DECLARE_ATTACKERS
    assert (
        action.attacker_names == [] and action.attacker_instance_ids == [] and action.attacker_targets == {}
    )
    assert action.guard_hold is True
    assert action.model_reasoning == f"{MODEL_REASON} | {MODEL_STRATEGY}"
    assert plan.fallback_reason == "planner_attack_line_guard"
    assert plan.overall_strategy == (
        "Hold back Slickshot Show-Off and Slickshot Show-Off: by the board math, attacking is dead on T16, "
        "holding survives (life 10, 6, 2)."
    )
    assert "attack_line_guard: holding Slickshot Show-Off, Slickshot Show-Off (attack dead T16" in caplog.text
    # The next prompt of the turn reads the hold (ledger #60).
    assert "attack held by guard: Slickshot Show-Off, Slickshot Show-Off not attacking" in (
        planner._memory().prompt_block(state)
    )
    # #75: the bridge does not refill a guard's hold with the solver's attack.
    pilot = _BridgeSubmitMixin()
    pilot._planner = Mock()
    pilot._planner.has_pending_attack_intent.return_value = True
    action.model_choice = not plan.fallback_reason
    assert pilot._explicit_no_attack(action) is True


def test_off_does_nothing(monkeypatch, caplog):
    monkeypatch.setenv("ARENAMCP_ATTACK_LINE_GUARD", "off")
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        plan = _run_guards(_both(), _t11())
    assert plan.actions[0].attacker_instance_ids == [336, 349]
    assert "attack_line_guard" not in caplog.text


def test_name_bound_attackers_are_resolved(monkeypatch):
    monkeypatch.setenv("ARENAMCP_ATTACK_LINE_GUARD", "on")
    plan = _run_guards(_both(names=("Slickshot Show-Off #1", "Slickshot Show-Off #2"), ids=()), _t11())
    assert plan.actions[0].attacker_names == [] and plan.actions[0].guard_hold is True


@pytest.mark.parametrize("case", ["lethal", "unsearched subset", "planeswalker recipient", "not our attack"])
def test_no_verdict_without_the_evidence(monkeypatch, case):
    monkeypatch.setenv("ARENAMCP_ATTACK_LINE_GUARD", "on")
    state, plan = _t11(), _both()
    if case == "lethal":
        state = _t11(their_life=2)
    elif case == "unsearched subset":
        # One Slickshot: the search compared none / both, never one; no searched line attacks as declared.
        plan = _both(names=("Slickshot Show-Off #1",), ids=(336,))
    elif case == "planeswalker recipient":
        state["battlefield"].append(
            {
                "instance_id": 360, "name": "Ajani, Caller of the Pride", "owner_seat_id": rb.OPPONENT,
                "controller_seat_id": rb.OPPONENT, "type_line": "Legendary Planeswalker — Ajani",
                "card_types": ["CardType_Planeswalker"], "loyalty": 4, "oracle_text": "", "is_tapped": False,
            }
        )  # fmt: skip
        plan.actions[0].attacker_targets = {
            "Slickshot Show-Off #1": "Ajani, Caller of the Pride",
            "Slickshot Show-Off #2": "Opponent",
        }
    elif case == "not our attack":
        state["turn"].update(step="Step_DeclareBlock")
        for card in state["battlefield"]:
            if card["instance_id"] in (336, 349):
                card.update(is_tapped=True, is_attacking=True)
    kept = [a.attacker_instance_ids for a in _run_guards(plan, state).actions]
    assert kept == [plan.actions[0].attacker_instance_ids] and not plan.actions[0].guard_hold
    assert plan.fallback_reason != "planner_attack_line_guard"


def test_the_verdict_itself():
    state = _t11()
    facts = assess(state)
    verdict = attack_line_guard(facts.line_search, [336, 349], state, lethal_now=False, all_in=False)
    assert verdict is not None
    assert verdict.hold_ids == [336, 349] and verdict.hold_names == [
        "Slickshot Show-Off",
        "Slickshot Show-Off",
    ]
    assert verdict.attack_line.cls != verdict.hold_line.cls and verdict.attack_line.dead_turn == 16
    assert verdict.applies is False and verdict.setting == "shadow"
    assert verdict.as_trace()["hold_ids"] == [336, 349]
    # The caller's lethal and all-in verdicts stand the guard down.
    assert attack_line_guard(facts.line_search, [336, 349], state, lethal_now=True, all_in=False) is None
    assert attack_line_guard(facts.line_search, [336, 349], state, lethal_now=False, all_in=True) is None
    assert attack_line_guard(facts.line_search, [], state, lethal_now=False, all_in=False) is None
