"""Unit test for WP-0.1 contract serialization in build_dataset & action_planner."""

import json
from arenamcp.action_planner import ActionType, GameAction, game_action_to_schema_json


def test_game_action_to_schema_json_serialization():
    action = GameAction(
        action_type=ActionType.CAST_SPELL,
        card_name="Lightning Bolt",
        target_names=["Opponent"],
        reasoning="Deal 3 damage to opponent face",
    )
    serialized = game_action_to_schema_json(action)
    data = json.loads(serialized)

    assert "actions" in data
    assert len(data["actions"]) == 1
    act = data["actions"][0]
    assert act["action_type"] == "cast_spell"
    assert act["card_name"] == "Lightning Bolt"
    assert act["target_names"] == ["Opponent"]
    assert act["reasoning"] == "Deal 3 damage to opponent face"


def test_menu_pick_to_schema_json_serialization():
    serialized = game_action_to_schema_json(3)
    data = json.loads(serialized)

    assert "actions" in data
    assert data["actions"][0]["pick"] == 3
