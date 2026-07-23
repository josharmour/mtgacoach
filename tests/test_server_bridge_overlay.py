import arenamcp.gre_bridge as gre_bridge_module
import arenamcp.server as server_module


class _StubGameState:
    def __init__(self, snapshot: dict):
        self._snapshot = snapshot

    def ensure_local_seat_id(self) -> None:
        return None

    def get_published_snapshot(self, deep_copy: bool = False) -> dict:
        return self._snapshot


class _DummyBridge:
    connected = True

    def __init__(self, game_state: dict, timer_state: dict | None = None):
        self._game_state = game_state
        self._timer_state = timer_state or {}

    def get_game_state(self) -> dict:
        return self._game_state

    def get_timer_state(self) -> dict | None:
        return self._timer_state


def _fake_enrich_with_oracle_text(grp_id: int) -> dict:
    names = {
        1001: "Alpha Myr",
        1002: "Island",
        1003: "Shock",
        1004: "Counterspell",
        1005: "Ghostly Prison",
    }
    return {
        "grp_id": grp_id,
        "name": names.get(grp_id, f"Card#{grp_id}"),
        "oracle_text": f"Oracle {grp_id}",
        "type_line": "Creature" if grp_id == 1001 else "Spell",
        "mana_cost": "{1}",
    }


def test_get_game_state_overlays_bridge_visible_state(monkeypatch):
    snapshot = {
        "match_id": "match-123",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn_info": {
            "turn_number": 3,
            "active_player": 2,
            "priority_player": 2,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
        },
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "mana_pool": {}},
            {"seat_id": 2, "life_total": 20, "is_local": False, "mana_pool": {}},
        ],
        "zones": {
            "battlefield": [
                {
                    "instance_id": 501,
                    "grp_id": 1001,
                    "owner_seat_id": 1,
                    "controller_seat_id": 1,
                    "object_kind": "CARD",
                    "turn_entered_battlefield": 2,
                },
                {
                    "instance_id": 999,
                    "grp_id": 9999,
                    "owner_seat_id": 1,
                    "controller_seat_id": 1,
                    "object_kind": "ABILITY",
                },
            ],
            "my_hand": [],
            "graveyard": [],
            "stack": [],
            "exile": [],
            "command": [],
        },
        "pending_decision": "Select Targets",
        "decision_seat_id": 1,
        "decision_context": {"type": "target_selection", "source_card": "Shock"},
        "legal_actions": ["Select target: Alpha Myr"],
        "legal_actions_raw": [{"actionType": "ActionType_Select"}],
        "pending_combat_steps": [],
        "deck_cards": [],
        "damage_taken": {},
        "designations": {},
        "dungeon_status": {1: {"dungeon": "Lost Mine", "room": "Mine Entrance"}},
        "timer_state": {1: {"time_remaining_ms": 11111}},
        "action_history": [],
        "sideboard_cards": [],
    }
    bridge_game_state = {
        "ok": True,
        "turn": {
            "turn_number": 4,
            "active_player": 1,
            "deciding_player": 1,
            "phase": "Phase_Main2",
            "step": "Step_PostCombatMain",
        },
        "players": [
            {"seat_id": 1, "life_total": 17, "is_local": True, "mana_pool": {"G": 2}},
            {"seat_id": 2, "life_total": 12, "is_local": False, "mana_pool": {"U": 1}},
        ],
        "zones": {
            "battlefield": {
                "cards": [
                    {
                        "instance_id": 501,
                        "grp_id": 1001,
                        "owner_id": 1,
                        "controller_id": 1,
                        "object_type": "Card",
                        "is_tapped": False,
                    },
                    {
                        "instance_id": 777,
                        "grp_id": 7777,
                        "owner_id": 1,
                        "controller_id": 1,
                        "object_type": "Ability",
                    },
                ]
            },
            "local_hand": {
                "cards": [
                    {
                        "instance_id": 601,
                        "grp_id": 1003,
                        "owner_id": 1,
                        "controller_id": 1,
                        "object_type": "Card",
                    }
                ]
            },
            "stack": {
                "cards": [
                    {
                        "instance_id": 701,
                        "grp_id": 1004,
                        "owner_id": 2,
                        "controller_id": 2,
                        "object_type": "Card",
                    }
                ]
            },
            "local_graveyard": {"cards": []},
            "opponent_graveyard": {
                "cards": [
                    {
                        "instance_id": 801,
                        "grp_id": 1005,
                        "owner_id": 2,
                        "controller_id": 2,
                        "object_type": "Card",
                    }
                ]
            },
            "exile": {"cards": []},
            "command": {"cards": []},
        },
    }
    bridge_timer_state = {
        "ok": True,
        "player_timers": {
            "1": [
                {
                    "timer_id": 10,
                    "type": "TimerType_Chess",
                    "duration_sec": 60,
                    "elapsed_sec": 15,
                    "running": True,
                    "behavior": "TimerBehavior_Normal",
                }
            ],
            "2": [
                {
                    "timer_id": 20,
                    "type": "TimerType_Chess",
                    "duration_sec": 60,
                    "elapsed_sec": 5,
                    "running": False,
                    "behavior": "TimerBehavior_Normal",
                }
            ],
        },
    }

    monkeypatch.setattr(server_module, "watcher", object())
    monkeypatch.setattr(server_module, "start_watching", lambda: None)
    monkeypatch.setattr(server_module, "game_state", _StubGameState(snapshot))
    monkeypatch.setattr(server_module, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server_module, "enrich_with_oracle_text", _fake_enrich_with_oracle_text)
    monkeypatch.setattr(
        gre_bridge_module,
        "get_bridge",
        lambda: _DummyBridge(bridge_game_state, bridge_timer_state),
    )

    result = server_module.get_game_state()

    assert result["bridge_connected"] is True
    assert result["_bridge_connected"] is True
    assert result["turn"]["turn_number"] == 4
    assert result["turn"]["priority_player"] == 1
    assert result["turn"]["phase"] == "Phase_Main2"
    assert result["local_seat_id"] == 1
    assert result["opponent_seat_id"] == 2
    assert result["players"][0]["life_total"] == 17
    assert result["players"][0]["mana_pool"] == {"G": 2}
    assert len(result["battlefield"]) == 1
    assert result["battlefield"][0]["name"] == "Alpha Myr"
    assert result["battlefield"][0]["turn_entered_battlefield"] == 2
    assert result["hand"][0]["name"] == "Shock"
    assert result["stack"][0]["name"] == "Counterspell"
    assert result["graveyard"][0]["name"] == "Ghostly Prison"
    assert result["pending_decision"] == "Select Targets"
    assert result["decision_context"]["type"] == "target_selection"
    assert result["dungeon_status"][1]["dungeon"] == "Lost Mine"
    assert result["timer_state"][1]["time_remaining_ms"] == 45000
    assert result["timer_state"][1]["is_ticking"] is True


def test_get_game_state_uses_bridge_local_seat_for_pending_decision(monkeypatch):
    snapshot = {
        "match_id": "match-456",
        "local_seat_id": None,
        "opponent_seat_id": None,
        "turn_info": {
            "turn_number": 1,
            "active_player": 0,
            "priority_player": 0,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
        },
        "players": [
            {"seat_id": 7, "life_total": 20},
            {"seat_id": 9, "life_total": 20},
        ],
        "zones": {
            "battlefield": [],
            "my_hand": [],
            "graveyard": [],
            "stack": [],
            "exile": [],
            "command": [],
        },
        "pending_decision": "Pay Costs",
        "decision_seat_id": None,
        "decision_context": {"type": "mana_payment"},
        "legal_actions": [],
        "legal_actions_raw": [],
        "pending_combat_steps": [],
        "deck_cards": [],
        "damage_taken": {},
        "designations": {},
        "dungeon_status": {},
        "timer_state": {},
        "action_history": [],
        "sideboard_cards": [],
    }
    bridge_game_state = {
        "ok": True,
        "turn": {
            "turn_number": 1,
            "active_player": 7,
            "deciding_player": 7,
            "phase": "Phase_Main1",
            "step": "Step_PreCombatMain",
        },
        "players": [
            {"seat_id": 7, "life_total": 20, "is_local": True, "mana_pool": {}},
            {"seat_id": 9, "life_total": 20, "is_local": False, "mana_pool": {}},
        ],
        "zones": {
            "battlefield": {"cards": []},
            "local_hand": {"cards": []},
            "local_graveyard": {"cards": []},
            "opponent_graveyard": {"cards": []},
            "stack": {"cards": []},
            "exile": {"cards": []},
            "command": {"cards": []},
        },
    }

    monkeypatch.setattr(server_module, "watcher", object())
    monkeypatch.setattr(server_module, "start_watching", lambda: None)
    monkeypatch.setattr(server_module, "game_state", _StubGameState(snapshot))
    monkeypatch.setattr(server_module, "_save_match_state_if_needed", lambda: None)
    monkeypatch.setattr(server_module, "enrich_with_oracle_text", _fake_enrich_with_oracle_text)
    monkeypatch.setattr(
        gre_bridge_module,
        "get_bridge",
        lambda: _DummyBridge(bridge_game_state),
    )

    result = server_module.get_game_state()

    assert result["local_seat_id"] == 7
    assert result["opponent_seat_id"] == 9
    assert result["pending_decision"] == "Pay Costs"
    assert result["decision_context"]["type"] == "mana_payment"
