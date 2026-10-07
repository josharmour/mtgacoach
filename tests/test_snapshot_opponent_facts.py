"""Log-derived opponent facts in the snapshot (multi-turn planning WP6).

Every GRE message below is a trimmed copy of a real Player.log message from
2026-10-06 (zones, objects and annotations the facts depend on; everything
else dropped). Field shapes are verbatim:

- A card bounced into the opponent's hand (Player.log line 3413) gets a
  GameObjectType_RevealedCard stand-in whose zoneId is their hand and which is
  listed in their ZoneType_Revealed zone. The real card stays a hidden id in
  the hand. When it is cast (line 4312) the GRE sends
  AnnotationType_RevealedCardDeleted and an empty Revealed zone, which omits
  objectInstanceIds.
- A basic fetched with "reveal it" (lines 54327/54590) produces a
  CardRevealed stand-in in the library and a RevealedCardCreated stand-in in
  the hand. Only the hand one is in their hand.
- Library ids are listed even though the zone is Visibility_Hidden, so the
  opponent's library size is zone membership.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from arenamcp import server
from arenamcp.gamestate import GameState, create_game_state_handler

MATCH_ID = "37c7b556-524d-48af-9dce-8e54e4aa0996"
EVENT_ID = "PremierDraft_FRA_20260929"
NAMES = {106321: "Pompous Battlemage", 106529: "Island", 74159: "Swamp", 74121: "Forest"}


def _event(gsm: dict, seat: int = 1) -> dict:
    return {
        "greToClientEvent": {
            "greToClientMessages": [
                {
                    "type": "GREMessageType_GameStateMessage",
                    "systemSeatIds": [seat],
                    "gameStateMessage": gsm,
                }
            ]
        }
    }


def _zone(zone_id: int, zone_type: str, owner: int, ids: list[int] | None = None) -> dict:
    zone = {"zoneId": zone_id, "type": f"ZoneType_{zone_type}", "ownerSeatId": owner}
    if ids is not None:
        zone["objectInstanceIds"] = ids
    return zone


def _ann(ann_id: int, ann_type: str, affected: list[int], affector: int | None = None, **details) -> dict:
    ann = {"id": ann_id, "affectedIds": affected, "type": [f"AnnotationType_{ann_type}"]}
    if affector is not None:
        ann["affectorId"] = affector
    if details:
        ann["details"] = [
            {"key": key, "type": "KeyValuePairValueType_int32", "valueInt32": [value]}
            for key, value in details.items()
        ]
    return ann


def _revealed_stand_in(instance_id: int, grp_id: int, zone_id: int, owner: int, viewer: int) -> dict:
    return {
        "instanceId": instance_id,
        "grpId": grp_id,
        "type": "GameObjectType_RevealedCard",
        "zoneId": zone_id,
        "visibility": "Visibility_Private",
        "ownerSeatId": owner,
        "controllerSeatId": owner,
        "viewers": [viewer],
        "overlayGrpId": grp_id,
    }


PLAYERS = [
    {"systemSeatNumber": 1, "lifeTotal": 20, "teamId": 1},
    {"systemSeatNumber": 2, "lifeTotal": 20, "teamId": 2},
]

# Match 37c7b556, game 1, we are seat 1. Line 2827: game start, both libraries 40.
GAME_START = {
    "type": "GameStateType_Full",
    "gameStateId": 1,
    "gameInfo": {"matchID": MATCH_ID, "gameNumber": 1, "stage": "GameStage_Start"},
    "turnInfo": {"decisionPlayer": 1},
    "players": PLAYERS,
    "zones": [
        _zone(19, "Revealed", 2),
        _zone(31, "Hand", 1),
        _zone(32, "Library", 1, list(range(119, 159))),
        _zone(35, "Hand", 2),
        _zone(36, "Library", 2, list(range(159, 199))),
    ],
}
# Line 2900: opening hands dealt; 33 left in each library.
OPENING_HANDS = {
    "type": "GameStateType_Diff",
    "gameStateId": 2,
    "turnInfo": {"activePlayer": 1, "decisionPlayer": 1},
    "players": PLAYERS,
    "zones": [
        _zone(31, "Hand", 1, [125, 124, 123, 122, 121, 120, 119]),
        _zone(32, "Library", 1, list(range(126, 159))),
        _zone(35, "Hand", 2, [165, 164, 163, 162, 161, 160, 159]),
        _zone(36, "Library", 2, list(range(166, 199))),
    ],
}
# Line 3413: our spell returns their 1-drop (202 -> 207) to hand; stand-in 208.
BOUNCED_TO_HAND = {
    "type": "GameStateType_Diff",
    "gameStateId": 35,
    "zones": [
        _zone(19, "Revealed", 2, [208]),
        _zone(35, "Hand", 2, [207, 200, 165, 164, 163, 160, 159]),
    ],
    "gameObjects": [_revealed_stand_in(208, 106321, 35, owner=2, viewer=1)],
    "annotations": [
        _ann(167, "ObjectIdChanged", [202], 205, orig_id=202, new_id=207),
        _ann(168, "RevealedCardCreated", [208], 208),
        _ann(170, "ZoneTransfer", [207], 205, zone_src=28, zone_dest=35),
    ],
    "diffDeletedInstanceIds": [204],
}
# Line 4312: they cast it (207 -> 267, hand -> stack); stand-in deleted.
CAST_FROM_HAND = {
    "type": "GameStateType_Diff",
    "gameStateId": 125,
    "zones": [
        _zone(19, "Revealed", 2),
        _zone(35, "Hand", 2, [265, 200, 165, 163, 160]),
    ],
    "annotations": [
        _ann(351, "ObjectIdChanged", [207], orig_id=207, new_id=267),
        _ann(352, "RevealedCardDeleted", [208], 208),
        _ann(353, "ZoneTransfer", [267], zone_src=35, zone_dest=27),
    ],
    "diffDeletedInstanceIds": [208, 268],
}


@pytest.fixture
def game(monkeypatch):
    state = GameState()
    state._card_name_cache.update(NAMES)
    monkeypatch.setattr(state, "prewarm_card_cache", Mock(return_value=0))
    return state


def _feed(state: GameState, *messages: dict, seat: int = 1) -> dict:
    handler = create_game_state_handler(state)
    for message in messages:
        handler(_event(message, seat))
    return state.get_published_snapshot()


# --- opponent library count ----------------------------------------------------------


def test_opponent_library_count_is_zone_membership(game):
    snapshot = _feed(game, GAME_START)
    assert snapshot["zones"]["opponent_library_count"] == 40
    snapshot = _feed(game, OPENING_HANDS)
    assert snapshot["zones"]["opponent_library_count"] == 33
    assert snapshot["zones"]["opponent_hand_count"] == 7
    # Our own count is unchanged by the new key.
    assert snapshot["zones"]["library_count"] == 33


def test_opponent_library_count_unknown_is_none_not_zero(game):
    # Hand zones in the game-start message carry no member list.
    start = dict(GAME_START, zones=[_zone(31, "Hand", 1), _zone(35, "Hand", 2)])
    snapshot = _feed(game, start)
    assert snapshot["opponent_seat_id"] == 2
    assert snapshot["zones"]["opponent_library_count"] is None
    # No opponent seat yet: still None.
    assert GameState().get_published_snapshot()["zones"]["opponent_library_count"] is None


# --- revealed cards in the opponent's hand -----------------------------------------


def test_bounced_card_is_revealed_in_their_hand_until_cast(game):
    snapshot = _feed(game, GAME_START, OPENING_HANDS)
    assert snapshot["revealed_in_opponent_hand"] == []

    snapshot = _feed(game, BOUNCED_TO_HAND)
    assert snapshot["revealed_in_opponent_hand"] == [
        {"instance_id": 208, "grp_id": 106321, "name": "Pompous Battlemage", "zone": "hand"}
    ]
    record = game.revealed_instances[208]
    assert (record["zone_id"], record["zone_type"], record["owner_seat_id"]) == (35, "ZoneType_Hand", 2)
    # The per-seat grp_id history keeps its old shape.
    assert snapshot["revealed_cards"] == {2: [106321]}

    snapshot = _feed(game, CAST_FROM_HAND)
    assert snapshot["revealed_in_opponent_hand"] == []
    assert snapshot["revealed_cards"] == {2: [106321]}


def test_library_search_reveal_counts_only_the_card_put_in_hand(game):
    # Match ef9761c6, turn 8 (lines 54327/54590): they fetch an Island and reveal it.
    start = {
        "type": "GameStateType_Full",
        "gameStateId": 1,
        "gameInfo": {"matchID": "ef9761c6-437a-404e-9db4-edf7ef995c2b", "gameNumber": 1},
        "players": PLAYERS,
        "zones": [_zone(19, "Revealed", 2), _zone(35, "Hand", 2, [163, 161, 160])],
    }
    fetched = {
        "type": "GameStateType_Diff",
        "gameStateId": 170,
        "zones": [
            _zone(19, "Revealed", 2, [245]),
            _zone(35, "Hand", 2, [244, 163, 161, 160]),
            _zone(36, "Library", 2, [*range(172, 195), 196, 197, 198, 199, 200]),
        ],
        "gameObjects": [
            dict(_revealed_stand_in(243, 106529, 36, owner=2, viewer=1), visibility="Visibility_Public"),
            _revealed_stand_in(245, 106529, 35, owner=2, viewer=1),
        ],
        "annotations": [
            _ann(364, "RevealedCardCreated", [245], 245),
            _ann(366, "ZoneTransfer", [244], 239, zone_src=36, zone_dest=35),
        ],
        "persistentAnnotations": [_ann(362, "CardRevealed", [243], 227, source_zone=35)],
    }
    played = {
        "type": "GameStateType_Diff",
        "gameStateId": 196,
        "zones": [_zone(19, "Revealed", 2), _zone(35, "Hand", 2, [280, 163, 161, 160])],
        "annotations": [
            _ann(426, "RevealedCardDeleted", [245], 245),
            _ann(427, "ZoneTransfer", [281], zone_src=35, zone_dest=28),
        ],
        "diffDeletedInstanceIds": [245],
    }
    snapshot = _feed(game, start, fetched)
    assert game.revealed_instances[243]["zone_type"] == "ZoneType_Library"
    assert snapshot["revealed_in_opponent_hand"] == [
        {"instance_id": 245, "grp_id": 106529, "name": "Island", "zone": "hand"}
    ]
    assert snapshot["zones"]["opponent_library_count"] == 28
    assert _feed(game, played)["revealed_in_opponent_hand"] == []


def test_two_reveals_retire_one_at_a_time(game):
    # Player-prev.log lines 5823/6713/7555: we are seat 2; they fetch Swamp + Forest.
    start = {
        "type": "GameStateType_Full",
        "gameStateId": 1,
        "gameInfo": {"matchID": "3da54de9-9820-4ae2-b49e-8f81c913c446", "gameNumber": 1},
        "players": PLAYERS,
        "zones": [_zone(18, "Revealed", 1), _zone(31, "Hand", 1, [307, 291, 124, 123])],
    }
    fetched = {
        "type": "GameStateType_Diff",
        "gameStateId": 143,
        "zones": [
            _zone(18, "Revealed", 1, [319, 320]),
            _zone(31, "Hand", 1, [317, 318, 307, 291, 124, 123]),
        ],
        "gameObjects": [
            _revealed_stand_in(319, 74159, 31, owner=1, viewer=2),
            _revealed_stand_in(320, 74121, 31, owner=1, viewer=2),
        ],
        "annotations": [
            _ann(361, "RevealedCardCreated", [319], 319),
            _ann(363, "ZoneTransfer", [318], 314, zone_src=32, zone_dest=31),
            _ann(365, "RevealedCardCreated", [320], 320),
            _ann(367, "ZoneTransfer", [317], 314, zone_src=32, zone_dest=31),
        ],
    }
    swamp_played = {
        "type": "GameStateType_Diff",
        "gameStateId": 198,
        "zones": [_zone(18, "Revealed", 1, [320]), _zone(31, "Hand", 1, [358, 317, 307, 291, 124, 123])],
        "annotations": [
            _ann(470, "RevealedCardDeleted", [319], 319),
            _ann(471, "ZoneTransfer", [359], zone_src=31, zone_dest=28),
        ],
        "diffDeletedInstanceIds": [319],
    }
    forest_played = {
        "type": "GameStateType_Diff",
        "gameStateId": 254,
        "zones": [_zone(18, "Revealed", 1), _zone(31, "Hand", 1, [376, 358, 307, 124])],
        "annotations": [
            _ann(610, "RevealedCardDeleted", [320], 320),
            _ann(611, "ZoneTransfer", [377], zone_src=31, zone_dest=28),
        ],
        "diffDeletedInstanceIds": [320],
    }
    snapshot = _feed(game, start, fetched, seat=2)
    assert snapshot["opponent_seat_id"] == 1
    assert [card["name"] for card in snapshot["revealed_in_opponent_hand"]] == ["Swamp", "Forest"]
    snapshot = _feed(game, swamp_played, seat=2)
    assert [card["instance_id"] for card in snapshot["revealed_in_opponent_hand"]] == [320]
    assert _feed(game, forest_played, seat=2)["revealed_in_opponent_hand"] == []


def test_our_own_reveal_is_not_an_opponent_card(game):
    # Line 3683: our landcycled Island is revealed to them (InstanceRevealedToOpponent).
    reveal = {
        "type": "GameStateType_Diff",
        "gameStateId": 62,
        "zones": [_zone(31, "Hand", 1, [217, 210, 123, 122, 121])],
        "gameObjects": [
            {
                "instanceId": 217,
                "grpId": 106529,
                "type": "GameObjectType_Card",
                "zoneId": 31,
                "ownerSeatId": 1,
            }
        ],
        "persistentAnnotations": [_ann(216, "InstanceRevealedToOpponent", [217], 217)],
    }
    snapshot = _feed(game, GAME_START, OPENING_HANDS, reveal)
    assert 217 in game.revealed_instances
    assert snapshot["revealed_in_opponent_hand"] == []


# --- restart: checkpoint carries event_id and reveals -----------------------------


def _start_match(monkeypatch, state: GameState) -> None:
    """The real room event (line 2806) shape; user ids/names replaced."""
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "_deactivate_draft_state", Mock())
    monkeypatch.setattr(server, "mark_match_ended", Mock())
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: "local-user")
    server._handle_match_created(
        {
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {
                        "matchId": MATCH_ID,
                        "reservedPlayers": [
                            {"userId": "local-user", "systemSeatId": 1, "teamId": 1, "eventId": EVENT_ID},
                            {"userId": "opponent-user", "systemSeatId": 2, "teamId": 2, "eventId": EVENT_ID},
                        ],
                    },
                    "stateType": "MatchGameRoomStateType_Playing",
                }
            }
        }
    )


def test_restart_mid_game_keeps_event_id_and_reveals(monkeypatch, game):
    _start_match(monkeypatch, game)
    _feed(game, GAME_START, OPENING_HANDS, BOUNCED_TO_HAND)
    assert game.event_id == EVENT_ID
    assert game.format_name == "PremierDraft FRA 20260929"

    # gamestate_persistence writes the checkpoint as JSON.
    checkpoint = json.loads(json.dumps(game.export_checkpoint()))
    restored = GameState()
    restored._card_name_cache.update(NAMES)
    monkeypatch.setattr(restored, "prewarm_card_cache", Mock(return_value=0))
    assert restored.restore_checkpoint(checkpoint)

    snapshot = restored.get_published_snapshot()
    assert snapshot["event_id"] == EVENT_ID
    assert snapshot["format_name"] == "PremierDraft FRA 20260929"
    assert snapshot["zones"]["opponent_library_count"] == 33
    assert snapshot["revealed_in_opponent_hand"] == [
        {"instance_id": 208, "grp_id": 106321, "name": "Pompous Battlemage", "zone": "hand"}
    ]
    # The resumed tail still retires the reveal.
    assert _feed(restored, CAST_FROM_HAND)["revealed_in_opponent_hand"] == []


def test_checkpoint_from_before_event_id_restores_empty(game):
    _feed(game, GAME_START, OPENING_HANDS)
    checkpoint = game.export_checkpoint()
    for key in ("event_id", "format_name", "revealed_instances"):
        checkpoint.pop(key)
    restored = GameState()
    assert restored.restore_checkpoint(checkpoint)
    assert (restored.event_id, restored.format_name, restored.revealed_instances) == ("", "", {})


# --- server.get_game_state pass-through -------------------------------------------


def _fake_enrich(grp_id: int) -> dict:
    return {"grp_id": grp_id, "name": NAMES.get(grp_id, f"Card#{grp_id}"), "oracle_text": "", "type_line": ""}


@pytest.fixture
def published(monkeypatch, game):
    monkeypatch.setattr(server, "game_state", game)
    monkeypatch.setattr(server, "watcher", object())
    monkeypatch.setattr(server, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server, "enrich_with_oracle_text", _fake_enrich)
    monkeypatch.setattr(server, "_get_bridge_overlay", lambda **_kwargs: {})
    return game


def test_get_game_state_publishes_library_count_and_revealed_cards(published):
    _feed(published, GAME_START, OPENING_HANDS, BOUNCED_TO_HAND)
    state = server.get_game_state()
    assert state["zones"]["opponent_library_count"] == 33
    assert state["revealed_cards"] == [
        {"instance_id": 208, "grp_id": 106321, "name": "Pompous Battlemage", "zone": "hand"}
    ]
    json.dumps(state)

    _feed(published, CAST_FROM_HAND)
    assert server.get_game_state()["revealed_cards"] == []


def test_get_game_state_library_count_survives_bridge_zone_overlay(monkeypatch, published):
    _feed(published, GAME_START, OPENING_HANDS)
    # A Windows bridge rebuilds "zones" from its own state.
    monkeypatch.setattr(
        server,
        "_get_bridge_overlay",
        lambda **_kwargs: {
            "bridge_connected": True,
            "zones": {"opponent_hand_count": 7, "library_count": 33},
        },
    )
    assert server.get_game_state()["zones"]["opponent_library_count"] == 33


def test_get_game_state_unknown_library_is_none(published):
    _feed(published, dict(GAME_START, zones=[_zone(35, "Hand", 2)]))
    state = server.get_game_state()
    assert state["zones"]["opponent_library_count"] is None
    assert state["revealed_cards"] == []


def test_coach_formatter_accepts_published_revealed_cards(published):
    """Planner and coach prompts are built from get_game_state output.

    coach.py read "revealed_cards" as a per-seat dict that the server never
    published; the published value is now a list of card dicts.
    """
    from arenamcp.coach import CoachEngine

    _feed(published, GAME_START, OPENING_HANDS, BOUNCED_TO_HAND)
    formatter = CoachEngine.__new__(CoachEngine)
    context = formatter._format_game_context(server.get_game_state(), for_planner=True)
    assert "Opp library: 33 card(s)" in context
    assert "Opp hand (revealed): Pompous Battlemage" in context


def test_coach_formatter_keeps_the_raw_per_seat_revealed_dict():
    """A raw GameState snapshot still carries {seat: [grp_id, ...]}."""
    from arenamcp.coach import CoachEngine

    formatter = CoachEngine.__new__(CoachEngine)
    context = formatter._format_game_context(
        {
            "turn": {"turn_number": 3, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
            "players": [
                {"seat_id": 1, "is_local": True, "life_total": 20},
                {"seat_id": 2, "is_local": False, "life_total": 20},
            ],
            "local_seat_id": 1,
            "battlefield": [],
            "hand": [],
            "revealed_cards": {"2": [106321]},
        },
        for_planner=True,
    )
    assert "Opp revealed 1 card(s) this game" in context
