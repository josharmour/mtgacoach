"""The chump guard weighs their next attack through the creatures we keep, not their raw board power (#171).

Match 2b77f14d (2026-10-19, Brawl, Leonardo), T9 at the DeclareBlockers
request (gameStateId 252, 15:05:39): The Unbeatable Squirrel Girl 5/5,
Bakersbane Duo 4/4 and two Squirrels 2/2 attacked us at 19 with sixteen more
creatures at home (twelve fresh Squirrels, a Wolf, Peregrin Took, Llanowar
Elves): 20 creatures, 47 power. The plan chumped all four attackers. The guard
compared our life with their whole board's power (47 > 19), so "the block keeps
us out of lethal range" could never hold, and it dropped all four ("at 19 life
we stay at 6 without the block"). We took 13; the next attack was lethal and
the coach conceded.
"""

from __future__ import annotations

from arenamcp.combat_strategy import (
    _after_combat,
    _chump_for_next_attack,
    _next_attack_damage,
    wasteful_chump_blocks,
)

US, THEM = 1, 2
SQUIRREL_GIRL, BAKERSBANE, SQUIRREL_A, SQUIRREL_B = 463, 451, 470, 496
PLANNED = {484: SQUIRREL_GIRL, 503: SQUIRREL_A, 504: SQUIRREL_B, 500: BAKERSBANE}
HOME_SQUIRRELS = (528, 529, 530, 531, 537, 538, 539, 540, 541, 542, 543, 544, 560)


def creature(iid, name, power, toughness, seat, *, tapped=False, attacking=False, text="", token=False):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Token Creature" if token else "Creature",
        "card_types": ["CardType_Creature"],
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_tapped": tapped,
        "is_attacking": attacking,
        "object_kind": "TOKEN" if token else "CARD",
        "oracle_text": text,
        "keywords": [],
    }


def t9_board(life=19, home_squirrels=HOME_SQUIRRELS):
    ours = [
        creature(
            514,
            "Leonardo, Big Brother",
            14,
            3,
            US,
            text="Sneak {W}\nLeonardo, Big Brother gets +1/+0 for each other creature you control.",
        ),
        *(creature(iid, "Hare Apparent", 2, 2, US) for iid in (506, 500)),
        *(creature(iid, "Hare Apparent", 2, 2, US, tapped=True) for iid in (480, 476, 473)),
        *(creature(iid, "Rabbit", 1, 1, US, token=True) for iid in (484, 503, 504, 505, 509, 510, 511, 512)),
    ]
    theirs = [
        creature(SQUIRREL_GIRL, "The Unbeatable Squirrel Girl", 5, 5, THEM, tapped=True, attacking=True),
        creature(BAKERSBANE, "Bakersbane Duo", 4, 4, THEM, tapped=True, attacking=True),
        *(
            creature(iid, "Squirrel", 2, 2, THEM, tapped=True, attacking=True, token=True)
            for iid in (SQUIRREL_A, SQUIRREL_B)
        ),
        creature(518, "Peregrin Took", 3, 4, THEM),
        creature(439, "Llanowar Elves", 2, 2, THEM, tapped=True, text="{T}: Add {G}."),
        creature(557, "Wolf", 3, 3, THEM, token=True),
        *(creature(iid, "Squirrel", 2, 2, THEM, token=True) for iid in home_squirrels),
    ]
    return {
        "match_id": "2b77f14d",
        "local_seat_id": US,
        "players": [
            {"seat_id": US, "is_local": True, "life_total": life},
            {"seat_id": THEM, "is_local": False, "life_total": 19},
        ],
        "battlefield": [*ours, *theirs],
        "hand": [],
        "stack": [],
        "turn": {
            "turn_number": 9,
            "active_player": THEM,
            "phase": "Phase_Combat",
            "step": "Step_DeclareBlock",
        },
    }


def test_all_four_chumps_stay_against_the_wide_board():
    assert wasteful_chump_blocks(t9_board(), PLANNED) == {}


def test_their_next_attack_goes_through_the_creatures_we_keep():
    state = t9_board()
    cards = {card["instance_id"]: card for card in state["battlefield"]}
    ours = [card for card in cards.values() if card["controller_seat_id"] == US]
    theirs = [card for card in cards.values() if card["controller_seat_id"] == THEM]
    attackers = [card for card in theirs if card["is_attacking"]]
    assert sum(card["power"] for card in theirs) == 47 and len(theirs) == 20 and len(ours) == 14
    # All four chumps: ten creatures left absorb 5, 4, 3, 3 and six 2s; 20 of the 47 get through.
    left, coming = _after_combat(attackers, PLANNED, cards, ours, theirs)
    assert len(left) == 10 and len(coming) == 20
    assert _next_attack_damage(coming, left) == 20
    # Without the cheapest chump (17 life after this combat) an eleventh blocker: 18 through.
    without = {b: a for b, a in PLANNED.items() if b != 503}
    left, coming = _after_combat(attackers, without, cards, ours, theirs)
    assert _next_attack_damage(coming, left) == 18


def test_far_from_their_next_attack_the_chumps_still_go():
    dropped = wasteful_chump_blocks(t9_board(life=60), PLANNED)
    assert set(dropped) == set(PLANNED)
    assert "at 60 life we stay at 47 without the block" in dropped[484]


def test_dead_next_turn_either_way_keeps_the_chump_only_against_a_wider_board():
    # Without the chump we die to their next attack and with it we live: it stays.
    assert _chump_for_next_attack(19, 17, 18, 18, wider=False)
    # Alive next turn without it: it goes.
    assert not _chump_for_next_attack(19, 17, 20, 16, wider=True)
    # Dead either way: kept against a wider board, or when the line search's best line lives on.
    assert _chump_for_next_attack(19, 17, 20, 18, wider=True)
    assert not _chump_for_next_attack(19, 17, 20, 18, wider=False)
    assert not _chump_for_next_attack(19, 17, 20, 18, wider=False, line_survives=lambda: None)
    assert not _chump_for_next_attack(19, 17, 20, 18, wider=False, line_survives=lambda: False)
    assert _chump_for_next_attack(19, 17, 20, 18, wider=False, line_survives=lambda: True)
