"""ConnectResp can arrive before the match room assigns the match ID."""

from unittest.mock import Mock

import pytest

from arenamcp import server
from arenamcp.gamestate import GameState, create_game_state_handler


@pytest.fixture
def state(monkeypatch):
    state = GameState()
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "_deactivate_draft_state", Mock())
    monkeypatch.setattr(server, "mark_match_ended", Mock())
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: "local-user")
    monkeypatch.setattr(state, "prewarm_card_cache", Mock(return_value=0))
    return state


def _connect(handler):
    handler(
        {
            "greToClientMessages": [
                {
                    "type": "GREMessageType_ConnectResp",
                    "systemSeatIds": [2],
                    "connectResp": {
                        "deckMessage": {
                            "deckCards": [75553] * 40 + list(range(1000, 1059)),
                            "sideboardCards": [2001],
                            "commanderGrpIds": [103511],
                        }
                    },
                }
            ]
        }
    )


@pytest.mark.parametrize("connect_first", [True, False])
def test_full_deck_available_on_first_turn_regardless_of_room_event_order(state, connect_first):
    handler = create_game_state_handler(state)
    if connect_first:
        _connect(handler)
    server._handle_match_created({"matchId": "hobbits-match", "systemSeatId": 2})
    if not connect_first:
        _connect(handler)
    state.update_from_message({"turnInfo": {"turnNumber": 1, "activePlayer": 2}})

    snapshot = state.get_published_snapshot()
    assert snapshot["match_id"] == "hobbits-match"
    assert snapshot["local_seat_id"] == 2
    assert len(snapshot["deck_cards"]) == 99
    assert snapshot["deck_cards"].count(75553) == 40
    assert snapshot["sideboard_cards"] == [2001]
    assert snapshot["commander_grp_ids"] == [103511]
    assert state.format_profile is not None


def test_new_known_match_clears_previous_deck_and_keeps_new_metadata(state):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "old-match", "systemSeatId": 2})
    state.turn_info.turn_number = 15

    server._handle_match_created({"matchId": "new-match", "systemSeatId": 1, "eventId": "Play_Brawl"})

    assert state.deck_cards == []
    assert state.sideboard_cards == []
    assert state.commander_grp_ids == []
    assert state.turn_info.turn_number == 0
    assert state.match_id == "new-match"
    assert state.local_seat_id == 1
    assert state.event_id == "Play_Brawl"
    server.mark_match_ended.assert_called_once()


def test_delayed_initial_match_id_keeps_already_received_game_state(state):
    _connect(create_game_state_handler(state))
    state.update_from_message(
        {"turnInfo": {"turnNumber": 1, "activePlayer": 2}, "players": [{"seatId": 2, "lifeTotal": 25}]}
    )
    server._handle_match_created({"matchId": "hobbits-match", "systemSeatId": 2})

    assert state.turn_info.turn_number == 1
    assert state.players[2].life_total == 25
    assert len(state.deck_cards) == 99


def test_explicit_reset_still_discards_connection_deck(state):
    _connect(create_game_state_handler(state))
    state.reset()
    server._handle_match_created({"matchId": "next-match", "systemSeatId": 2})
    assert state.deck_cards == []
    assert state.commander_grp_ids == []


def _new_connection(state, *, cards=None, commanders=None, sideboard=None):
    create_game_state_handler(state)(
        {
            "greToClientMessages": [
                {
                    "type": "GREMessageType_ConnectResp",
                    "systemSeatIds": [2],
                    "connectResp": {
                        "deckMessage": {
                            "deckCards": cards
                            if cards is not None
                            else [3000] * 24 + list(range(4000, 4075)),
                            "commanderGrpIds": commanders if commanders is not None else [5000],
                            "sideboardCards": sideboard if sideboard is not None else [6000],
                        }
                    },
                }
            ],
        }
    )


@pytest.mark.parametrize("connect_first", [True, False])
def test_second_match_connection_deck_survives_either_room_order(state, connect_first):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "old-match", "systemSeatId": 2})
    state.turn_info.turn_number = 15
    if connect_first:
        _new_connection(state)
    server._handle_match_created({"matchId": "new-match", "systemSeatId": 1})
    if not connect_first:
        _new_connection(state)
    state.publish_snapshot()
    snapshot = state.get_published_snapshot()
    assert snapshot["match_id"] == "new-match"
    assert len(snapshot["deck_cards"]) == 99
    assert snapshot["deck_cards"].count(3000) == 24
    assert snapshot["commander_grp_ids"] == [5000]
    assert snapshot["sideboard_cards"] == [6000]
    assert snapshot["turn_info"]["turn_number"] == 0
    assert state._connection_deck_metadata["match_id"] == "new-match"


@pytest.mark.parametrize("connect_first", [True, False])
def test_bound_connection_never_carries_into_third_match_without_new_connect(state, connect_first):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "first", "systemSeatId": 2})
    if connect_first:
        _new_connection(state)
    server._handle_match_created({"matchId": "second", "systemSeatId": 2})
    if not connect_first:
        _new_connection(state)
    assert len(state.deck_cards) == 99
    server._handle_match_created({"matchId": "third", "systemSeatId": 1})
    assert state.deck_cards == []
    assert state.sideboard_cards == []
    assert state.commander_grp_ids == []


def test_reconnect_bound_by_gameinfo_cannot_be_reused_as_next_matches_deck(state):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "same-match", "systemSeatId": 2})
    _new_connection(state)
    state.update_from_message({"gameInfo": {"matchID": "same-match"}})
    server._handle_match_created({"matchId": "next-match", "systemSeatId": 1})
    assert state.deck_cards == []


def test_unidentified_gameplay_consumes_reconnect_cross_match_eligibility(state):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "same-match", "systemSeatId": 2})
    _new_connection(state)
    state.update_from_message({"turnInfo": {"turnNumber": 6}})
    assert len(state.deck_cards) == 99
    server._handle_match_created({"matchId": "next-match", "systemSeatId": 1})
    assert state.deck_cards == []


def test_new_gameinfo_before_room_keeps_its_fresh_connection_deck(state):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "old-match", "systemSeatId": 2})
    _new_connection(state)
    state.update_from_message({"gameInfo": {"matchID": "new-match"}})
    server._handle_match_created({"matchId": "new-match", "systemSeatId": 2})
    assert len(state.deck_cards) == 99
    assert state.deck_cards.count(3000) == 24


def test_initial_gameinfo_then_room_then_connect_is_bound_once(state):
    state.update_from_message({"gameInfo": {"matchID": "first"}})
    server._handle_match_created({"matchId": "first", "systemSeatId": 2})
    _new_connection(state)
    assert state._connection_deck_metadata["match_id"] == "first"
    server._handle_match_created({"matchId": "second", "systemSeatId": 2})
    assert state.deck_cards == []


def test_delayed_initial_room_does_not_misbind_second_connection_to_first(state):
    _connect(create_game_state_handler(state))
    state.update_from_message({"turnInfo": {"turnNumber": 1}})
    server._handle_match_created({"matchId": "first", "systemSeatId": 2})
    _new_connection(state)
    server._handle_match_created({"matchId": "second", "systemSeatId": 2})
    assert len(state.deck_cards) == 99
    assert state.deck_cards.count(3000) == 24


def test_empty_new_sideboard_and_commander_do_not_retain_previous_deck_metadata(state):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "brawl", "systemSeatId": 2})
    _new_connection(state, cards=[3000] * 60, sideboard=[], commanders=[])
    server._handle_match_created({"matchId": "constructed", "systemSeatId": 2})
    assert len(state.deck_cards) == 60
    assert state.sideboard_cards == []
    assert state.commander_grp_ids == []


def test_completed_old_room_does_not_consume_incoming_connection(state):
    _connect(create_game_state_handler(state))
    server._handle_match_created({"matchId": "old", "systemSeatId": 2})
    _new_connection(state)
    server._handle_match_created({"matchId": "old", "stateType": "MatchGameRoomStateType_MatchCompleted"})
    server._handle_match_created({"matchId": "new", "systemSeatId": 2})
    assert state.deck_cards.count(3000) == 24


def test_command_zone_teaches_commander_identity_when_connect_omits_it(state):
    _new_connection(state, commanders=[])
    server._handle_match_created({"matchId": "brawl", "systemSeatId": 2})
    state.update_from_message(
        {
            "gameObjects": [
                {
                    "instanceId": 10,
                    "grpId": 5000,
                    "zoneId": 9,
                    "ownerSeatId": 2,
                    "type": "GameObjectType_Card",
                },
                {
                    "instanceId": 11,
                    "grpId": 5001,
                    "zoneId": 9,
                    "ownerSeatId": 2,
                    "type": "GameObjectType_Emblem",
                },
                {
                    "instanceId": 12,
                    "grpId": 5002,
                    "zoneId": 9,
                    "ownerSeatId": 2,
                    "type": "GameObjectType_Ability",
                },
                {
                    "instanceId": 13,
                    "grpId": 5003,
                    "zoneId": 9,
                    "ownerSeatId": 1,
                    "type": "GameObjectType_Card",
                },
                {
                    "instanceId": 14,
                    "grpId": 5004,
                    "zoneId": 9,
                    "ownerSeatId": 2,
                    "type": "GameObjectType_Token",
                },
            ],
            "zones": [{"zoneId": 9, "type": "ZoneType_Command", "objectInstanceIds": [10, 11, 12, 13, 14]}],
        }
    )
    assert state.commander_grp_ids == [5000]
    state.update_from_message(
        {
            "gameObjects": [{"instanceId": 10, "grpId": 5000, "zoneId": 8}],
            "zones": [
                {"zoneId": 9, "type": "ZoneType_Command", "objectInstanceIds": []},
                {"zoneId": 8, "type": "ZoneType_Battlefield", "objectInstanceIds": [10]},
            ],
        }
    )
    assert state.get_published_snapshot()["commander_grp_ids"] == [5000]
    server._handle_match_created({"matchId": "next", "systemSeatId": 2})
    assert state.commander_grp_ids == []
