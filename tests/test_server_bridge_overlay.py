import pytest

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


@pytest.mark.parametrize(
    "bridge_count, fallback_count, source, expected",
    [
        (0, 90, "log_zone_membership", 0),
        (53, 0, "", 53),
        (None, 90, "log_zone_membership", 90),
        (None, 0, "", "?"),
        (None, 0, "log_zone_membership", 0),
        (True, 90, "log_zone_membership", 90),
    ],
)
def test_library_overlay_distinguishes_unknown_and_empty(
    monkeypatch, bridge_count, fallback_count, source, expected
):
    bridge_state = {"ok": True, "zones": {"local_library": {"total_count": bridge_count, "cards": []}}}
    monkeypatch.setattr(gre_bridge_module, "get_bridge", lambda: _DummyBridge(bridge_state))
    overlay = server_module._get_bridge_overlay(
        {}, [], {"library_count": fallback_count, "library_count_source": source}, 2, 1
    )
    assert overlay["zones"]["library_count"] == expected
    if type(bridge_count) is int:
        assert overlay["zones"]["library_count_source"] == "bridge_total_card_count"


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
        "commander_casts": {103511: 1},
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
    assert result["commander_casts"] == {103511: 1}
    result["commander_casts"][103511] = 3
    assert snapshot["commander_casts"] == {103511: 1}
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
    # Arena's clock stays the Player.log reading; the plugin's untimed MtgTimer
    # snapshot read "2s left" on fresh windows (2026-10-18 21:22).
    assert result["timer_state"] == {1: {"time_remaining_ms": 11111}}


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


# bug_20261012_225437 (22:54:49): the log had parsed turn 7 Main 1 (gameStateId 130,
# ActionsAvailableReq with a land, The Ooze and Mutagen payable) while the Mac bridge's
# CurrentGameState (MTGA's playback, behind the GRE during turn-change animations) still
# showed the opponent's turn 6. The overlay replaced the log's turn and hand; the planner
# read "opponent's turn" and passed our Main 1.
_STALE_MAC_BRIDGE = {
    "ok": True,
    "turn": {"turn_number": 6, "phase": "Main1", "active_player": 2, "game_state_id": 121},
    "players": [{"seat_id": 1, "life_total": 25}, {"seat_id": 2, "life_total": 24}],
    "zones": {"battlefield": {"cards": []}, "local_hand": {"cards": []}},
}
_LOG_TURN_7 = {"turn_number": 7, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1", "step": ""}


def test_a_bridge_state_behind_the_log_is_not_overlaid(monkeypatch):
    monkeypatch.setattr(gre_bridge_module, "get_bridge", lambda: _DummyBridge(_STALE_MAC_BRIDGE))
    overlay = server_module._get_bridge_overlay(_LOG_TURN_7, [], {}, 1, 2, log_game_state_id=130)
    assert overlay.get("_bridge_state_stale") is True
    assert "turn" not in overlay and "hand" not in overlay and "battlefield" not in overlay
    assert overlay["bridge_connected"] is True


def test_an_older_bridge_turn_is_stale_even_without_ids(monkeypatch):
    state = {**_STALE_MAC_BRIDGE, "turn": {**_STALE_MAC_BRIDGE["turn"], "game_state_id": 0}}
    monkeypatch.setattr(gre_bridge_module, "get_bridge", lambda: _DummyBridge(state))
    overlay = server_module._get_bridge_overlay(_LOG_TURN_7, [], {}, 1, 2)
    assert "turn" not in overlay


def test_a_current_bridge_state_still_overlays(monkeypatch):
    fresh = {
        **_STALE_MAC_BRIDGE,
        "turn": {"turn_number": 7, "phase": "Main1", "active_player": 1, "game_state_id": 130},
    }
    monkeypatch.setattr(gre_bridge_module, "get_bridge", lambda: _DummyBridge(fresh))
    overlay = server_module._get_bridge_overlay(_LOG_TURN_7, [], {}, 1, 2, log_game_state_id=130)
    assert overlay["turn"]["turn_number"] == 7 and overlay["turn"]["active_player"] == 1
    # The Mac id is compared, not published: autopilot window checks read _bridge_game_state_id.
    assert "_bridge_game_state_id" not in overlay


@pytest.mark.parametrize(
    "live",
    [
        {"keywords": ["flying", "vigilance", "double strike"], "ability_grp_ids": [42]},
        {"keywords": [], "ability_grp_ids": [], "modified_power": 0, "is_phased_out": False},
    ],
)
def test_live_bridge_effects_replace_stale_log_effects(monkeypatch, live):
    monkeypatch.setattr(
        server_module,
        "card_object_info",
        lambda card: {"name": "Rabbit", "type_line": "Creature", "oracle_text": ""},
    )
    fallback = {"keywords": ["reach"], "ability_grp_ids": [99], "modified_power": 8, "is_phased_out": True}
    actual = server_module._serialize_bridge_card({"instance_id": 1, "grp_id": 1001, **live}, {1: fallback})
    for key, value in live.items():
        assert actual[key] == value
    if live["keywords"]:
        actual["keywords"].append("trample")
        assert "trample" not in live["keywords"]


def test_missing_bridge_effect_fields_retain_log_fallback(monkeypatch):
    monkeypatch.setattr(
        server_module,
        "card_object_info",
        lambda card: {"name": "Rabbit", "type_line": "Creature", "oracle_text": ""},
    )
    actual = server_module._serialize_bridge_card(
        {"instance_id": 1, "grp_id": 1001}, {1: {"keywords": ["flying"], "ability_grp_ids": [42]}}
    )
    assert actual["keywords"] == ["flying"]
    assert actual["ability_grp_ids"] == [42]


def test_explicit_empty_bridge_abilities_do_not_enrich_from_stale_abilities(monkeypatch):
    seen = []

    def info(card):
        seen.append(card)
        return {"name": "Rabbit", "type_line": "Creature", "oracle_text": ""}

    monkeypatch.setattr(server_module, "card_object_info", info)
    server_module._serialize_bridge_card(
        {"instance_id": 1, "grp_id": 1001, "ability_grp_ids": []}, {1: {"ability_grp_ids": [42]}}
    )
    assert seen[0]["ability_grp_ids"] == []


@pytest.mark.parametrize(
    "cards_count,total_count,incomplete",
    [(128, 333, True), (0, 1, True), (1, 1, False), (0, 0, False), (1, None, False), (0, True, False)],
)
def test_partial_public_battlefield_does_not_replace_log_board(
    monkeypatch, cards_count, total_count, incomplete
):
    board = [{"instance_id": i + 1, "grp_id": 1001} for i in range(333)]
    bridge_state = {"zones": {"battlefield": {"total_count": total_count, "cards": board[:cards_count]}}}
    monkeypatch.setattr(gre_bridge_module, "get_bridge", lambda: _DummyBridge(bridge_state))
    monkeypatch.setattr(server_module, "enrich_with_oracle_text", _fake_enrich_with_oracle_text)
    overlay = server_module._get_bridge_overlay({}, [], {"battlefield": board}, 1, 2)
    response = {"zones": {"battlefield": board}}
    response.update(overlay)
    assert bool(overlay.get("_bridge_state_incomplete")) is incomplete
    assert overlay["bridge_connected"] is True
    if incomplete:
        assert response["zones"]["battlefield"] == board
        assert response["zones"]["battlefield"][-1]["instance_id"] == 333
        assert "players" not in overlay
    else:
        assert len(response["zones"]["battlefield"]) == cards_count
