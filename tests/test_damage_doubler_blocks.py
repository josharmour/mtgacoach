"""The chump-block guard counts damage doublers (bug_20261012_224324).

Turn 9, their combat, us at 21: Railway Brawler (11/11 trample) and
Ouroboroid (8/10) attacked with Absorbing Man and Titania ("Double all damage
that creature sources you control would deal") on their side. The planner
blocked Brawler with Arboreal Grazer (0/3) and Ouroboroid with The Notary
Hobbits; trample over Grazer is 8, doubled 16, leaving us at 5. The guard
read "save 3 damage; at 21 life we stay at 10 without the block", dropped the
Grazer block, and the unblocked Brawler's doubled 22 killed us at -1.
"""

from __future__ import annotations

from arenamcp.combat_strategy import (
    _after_combat,
    _next_attack_damage,
    damage_to_us_factor,
    safe_default_blocks,
    wasteful_chump_blocks,
)
from test_combat_safe_defaults import LOCAL, OPPONENT, board, creature

BRAWLER = creature(
    659,
    "Railway Brawler",
    11,
    11,
    OPPONENT,
    is_attacking=True,
    oracle_text="Reach\nTrample\nWhenever another creature you control enters, put X +1/+1 counters on it.",
    keywords=["trample", "reach"],
)
OUROBOROID = creature(660, "Ouroboroid", 8, 10, OPPONENT, is_attacking=True)
TITANIA = creature(
    681,
    "Absorbing Man and Titania",
    12,
    13,
    OPPONENT,
    oracle_text="Double all damage that creature sources you control would deal.",
)
GRAZER = creature(677, "Arboreal Grazer", 0, 3, LOCAL, oracle_text="Reach")
HOBBITS = [creature(iid, "The Notary Hobbits", 1, 1, LOCAL) for iid in (668, 675, 676)]
BLOCKS = [
    {"blockerInstanceId": iid, "attackerInstanceIds": [659, 660], "maxAttackers": 1}
    for iid in (677, 668, 675, 676)
]
PLANNED = {677: 659, 668: 660}


def _state(life: int = 21, doubler=TITANIA) -> dict:
    cards = [BRAWLER, OUROBOROID, GRAZER, *HOBBITS] + ([doubler] if doubler else [])
    return board(life, cards, BLOCKS)


def test_the_live_grazer_block_is_kept():
    assert wasteful_chump_blocks(_state(), PLANNED) == {}


def test_without_the_doubler_the_same_chump_is_still_dropped():
    dropped = wasteful_chump_blocks(_state(life=40, doubler=None), PLANNED)
    assert 677 in dropped
    assert "save 3 damage" in dropped[677]


def test_their_next_attack_is_doubled_through_the_creatures_we_keep():
    # #171 (match 2b77f14d T9): their next attack goes through the creatures this combat leaves us,
    # each absorbing one of their biggest attackers, no longer their raw board power (62 here).
    # With both chumps our two Hobbits absorb Titania and the Brawler (10 tramples over a 1/1) and
    # Ouroboroid connects: (10 + 8) x 2 = 36. Without the Grazer chump it absorbs one more: 10 x 2.
    state = _state(life=80)
    cards = {card["instance_id"]: card for card in state["battlefield"]}
    threats = [BRAWLER, OUROBOROID, TITANIA]
    ours = [cards[iid] for iid in (677, 668, 675, 676)]
    factors = {OPPONENT: 2}
    left, coming = _after_combat([BRAWLER, OUROBOROID], PLANNED, cards, ours, threats)
    assert _next_attack_damage(coming, left, factors) == 36
    left, coming = _after_combat([BRAWLER, OUROBOROID], {668: 660}, cards, ours, threats)
    assert _next_attack_damage(coming, left, factors) == 20
    assert _next_attack_damage(coming, left, {}) == 10
    # At 80 life (58 without the block) that is far from lethal: the chump goes, doubled numbers named.
    dropped = wasteful_chump_blocks(state, PLANNED)
    assert "save 6 damage" in dropped[677] and "stay at 58" in dropped[677]


def test_doubled_damage_is_reported_in_the_reason():
    # At 200 nothing is close: the drop names the doubled numbers.
    dropped = wasteful_chump_blocks(_state(life=200), PLANNED)
    assert "save 6 damage" in dropped[677]
    assert "stay at 178" in dropped[677]


def test_factor_recognises_doubler_wordings_and_owners():
    state = _state()
    assert damage_to_us_factor(state, OPPONENT, LOCAL) == 2
    # Our own doubler does not double their creatures.
    ours = {**TITANIA, "controller_seat_id": LOCAL}
    assert damage_to_us_factor(_state(doubler=ours), OPPONENT, LOCAL) == 1
    marauders = creature(
        700,
        "Angrath's Marauders",
        4,
        4,
        OPPONENT,
        oracle_text="If a source you control would deal damage to a permanent or player, it deals double that "
        "damage to that permanent or player instead.",
    )
    assert damage_to_us_factor(_state(doubler=marauders), OPPONENT, LOCAL) == 2
    emancipation = {
        **marauders,
        "oracle_text": "If a source you control would deal damage to a permanent or player, it deals triple that "
        "damage to that permanent or player instead.",
    }
    assert damage_to_us_factor(_state(doubler=emancipation), OPPONENT, LOCAL) == 3
    furnace = creature(
        701,
        "Furnace of Rath",
        0,
        0,
        LOCAL,
        oracle_text="If a source would deal damage to a permanent or player, it deals double that damage to "
        "that permanent or player instead.",
    )
    assert damage_to_us_factor(_state(doubler=furnace), OPPONENT, LOCAL) == 2


def test_repeated_clause_on_one_card_counts_once():
    # Arena's text repeats clauses (Railway Brawler's trigger appears three times).
    tripled = {**TITANIA, "oracle_text": "\n".join([TITANIA["oracle_text"]] * 3)}
    assert damage_to_us_factor(_state(doubler=tripled), OPPONENT, LOCAL) == 2
    state = _state()
    state["battlefield"].append({**TITANIA, "instance_id": 682})
    assert damage_to_us_factor(state, OPPONENT, LOCAL) == 4


def test_safe_default_blocks_survive_the_doubled_attack():
    assignments, reason = safe_default_blocks(_state())
    # Whatever it picks must keep us alive: under 21 doubled damage.
    through = int(reason.split(" damage at ")[0].rsplit(" ", 1)[-1])
    assert through < 21, reason
    assert assignments
