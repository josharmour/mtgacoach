"""Their creatures that grow when they cast a spell grow in the lines (ledger #82, match 772233af).

2026-10-19, T14-T15 (standalone.log 11:37:50): we were at 8 facing Baron
Helmut Zemo 4/4 ("Whenever you cast a black spell from your hand, Baron Helmut
Zemo connives") and Kaervek, the Punisher 3/3. The line said "dead on T17"
(7 through on T15, survive at 1); they cast Mabel, Bitter Recluse, Zemo
connived to 5/5 and 8 damage met our 8 life on T15. The connive cousin of
#27's landfall growth: before each later attack of theirs, a creature with an
unconditional "whenever you cast a ... spell" +1/+1-counter or connive trigger
gets one spell's worth of counters (paranoid: they cast one spell a turn).
"""

from __future__ import annotations

from copy import deepcopy

from tests.race_boards_20261019 import CARDS, board, card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import DEAD, search_lines
from arenamcp.line_search_moves import cast_growth

# Arena's own rules text (MTGA card DB, grpIds 104982 and 90438).
CARDS.setdefault(
    "Baron Helmut Zemo",
    {
        "type_line": "Legendary Creature — Human Noble Villain", "mana_cost": "{B}{B}{B}", "power": 3,
        "toughness": 3, "card_types": ["CardType_Creature"],
        "oracle_text": "Whenever you cast a black spell from your hand, Baron Helmut Zemo connives.\n"
        "Boast — Exile any number of black cards from your graveyard with fifteen or more black mana symbols "
        "among their mana costs: Copy those exiled cards. You may cast up to three of the copies without paying "
        "their mana costs.",
    },
)  # fmt: skip
CARDS.setdefault(
    "Kaervek, the Punisher",
    {
        "type_line": "Legendary Creature — Human Warlock", "mana_cost": "{1}{B}{B}", "power": 3, "toughness": 3,
        "card_types": ["CardType_Creature"],
        "oracle_text": "Whenever you commit a crime, exile up to one target black card from your graveyard and "
        "copy it. You may cast the copy. If you do, you lose 2 life. (Copies of permanent spells become tokens.)",
    },
)  # fmt: skip
CARDS.setdefault(
    "Growing Adept",
    {
        "type_line": "Creature — Human Wizard", "mana_cost": "{1}{U}", "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"],
        "oracle_text": "Whenever you cast a noncreature spell, put a +1/+1 counter on this creature.",
    },
)  # fmt: skip
CARDS.setdefault(
    "Prowess Monk",
    {
        "type_line": "Creature — Human Monk", "mana_cost": "{1}{R}", "power": 2, "toughness": 2,
        "card_types": ["CardType_Creature"],
        "oracle_text": "Prowess (Whenever you cast a noncreature spell, this creature gets +1/+1 until end of turn.)",
    },
)  # fmt: skip

_SIX_MOUNTAINS = [(401 + i, "Mountain", False, 1) for i in range(6)]
_ZEMO_44 = (460, "Baron Helmut Zemo", False, 9, {"power": 4, "toughness": 4, "counters": {"+1/+1": 1}})
_KAERVEK = (461, "Kaervek, the Punisher", False, 11)


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9)


def test_cast_growth_reads_counter_and_connive_triggers_only():
    assert cast_growth(card(1, "Baron Helmut Zemo", 2)) == 1
    assert cast_growth(card(2, "Growing Adept", 2)) == 1
    assert cast_growth(card(3, "Kaervek, the Punisher", 2)) == 0
    assert cast_growth(card(4, "Prowess Monk", 2)) == 0  # a temporary pump, not growth
    heroic = {"name": "Akroan Hoplite", "oracle_text": "Whenever you cast a spell that targets Akroan Hoplite, "
              "put a +1/+1 counter on Akroan Hoplite."}  # fmt: skip
    assert cast_growth(heroic) == 0  # conditional on what the spell targets
    second = {"name": "Tenth Spell", "oracle_text": "Whenever you cast your second spell each turn, put a +1/+1 "
              "counter on Tenth Spell."}  # fmt: skip
    assert cast_growth(second) == 0


def test_zemo_connives_into_lethal_on_their_next_turn():
    # 772233af T14 in short: we are at 8 with an empty hand; Zemo 4/4 + Kaervek 3/3 attack on T15.
    # Before: 7 through, "dead on T17". Their spell makes Zemo 5/5: 8 through on T15.
    source = board(turn=14, life=8, their_life=9, ours=_SIX_MOUNTAINS, theirs=[_ZEMO_44, _KAERVEK], hand=[])
    result = _search(source)
    assert result.best.cls == DEAD and result.best.dead_turn == 15, result.best.summary()


def test_the_growth_keeps_coming_every_turn():
    # Zemo alone: 4/4 now, 5 on T15 (12 -> 7), 6 on T17 (7 -> 1), 7 on T19 (dead).
    source = board(turn=14, life=12, their_life=9, ours=_SIX_MOUNTAINS, theirs=[_ZEMO_44], hand=[])
    result = _search(source)
    assert result.best.lives()[:2] == [7, 1], result.best.summary()


def test_creatures_without_cast_growth_stay_put():
    source = board(turn=14, life=8, their_life=9, ours=_SIX_MOUNTAINS, theirs=[_KAERVEK], hand=[])
    result = _search(source)
    assert result.best.lives()[:2] == [5, 2], result.best.summary()
