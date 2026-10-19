"""The board facts' clock says their next hit and races our attackers (ledger #98, match 18b1601a T10).

standalone.log 12:05:22 / 12:05:38: "they kill us in 5 attack(s), we kill them
in 2 | race AHEAD (our clock 2 vs their 5, they strike first)". Their first
attack took us 9 -> 4 and the 5 counted three chump blocks by the very fliers
"we kill in 2" attacked with; Ordeal of Nylea's attack trigger (it fetched two
lands that attack) was nowhere. The facts now state their next attack, their
clock while our clock's attackers keep attacking (they can't block), race on
that, and name the permanents whose triggers the clock leaves out.
"""

from __future__ import annotations

from copy import deepcopy

from tests.race_boards_20261019 import OPPONENT, ORDEAL_T10, board, card

from arenamcp import board_assessment as ba


def _assess(source: dict):
    ba._CACHE.clear()
    return ba.assess(deepcopy(source))


def test_t10_facts_state_the_first_hit_and_the_racing_clock():
    facts = _assess(ORDEAL_T10)
    line = facts.facts_line()
    assert line.startswith(
        "they kill us in 5 attack(s) (3 while ours attack; their next attack: 5 -> you at 4), we kill them in 2"
    ), line
    assert "race AHEAD (our clock 2 vs their 3 while ours attack, they strike first)" in line
    assert "clock leaves out: Ordeal of Nylea (attack trigger)" in line
    assert (facts.their_clock, facts.their_clock_racing, facts.their_next_attack) == (5, 3, (5, 4))


def test_attackers_that_race_cannot_also_block():
    # Our 3/3 kills them in four attacks (their 2/2 chumps once) while their 2/2 can't attack
    # into it: before, "race AHEAD (we kill in 4, they get nothing through)". But a 3/3 that
    # attacks every turn is not home to block: at 2 life the 2/2 kills us first.
    source = board(
        turn=10, life=2, their_life=9, lands_played=0,
        ours=[(401, "Mountain", False, 1), (321, "Hill Giant", False, 3)],
        theirs=[(501, "Forest", False, 2), (601, "Grizzly Bears", False, 4)], hand=[],
    )  # fmt: skip
    facts = _assess(source)
    assert (facts.our_clock, facts.their_clock, facts.their_clock_racing) == (4, None, 2)
    assert facts.race == "behind", facts.race_detail
    assert "our clock 4 vs their 2 while ours attack" in facts.race_detail


def test_clock_unmodelled_names_attack_triggers_and_aura_triggers_only():
    courage = {
        "name": "Unflinching Courage",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +2/+2 and has trample and lifelink.",
    }
    growth = {
        "name": "Growth Aura",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nAt the beginning of your upkeep, put a +1/+1 counter on enchanted creature.",
    }
    cards = [
        card(1, "Ordeal of Nylea", OPPONENT),
        card(2, "Hired Claw", 1),
        card(3, "Grizzly Bears", OPPONENT),
        courage,
        growth,
    ]
    assert ba.clock_unmodelled(cards) == [
        "Ordeal of Nylea (attack trigger)",
        "Hired Claw (attack trigger)",
        "Growth Aura (aura trigger)",
    ]
