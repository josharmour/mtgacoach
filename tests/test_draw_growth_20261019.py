"""Their creatures that grow on their draws (ledger #143, match 970d9b41, 2026-10-19).

Ravenhill Flock ("Whenever you draw a card, put a +1/+1 counter on this creature.") and Lakeshore
Apothecary ("Whenever you draw your second card each turn, ..."). On T12 the mode comparison
projected Iroh's Demonstration's 1-damage mode at 19, 18, 17 (a static 1/2 Flock) and the facts
said "they kill us in 20"; the 1-damage mode was chosen, the Flock grew on every draw and the real
lives were 18, 15, 10, 2. Their draw growth now comes before each of their attacks: one draw a
turn, two once they have shown a cantrip (Confusticate and Bebother in their graveyard).
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import G970_T12
from tests.race_boards_20261019 import board, card

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model, their_draws
from arenamcp.line_search import search_lines
from arenamcp.line_search_moves import Moves, draw_growth


def _search(source: dict):
    survival, lethal_now = ba.search_hints(build_board_model(deepcopy(source)))
    return search_lines(
        build_board_model(deepcopy(source)), survival=survival, lethal_now=lethal_now, hard_ms=1e9
    )


def test_draw_growth_reads_each_card_and_the_second_card():
    assert draw_growth(card(1, "Ravenhill Flock", 2)) == (1, 0)
    assert draw_growth(card(2, "Lakeshore Apothecary", 2)) == (0, 1)
    assert draw_growth(card(3, "Hired Claw", 2)) == (0, 0)
    temporary = {
        "name": "Temp",
        "oracle_text": "Whenever you draw a card, this creature gets +1/+1 until end of turn.",
    }
    assert draw_growth(temporary) == (0, 0)
    assert their_draws(G970_T12) == 2  # Confusticate and Bebother: "Draw two cards, then discard a card."
    assert their_draws({**G970_T12, "graveyard": []}) == 1


def test_t12_the_one_damage_mode_leaves_a_growing_flock():
    result = _search(G970_T12)
    sweep = result.first_action[("cast", 325, 0)]
    kill = result.first_action[("cast", 325, 1)]
    # Two draws a turn: the Flock attacks as 3/4, 5/6, 7/8 (was 19, 18, 17; real 18, 15, 10).
    assert sweep.lives()[:3] == [17, 12, 5], sweep.summary()
    assert kill.lives()[:3] == [20, 20, 20] and kill.score > sweep.score
    # One draw a turn without the cantrip: 18, 15, 11.
    alone = _search({**deepcopy(G970_T12), "graveyard": []}).first_action[("cast", 325, 0)]
    assert alone.lives()[:3] == [18, 15, 11], alone.summary()


def test_t12_their_clock_and_threats_see_the_growth():
    ba._CACHE.clear()
    facts = ba.assess(deepcopy(G970_T12))
    assert facts.their_clock == 4  # was 20
    assert "their clock counts: Ravenhill Flock grows +2/+2 a turn" in facts.facts_line()
    assert "grows +2/+2 each turn of theirs" in facts.threats[0].why


def test_the_second_card_counts_only_with_two_draws():
    apothecary = (384, "Lakeshore Apothecary", False, 15)
    source = board(
        turn=16, life=15, their_life=13, ours=[(401, "Mountain", False, 1)], theirs=[apothecary], hand=[]
    )
    model = build_board_model(deepcopy(source))
    assert [b["_growth"] for b in model.theirs] == [0]
    source["graveyard"] = [{**G970_T12["graveyard"][0]}]
    model = build_board_model(deepcopy(source))
    assert [b["_growth"] for b in model.theirs] == [1]


def test_a_growing_flier_outranks_a_one_one_looter():
    # 970d9b41 T14: the Flock 2/3 beside Bilbo, Luckwearer 1/1 ("can't be blocked"; loots on damage).
    source = board(
        turn=14, life=18, their_life=16, ours=[(401, "Mountain", False, 1)],
        theirs=[(351, "Ravenhill Flock", False, 11, {"power": 2, "toughness": 3, "counters": {"P1P1": 1}}),
                (367, "Bilbo, Luckwearer", False, 13)],
        hand=[(376, "Shock")],
    )  # fmt: skip
    model = build_board_model(deepcopy(source))
    moves = Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None)
    order = sorted(model.theirs, key=moves._threat_order)
    assert [b["name"] for b in order] == ["Ravenhill Flock", "Bilbo, Luckwearer"]
    ba._CACHE.clear()
    assert ba.assess(deepcopy(source)).threats[0].name == "Ravenhill Flock"
