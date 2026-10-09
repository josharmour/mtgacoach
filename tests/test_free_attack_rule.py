"""The free-attack rule (WP13, first half) on the 2026-10-08 FRA QuickDraft boards.

Game 1 (match 07c043d8): the legacy planner answered the T9, T11 and T15
declare-attackers windows with "not attacking" and T13 with Keeper + Foreseer.
Game 2 (match 3288cb65): Arni looted before combat every turn and never
attacked, including T8 into an EMPTY opposing board; the T8-T14 windows then
offered only the creatures left untapped.

The rule is deterministic: an attack set is free when their best blocks kill
none of it, something gets through, and holding everything would not stop
more crackback. That set is always declared; the model may add to it.
"""

from __future__ import annotations

import json
import logging

import pytest
from tests.fra_quickdraft_states import state

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.board_assessment import _CACHE, ROLE_DEFENDER, ROLE_RACE, assess
from arenamcp.combat_strategy import able_attackers, free_attackers
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.play_safety import filter_play_options, tap_forfeits_free_attack

ATTACK_MENU = ["Attack with: Keeper of the Quiet Hour", "Done (confirm attackers)"]


def _with_arni_untapped(window: dict) -> dict:
    """Game 2 T8 as it would have been had Arni not looted precombat."""
    for card in window["battlefield"]:
        if card["instance_id"] == 221:
            card["is_tapped"] = False
    context = window["decision_context"]
    context["legal_attackers"].append("Arni, Humble Scribe")
    context["legal_attacker_ids"].append(221)
    context["raw_attackers"].append(
        {
            "attackerInstanceId": 221,
            "legalDamageRecipients": [
                {"type": "DamageRecType_Player", "playerSystemSeatId": 1},
                {"type": "DamageRecType_PlanesWalker", "planeswalkerInstanceId": 216},
            ],
        }
    )
    return window


# --- the search ---------------------------------------------------------------


def test_empty_opposing_board_makes_every_able_attacker_free():
    free = free_attackers(_with_arni_untapped(state("G2_T8_ATTACK")))
    assert free is not None
    assert set(free.attacker_names) == {"Surveillance Phantasm", "Arni, Humble Scribe"}
    assert free.damage == 5 and free.crackback == 0 == free.hold_crackback


def test_t8_as_played_only_the_flyer_was_free():
    free = free_attackers(state("G2_T8_ATTACK"))
    assert free is not None and free.attacker_names == ["Surveillance Phantasm"] and free.damage == 2


def test_keeper_into_traxos_is_not_free_nothing_gets_through():
    # G1 T9: Traxos 1/5 blocks Keeper 3/2 for free; the attack gains nothing.
    assert free_attackers(state("G1_T9_ATTACK")) is None


def test_attack_that_dies_to_jiang_is_not_free():
    # G1 T11: Jiang 4/4 kills Keeper, Traxos kills Oculus; Geist 1/2 flying is blocked by Traxos.
    assert free_attackers(state("G1_T11_ATTACK")) is None


def test_t13_keeper_and_foreseer_are_free_oculus_is_not():
    free = free_attackers(state("G1_T13_ATTACK"))
    assert free is not None
    assert set(free.attacker_names) == {"Keeper of the Quiet Hour", "Semester Foreseer"}
    assert free.damage == 3 and free.crackback == free.hold_crackback == 3


def test_all_out_attack_at_seven_life_is_not_free():
    # G1 T15: Puller/Koth/Cadet kill three attackers and the crackback is lethal.
    assert free_attackers(state("G1_T15_ATTACK")) is None


def test_precombat_candidates_skip_defenders_and_sick_creatures():
    window = state("G2_T8_MAIN1")
    assert [card["name"] for card in able_attackers(window)] == ["Arni, Humble Scribe"]
    free = free_attackers(window)
    assert free is not None and free.attacker_names == ["Arni, Humble Scribe"] and free.damage == 3


# --- the planner hook -----------------------------------------------------------


def _planner_with(response: dict) -> ActionPlanner:
    class Backend:
        def complete(self, system, user, *args, **kwargs):
            return json.dumps(response)

    return ActionPlanner(Backend(), timeout=1, land_drop_first=False)


def test_a_done_click_becomes_the_free_declaration(caplog):
    window = _with_arni_untapped(state("G2_T8_ATTACK"))
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(
        actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")],
        overall_strategy="Develop behind their clock.",
    )
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner._ensure_free_attacks(plan, window, window["decision_context"])
    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.action_type == ActionType.DECLARE_ATTACKERS
    assert set(action.attacker_names) == {"Surveillance Phantasm", "Arni, Humble Scribe"}
    assert set(action.attacker_instance_ids) == {206, 221}
    assert action.attacker_targets == {"Surveillance Phantasm": "Opponent", "Arni, Humble Scribe": "Opponent"}
    assert plan.fallback_reason == "planner_free_attack"
    assert "Free attack: adding" in caplog.text


def test_the_model_may_add_attackers_but_never_drop_free_ones():
    window = _with_arni_untapped(state("G2_T8_ATTACK"))
    planner = _planner_with(
        {
            "actions": [
                {
                    "action_type": "declare_attackers",
                    "attacker_names": ["Surveillance Phantasm"],
                    "attacker_targets": {"Surveillance Phantasm": "Opponent"},
                    "reasoning": "Chip with the flyer.",
                }
            ],
            "overall_strategy": "Chip the opponent with evasive flyers while developing the loot engine.",
        }
    )
    menu = [
        "Attack with: Surveillance Phantasm",
        "Attack with: Arni, Humble Scribe",
        "Done (confirm attackers)",
    ]
    plan = planner.plan_actions(window, "decision_required", menu, window["decision_context"])
    action = next(a for a in plan.actions if a.action_type == ActionType.DECLARE_ATTACKERS)
    assert action.attacker_names == ["Surveillance Phantasm", "Arni, Humble Scribe"]
    assert action.attacker_targets["Arni, Humble Scribe"] == "Opponent"
    assert plan.fallback_reason == "planner_free_attack"


def test_t13_plan_with_keeper_alone_gains_foreseer():
    window = state("G1_T13_ATTACK")
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Keeper of the Quiet Hour"],
                attacker_instance_ids=[227],
                attacker_targets={"Keeper of the Quiet Hour": "Opponent"},
            )
        ]
    )
    planner._ensure_free_attacks(plan, window, window["decision_context"])
    action = plan.actions[0]
    assert action.attacker_names == ["Keeper of the Quiet Hour", "Semester Foreseer"]
    assert action.attacker_instance_ids == [227, 283]


@pytest.mark.parametrize("name", ["G1_T9_ATTACK", "G1_T11_ATTACK", "G1_T15_ATTACK"])
def test_holding_stands_where_no_attack_is_free(name):
    window = state(name)
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._ensure_free_attacks(plan, window, window["decision_context"])
    assert plan.actions[0].action_type == ActionType.CLICK_BUTTON
    assert plan.fallback_reason == ""


def test_the_rule_only_runs_at_the_declare_attackers_window():
    window = state("G2_T8_MAIN1")
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.PASS_PRIORITY)])
    planner._ensure_free_attacks(plan, window, {"type": "actions_available"})
    assert plan.actions[0].action_type == ActionType.PASS_PRIORITY


# --- the precombat tap guard ----------------------------------------------------


def _loot_decision() -> PendingDecision:
    return PendingDecision(
        request_id=(169, 180),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:6",
                "Activate: Arni, Humble Scribe [{oT}: Draw a card, then discard a card.]",
                meta={
                    "actionType": "ActionType_Activate",
                    "instanceId": 221,
                    "grpId": 0,
                    "abilityGrpId": 1186,
                },
            ),
            DecisionOption("pass", "Pass", meta={"actionType": "ActionType_Pass"}),
        ),
        can_pass=True,
    )


def test_arni_loot_is_withheld_before_combat_against_an_empty_board(caplog):
    window = state("G2_T8_MAIN1")
    with caplog.at_level(logging.INFO, logger="arenamcp.play_safety"):
        decision = filter_play_options(_loot_decision(), window)
    assert [option.option_id for option in decision.options] == ["pass"]
    assert "Free attack: Arni, Humble Scribe attacks for 3" in caplog.text


def test_the_loot_stays_when_a_blocker_would_kill_arni():
    window = state("G2_T8_MAIN1")
    blocker = dict(next(card for card in window["battlefield"] if card["name"] == "Surveillance Phantasm"))
    blocker.update(instance_id=245, controller_seat_id=1, owner_seat_id=1, turn_entered_battlefield=5)
    window["battlefield"].append(blocker)
    arni = next(card for card in window["battlefield"] if card["instance_id"] == 221)
    assert tap_forfeits_free_attack(_loot_decision().options[0].label, arni, window) == ""


def test_the_loot_stays_after_combat_and_on_their_turn():
    window = state("G2_T8_MAIN1")
    arni = next(card for card in window["battlefield"] if card["instance_id"] == 221)
    label = _loot_decision().options[0].label
    window["turn"]["phase"] = "Phase_Main2"
    assert tap_forfeits_free_attack(label, arni, window) == ""
    window["turn"].update(phase="Phase_Main1", active_player=1)
    assert tap_forfeits_free_attack(label, arni, window) == ""


def test_mana_and_damage_tap_abilities_are_never_withheld():
    window = state("G2_T8_MAIN1")
    arni = next(card for card in window["battlefield"] if card["instance_id"] == 221)
    assert tap_forfeits_free_attack("Activate: Arni [{oT}: Add {U}.]", arni, window) == ""
    assert (
        tap_forfeits_free_attack("Activate: Arni [{oT}: It deals 1 damage to any target.]", arni, window)
        == ""
    )
    assert tap_forfeits_free_attack("Activate: Arni [{o2}: Draw a card.]", arni, window) == ""


# --- the role behind an evasive clock ---------------------------------------------


def _fresh(window: dict):
    _CACHE.clear()
    return assess(window)


def test_an_unblockable_clock_is_a_race_not_a_defender_lane():
    # G1 T9: Tetsuko makes Geist, Traxos and herself (all 1 power) unblockable.
    assessment = _fresh(state("G1_T9_ATTACK"))
    assert assessment.role == ROLE_RACE
    assert "evasive" in assessment.role_reason and "stop none of it" in assessment.role_reason


def test_a_blockable_clock_keeps_the_defender_lane():
    # G1 T11: Jiang Yanggu 4/4 can be blocked (and was, two turns later).
    assessment = _fresh(state("G1_T11_ATTACK"))
    assert assessment.role == ROLE_DEFENDER
