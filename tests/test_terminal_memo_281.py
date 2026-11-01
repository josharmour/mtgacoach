"""Combat caching ignores draws only when library size cannot affect blocks."""

from arenamcp.board_model import build_board_model
from arenamcp.combat_loss import LossState
from arenamcp.line_search_moves import Moves
from test_combat_loss_281 import board, creature


def moves(attacker):
    model = build_board_model(board([attacker]))
    return Moves(model, survival=False, lethal_now=False, hard_ms=1e9, opponent=None), model.ours


def test_non_mill_combat_reuses_blocks_but_preserves_each_library():
    work, attackers = moves(creature(1, text="Toxic 1"))
    first = work.combat_detail(attackers, [], 30, defender=LossState(8, 40))
    second = work.combat_detail(attackers, [], 30, defender=LossState(8, 39))
    assert work.combats == 1
    assert first.loss == LossState(9, 40)
    assert second.loss == LossState(9, 39)
    assert not first.terminal and not second.terminal
    lethal = work.combat_detail(attackers, [], 30, defender=LossState(9, 39))
    assert work.combats == 2 and lethal.terminal
    assert lethal.loss == LossState(10, 39)


def test_mill_threshold_and_known_empty_library_stay_distinct():
    work, attackers = moves(creature(1, text=(
        "Whenever this creature deals combat damage to a player, that player mills three cards."
    )))
    safe = work.combat_detail(attackers, [], 30, defender=LossState(0, 4))
    empty = work.combat_detail(attackers, [], 30, defender=LossState(0, 3))
    unknown = work.combat_detail(attackers, [], 30, defender=LossState())
    assert work.combats == 3
    assert safe.loss.library == 1
    assert empty.loss.draw_loses and not empty.terminal
    assert unknown.loss.library is None
    ordinary, attackers = moves(creature(1))
    ordinary.combat_detail(attackers, [], 30, defender=LossState())
    empty = ordinary.combat_detail(attackers, [], 30, defender=LossState(0, 0))
    assert ordinary.combats == 2 and empty.loss.draw_loses
