"""A finished match stops tactical advice and inputs until the next match starts."""

import json
from unittest.mock import Mock

import pytest

from arenamcp.standalone import StandaloneCoach


def test_completed_match_stops_coaching_until_new_match(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "_completed_match_for_navigation", {"match_id": "finished"})
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach.ui = Mock()
    assert coach._completed_match_wait({"match_id": "finished"})
    assert coach._completed_match_wait({"match_id": "finished"})
    coach.ui.advice.assert_called_once_with("Match finished.", "MATCH")
    assert not coach._completed_match_wait({"match_id": "new-match"})
    assert not coach._completed_match_wait({})


def test_low_life_without_match_result_does_not_stop_coaching(monkeypatch):
    from arenamcp import server

    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach.ui = Mock()
    state = {"match_id": "active", "players": [{"is_local": True, "life_total": -2}]}
    assert not coach._completed_match_wait(state)
    coach.ui.advice.assert_not_called()


def _room(match_id="synthetic-finished", state_type="MatchGameRoomStateType_MatchCompleted"):
    return json.dumps(
        {
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {"stateType": state_type, "gameRoomConfig": {"matchId": match_id}}
            }
        }
    )


@pytest.mark.parametrize(
    ("visible_match", "held"),
    [("synthetic-finished", True), ("synthetic-new", False), (None, False), ({"$bad_ptr": True}, False)],
)
def test_post_reload_terminal_room_binds_the_same_visible_board(monkeypatch, tmp_path, visible_match, held):
    from tests.test_mac_game_state import scripted_probe
    from tests.test_server_bridge_overlay import _DummyBridge, _StubGameState

    from arenamcp import gre_bridge, server
    from arenamcp.mac_game_state import fetch_game_state
    from arenamcp.watcher import MTGALogHandler, MTGALogWatcher

    # Normal watcher resumes at EOF, so the in-process parser never received
    # the completed room. The connected bridge still shows the last board.
    path = tmp_path / "Player.log"
    path.write_text(_room() + "\n")
    watcher = MTGALogWatcher(callback=lambda _: None, log_path=str(path))
    watcher._handler = MTGALogHandler(str(path), lambda _: None)
    assert watcher.find_relevant_start() == (path.stat().st_size, "idle_or_completed")
    assert watcher.idle_completed_match_id == "synthetic-finished"
    monkeypatch.setattr(server, "watcher", watcher)
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    monkeypatch.setattr(server, "game_state", _StubGameState({}))
    monkeypatch.setattr(server, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server, "enrich_with_oracle_text", lambda _: {})
    probe, _ = scripted_probe()
    probe.scalars["GameInfo"] = {"MatchID": visible_match}
    bridge = _DummyBridge(fetch_game_state(probe.send, None))
    monkeypatch.setattr(gre_bridge, "get_bridge", lambda: bridge)
    state = server.get_game_state()
    assert state["match_id"] is None and state["turn"]["turn_number"] > 0
    assert "visible_match_id" not in state
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach.ui = Mock()
    assert coach._completed_match_wait(state) is held
    assert coach._completed_match_wait(state) is held
    assert coach.ui.advice.call_count == int(held)
    if held:
        assert coach._completed_match_wait({**state, "match_id": "synthetic-finished"})
        assert not coach._completed_match_wait({**state, "match_id": "synthetic-new"})
        with path.open("a") as file:
            file.write('{"type":"GREMessageType_MulliganReq"}\n')
        watcher.poll()
        assert not coach._completed_match_wait(server.get_game_state())


@pytest.mark.parametrize(
    "newer",
    [
        "==> EventEnterPairing {}",
        '{"greToClientEvent":{}}',
        '{"CardsInPack":[]}',
        '{"type":"GREMessageType_SubmitDeckReq"}',
        "MatchCreated",
        _room("synthetic-next", "MatchGameRoomStateType_Playing"),
    ],
)
def test_new_activity_revokes_the_idle_room_for_startup_and_incremental_reads(newer):
    from arenamcp.watcher import _idle_completed_room

    assert _idle_completed_room(_room() + "\n" + newer) is None
    assert _idle_completed_room(newer, "synthetic-finished") is None


def test_bo3_game_complete_and_unbound_room_do_not_end_coaching():
    from arenamcp.watcher import _idle_completed_room

    assert _idle_completed_room('{"matchState":"MatchState_GameComplete","gameNumber":1}') is None
    assert _idle_completed_room('{"type":"GREMessageType_IntermissionReq"}') is None
    assert _idle_completed_room(_room(None)) is None
    assert _idle_completed_room(_room("synthetic-finished") + "\n" + '{"gold":0}') == "synthetic-finished"
    payload = json.loads(_room())
    payload["matchGameRoomStateChangedEvent"]["gameRoomInfo"]["finalMatchResult"] = {"matchId": "other"}
    assert _idle_completed_room(json.dumps(payload)) is None
