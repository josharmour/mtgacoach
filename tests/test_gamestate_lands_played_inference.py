"""Regression tests for _infer_lands_played and the play-land vs ramp distinction.

Bug: when Rampant Growth resolves and puts a Forest onto the battlefield, the
Forest's zone-transfer annotation has category="Put" (or other non-PlayLand
value). The old inference code only checked turn_entered_battlefield, so it
counted the ramp Forest as the player's land drop. The land-drop preflight
in ActionPlanner then refused to play the actual land for the turn because
"lands_played > 0".

Fix: only count lands whose entered_via_play_land flag was set by an
AnnotationType_ZoneTransfer with category="PlayLand".
"""

from arenamcp.gamestate import GameObject, GameObjectKind, GameState


def _land(instance_id: int, *, seat: int, turn: int, via_play_land: bool) -> GameObject:
    return GameObject(
        instance_id=instance_id,
        grp_id=2000 + instance_id,
        zone_id=100,
        owner_seat_id=seat,
        controller_seat_id=seat,
        card_types=["CardType_Land"],
        object_kind=GameObjectKind.CARD,
        turn_entered_battlefield=turn,
        entered_via_play_land=via_play_land,
    )


def _setup_player_on_battlefield(state: GameState, seat: int, *, lands_reported: int = 0) -> None:
    state.players.clear()
    state.update_from_message(
        {
            "players": {"seatId": seat, "lifeTotal": 20, "landsPlayedThisTurn": lands_reported},
            "turnInfo": {"activePlayer": seat, "turnNumber": 4, "phase": "Phase_Main1"},
        }
    )


def test_infer_counts_play_land_but_not_ramp_put():
    state = GameState()
    state.turn_info.turn_number = 4
    _setup_player_on_battlefield(state, seat=1)

    bf_zone_id = 999
    state.update_from_message(
        {
            "zones": {
                "zoneId": bf_zone_id,
                "type": "ZoneType_Battlefield",
                "ownerSeatId": 1,
                "objectInstanceIds": [],
            },
        }
    )

    forest_played = _land(1, seat=1, turn=4, via_play_land=True)
    forest_played.zone_id = bf_zone_id
    forest_ramped = _land(2, seat=1, turn=4, via_play_land=False)
    forest_ramped.zone_id = bf_zone_id
    state.game_objects[1] = forest_played
    state.game_objects[2] = forest_ramped
    state.zones[bf_zone_id].object_instance_ids = [1, 2]
    state.players[1].lands_played = 0

    state._infer_lands_played()

    assert state.players[1].lands_played == 1, (
        "Only the actually-played land should count; ramp-put lands must not."
    )


def test_zone_transfer_annotation_sets_play_land_flag():
    state = GameState()
    state.turn_info.turn_number = 3
    state.game_objects[5] = _land(5, seat=1, turn=3, via_play_land=False)

    state._process_annotations(
        [
            {
                "type": "AnnotationType_ZoneTransfer",
                "affectedIds": [5],
                "details": [
                    {"key": "zone_src", "valueString": "ZoneType_Hand"},
                    {"key": "zone_dest", "valueString": "ZoneType_Battlefield"},
                    {"key": "category", "valueString": "PlayLand"},
                ],
            }
        ]
    )

    assert state.game_objects[5].entered_via_play_land is True


def test_zone_transfer_annotation_with_put_category_does_not_set_flag():
    state = GameState()
    state.turn_info.turn_number = 3
    state.game_objects[6] = _land(6, seat=1, turn=3, via_play_land=False)

    state._process_annotations(
        [
            {
                "type": "AnnotationType_ZoneTransfer",
                "affectedIds": [6],
                "details": [
                    {"key": "zone_src", "valueString": "ZoneType_Library"},
                    {"key": "zone_dest", "valueString": "ZoneType_Battlefield"},
                    {"key": "category", "valueString": "Put"},
                ],
            }
        ]
    )

    assert state.game_objects[6].entered_via_play_land is False


def test_infer_skips_lands_with_no_play_land_flag():
    """All-ramp scenario: every land on the battlefield came from a spell."""
    state = GameState()
    state.turn_info.turn_number = 5
    _setup_player_on_battlefield(state, seat=1)

    bf_zone_id = 1010
    state.update_from_message(
        {
            "zones": {
                "zoneId": bf_zone_id,
                "type": "ZoneType_Battlefield",
                "ownerSeatId": 1,
                "objectInstanceIds": [],
            },
        }
    )

    forest_a = _land(10, seat=1, turn=5, via_play_land=False)
    forest_a.zone_id = bf_zone_id
    forest_b = _land(11, seat=1, turn=5, via_play_land=False)
    forest_b.zone_id = bf_zone_id
    state.game_objects[10] = forest_a
    state.game_objects[11] = forest_b
    state.zones[bf_zone_id].object_instance_ids = [10, 11]
    state.players[1].lands_played = 0

    state._infer_lands_played()

    assert state.players[1].lands_played == 0, (
        "Player has not used their land drop; ramp-put lands must not flip the counter."
    )
