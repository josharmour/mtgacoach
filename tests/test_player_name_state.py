"""Room metadata names survive publication/restart but never leak across matches."""

import json
from unittest.mock import Mock

import pytest

from arenamcp import server
from arenamcp.gamestate import GameState
from arenamcp.player_names import clean_names, snapshot_names


@pytest.fixture
def game(monkeypatch):
    state = GameState()
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "_deactivate_draft_state", Mock())
    monkeypatch.setattr(server, "mark_match_ended", Mock())
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: "local-test")
    return state


def room(match="named-match", local=1):
    return {
        "matchId": match,
        "systemSeatId": local,
        "gameRoomConfig": {
            "matchId": match,
            "reservedPlayers": [
                {"systemSeatId": local, "userId": "local-test", "playerName": "Armour"},
                {"systemSeatId": 3 - local, "userId": "other-test", "screenName": "mrpickle802"},
            ],
        },
    }


@pytest.mark.parametrize("seat", [1, 2])
def test_room_retains_both_names_and_checkpoint_keeps_seats(game, seat):
    server._handle_match_created(room(local=seat))
    game.game_number = 1
    expected = {seat: "Armour", 3 - seat: "mrpickle802"}
    assert game.get_snapshot()["player_names"] == expected
    assert game.opponent_name == "mrpickle802"
    checkpoint = json.loads(json.dumps(game.export_checkpoint()))
    restored = GameState()
    assert restored.restore_checkpoint(checkpoint)
    assert restored.get_published_snapshot()["player_names"] == expected
    game.note_game_identity("different-match", 1)
    assert not game.player_names


def test_new_room_clears_names_and_late_metadata_only_updates_current_match(game):
    server._handle_match_created(room())
    server._handle_match_created({"matchId": "new", "systemSeatId": 1})
    assert game.player_names == {}
    game.record_player_names("named-match", room()["gameRoomConfig"]["reservedPlayers"])
    assert not game.player_names
    game.record_player_names("new", [{"systemSeatId": "2", "playerName": "New rival"}, None])
    assert game.get_snapshot()["player_names"] == {2: "New rival"}
    game.reset()
    assert not game.player_names


def test_old_checkpoint_uses_only_known_opponent_and_no_guessed_local_name():
    state = GameState()
    state.match_id = "old"
    state.local_seat_id = 1
    state.opponent_name = "Known rival"
    checkpoint = state.export_checkpoint()
    checkpoint.pop("player_names")
    restored = GameState()
    assert restored.restore_checkpoint(checkpoint)
    snap = restored.get_snapshot()
    snap["opponent_seat_id"] = 2
    assert snapshot_names(snap) == {2: "Known rival"}
    assert clean_names({"1": "Name", True: "bad", "2": {"name": "bad"}, "9" * 5000: "bad"}) == {1: "Name"}
