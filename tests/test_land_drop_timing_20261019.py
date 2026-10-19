"""The land drop counts on the GSM that plays the land, not one message later (#76).

2026-10-19 T6 (match abde2a07, 11:29:58): the autopilot played Mountain#305.
The GSM that moved it (gameStateId 117) carries no player data, so
``lands_played`` comes from ``_infer_lands_played``. That ran before the
message's annotations, and the annotations' PlayLand zone transfer is what
marks the land ``entered_via_play_land``. ``lands_played`` stayed 0 until
the next GSM (11:30:02.745, after the Lightning Strike cast), and the
land_played trigger's planning at 11:29:58.9 saw ``land_drop_now=True``:
"the best searched line is Mountain + Emberheart Challenger + Lightning
Strike" with the land already on the battlefield.
"""

from __future__ import annotations

from arenamcp.board_model import build_board_model
from arenamcp.gamestate import GameState

MOUNTAIN = {
    "grpId": 65385,
    "type": "GameObjectType_Card",
    "ownerSeatId": 1,
    "controllerSeatId": 1,
    "superTypes": ["SuperType_Basic"],
    "cardTypes": ["CardType_Land"],
    "subtypes": ["SubType_Mountain"],
    "name": 1250,
}


def _board(state: GameState):
    """``build_board_model`` on the planner's view of the state (``server.get_game_state``'s shape)."""
    snap = state.get_snapshot()
    zones = snap["zones"]
    return build_board_model(
        {
            "turn": snap["turn_info"],
            "players": snap["players"],
            "battlefield": zones.get("battlefield", []),
            "hand": zones.get("my_hand", []),
            "local_seat_id": snap["local_seat_id"],
            "opponent_seat_id": snap.get("opponent_seat_id"),
        }
    )


def _turn_six_before_the_land() -> GameState:
    """Our turn 6, main phase 1: two Mountains from earlier turns, Mountain#165 in hand."""
    state = GameState()
    state.local_seat_id = 1
    state.update_from_message(
        {
            "type": "GameStateType_Full",
            "gameStateId": 116,
            "turnInfo": {
                "turnNumber": 6,
                "activePlayer": 1,
                "priorityPlayer": 1,
                "decisionPlayer": 1,
                "phase": "Phase_Main1",
            },
            "players": [
                {"systemSeatNumber": 1, "seatId": 1, "lifeTotal": 20, "landsPlayedThisTurn": 0},
                {"systemSeatNumber": 2, "seatId": 2, "lifeTotal": 18, "landsPlayedThisTurn": 0},
            ],
            "zones": [
                {"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [281, 290]},
                {"zoneId": 31, "type": "ZoneType_Hand", "ownerSeatId": 1, "objectInstanceIds": [165]},
            ],
            "gameObjects": [
                {**MOUNTAIN, "instanceId": 281, "zoneId": 28},
                {**MOUNTAIN, "instanceId": 290, "zoneId": 28},
                {**MOUNTAIN, "instanceId": 165, "zoneId": 31},
            ],
        }
    )
    # The earlier lands entered on earlier turns.
    for identity, turn in ((281, 2), (290, 4)):
        state.game_objects[identity].turn_entered_battlefield = turn
    return state


# gameStateId 117 from the match's Player.log, timers/actions trimmed: no "players".
PLAY_MOUNTAIN_GSM = {
    "type": "GameStateType_Diff",
    "gameStateId": 117,
    "zones": [
        {"zoneId": 28, "type": "ZoneType_Battlefield", "objectInstanceIds": [305, 290, 281]},
        {"zoneId": 30, "type": "ZoneType_Limbo", "objectInstanceIds": [165]},
        {"zoneId": 31, "type": "ZoneType_Hand", "ownerSeatId": 1, "objectInstanceIds": []},
    ],
    "gameObjects": [
        {**MOUNTAIN, "instanceId": 165, "zoneId": 30},
        {**MOUNTAIN, "instanceId": 305, "zoneId": 28},
    ],
    "annotations": [
        {
            "id": 362,
            "affectedIds": [165],
            "type": ["AnnotationType_ObjectIdChanged"],
            "details": [
                {"key": "orig_id", "type": "KeyValuePairValueType_int32", "valueInt32": [165]},
                {"key": "new_id", "type": "KeyValuePairValueType_int32", "valueInt32": [305]},
            ],
        },
        {
            "id": 363,
            "affectedIds": [305],
            "type": ["AnnotationType_ZoneTransfer"],
            "details": [
                {"key": "zone_src", "type": "KeyValuePairValueType_int32", "valueInt32": [31]},
                {"key": "zone_dest", "type": "KeyValuePairValueType_int32", "valueInt32": [28]},
                {"key": "category", "type": "KeyValuePairValueType_string", "valueString": ["PlayLand"]},
            ],
        },
        {
            "id": 364,
            "affectorId": 1,
            "affectedIds": [305],
            "type": ["AnnotationType_UserActionTaken"],
            "details": [
                {"key": "actionType", "type": "KeyValuePairValueType_int32", "valueInt32": [3]},
                {"key": "abilityGrpId", "type": "KeyValuePairValueType_int32", "valueInt32": [0]},
            ],
        },
    ],
    "prevGameStateId": 116,
}


def test_the_land_drop_counts_on_the_message_that_plays_the_land():
    state = _turn_six_before_the_land()
    assert state.players[1].lands_played == 0
    before = _board(state)
    assert before is not None and before.land_drop_now is True

    state.update_from_message(PLAY_MOUNTAIN_GSM)

    assert state.game_objects[305].entered_via_play_land is True
    assert state.players[1].lands_played == 1
    snapshot = state.get_snapshot()
    local = next(p for p in snapshot["players"] if p.get("seat_id") == 1)
    assert local["lands_played"] == 1
    after = _board(state)
    assert after is not None and after.land_drop_now is False
    # The opponent's drop is untouched.
    assert state.players[2].lands_played == 0


def test_a_ramped_land_on_the_same_message_still_does_not_count():
    state = _turn_six_before_the_land()
    ramp = {
        **PLAY_MOUNTAIN_GSM,
        "annotations": [
            {
                **PLAY_MOUNTAIN_GSM["annotations"][1],
                "details": [
                    *PLAY_MOUNTAIN_GSM["annotations"][1]["details"][:2],
                    {"key": "category", "type": "KeyValuePairValueType_string", "valueString": ["Put"]},
                ],
            }
        ],
    }
    state.update_from_message(ramp)
    assert state.players[1].lands_played == 0
