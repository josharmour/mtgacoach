"""Gang and exactly-lethal blocks into open mana are charged their trick risk (#74).

2026-10-19 T13 (match abde2a07, 11:32:57): a lone Creakwood Safewright 5/5
attacked into our three untapped creatures (Emberheart Challenger 2/2 x2,
Slickshot Show-Off 1/2) with Swamp + Forest open and 4 cards in hand.
``combat_solver.optimal_blocks`` scored the triple block with exactly 5 power
at 0.0 against -6.67 for no block, at every life total from 8 to 20; the coach
prompt called it "Computed optimal blocks" and the planner followed ("Trade
the whole board to kill Creakwood Safewright"). Scarblade's Malice ({B}:
deathtouch and lifelink) killed all three blockers and gained them 5. The
board is the match's Player.log at the DeclareBlockersReq.
"""

from __future__ import annotations

import pytest

from arenamcp.combat_solver import (
    TrickExposure,
    attacking_trick_exposure,
    optimal_blocks,
    trick_probability,
)
from arenamcp.combat_strategy import safe_default_blocks

US, THEM = 1, 2
EMBERHEART_TEXT = (
    "Haste\nProwess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.)\n"
    "Valiant — Whenever this creature becomes the target of a spell or ability you control for the first "
    "time each turn, exile the top card of your library. Until end of turn, you may play that card."
)
SLICKSHOT_TEXT = (
    "Flying, haste\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.\n"
    "Plot {1}{R} (You may pay {1}{R} and exile this card from your hand. Cast it as a sorcery on a later "
    "turn without paying its mana cost. Plot only as a sorcery.)"
)
SAFEWRIGHT_TEXT = (
    "This creature enters with three -1/-1 counters on it.\nAt the beginning of your end step, if there is "
    "an Elf card in your graveyard and this creature has a -1/-1 counter on it, remove a -1/-1 counter from "
    "this creature."
)
ADHERENTS_TEXT = "Trample\nThis creature gets +1/+1 for each creature you control and each creature card in your graveyard."
OPEN = TrickExposure(open_mana=2, hand_size=4)


def creature(iid, name, power, toughness, seat, text="", *, type_line="Creature", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": text,
        "is_tapped": False,
        **extra,
    }


def land(iid, name, seat, *, tapped=False, color="R"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": f"Basic Land — {name}",
        "card_types": ["CardType_Land"],
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "oracle_text": f"({{T}}: Add {{{color}}}.)",
        "is_tapped": tapped,
    }


def safewright():
    return creature(
        321,
        "Creakwood Safewright",
        5,
        5,
        THEM,
        SAFEWRIGHT_TEXT,
        type_line="Creature — Elf Warrior",
        is_attacking=True,
        is_tapped=True,
    )


def our_blockers():
    return [
        *(
            creature(
                iid,
                "Emberheart Challenger",
                2,
                2,
                US,
                EMBERHEART_TEXT,
                type_line="Creature — Mouse Warrior",
                keywords=["haste"],
            )
            for iid in (330, 333)
        ),
        creature(
            343,
            "Slickshot Show-Off",
            1,
            2,
            US,
            SLICKSHOT_TEXT,
            type_line="Creature — Bird Wizard",
            keywords=["flying", "haste"],
        ),
    ]


def turn_13_board(*, life=20, opponent_hand=4, open_lands=True) -> dict:
    """11:32:57: Moon-Vigil Adherents cast precombat, Safewright attacks alone, Swamp + Forest open."""
    return {
        "turn": {
            "turn_number": 13,
            "active_player": THEM,
            "priority_player": US,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
        "local_seat_id": US,
        "opponent_seat_id": THEM,
        "players": [
            {"seat_id": US, "life_total": life, "is_local": True, "mana_pool": {}},
            {"seat_id": THEM, "life_total": 11, "is_local": False, "mana_pool": {}},
        ],
        "battlefield": [
            *our_blockers(),
            *(land(iid, "Mountain", US) for iid in (281, 290, 305, 329, 342)),
            safewright(),
            creature(
                363,
                "Moon-Vigil Adherents",
                5,
                5,
                THEM,
                ADHERENTS_TEXT,
                type_line="Creature — Elf Druid",
                keywords=["trample"],
                turn_entered_battlefield=13,
            ),
            creature(
                324, "Lys Alana Dignitary", 2, 3, THEM, "", type_line="Creature — Elf Advisor", is_tapped=True
            ),
            creature(
                337,
                "Thornweald Archer",
                2,
                1,
                THEM,
                "Reach\nDeathtouch",
                type_line="Creature — Elf Archer",
                keywords=["reach", "deathtouch"],
            ),
            land(279, "Forest", THEM, tapped=not open_lands, color="G"),
            land(285, "Swamp", THEM, tapped=not open_lands, color="B"),
            land(299, "Forest", THEM, tapped=True, color="G"),
            land(320, "Forest", THEM, tapped=True, color="G"),
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"opponent_hand_count": opponent_hand},
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": [330, 333, 343],
            "raw_blockers": [
                {"blockerInstanceId": iid, "attackerInstanceIds": [321], "maxAttackers": 1}
                for iid in (330, 333, 343)
            ],
            "attacker_ids": [321],
        },
    }


TRIPLE = {330: 321, 333: 321, 343: 321}


def test_the_t13_exposure_is_two_open_mana_and_four_cards():
    assert attacking_trick_exposure(turn_13_board()) == OPEN
    assert attacking_trick_exposure(turn_13_board(open_lands=False)) == TrickExposure(0, 4)
    assert attacking_trick_exposure({**turn_13_board(), "zones": {}}) is None  # hand unknown: no charge
    # A lone attacker into two or more blockers is the attack that only makes sense with a trick.
    assert trick_probability(OPEN, lone_attacker=True) > trick_probability(OPEN) > 0
    assert trick_probability(TrickExposure(0, 4)) == trick_probability(TrickExposure(2, 0)) == 0


@pytest.mark.parametrize("life", [20, 15, 12, 10, 8])
def test_the_zero_slack_triple_block_is_rejected_into_open_mana(life):
    plan = optimal_blocks([safewright()], our_blockers(), life, trick_exposure=OPEN)
    assert plan is not None and plan.assignments != TRIPLE
    # At most one body is put at risk: no gang block on the 5/5.
    assert len(plan.assignments) <= 1
    assert len(plan.blockers_lost_ids) <= 1
    assert plan.damage_through < life
    assert plan.trick_rejected.startswith(
        "Emberheart Challenger blocks Creakwood Safewright, Emberheart Challenger blocks Creakwood "
        "Safewright, Slickshot Show-Off blocks Creakwood Safewright"
    )
    assert "on Creakwood Safewright 5/5 needs exactly lethal damage" in plan.trick_rejected
    assert "a deathtouch trick kills all 3 blockers" in plan.trick_rejected
    assert "(they have 2 open mana and 4 cards in hand)" in plan.trick_rejected


def test_without_open_mana_or_cards_the_triple_block_still_stands():
    # Nothing to cast: the kill is safe, and callers without a hand (the opponent's view) are unchanged.
    for exposure in (None, TrickExposure(0, 4), TrickExposure(2, 0)):
        plan = optimal_blocks([safewright()], our_blockers(), 20, trick_exposure=exposure)
        assert plan.assignments == TRIPLE and plan.trick_cost == 0 and not plan.trick_rejected


def test_a_gang_block_that_is_the_only_safe_line_is_kept():
    # 3 life against a 5/5 trampler: a chump lets 3 trample over (dead); the triple block holds it.
    trampler = {**safewright(), "oracle_text": "Trample", "keywords": ["trample"]}
    plan = optimal_blocks([trampler], our_blockers(), 3, trick_exposure=OPEN)
    assert plan.damage_through < 3
    assert plan.assignments == TRIPLE
    assert "Creakwood Safewright 5/5 needs exactly lethal damage" in plan.trick_risk


def test_an_overkill_single_block_is_not_charged():
    # A 4/4 blocking a 2/2 kills it with two to spare and survives a +2/+2: no exact damage, one blocker.
    attacker = creature(500, "Bear", 2, 2, THEM, is_attacking=True)
    wall = creature(501, "Wall", 4, 4, US)
    plan = optimal_blocks([attacker], [wall], 20, trick_exposure=OPEN)
    assert plan.assignments == {501: 500} and plan.trick_cost == 0


def test_safe_default_blocks_price_the_trick():
    blocks, reason = safe_default_blocks(turn_13_board())
    assert len(blocks) <= 1
    assert set(blocks) != set(TRIPLE)


def test_the_block_prompt_names_the_rejected_gang_block():
    from arenamcp.coach import CoachEngine

    state = turn_13_board()
    ours = [card for card in state["battlefield"] if card["controller_seat_id"] == US]
    theirs = [card for card in state["battlefield"] if card["controller_seat_id"] == THEM]
    local = next(player for player in state["players"] if player["is_local"])
    lines = CoachEngine.__new__(CoachEngine)._format_block_combat(
        ours, theirs, local, 13, "Combat", set(), state["decision_context"], state
    )
    prompt = "\n".join(lines)
    computed = next(line for line in lines if line.startswith("Computed optimal blocks:"))
    assert computed.count(" blocks Creakwood Safewright") <= 1
    risk = next(line for line in lines if line.startswith("Trick risk: rejected "))
    assert (
        "Emberheart Challenger, Emberheart Challenger and Slickshot Show-Off on Creakwood Safewright 5/5"
        in risk
    )
    assert "they have 2 open mana and 4 cards in hand" in risk
    assert "take the hit or block with one creature rather than risk two or more" in prompt
