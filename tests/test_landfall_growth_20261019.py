"""Their landfall creatures grow in the lines (ledger #27, match 92c09c7a, 2026-10-19 09:30–09:34).

Pinky's mono-G landfall deck: Sazh's Chocobo ("Landfall — Whenever a land you
control enters, put a +1/+1 counter on this creature") grew a counter on each
of their turns, yet the T10 line said "survives (life 3, 3, 3)" against a
static 7/8 that was an 11/12 when it killed us on T11. The search now plays
one land per turn of theirs (more with "play an additional land") before each
of their attacks: counters stay, "gets +N/+N until end of turn" lasts the
attack.
"""

from __future__ import annotations

from copy import deepcopy

from tests.race_boards_20261019 import board, card

from arenamcp.board_model import build_board_model
from arenamcp.line_search import DEAD, search_lines
from arenamcp.line_search_moves import landfall_growth

_MOUNTAINS = [(401, "Mountain", False, 1), (402, "Mountain", False, 1)]
_CHOCOBO_78 = (450, "Sazh's Chocobo", False, 1, {"power": 7, "toughness": 8, "counters": {"+1/+1": 7}})


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9)


def test_landfall_growth_reads_counters_and_pumps_once_per_line():
    # The Chocobo's Arena text repeats its landfall line in a markup variant: one counter, not two.
    assert landfall_growth(card(1, "Sazh's Chocobo", 2)) == (1, 0, 0)
    assert landfall_growth(card(2, "Steppe Lynx", 2)) == (0, 2, 2)
    assert landfall_growth(card(3, "Icetill Explorer", 2)) == (0, 0, 0)  # its landfall only mills


def test_the_chocobo_hits_harder_every_turn():
    # 92c09c7a T10 in short: we are at 10 with nothing to block; their 7/8 Chocobo attacks
    # as an 8/9 on T11 (10 -> 2) and a 9/10 on T13. Before: "life 3" after T11.
    source = board(turn=10, life=10, their_life=11, ours=_MOUNTAINS, theirs=[_CHOCOBO_78], hand=[])
    result = _search(source)
    assert result.best.lives()[0] == 2, result.best.summary()
    assert result.best.cls == DEAD and result.best.dead_turn == 13


def test_an_extra_land_drop_grows_it_twice():
    explorer = (677, "Icetill Explorer", True, 3)  # tapped: it attacks on T11, not blocks on T10
    source = board(turn=10, life=12, their_life=11, ours=_MOUNTAINS, theirs=[_CHOCOBO_78, explorer], hand=[])
    result = _search(source)
    # 7/8 + two lands = 9/10, plus the Explorer's 2: 12 -> 1.
    assert result.best.lives()[0] == 1, result.best.summary()


def test_a_temporary_landfall_pump_lasts_their_attack():
    source = board(
        turn=10, life=10, their_life=11, ours=_MOUNTAINS[:1], theirs=[(451, "Steppe Lynx", False, 1)], hand=[]
    )
    result = _search(source)
    # A 0/1 that attacks as a 2/3 each turn: 2 a turn, not 0.
    assert result.best.lives()[:3] == [8, 6, 4], result.best.summary()
