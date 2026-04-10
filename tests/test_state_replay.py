
import json
import pytest
from arenamcp.gamestate import GameState, create_game_state_handler, ZoneType

# Frame 1: Creature enters battlefield (Turn 2)
# Simulating LogParser accumulated JSON block
FRAME_1_JSON = """
{
  "greToClientEvent": {
    "greToClientMessages": [
      {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
          "type": "GameStateType_Full", 
          "turnInfo": { "turnNumber": 2, "activePlayer": 1, "phase": "Phase_Main1" },
          "zones": [
            { "zoneId": 12, "type": "ZoneType_Battlefield", "ownerSeatId": 1, "objectInstanceIds": [100] }
          ],
          "gameObjects": [
            { "instanceId": 100, "grpId": 555, "zoneId": 12, "ownerSeatId": 1, "cardTypes": ["Creature"], "isTapped": false }
          ]
        }
      }
    ]
  }
}
"""

# Frame 2: Creature gets Tapped (Turn 2 Update)
# Note: This update sends ONLY the changed fields (isTapped).
# Under previous logic, missing fields might have defaulted to None/False.
# Under new Sticky State logic, they should persist.
FRAME_2_JSON = """
{
  "greToClientEvent": {
    "greToClientMessages": [
      {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
          "type": "GameStateType_Diff",
          "turnInfo": { "turnNumber": 2 }, 
          "gameObjects": [
            { "instanceId": 100, "isTapped": true } 
          ]
        }
      }
    ]
  }
}
"""

# Frame 3: Turn 3 Starts
FRAME_3_JSON = """
{
  "greToClientEvent": {
    "greToClientMessages": [
      {
        "type": "GREMessageType_GameStateMessage",
        "gameStateMessage": {
          "type": "GameStateType_Diff",
          "turnInfo": { "turnNumber": 3, "activePlayer": 1, "phase": "Phase_Main1" }
        }
      }
    ]
  }
}
"""

def test_summoning_sickness_persistence():
    """Verify that turn_entered_battlefield persists across object updates."""
    game_state = GameState()
    handler = create_game_state_handler(game_state)
    
    # 1. Process Frame 1 (Entry)
    json_1 = json.loads(FRAME_1_JSON)
    handler(json_1)
    
    obj = game_state.game_objects[100]
    assert obj.turn_entered_battlefield == 2
    assert not obj.is_tapped
    assert obj.card_types == ["Creature"]
    
    # 2. Process Frame 2 (Update - Tapped)
    json_2 = json.loads(FRAME_2_JSON)
    handler(json_2)
    
    obj = game_state.game_objects[100]
    assert obj.is_tapped
    # Sticky State Checks:
    assert obj.turn_entered_battlefield == 2, f"Expected turn 2, got {obj.turn_entered_battlefield}"
    assert obj.card_types == ["Creature"], "Lost card types!"
    assert obj.grp_id == 555, "Lost GrpID!"

def test_snapshot_structure():
    """Verify the snapshot structure is LLM-friendly."""
    game_state = GameState()
    game_state.local_seat_id = 1
    handler = create_game_state_handler(game_state)

    json_1 = json.loads(FRAME_1_JSON)
    handler(json_1)
    
    snapshot = game_state.get_snapshot()
    
    assert snapshot["local_seat_id"] == 1
    assert "zones" in snapshot
    assert "battlefield" in snapshot["zones"]
    assert len(snapshot["zones"]["battlefield"]) == 1
    
    bf_obj = snapshot["zones"]["battlefield"][0]
    assert bf_obj["instance_id"] == 100
    assert bf_obj["turn_entered_battlefield"] == 2
