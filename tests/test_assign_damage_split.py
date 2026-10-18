"""Combat damage assignment without a model split (2026-10-18 pro game, match 2 turn 13).

Ruric Thar, Magecrusher (7/7 trample, #283) was triple-blocked by Tetsuko
Umezawa, Pursuer (#254, 3/5 after Multiply by Zero wore off its pump: lethal 5),
Stingerquill Voxmancer (#300, lethal 2) and Hallway Heckler (#302, lethal 3).
The pro's AssignDamageResp put 5 on Tetsuko and 2 on Voxmancer, killing two.
Without a model split the bridge used to put all 7 on the first blocker.
"""

from __future__ import annotations

from unittest.mock import Mock

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin, _solver_damage_split

BATTLEFIELD = [
    {"instance_id": 283, "name": "Ruric Thar, Magecrusher", "type_line": "Legendary Creature — Ogre Warrior",
     "power": 7, "toughness": 7, "controller_seat_id": 2},
    {"instance_id": 254, "name": "Tetsuko Umezawa, Pursuer", "type_line": "Legendary Creature — Human Mercenary",
     "power": 3, "toughness": 5, "controller_seat_id": 1},
    {"instance_id": 300, "name": "Stingerquill Voxmancer", "type_line": "Creature — Human Wizard",
     "power": 1, "toughness": 2, "controller_seat_id": 1},
    {"instance_id": 302, "name": "Hallway Heckler", "type_line": "Creature — Human Rogue",
     "power": 2, "toughness": 3, "controller_seat_id": 1},
]  # fmt: skip
# Arena's assigner, as recorded in the pro's response.
ASSIGNER = {
    "instanceId": 283,
    "totalDamage": 7,
    "assignments": [
        {"instanceId": 254, "minDamage": 5, "maxDamage": 7},
        {"instanceId": 300, "minDamage": 2, "maxDamage": 7},
        {"instanceId": 302, "minDamage": 3, "maxDamage": 7},
    ],
}


def _pilot():
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "AssignDamageRequest",
        "assigners": [ASSIGNER],
    }
    pilot._gre_bridge.submit_assign_damage.return_value = True
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    return pilot


def _submitted(pilot) -> dict[int, int]:
    (assigners,) = pilot._gre_bridge.submit_assign_damage.call_args.args
    (assigner,) = assigners
    return {entry["instanceId"]: entry["damage"] for entry in assigner["assignments"]}


def test_the_solver_split_kills_two_blockers():
    assert _solver_damage_split(ASSIGNER["assignments"], BATTLEFIELD, 7) == [
        {"instanceId": 300, "damage": 2},
        {"instanceId": 254, "damage": 5},
    ]


def test_no_model_split_submits_the_solver_split():
    pilot = _pilot()
    result = pilot._try_gre_bridge_assign_damage(
        GameAction(ActionType.ASSIGN_DAMAGE), {"battlefield": BATTLEFIELD}
    )
    assert result is not None and result.success
    assert _submitted(pilot) == {300: 2, 254: 5}


def test_a_model_split_is_capped_at_the_damage_left():
    # 6 on Tetsuko then 4 on Voxmancer: only 1 is left for Voxmancer, never 4.
    pilot = _pilot()
    action = GameAction(
        ActionType.ASSIGN_DAMAGE,
        distribution={"Tetsuko Umezawa, Pursuer": 6, "Stingerquill Voxmancer": 4},
    )
    pilot._try_gre_bridge_assign_damage(action, {"battlefield": BATTLEFIELD})
    assert _submitted(pilot) == {254: 6, 300: 1}


def test_leftover_trample_damage_goes_to_the_player():
    assignments = [{"instanceId": 300, "minDamage": 2}, {"instanceId": 1}]  # 1: the defending player
    assert _solver_damage_split(assignments, BATTLEFIELD, 7) == [
        {"instanceId": 300, "damage": 2},
        {"instanceId": 1, "damage": 5},
    ]
