"""Regression test for the self-play orchestrator and named pipe server."""

from __future__ import annotations

from typing import Any

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.self_play import map_game_action_to_pipe_command


def test_map_game_action_to_pipe_command_pass():
    action = GameAction(action_type=ActionType.PASS_PRIORITY)
    payload = {"actions": [{"actionType": "ActionType_Pass"}]}
    context = {"type": "pass_priority"}
    game_state: dict[str, Any] = {}

    res = map_game_action_to_pipe_command(action, payload, context, game_state)
    assert res == {"action": "submit_pass"}


def test_map_game_action_to_pipe_command_mulligan():
    action = GameAction(action_type=ActionType.MULLIGAN_KEEP)
    payload = {}
    context = {"type": "mulligan"}
    game_state: dict[str, Any] = {}

    res = map_game_action_to_pipe_command(action, payload, context, game_state)
    assert res == {"action": "submit_mulligan", "keep": True}


def test_map_game_action_to_pipe_command_attackers():
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Grizzly Bears"],
    )
    payload = {"attackers": [{"attackerInstanceId": 12}]}
    context = {"type": "combat_attackers"}
    game_state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "battlefield": [
            {"name": "Grizzly Bears", "instance_id": 12},
        ],
    }

    res = map_game_action_to_pipe_command(action, payload, context, game_state)
    assert res["action"] == "submit_attackers"
    assert len(res["attackers"]) == 1
    assert res["attackers"][0]["attackerInstanceId"] == 12
    assert res["attackers"][0]["damageRecipient"]["playerSystemSeatId"] == 2


def test_map_game_action_to_pipe_command_blockers():
    action = GameAction(
        action_type=ActionType.DECLARE_BLOCKERS,
        blocker_assignments={"Grizzly Bears": "Colossal Dreadmaw"},
    )
    payload = {
        "blockers": [
            {
                "blockerInstanceId": 15,
                "attackerInstanceIds": [22],
            }
        ]
    }
    context = {"type": "combat_blockers"}
    game_state = {
        "battlefield": [
            {"name": "Grizzly Bears", "instance_id": 15},
            {"name": "Colossal Dreadmaw", "instance_id": 22},
        ]
    }

    res = map_game_action_to_pipe_command(action, payload, context, game_state)
    assert res["action"] == "submit_blockers"
    assert len(res["blockers"]) == 1
    assert res["blockers"][0]["blockerInstanceId"] == 15
    assert res["blockers"][0]["attackerInstanceIds"] == [22]
