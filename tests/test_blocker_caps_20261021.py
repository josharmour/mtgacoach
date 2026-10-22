"""Blocker caps: "can't be blocked by more than N creatures" (2026-10-21 22:09, match 7b898598).

bug_20261021_220913: the opponent's Flopsie, Bumi's Buddy ("Each creature you
control with power 4 or greater can't be blocked by more than one creature")
was out; the planner sent nine Rabbits at the 9/9 trampling Groundchuck &
Dirtbag. The Mac adapter's DeclareBlockersResp (gsid 334) came back
IllegalRequest FailureReason_InvalidBlocker, the refreshed request held no
selection, and the autopilot stood down MANUAL REQUIRED; the attack went
unblocked. The declaration should have been one Rabbit.
"""

from __future__ import annotations

from tests.test_block_declaration_20261019 import creature

from arenamcp.combat_strategy import max_blockers, repair_blocker_caps, repair_menace_blocks

OPPONENT, LOCAL = 1, 2
GROUNDCHUCK, FLOPSIE, FLOODCALLER = 520, 557, 514
RABBITS = (506, 512, 513, 532, 533)
HARE = 528
FLOPSIE_TEXT = (
    "When Flopsie enters, put a +1/+1 counter on each creature you control.\n"
    "Each creature you control with power 4 or greater can't be blocked by more than one creature."
)


def board(attacker_power=9, attacker_toughness=9):
    battlefield = [
        creature(
            GROUNDCHUCK,
            "Groundchuck & Dirtbag",
            attacker_power,
            attacker_toughness,
            OPPONENT,
            keywords=["trample"],
            attacking=True,
            text="Trample\nWhenever you tap a land for mana, add {G}.",
        ),
        creature(FLOPSIE, "Flopsie, Bumi's Buddy", 5, 5, OPPONENT, text=FLOPSIE_TEXT),
        creature(FLOODCALLER, "Valley Floodcaller", 3, 3, OPPONENT, attacking=True),
        creature(HARE, "Hare Apparent", 2, 2, LOCAL),
        *(creature(iid, "Rabbit", 1, 1, LOCAL, token=True) for iid in RABBITS),
    ]
    return {
        "local_seat_id": LOCAL,
        "players": [
            {"seat_id": LOCAL, "is_local": True, "life_total": 25},
            {"seat_id": OPPONENT, "is_local": False, "life_total": 20},
        ],
        "battlefield": battlefield,
        "hand": [],
        "turn": {"turn_number": 13, "active_player": OPPONENT, "step": "Step_DeclareBlock"},
    }


def test_flopsie_caps_big_attackers_at_one_blocker_only():
    state = board()
    cards = {card["instance_id"]: card for card in state["battlefield"]}
    assert max_blockers(cards[GROUNDCHUCK], cards) == 1
    assert max_blockers(cards[FLOODCALLER], cards) is None  # power 3: Flopsie does not apply
    cards[GROUNDCHUCK]["oracle_text"] = "Trample\nThis creature can't be blocked by more than two creatures."
    assert max_blockers(cards[GROUNDCHUCK], cards) == 1  # the stricter of the two still rules
    del cards[FLOPSIE]
    assert max_blockers(cards[GROUNDCHUCK], cards) == 2


def test_nine_rabbit_gang_block_becomes_one_chump():
    state = board()
    plan = {iid: GROUNDCHUCK for iid in RABBITS}
    repaired, notes = repair_blocker_caps(state, plan)
    assert len(repaired) == 1 and set(repaired.values()) == {GROUNDCHUCK}
    assert len(notes) == 1 and "can't be blocked by more than 1 creature" in notes[0]
    # The menace repair runs the cap too, so both the planner guard and the bridge see it.
    assert repair_menace_blocks(state, plan) == (repaired, notes)
    # A legal pair on the uncapped attacker is untouched.
    pair = {506: FLOODCALLER, 512: FLOODCALLER}
    assert repair_menace_blocks(state, pair) == (pair, [])


def test_the_kept_blocker_is_the_one_that_kills_when_one_can():
    state = board(attacker_power=4, attacker_toughness=2)
    plan = {HARE: GROUNDCHUCK, 506: GROUNDCHUCK, 512: GROUNDCHUCK}
    repaired, _notes = repair_blocker_caps(state, plan)
    assert repaired == {HARE: GROUNDCHUCK}  # the 2/2 kills the x/2; the 1/1s would not
