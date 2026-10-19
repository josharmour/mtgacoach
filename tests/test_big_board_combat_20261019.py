"""The attack search on big Brawl boards (ledger #162).

2026-10-19 15:05:42 (match 2b77f14d T10, our main 1): "attack search hit its
2.0s deadline with 14 attackers; best so far: attack with nobody". We had
Leonardo 14/3, five Hare Apparents and eight Rabbits at 6 life; they had
Peregrin Took, a Wolf and thirteen untapped 2/2 Squirrels at 19. Each
one-attacker subset enumerated 2^15 block assignments (0.6 s apiece), so the
search ran out of time after "nobody" and a few singles; the coaching thread
stalled 16 s. Interchangeable bodies (the Squirrels, our Rabbits) are now one
class: blocks are enumerated once per permutation, attacks once per count of
each class, all-in and all-in less the crackback reserve first.
"""

from __future__ import annotations

import logging
import time
from itertools import product

import arenamcp.combat_solver as solver


def _c(iid: int, name: str, power: int, toughness: int, controller: int, **extra) -> dict:
    card = {
        "instance_id": iid, "name": name, "power": power, "toughness": toughness,
        "controller_seat_id": controller, "owner_seat_id": controller, "type_line": "Creature",
        "card_types": ["CardType_Creature"], "oracle_text": "",
    }  # fmt: skip
    card.update(extra)
    return card


def board_t10():
    """Player.log gsid 267 (T10 main 1), replayed through gamestate."""
    ours = [
        _c(514, "Leonardo, Big Brother", 14, 3, 1,
           oracle_text="Sneak {oW}\nLeonardo gets +1/+0 for each other creature you control."),
    ]  # fmt: skip
    ours += [_c(i, "Hare Apparent", 2, 2, 1) for i in (506, 500, 480, 476, 473)]
    ours += [_c(i, "Rabbit", 1, 1, 1, object_kind="TOKEN") for i in (484, 503, 504, 505, 509, 510, 511, 512)]
    squirrels = (528, 529, 530, 531, 537, 538, 539, 540, 541, 542, 543, 544, 560)
    blockers = [_c(518, "Peregrin Took", 3, 4, 2), _c(557, "Wolf", 3, 3, 2, object_kind="TOKEN")]
    blockers += [_c(i, "Squirrel", 2, 2, 2, object_kind="TOKEN") for i in squirrels]
    tapped = [
        _c(463, "The Unbeatable Squirrel Girl", 5, 5, 2, is_tapped=True),
        _c(451, "Bakersbane Duo", 4, 4, 2, is_tapped=True),
        _c(439, "Llanowar Elves", 2, 2, 2, is_tapped=True),
        _c(470, "Squirrel", 2, 2, 2, is_tapped=True, object_kind="TOKEN"),
        _c(496, "Squirrel", 2, 2, 2, is_tapped=True, object_kind="TOKEN"),
    ]
    return ours, blockers, blockers + tapped


def test_the_fourteen_attacker_board_is_searched_to_the_end(caplog):
    solver._MEMO.clear()
    ours, blockers, crackers = board_t10()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="arenamcp.combat_solver"):
        plan = solver.optimal_attacks(ours, blockers, 19, 6, crackers, [])
    assert time.monotonic() - started < 2.0
    assert "deadline" not in caplog.text
    # Their fifteen untapped blockers stop all fourteen attackers: nothing gets through, and
    # their crackback kills us whatever we keep home. "Nobody" is the verdict, not a timeout.
    assert plan.damage_through == 0 and plan.attacker_ids == []


def test_all_in_and_the_crackback_reserve_are_scored_first():
    ours, _blockers, crackers = board_t10()
    seeds = [tuple(range(len(ours)))]
    order = list(solver._attack_order(ours, seeds))
    assert order[0] == tuple(range(14))
    # One attack per count of Leonardos (0-1), Hares (0-5) and Rabbits (0-8).
    assert len(order) == 2 * 6 * 9
    assert len(set(order)) == len(order)
    reserve = solver._crackback_reserve(ours[:6], [], crackers[:2], 20)
    assert reserve == tuple(range(6))  # two 2/2s attacking into twenty life need no one home


def test_a_big_board_lethal_all_in_is_found():
    solver._MEMO.clear()
    ours = [_c(1, "Leonardo, Big Brother", 38, 3, 1)] + [
        _c(10 + i, "Hare Apparent", 2, 2, 1) for i in range(8)
    ]
    ours += [_c(100 + i, "Rabbit", 1, 1, 1, object_kind="TOKEN") for i in range(16)]
    theirs = [_c(500, "Soul Warden", 14, 14, 2), _c(501, "Ranger-Captain of Eos", 15, 15, 2)]
    started = time.monotonic()
    plan = solver.optimal_attacks(ours, theirs, 30, 14, theirs, [])
    assert time.monotonic() - started < 2.0
    assert plan.damage_through >= 30 and plan.score == 10_000.0


def test_symmetric_block_enumeration_matches_the_exhaustive_search():
    attackers = [_c(1, "Hare Apparent", 2, 2, 1), _c(2, "Rabbit", 1, 1, 1), _c(3, "Big", 5, 3, 1)]
    blockers = [_c(10 + i, "Squirrel", 2, 2, 2) for i in range(5)] + [_c(20, "Wall", 0, 6, 2)]
    for life in (3, 6, 20):
        exhaustive = solver._search_blocks(attackers, blockers, life, max_options=10**6)
        symmetric = solver._search_blocks(attackers, blockers, life, max_options=500, symmetric=True)
        assert symmetric.evaluated < exhaustive.evaluated
        assert (symmetric.score, symmetric.damage_through) == (exhaustive.score, exhaustive.damage_through)
    # Off by default: the other callers keep their greedy plan past the cap.
    greedy = solver._search_blocks(attackers, blockers, 6, max_options=500)
    assert greedy.explanation.startswith("greedy")


def test_canonical_choices_are_one_per_permutation_class():
    options = [[None, 0, 1]] * 3 + [[None, 0]]
    groups = solver._symmetric_groups([_c(1, "S", 2, 2, 2), _c(2, "S", 2, 2, 2), _c(3, "S", 2, 2, 2),
                                       _c(4, "W", 0, 4, 2)], options)  # fmt: skip
    choices = list(solver._canonical_choices(groups, options))
    assert len(choices) == solver._canonical_count(groups, options) == 10 * 2
    classes = {(tuple(sorted(c[:3], key=repr)), c[3]) for c in product(*options)}
    assert len(classes) == len(choices)


def test_a_god_below_its_devotion_is_no_blocker():
    """Match 2bb162d7 T9/T11 (14:22:21, 14:25): "Heliod, Sun-Crowned 5/5 can block Leonardo, Big
    Brother" held back a 27/3 Leonardo; Player.log L364471 shows Heliod as CardType_Enchantment only
    (devotion to white below five) and no creature of theirs untapped."""
    from arenamcp.combat_keywords import is_creature_now
    from arenamcp.combat_strategy import _eligible_blockers

    heliod = _c(
        480, "Heliod, Sun-Crowned", 5, 5, 2, type_line="Legendary Enchantment Creature — God",
        card_types=["CardType_Enchantment"],
    )  # fmt: skip
    awake = {**heliod, "instance_id": 481, "card_types": ["CardType_Enchantment", "CardType_Creature"]}
    assert not is_creature_now(heliod) and is_creature_now(awake)
    assert is_creature_now({"type_line": "Creature — Rabbit"})  # no GRE types: the printed line
    assert [c["instance_id"] for c in _eligible_blockers({"battlefield": [heliod, awake]}, 2)] == [481]
