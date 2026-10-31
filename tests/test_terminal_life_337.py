from copy import deepcopy

import pytest
from tests.test_life_gained_tracking import _life_annotation

from arenamcp.gamestate import GameState


@pytest.mark.parametrize("before,loss", [(10, -10), (12, -12), (11, -11), (10, -13)])
def test_terminal_signed_life_reconciles_omitted_total_once(before, loss):
    gs = GameState()
    gs.update_from_message({"players": [{"systemSeatNumber": 1, "lifeTotal": before}]})
    message = {
        "type": "GameStateType_Diff",
        "players": [{"systemSeatNumber": 1, "status": "PlayerStatus_PendingLoss"}],
        "annotations": [_life_annotation(99, 1, loss)],
    }
    original = deepcopy(message)
    gs.update_from_message(message)
    assert gs.players[1].life_total == before + loss
    gs.update_from_message(message)
    assert gs.players[1].life_total == before + loss
    assert message == original


@pytest.mark.parametrize(
    "status,life,annotations,expected",
    [
        ("PlayerStatus_InGame", None, [_life_annotation(99, 1, -10)], 10),
        ("PlayerStatus_PendingLoss", None, [], 10),
        ("PlayerStatus_PendingLoss", None, [_life_annotation(99, 1, -2)], 10),
        ("PlayerStatus_PendingLoss", 3, [_life_annotation(99, 1, -10)], 3),
    ],
)
def test_sparse_concession_and_explicit_totals_are_not_assumed_zero(status, life, annotations, expected):
    gs = GameState()
    gs.update_from_message({"players": [{"systemSeatNumber": 1, "lifeTotal": 10}]})
    data = {"systemSeatNumber": 1, "status": status}
    if life is not None:
        data["lifeTotal"] = life
    gs.update_from_message({"players": [data], "annotations": annotations})
    assert gs.players[1].life_total == expected
