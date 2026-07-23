from arenamcp.gamestate import GameObject, GameObjectKind, GameState, create_game_state_handler


def test_update_from_message_accepts_scalar_repeated_fields():
    state = GameState()

    state.update_from_message(
        {
            "gameObjects": {
                "instanceId": 1,
                "grpId": 1001,
                "zoneId": 10,
                "ownerSeatId": 1,
                "controllerSeatId": 1,
                "cardTypes": "CardType_Creature",
                "subtypes": "SubType_Wizard",
                "counters": {"type": "CounterType_P1P1", "count": 2},
            },
            "zones": {
                "zoneId": 10,
                "type": "ZoneType_Battlefield",
                "ownerSeatId": 1,
                "objectInstanceIds": 1,
            },
            "players": {
                "seatId": 1,
                "lifeTotal": 20,
                "manaPool": {"color": "ManaColor_Green", "count": 2},
            },
            "annotations": {
                "type": "AnnotationType_TargetSpec",
                "affectedIds": 2,
                "details": [
                    {"key": "sourceId", "valueInt32": 1},
                    {"key": "targetIds", "valueInt32": 2},
                ],
            },
        }
    )

    obj = state.game_objects[1]
    assert obj.card_types == ["CardType_Creature"]
    assert obj.subtypes == ["Wizard"]
    assert obj.counters == {"CounterType_P1P1": 2}
    assert obj.targeting == [2]
    assert state.zones[10].object_instance_ids == [1]
    assert state.players[1].mana_pool == {"G": 2}


def test_process_annotations_preserves_multi_target_lists():
    state = GameState()
    state.game_objects[1] = GameObject(
        instance_id=1,
        grp_id=1001,
        zone_id=10,
        owner_seat_id=1,
        controller_seat_id=1,
        object_kind=GameObjectKind.CARD,
    )

    state._process_annotations(
        {
            "type": "AnnotationType_TargetSpec",
            "affectedIds": 2,
            "details": [
                {"key": "sourceId", "valueInt32": 1},
                {"key": "targetIds", "valueInt32": [2, 3]},
            ],
        }
    )

    assert state.game_objects[1].targeting == [2, 3]
    assert state.recent_events[-1]["target_ids"] == [2, 3]


def test_process_annotations_marks_loop_count_as_engine_busy():
    state = GameState()

    state._process_annotations(
        {
            "type": "AnnotationType_LoopCount",
            "affectedIds": [77],
            "details": [
                {"key": "loopCount", "valueInt32": 3},
                {"key": "sourceId", "valueInt32": 77},
            ],
        }
    )
    state.publish_snapshot()

    snapshot = state.get_published_snapshot()
    assert snapshot["game_engine_busy"] is True
    assert "loop_count" in snapshot["engine_busy"]
    assert snapshot["engine_busy"]["loop_count"]["affected_ids"] == [77]
    assert state.recent_events[-1]["type"] == "engine_busy"


def test_create_game_state_handler_accepts_scalar_messages_and_timers():
    state = GameState()
    handler = create_game_state_handler(state)

    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": {
                    "type": "GREMessageType_TimerStateMessage",
                    "systemSeatIds": 7,
                    "timerStateMessage": {
                        "timers": {
                            "playerId": 7,
                            "type": "TimerType_Chess",
                            "timeRemainingMs": 30000,
                            "behavior": "TimerBehavior_Normal",
                            "isTicking": True,
                        }
                    },
                }
            }
        }
    )

    assert state.local_seat_id == 7
    assert state.timer_state[7]["time_remaining_ms"] == 30000
    assert state.timer_state[7]["is_ticking"] is True


def test_create_game_state_handler_exports_bounded_raw_gre_history():
    state = GameState()
    state._max_raw_gre_events = 3
    state._max_snapshot_raw_gre_events = 3
    handler = create_game_state_handler(state)

    for idx in range(5):
        handler(
            {
                "greToClientEvent": {
                    "greToClientMessages": {
                        "type": "GREMessageType_TimerStateMessage",
                        "systemSeatIds": 1,
                        "timerStateMessage": {
                            "timers": {
                                "playerId": 1,
                                "type": "TimerType_Chess",
                                "timeRemainingMs": 30000 - idx,
                            }
                        },
                    }
                }
            }
        )

    snapshot = state.get_snapshot()

    assert len(state.raw_gre_events) == 3
    assert [entry["seq"] for entry in state.raw_gre_events] == [3, 4, 5]
    assert snapshot["raw_gre_event_count"] == 3
    assert len(snapshot["raw_gre_events"]) == 3
    assert snapshot["raw_gre_events"][-1]["type"] == "GREMessageType_TimerStateMessage"
    assert (
        snapshot["raw_gre_events"][-1]["payload"]["timerStateMessage"]["timers"]["timeRemainingMs"] == 29996
    )


def test_create_game_state_handler_truncates_large_raw_gre_payloads():
    state = GameState()
    state._max_snapshot_raw_gre_events = 5
    handler = create_game_state_handler(state)

    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": {
                    "type": "GREMessageType_UnknownReq",
                    "systemSeatIds": 2,
                    "unknownReq": {
                        "numbers": list(range(100)),
                        "nested": {"values": list(range(100))},
                    },
                }
            }
        }
    )

    snapshot = state.get_snapshot()
    raw_entry = snapshot["raw_gre_events"][-1]

    assert raw_entry["type"] == "GREMessageType_UnknownReq"
    assert raw_entry["payload_truncated"] is True
    assert len(raw_entry["payload"]["unknownReq"]["numbers"]) == 24
    assert len(raw_entry["payload"]["unknownReq"]["nested"]["values"]) == 24


def test_create_game_state_handler_records_ui_message_summary_and_raw_payload():
    state = GameState()
    handler = create_game_state_handler(state)

    handler(
        {
            "greToClientEvent": {
                "greToClientMessages": {
                    "type": "GREMessageType_UIMessage",
                    "systemSeatIds": 4,
                    "uiMessage": {
                        "title": "Choose attackers",
                        "body": "Select any number of creatures to attack.",
                        "tooltip": {"text": "Attack with caution."},
                    },
                }
            }
        }
    )

    snapshot = state.get_snapshot()

    assert state.recent_events[-1]["type"] == "ui_message"
    assert state.recent_events[-1]["text"] == "Choose attackers"
    assert "Attack with caution." in state.recent_events[-1]["texts"]
    assert snapshot["raw_gre_events"][-1]["type"] == "GREMessageType_UIMessage"
    assert snapshot["raw_gre_events"][-1]["payload"]["uiMessage"]["title"] == "Choose attackers"
