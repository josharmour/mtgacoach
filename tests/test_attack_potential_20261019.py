"""Their attack's pumps, attack-trigger grants and extra combats (ledger #191, #193, #194).

#193 (match 62d61e75 T11, 16:56:39): the chump guard counted 3+2+2+2 = 9 and
dropped Rabbit->Isshin ("at 19 life we stay at 10 without the block"); after
blocks they sacrificed four Treasures, each Captain Lannery Storm got +4 and
3+6+6+6 = 21 killed us from 19.
#191 (match f80ca0cf T13): Aurelia, the Warleader's extra combat was "left
out" of the clock; five chumps took us 7 -> 4 and the second combat came.
#194 (match 3ac9d511 T11): Harbin, Vanguard Aviator's "five or more
Soldiers" trigger was "left out" of every projection.
Boards: ``tests/threat_boards_20261019.py`` (rebuilt from Player.log).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from arenamcp import combat_strategy as cs
from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.attack_potential import extra_combat_of, grants_of, pumped_attackers, unpriced_pumps
from arenamcp.board_assessment import assess, clock_unmodelled
from arenamcp.concede import estimate_loss
from threat_boards_20261019 import (
    LANNERY_IDS,
    ME,
    OPP,
    PLANNED_62D61E75,
    PLANNED_F80CA0CF,
    US,
    aurelia_t13,
    card,
    harbin_t11,
    land,
    lannery_t11_blocks,
    state,
)

pytestmark = pytest.mark.usefixtures("unlimited_search_deadline")


def incoming(board, seat):
    return {
        a["instance_id"]: a for a in cs._incoming_attackers(board, seat, cs._legal_block_map(board, None))
    }


def their_creatures(board, seat):
    return [
        c
        for c in board["battlefield"]
        if c["controller_seat_id"] == seat and "CardType_Creature" in c["card_types"]
    ]


# --- #193: Treasure-fed and mana pumps --------------------------------------------------------


def test_each_lannery_storm_counts_their_four_treasures():
    attackers = incoming(lannery_t11_blocks(), US)
    for iid in LANNERY_IDS:
        assert (attackers[iid]["power"], attackers[iid]["toughness"]) == (6, 2)
        assert attackers[iid]["potential_pump_note"] == "4 treasure"
    assert (attackers[455]["power"], attackers[499]["power"]) == (3, 3)  # Isshin and Delina: no pump
    assert sum(a["power"] for a in attackers.values()) == 24  # what they could deal: 21 got through


def test_the_rabbit_chump_on_isshin_stays():
    # Before: {521: "... at 19 life we stay at 10 without the block"}. With the pumps the plan lets
    # 18 through (19 -> 1) and dropping the chump is 21: lethal.
    assert cs.wasteful_chump_blocks(lannery_t11_blocks(), dict(PLANNED_62D61E75)) == {}


def test_without_their_treasures_the_chump_still_goes():
    dropped = cs.wasteful_chump_blocks(lannery_t11_blocks(treasures=()), dict(PLANNED_62D61E75))
    assert set(dropped) == {521}
    assert "at 19 life we stay at 10 without the block" in dropped[521]


def test_the_safe_default_blocks_two_lannery_storms():
    assignments, reason = cs.safe_default_blocks(lannery_t11_blocks())
    assert set(assignments) == {517, 521}
    assert set(assignments.values()) <= set(LANNERY_IDS)
    assert "12 damage at 19 life (no-block 24)" in reason


def test_the_planner_block_guard_replaces_the_plan_that_leaves_one_life():
    board = lannery_t11_blocks()
    context = {
        "type": "declare_blockers",
        "legal_blocker_ids": [517, 521],
        "raw_blockers": board["decision_context"]["raw_blockers"],
    }
    plan = ActionPlan(
        actions=[
            GameAction(
                action_type=ActionType.DECLARE_BLOCKERS, blocker_instance_assignments=dict(PLANNED_62D61E75)
            )
        ]
    )
    ActionPlanner(Mock())._check_block_survival(plan, board, context)
    action = plan.actions[0]
    assert set(action.blocker_instance_assignments.values()) <= set(LANNERY_IDS)
    assert plan.fallback_reason == "planner_danger_block"
    assert "18 damage at 19 life" in action.reasoning


def test_the_board_facts_count_the_pumps_in_the_attack_under_way():
    facts = assess(lannery_t11_blocks())
    assert facts.our_life_now_attack == 7  # 24 potential, 12 through our best blocks


def firebreather_board(untapped_mountains):
    breather = card(
        700, "Furnace Whelp", OPP, power=2, toughness=2, text="Flying\n{oR}: This creature gets +1/+0 until end of turn.",
        tapped=True, attacking=True,
    )  # fmt: skip
    mountains = [
        land(710 + i, "Mountain", OPP, tapped=i >= untapped_mountains, text="({T}: Add {R}.)")
        for i in range(4)
    ]
    ours = [card(720, "Rabbit", ME, power=1, toughness=1, token=True)]
    return state(
        [breather, *mountains, *ours], match="test", local=ME, life=20, opp_life=20, turn=5, active=OPP,
        phase="Phase_Combat", step="Step_DeclareBlock", their_hand=3,
    )  # fmt: skip


def test_a_mana_pump_is_bounded_by_their_open_mana():
    for untapped in (0, 1, 3):
        board = firebreather_board(untapped)
        (whelp,) = pumped_attackers([board["battlefield"][0]], board)
        assert whelp["power"] == 2 + untapped
    # Their next attack: every land untaps.
    board = firebreather_board(1)
    (whelp,) = pumped_attackers([board["battlefield"][0]], board, next_turn=True)
    assert whelp["power"] == 6


def test_a_pump_we_cannot_price_keeps_every_block():
    board = lannery_t11_blocks(treasures=())
    isshin = next(c for c in board["battlefield"] if c["instance_id"] == 455)
    isshin["oracle_text"] += "\nSacrifice another creature: Isshin gets +2/+0 until end of turn."
    assert unpriced_pumps(list(incoming(board, US).values()), board)
    assert cs.wasteful_chump_blocks(board, dict(PLANNED_62D61E75)) == {}


# --- #191: an extra combat ----------------------------------------------------------------------


def test_aurelia_grants_one_extra_combat_with_everyone_untapped():
    aurelia = next(c for c in aurelia_t13()["battlefield"] if c["instance_id"] == 548)
    extra = extra_combat_of(aurelia)
    assert extra is not None and extra.untap_all


def test_the_extra_combat_after_these_blocks_deals_aurelias_three_again():
    # Five Rabbits chump the ground (Bofur 5/1 dies to its Rabbit): Aurelia's 3 in the air, then
    # the second combat: Bombur, Glóin, Roaming Throne, Thorin and Aurelia into our 12 left: 3 more.
    board = aurelia_t13()
    attackers = list(incoming(board, ME).values())
    assert cs.extra_combat_damage(board, attackers, dict(PLANNED_F80CA0CF)) == 3
    assert cs.wasteful_chump_blocks(board, dict(PLANNED_F80CA0CF)) == {}


def test_no_extra_combat_once_their_attackers_are_tapped():
    # The second combat itself: the trigger is spent (first time each turn), attackers stay tapped.
    board = aurelia_t13()
    for permanent in board["battlefield"]:
        if permanent.get("is_attacking") and permanent["instance_id"] != 548:
            permanent["is_tapped"] = True
    attackers = list(incoming(board, ME).values())
    assert cs.extra_combat_damage(board, attackers, dict(PLANNED_F80CA0CF)) == 0


def test_the_board_facts_play_both_combats():
    for board in (aurelia_t13(blocks=False), aurelia_t13()):
        facts = assess(board)
        # Before: "their next attack: 3 -> you at 4", "clock leaves out: Aurelia, the Warleader".
        assert facts.their_next_attack == (6, 1)
        assert not any("Aurelia" in note for note in facts.clock_unmodelled)
        assert any("Reyav" in note for note in facts.clock_unmodelled)  # still not played
    assert assess(aurelia_t13()).our_life_now_attack == 1
    assert assess(aurelia_t13(blocks=False)).their_clock == 2


def test_the_chump_guard_forecast_counts_two_combats():
    board = aurelia_t13(blocks=False)
    _threats, combats = cs._their_next_attack(board, their_creatures(board, OPP), ME)
    assert combats == 2


def test_only_their_side_of_the_trigger_counts_as_modelled():
    # The projections play Aurelia for the opponent only; one of ours would still be left out.
    board = aurelia_t13(blocks=False)
    aurelia = next(c for c in board["battlefield"] if c["instance_id"] == 548)
    assert clock_unmodelled([aurelia], modelled_seat=OPP) == []
    assert clock_unmodelled([aurelia], modelled_seat=ME) == ["Aurelia, the Warleader (attack trigger)"]
    assert clock_unmodelled([aurelia]) == ["Aurelia, the Warleader (attack trigger)"]


def test_the_concede_check_counts_the_second_combat():
    # At 6 life Aurelia's 3 in the air twice is lethal; at 7 it is not.
    assert estimate_loss(aurelia_t13()).reason == "not dead next attack"
    loss = estimate_loss(aurelia_t13(life=6))
    assert loss.facts["evasive_power"] == 6
    assert loss.facts["defensive_life_after"] == 0


# --- #194: attack-trigger team grants -------------------------------------------------------------


def test_harbins_trigger_is_read():
    harbin = next(c for c in harbin_t11()["battlefield"] if c["instance_id"] == 444)
    (grant,) = grants_of(harbin)
    assert (grant.need, grant.kind, grant.power, grant.toughness, grant.keywords) == (
        5,
        "soldiers",
        1,
        1,
        ("flying",),
    )


def test_four_soldiers_do_not_meet_it():
    # The real T11: Harbin, Shackle Slinger, Siege Veteran and the Soldier token. Four Soldiers do
    # not meet Harbin's 5-Soldier trigger, while Siege Veteran's beginning-of-combat trigger puts
    # a +1/+1 counter on Harbin (6/5 -> 7/6 in the air, ledger #198).
    facts = assess(harbin_t11())
    assert facts.their_next_attack == (7, 1)
    assert facts.clock_unmodelled == []
    board = harbin_t11()
    threats, _combats = cs._their_next_attack(board, their_creatures(board, OPP), ME)
    assert [t["power"] for t in threats] == [7, 3, 2, 1]
    assert not any("flying" in t["keywords"] for t in threats)


def test_a_fifth_soldier_gives_the_whole_attack_plus_one_and_flying():
    # Rescue Retriever (cast that turn) able to attack: five Soldiers, all +1/+1 and flying over our
    # ground blockers, plus Siege Veteran's +1/+1 counter on Harbin: 8+4+3+2+4 = 21 against our 8.
    board = harbin_t11(fifth_soldier=True)
    threats, _combats = cs._their_next_attack(board, their_creatures(board, OPP), ME)
    assert [t["power"] for t in threats] == [8, 4, 3, 2, 4]
    assert all("flying" in t["keywords"] for t in threats)
    facts = assess(board)
    assert facts.their_next_attack == (21, -13)
    assert facts.opp_lethal_on_board
    loss = estimate_loss(board)
    assert loss.facts["evasive_power"] == 21
    assert estimate_loss(harbin_t11()).reason == "not dead next attack"
