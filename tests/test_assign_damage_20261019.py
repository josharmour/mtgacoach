"""Combat damage assignment on Arena's AssignDamageRequest (ledger #102, bug_20261019_122251).

Match 5d5d32f8, turn 13: Giant Cindermaw (4/3 trample, #344) attacked the
opponent (seat 1, 6 life); Pyrewood Gearhulk (7/7 with 5 damage marked, #350)
blocked. Arena asked (Player.log 12:22:18, msgId 410, gameStateId 307):

    {"instanceId": 344, "totalDamage": 4, "assignments": [
        {"instanceId": 350, "minDamage": 2, "assignedDamage": 2},
        {"instanceId": 1, "maxDamage": 2, "assignedDamage": 2}]}

The model's answer broke off ("Failed to parse action plan JSON: Expecting ','
delimiter: line 1 column 202"), the fallback picked the menu's "Done", the
bridge sent it as a pass ("Pending is AssignDamageRequest, not
ActionsAvailableRequest"), the autopilot went MANUAL REQUIRED and sent Oops,
and the user clicked Done at 12:23:04: AssignDamageResp 350 <- 2, 1 <- 2 (the
opponent went to 4).
"""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

from tests.fakes import make_engine
from tests.mac_bridge_fakes import MSG, FakeGame, adapter_for, listing

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotState
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.damage_assignment import (
    FALLBACK_DAMAGE_ASSIGNMENT,
    deterministic_split,
    illegal_reason,
    plan_assigners,
    receivers,
)

CINDERMAW = {
    "instance_id": 344,
    "grp_id": 94052,
    "name": "Giant Cindermaw",
    "oracle_text": "Trample\nPlayers can't gain life.",
    "type_line": "Creature — Dinosaur Beast",
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "power": 4,
    "toughness": 3,
    "is_tapped": True,
    "is_attacking": True,
    "card_types": ["Creature"],
    "keywords": ["trample"],
    "attack_target_id": 1,
}
GEARHULK = {
    "instance_id": 350,
    "grp_id": 95018,
    "name": "Pyrewood Gearhulk",
    "oracle_text": "Vigilance\nMenace",
    "type_line": "Artifact Creature — Construct",
    "controller_seat_id": 1,
    "owner_seat_id": 1,
    "power": 7,
    "toughness": 7,
    "damage": 5,
    "is_blocking": True,
    "card_types": ["Artifact", "Creature"],
    "keywords": ["vigilance", "menace"],
}
MOUNTAIN = {
    "instance_id": 310,
    "name": "Mountain",
    "type_line": "Basic Land — Mountain",
    "controller_seat_id": 2,
    "card_types": ["Land"],
}
BATTLEFIELD = [MOUNTAIN, CINDERMAW, GEARHULK]
PLAYERS = [
    {"seat_id": 1, "life_total": 6, "is_local": False},
    {"seat_id": 2, "life_total": 20, "is_local": True},
]
# Player.log 12:22:18 (the log's shape: unset fields are absent).
REQUEST = {
    "instanceId": 344,
    "totalDamage": 4,
    "assignments": [
        {"instanceId": 350, "minDamage": 2, "assignedDamage": 2},
        {"instanceId": 1, "maxDamage": 2, "assignedDamage": 2},
    ],
}
# The macOS bridge's get_pending_actions for the same request (bug report, autopilot.engine).
POLL = {
    "ok": True,
    "has_pending": True,
    "request_type": "AssignDamage",
    "request_class": "AssignDamageRequest",
    "can_pass": False,
    "can_cancel": False,
    "bridge_runtime": "il2cpp-macos",
    "assigners": [
        {
            "instanceId": 344,
            "totalDamage": 4,
            "assignments": [
                {"instanceId": 350, "minDamage": 2, "maxDamage": 0, "assignedDamage": 2},
                {"instanceId": 1, "minDamage": 0, "maxDamage": 2, "assignedDamage": 2},
            ],
        }
    ],
}
# 12:22:20: the model's answer, cut off after the action (the actions array never closes).
TRUNCATED = (
    '{"actions":[{"action_type":"assign_damage","distribution":{"Pyrewood Gearhulk [350]":2,'
    '"Opponent [1]":2},"reasoning":"2 kills the 5-damaged Gearhulk, 2 tramples through to opponent"},'
    '"overall_strategy":"Kill the Gearhulk and push trample damage."}'
)
LEGAL = ["Assign damage (order targets by priority)", "Done"]
USERS_DONE = {350: 2, 1: 2}  # the AssignDamageResp the user sent at 12:23:04


def bug_state() -> dict:
    """The log snapshot at 12:22:18 (bug report game_state)."""
    return {
        "match_id": "5d5d32f8-b97a-424d-919d-db52dc25dfe2",
        "opponent_name": "Zac",
        "turn": {
            "turn_number": 13,
            "active_player": 2,
            "priority_player": 2,
            "phase": "Combat",
            "step": "CombatDamage",
        },
        "players": PLAYERS,
        "local_seat_id": 2,
        "battlefield": BATTLEFIELD,
        "hand": [],
        "stack": [],
        "graveyard": [],
        "pending_decision": "Assign Damage",
        "decision_context": {"type": "assign_damage", "raw": {"damageAssigners": [REQUEST]}},
        "legal_actions": LEGAL,
        "_bridge_connected": True,
        "_bridge_request_type": "AssignDamage",
        "_bridge_request_class": "AssignDamageRequest",
    }


def _as_map(split: list[dict]) -> dict[int, int]:
    return {entry["instanceId"]: entry["damage"] for entry in split}


# --- the deterministic split ----------------------------------------------------------


def test_the_real_request_splits_like_the_users_done():
    split = deterministic_split(REQUEST["assignments"], BATTLEFIELD, 4, attacker=CINDERMAW, players=PLAYERS)
    assert _as_map(split) == USERS_DONE


def test_without_arenas_lethal_amount_marked_damage_counts():
    # The 7/7 has 5 damage marked: 2 is lethal, the other 2 trample over.
    bare = [{"instanceId": 350}, {"instanceId": 1, "maxDamage": 2}]
    split = deterministic_split(bare, BATTLEFIELD, 4, attacker=CINDERMAW, players=PLAYERS)
    assert _as_map(split) == USERS_DONE


def test_a_deathtouch_attacker_needs_1_per_blocker():
    attacker = {**CINDERMAW, "keywords": ["trample", "deathtouch"]}
    bare = [{"instanceId": 350}, {"instanceId": 1}]
    split = deterministic_split(bare, BATTLEFIELD, 4, attacker=attacker, players=PLAYERS)
    assert _as_map(split) == {350: 1, 1: 3}


def test_without_trample_the_excess_stays_on_the_last_blocker():
    # Player-0802.log 07:38 shape: two blockers, no quarry entry (no trample).
    a = {**GEARHULK, "instance_id": 589, "name": "Blocker A", "damage": 0, "toughness": 1}
    b = {**GEARHULK, "instance_id": 590, "name": "Blocker B", "damage": 0, "toughness": 2}
    assignments = [{"instanceId": 589, "minDamage": 1}, {"instanceId": 590, "minDamage": 2}]
    assert _as_map(deterministic_split(assignments, [a, b], 5)) == {589: 1, 590: 4}


def test_damage_short_of_every_blocker_kills_one_and_wounds_the_other():
    # 2026-10-18 pro game (26a87f02 T10): Dina (5 power) into Campus Crier
    # (lethal 2) and Zimone (lethal 4). The pro's answer: 2 and 3.
    crier = {
        "instance_id": 207,
        "name": "Campus Crier",
        "type_line": "Creature — Human",
        "power": 4,
        "toughness": 2,
    }
    zimone = {
        "instance_id": 217,
        "name": "Zimone",
        "type_line": "Legendary Creature — Human",
        "power": -1,
        "toughness": 4,
    }
    assignments = [{"instanceId": 207, "minDamage": 2}, {"instanceId": 217, "minDamage": 4}]
    assert _as_map(deterministic_split(assignments, [crier, zimone], 5)) == {207: 2, 217: 3}


def test_trample_never_reaches_the_player_past_a_blocker_without_lethal():
    a = {**GEARHULK, "instance_id": 21, "name": "A", "damage": 0, "toughness": 2}
    b = {**GEARHULK, "instance_id": 22, "name": "B", "damage": 0, "toughness": 2}
    assignments = [{"instanceId": 21, "minDamage": 2}, {"instanceId": 22, "minDamage": 2}, {"instanceId": 1}]
    split = _as_map(deterministic_split(assignments, [a, b], 3, attacker=CINDERMAW, players=PLAYERS))
    assert split.get(1, 0) == 0 and sum(split.values()) == 3
    recs = receivers(assignments, [a, b], attacker=CINDERMAW, players=PLAYERS)
    assert illegal_reason(recs, split, 3) == ""
    assert illegal_reason(recs, {21: 2, 1: 1}, 3) != ""


# --- the model's split: used only when Arena would take it ------------------------------


def test_the_models_split_from_the_bug_is_legal_and_used():
    distribution = {"Pyrewood Gearhulk [350]": 2, "Opponent [1]": 2}
    assigners, source = plan_assigners(POLL["assigners"], bug_state(), distribution)
    assert source == "model"
    assert _as_map(assigners[0]["assignments"]) == USERS_DONE


def test_the_prompts_player_label_names_the_opponent():
    distribution = {"Pyrewood Gearhulk": 2, "Opponent Zac (6 life)": 2}
    assigners, source = plan_assigners(POLL["assigners"], bug_state(), distribution)
    assert source == "model" and _as_map(assigners[0]["assignments"]) == USERS_DONE


def test_trampling_past_an_unkilled_blocker_is_replaced():
    assigners, source = plan_assigners(POLL["assigners"], bug_state(), {"Opponent [1]": 4})
    assert source == "deterministic"
    assert _as_map(assigners[0]["assignments"]) == USERS_DONE


def test_a_receiver_arena_did_not_offer_is_replaced():
    assigners, source = plan_assigners(POLL["assigners"], bug_state(), {"Giant Cindermaw [344]": 4})
    assert source == "deterministic" and _as_map(assigners[0]["assignments"]) == USERS_DONE


def test_trample_damage_kept_on_a_dead_blocker_is_replaced():
    # 2026-10-18 18:41 (new_build Player_1792357682.log): 5 on a lethal-3 blocker,
    # the player's 2 left out, refused FailureReason_InvalidDamageAssignment 7 times.
    blocker = {"instance_id": 455, "name": "Wall", "type_line": "Creature — Wall", "power": 0, "toughness": 3}
    attacker = {**CINDERMAW, "instance_id": 448, "power": 5}
    state = {**bug_state(), "battlefield": [attacker, blocker]}
    request = {
        "instanceId": 448,
        "totalDamage": 5,
        "assignments": [
            {"instanceId": 455, "minDamage": 3, "assignedDamage": 3},
            {"instanceId": 1, "maxDamage": 2, "assignedDamage": 2},
        ],
    }
    assigners, source = plan_assigners([request], state, {"Wall": 5})
    assert source == "deterministic"
    assert _as_map(assigners[0]["assignments"]) == {455: 3, 1: 2}


# --- the planner: a broken answer falls straight to the split --------------------------


class _Backend:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls = 0

    def complete(self, *args, **kwargs) -> str:
        self.calls += 1
        return self.answer


def test_the_cut_off_answer_plans_the_assignment_not_done():
    planner = ActionPlanner(backend=_Backend(TRUNCATED))
    state = bug_state()
    plan = planner.plan_actions(state, "decision_required", LEGAL, state["decision_context"])

    assert [a.action_type for a in plan.actions] == [ActionType.ASSIGN_DAMAGE]
    assert plan.fallback_reason == FALLBACK_DAMAGE_ASSIGNMENT
    assert "2 to Pyrewood Gearhulk and 2 to the opponent" in plan.actions[0].reasoning


# --- the bridge: Done on AssignDamage is the assignment, never a pass ----------------------


def _pilot(poll: dict = POLL):
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.get_pending_actions.return_value = poll
    pilot._gre_bridge.submit_assign_damage.return_value = True
    pilot._gre_bridge.submit_pass.return_value = False
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    return pilot


def _submitted(pilot) -> dict[int, int]:
    (assigners,) = pilot._gre_bridge.submit_assign_damage.call_args.args
    (assigner,) = assigners
    assert assigner["instanceId"] == 344
    return _as_map(assigner["assignments"])


def test_done_on_assign_damage_submits_the_split_not_a_pass():
    pilot = _pilot()
    result = pilot._try_gre_bridge(GameAction(ActionType.CLICK_BUTTON, card_name="done"), bug_state())

    assert result is not None and result.success
    pilot._gre_bridge.submit_pass.assert_not_called()
    assert _submitted(pilot) == USERS_DONE
    spoken = ActionPlan(actions=[result.submitted_action]).spoken_actions()
    assert spoken == "Assign 2 to Pyrewood Gearhulk and 2 to the opponent."


def test_an_illegal_model_split_is_replaced_and_said_so():
    pilot = _pilot()
    action = GameAction(ActionType.ASSIGN_DAMAGE, distribution={"Opponent [1]": 4})
    result = pilot._try_gre_bridge_assign_damage(action, bug_state())

    assert result is not None and result.success
    assert _submitted(pilot) == USERS_DONE
    assert "not legal" in result.submitted_action.override_note


def test_the_autopilot_finishes_the_reported_window():
    """End to end: the cut-off answer at 12:22:20 now submits 2 + 2 instead of MANUAL REQUIRED."""
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = POLL

    def submitted(assigners):
        bridge.get_pending_actions.return_value = {"ok": True, "has_pending": False}
        return True

    bridge.submit_assign_damage.side_effect = submitted
    engine = make_engine(bridge, ActionPlanner(backend=_Backend(TRUNCATED)), game_state=bug_state)
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()

    assert engine.process_trigger(bug_state(), "decision_required") is True

    bridge.submit_assign_damage.assert_called_once()
    (assigners,) = bridge.submit_assign_damage.call_args.args
    assert _as_map(assigners[0]["assignments"]) == USERS_DONE
    bridge.submit_pass.assert_not_called()
    engine._pause_for_manual.assert_not_called()
    assert engine._state != AutopilotState.PAUSED


class _DeadBackend:
    def complete(self, *args, **kwargs) -> str:
        raise ConnectionError("model server down")


def test_without_the_model_the_safe_default_assigns_the_split():
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = POLL
    bridge.submit_assign_damage.return_value = True
    engine = make_engine(bridge, ActionPlanner(backend=_DeadBackend()), game_state=bug_state)
    engine._game_plan_mgr = None
    engine._pause_for_manual = MagicMock()

    assert engine.process_trigger(bug_state(), "decision_required") is True

    (assigners,) = bridge.submit_assign_damage.call_args.args
    assert _as_map(assigners[0]["assignments"]) == USERS_DONE
    bridge.submit_pass.assert_not_called()
    engine._pause_for_manual.assert_not_called()


# --- the macOS bridge answers with every entry, as Arena's Done does --------------------------


def _assigner(handle: int, attacker: int, total: int, *entries: tuple[int, int, int, int]) -> dict:
    return {
        "$c": MSG + "DamageAssigner",
        "$h": handle,
        "InstanceId": attacker,
        "TotalDamage": total,
        "Assignments": listing(
            *[
                {
                    "$c": MSG + "DamageAssignment",
                    "$h": handle * 10 + i,
                    "InstanceId": rid,
                    "MinDamage": low,
                    "MaxDamage": high,
                    "AssignedDamage": assigned,
                }
                for i, (rid, low, high, assigned) in enumerate(entries)
            ]
        ),
    }


def _damage_request(*assigners: dict) -> dict:
    return {"$c": "GreClient.Rules.AssignDamageRequest", "$h": 5, "Assigners": listing(*assigners)}


def _sent_assigners(game: FakeGame) -> list[tuple[int, list[tuple[int, int]]]]:
    """(attacker, [(receiver, assigned), ...]) as the submit batch builds them."""
    (batch,) = game.submits()
    values: dict[int, dict[str, int]] = {}
    kinds: dict[int, str] = {}
    for index, op in enumerate(batch):
        if op["op"] == "new":
            kinds[index] = op["class"].rsplit(".", 1)[-1]
            values[index] = {}
        elif op["op"] == "set" and op["target"].get("ref") in values:
            values[op["target"]["ref"]][op["member"]] = op["value"]["uint"]
    out: list[tuple[int, list[tuple[int, int]]]] = []
    for index in sorted(values):
        if kinds[index] == "DamageAssigner":
            out.append((values[index]["InstanceId"], []))
        elif kinds[index] == "DamageAssignment":
            out[-1][1].append((values[index]["InstanceId"], values[index]["AssignedDamage"]))
    return out


def test_mac_bridge_sends_every_receiver_in_arenas_order():
    # The pro's Ruric Thar block (2026-10-18): Heckler gets nothing but is still listed, as Arena does.
    game = FakeGame(_damage_request(_assigner(30, 283, 7, (254, 5, 0, 5), (300, 2, 0, 2), (302, 3, 0, 0))))
    response = adapter_for(game).handle(
        {
            "action": "submit_assign_damage",
            "assigners": [
                {
                    "instanceId": 283,
                    "assignments": [{"instanceId": 300, "damage": 2}, {"instanceId": 254, "damage": 5}],
                }
            ],
        }
    )
    assert response["ok"] is True
    assert _sent_assigners(game) == [(283, [(254, 5), (300, 2), (302, 0)])]


def test_mac_bridge_keeps_arenas_answer_for_an_attacker_left_out():
    game = FakeGame(
        _damage_request(
            _assigner(30, 344, 4, (350, 2, 0, 2), (1, 0, 2, 2)),
            _assigner(40, 345, 3, (360, 3, 0, 3)),
        )
    )
    response = adapter_for(game).handle(
        {
            "action": "submit_assign_damage",
            "assigners": [{"instanceId": 344, "assignments": [{"instanceId": 350, "damage": 4}]}],
        }
    )
    assert response["ok"] is True
    assert _sent_assigners(game) == [(344, [(350, 4), (1, 0)]), (345, [(360, 3)])]


def test_mac_bridge_refuses_a_receiver_arena_did_not_list():
    game = FakeGame(_damage_request(_assigner(30, 344, 4, (350, 2, 0, 2), (1, 0, 2, 2))))
    response = adapter_for(game).handle(
        {
            "action": "submit_assign_damage",
            "assigners": [{"instanceId": 344, "assignments": [{"instanceId": 999, "damage": 4}]}],
        }
    )
    assert response["ok"] is False
    assert game.submits() == []
