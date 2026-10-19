"""Their planeswalkers in the line search and the board facts (ledger #42, a51dea64 10:19–10:25).

Opponent Azorius Angels with Kaito, Cunning Infiltrator and Ajani Resolute:
the lines, the board facts ("1 vs 0 creatures") and the line guard ignored
both walkers. Face burn was tagged "~best" while the right play killed
Youthful Valkyrie so Slickshot Show-Off could finish Ajani, and the shadow
guard described Lightning Strike at Kaito as "(on opponent)". Their walkers are
now burn targets ("any target" and "... or planeswalker" damage) and attack
recipients (their blockers protect the walker first), each worth
``combat_solver.walker_value`` to them and going up one loyalty a turn.
"""

from __future__ import annotations

from copy import deepcopy

from tests.race_boards_20261019 import board
from tests.strategic_states import actions_decision, cast

from arenamcp import board_assessment as ba
from arenamcp.board_model import build_board_model
from arenamcp.line_guard import option_note
from arenamcp.line_search import proxy_outcome, search_lines
from arenamcp.line_search_moves import walker_burn

_LANDS = [(401, "Mountain", False, 1), (402, "Mountain", False, 1)]


def _search(source: dict):
    return search_lines(build_board_model(deepcopy(source)), survival=False, lethal_now=False, hard_ms=1e9)


def _kaito(loyalty: int) -> tuple:
    return (329, "Kaito, Cunning Infiltrator", False, 8, {"counters": {"Loyalty": loyalty}})


def _ajani(loyalty: int) -> tuple:
    return (348, "Ajani Resolute", False, 10, {"counters": {"Loyalty": loyalty}})


def test_walker_burn_reads_any_target_and_planeswalker_targets():
    assert walker_burn("~ deals 3 damage to any target.") == 3
    assert walker_burn("~ deals 3 damage to up to one target creature or planeswalker.") == 3
    assert walker_burn("~ deals 2 damage to target player or planeswalker.") == 2
    assert walker_burn("~ deals 3 damage to target creature.") == 0
    assert walker_burn("~ deals 3 damage to each opponent.") == 0


def test_lightning_strike_kills_kaito_rather_than_going_face():
    # 10:22:17 (T9): Strike took Kaito from 4 to 1. At 3 loyalty one Strike kills it.
    source = board(
        turn=9, life=20, their_life=16, ours=_LANDS, theirs=[_kaito(3)], hand=[(338, "Lightning Strike")]
    )
    result = _search(source)
    assert result.best.steps[0].targets == (("Lightning Strike", "Kaito, Cunning Infiltrator"),)
    assert "on Kaito, Cunning Infiltrator" in result.best.summary()
    decision = actions_decision(
        [("idx:0", "Cast Lightning Strike", cast(338), True), ("pass", "Pass", None, None)]
    )
    assert option_note(result, decision.options[0], deepcopy(source)).startswith("[LINE best: ")


def test_strike_the_valkyrie_so_slickshot_finishes_ajani():
    # The review's right play: Youthful Valkyrie (1/3 flier) would block Slickshot to save Ajani;
    # Strike kills the Valkyrie and the Slickshot finishes Ajani at 3: its printed 1/2, 3/2 once
    # Strike's cast pumps it (ledger #94; the board said 3 power while the search couldn't see that).
    source = board(
        turn=11, life=20, their_life=16, ours=[*_LANDS, (320, "Slickshot Show-Off", False, 7)],
        theirs=[_ajani(3), (345, "Youthful Valkyrie", False, 10)], hand=[(338, "Lightning Strike")],
    )  # fmt: skip
    result = _search(source)
    first = result.best.steps[0]
    assert first.targets == (("Lightning Strike", "Youthful Valkyrie"),), result.best.summary()
    assert first.at_walkers == (("Slickshot Show-Off", "Ajani Resolute"),)
    assert "Slickshot Show-Off (at Ajani Resolute)" in first.text()
    # The +2/+0 trick replay keeps the split attack.
    replay = proxy_outcome(result, result.best)
    assert replay is not None and replay.steps[0].at_walkers == first.at_walkers


def test_their_blockers_protect_the_walker():
    # Without the Strike the Valkyrie blocks the Slickshot sent at Ajani: Ajani lives.
    source = board(
        turn=11, life=20, their_life=16, ours=[*_LANDS, (320, "Slickshot Show-Off", False, 7, {"power": 3})],
        theirs=[_ajani(3), (345, "Youthful Valkyrie", False, 10)], hand=[],
    )  # fmt: skip
    from arenamcp.line_search_moves import Moves

    moves = Moves(
        build_board_model(deepcopy(source)), survival=False, lethal_now=False, hard_ms=1e9, opponent=None
    )
    moves.free = True
    mid = moves.begin(moves.root_node(), None, ())
    at_ajani = [option for option in moves.attack_choices(mid)[0] if option[0].startswith("at:348:")]
    assert at_ajani, moves.attack_choices(mid)[0]
    node = moves.finish(mid, at_ajani[0])
    # The Valkyrie blocked (and died to the 3 damage); Ajani lives and goes up to 4.
    assert node.walkers == ((348, "Ajani Resolute", 4),)
    assert [b["name"] for b in node.theirs] == []


def test_board_facts_name_their_planeswalkers():
    source = board(
        turn=9, life=20, their_life=14, ours=[*_LANDS, (320, "Slickshot Show-Off", False, 7)],
        theirs=[_kaito(4)], hand=[],
    )  # fmt: skip
    ba._CACHE.clear()
    assessment = ba.assess(deepcopy(source))
    assert "their planeswalkers Kaito, Cunning Infiltrator (loyalty 4)" in assessment.role_reason
