"""Cast creatures are not permanent untapped blockers (ledger #129, match 3e545401 T17, 2026-10-19).

The lines kept Fleeting Effigy ("At the beginning of your end step, return this creature to its
owner's hand.") on the battlefield as a blocker for their next attack ("Fleeting Effigy — dead on
T22 (12, 4, -4)": it blocked the Pridemate), and ignored their Authority of the Consuls:
"Creatures your opponents control enter tapped. Whenever a creature an opponent controls enters,
you gain 1 life." The model said it in words (13:02:17: "Effigy enters tapped ..., returns to hand
at end step, and grants the lifegain engine 1 life"); the lines did not. Now a creature of ours
that leaves at our end step is gone before their turn (back in hand when we cast it in the line),
and one entering under their Consuls is tapped (no attack, no block on their turn) and gains them
life, a life-gain event their Hortimancer, Pridemate and Ajani grow on.
"""

from __future__ import annotations

from copy import deepcopy

from tests.engine_boards_20261019 import G3E5_T17
from tests.race_boards_20261019 import board

from arenamcp.board_model import build_board_model
from arenamcp.line_search import search_lines
from arenamcp.line_search_moves import Moves


def _moves(source: dict) -> Moves:
    moves = Moves(
        build_board_model(deepcopy(source)), survival=True, lethal_now=False, hard_ms=1e9, opponent=None
    )
    moves.free = True
    return moves


def _cast(moves: Moves, node, name: str) -> tuple:
    sets = moves.cast_sets(node, moves.sources(node, None)[1], instant_only=False)
    return next(c for c in sets if [moves.spells[v.spell].name for v, _ in c] == [name])


def test_effigy_under_their_consuls_enters_tapped_feeds_them_and_goes_home():
    moves = _moves(G3E5_T17)
    root = moves.root_node()
    mid = moves.begin(root, None, _cast(moves, root, "Fleeting Effigy"))
    effigy = next(b for b in mid.ours if b["name"] == "Fleeting Effigy")
    assert effigy["instance_id"] in mid.our_tapped  # enters tapped: its haste attacks nothing
    assert mid.opp_life == 15  # the Consuls' 1 life
    options, _possible = moves.attack_choices(mid)
    assert all(effigy["instance_id"] not in ids for _name, ids in options)
    node = moves.finish(mid, ("none", ()))
    # Gone at our end step and back in hand; their attack meets Slickshot alone, and their
    # creatures grew twice (Ajani's life and the Consuls'): Hortimancer 10/9, Pridemate 4/4.
    assert all(b["name"] != "Fleeting Effigy" for b in node.ours)
    assert any(moves.spells[i].name == "Fleeting Effigy" for i in node.hand)
    assert node.history[0].life_after == 8
    held = moves.finish(moves.begin(root, None, ()), ("none", ()))
    assert held.history[0].life_after == 9  # casting it costs us a life


def test_the_t17_lines_see_the_effigy_as_no_blocker():
    result = search_lines(build_board_model(deepcopy(G3E5_T17)), survival=True, lethal_now=False, hard_ms=1e9)
    cast = result.first_action[("cast", 392, None)]
    hold = result.first_action[("noattack",)]
    assert cast.steps[0].casts == ("Fleeting Effigy",) and cast.steps[0].life_after == 8, cast.summary()
    assert hold.steps[0].life_after == 9 and cast.score < hold.score  # was 12 with the Effigy blocking


def test_an_effigy_cast_after_combat_is_no_blocker_on_their_turn():
    # Our main 2, no Consuls: Effigy cast now still returns at our end step; their Bears attack
    # unblocked. Next turn it is ours to cast again.
    source = board(
        turn=9, life=20, their_life=20, phase="Phase_Main2", lands_played=1,
        ours=[(401, "Mountain", False, 1)], theirs=[(501, "Grizzly Bears", False, 2)],
        hand=[(343, "Fleeting Effigy")],
    )  # fmt: skip
    moves = _moves(source)
    root = moves.root_node()
    node = moves.finish(moves.begin(root, None, _cast(moves, root, "Fleeting Effigy")), ("none", ()))
    assert node.history[0].life_after == 18  # the Bears' 2 (an Effigy blocker took them before)
    assert [moves.spells[i].name for i in node.hand] == ["Fleeting Effigy"]
    again = moves.cast_sets(node, moves.sources(node, None)[1], instant_only=False)
    assert any([moves.spells[v.spell].name for v, _ in c] == ["Fleeting Effigy"] for c in again)
