"""WP-0.4 regression tests: trajectory recording in the self-play orchestrator.

Every trajectory record must carry what was ACTUALLY submitted
(``submitted_action``) plus a ``fallback`` flag, so ``build_dataset.py`` can
drop decisions the model never made. Also covers winner attribution, which used
to assume the local bot always occupied seat 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.self_play import (
    SelfPlayOrchestrator,
    map_game_action_to_pipe_command,
    map_game_action_to_pipe_command_ex,
    plan_fallback_reason,
    resolve_match_winner,
)

# --------------------------------------------------------------- winner


def _players(local_seat: int, losing_seat: int, with_is_local: bool = True):
    out = []
    for seat in (1, 2):
        p: dict[str, Any] = {"seat_id": seat}
        if with_is_local:
            p["is_local"] = seat == local_seat
        if seat == losing_seat:
            p["status"] = "PlayerStatus_Lost"
        out.append(p)
    return out


def test_winner_attribution_when_local_is_seat_two():
    """Regression: the old code hardcoded `losing_seat == 1` => opp lost.

    With the local bot on seat 2 that inverted every label in the match.
    """
    # local on seat 2, seat 2 lost -> the local model LOST.
    assert resolve_match_winner(_players(local_seat=2, losing_seat=2)) == "opp"
    # local on seat 2, seat 1 lost -> the local model WON.
    assert resolve_match_winner(_players(local_seat=2, losing_seat=1)) == "local"


def test_winner_attribution_when_local_is_seat_one():
    assert resolve_match_winner(_players(local_seat=1, losing_seat=1)) == "opp"
    assert resolve_match_winner(_players(local_seat=1, losing_seat=2)) == "local"


def test_winner_is_none_without_a_loss_or_when_undeterminable():
    assert resolve_match_winner([]) is None
    assert resolve_match_winner([{"seat_id": 1, "is_local": True}, {"seat_id": 2}]) is None
    # Both players lost -> no winner, never a coin flip.
    both = [
        {"seat_id": 1, "is_local": True, "status": "PlayerStatus_Lost"},
        {"seat_id": 2, "is_local": False, "status": "PlayerStatus_Lost"},
    ]
    assert resolve_match_winner(both) is None
    # No is_local anywhere -> refuse to guess by seat id.
    assert resolve_match_winner(_players(local_seat=1, losing_seat=1, with_is_local=False)) is None


# ------------------------------------------------- submission tagging


def test_matched_action_is_not_a_fallback():
    action = GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")
    payload = {"actions": [{"actionType": "ActionType_Play", "grpId": 111, "instanceId": 7}]}
    game_state = {"hand": [{"name": "Forest", "instance_id": 7, "grp_id": 111}]}

    cmd, info = map_game_action_to_pipe_command_ex(action, payload, {}, game_state)
    assert cmd["action"] == "submit_action"
    assert cmd["action_index"] == 0
    assert info["fallback"] is False
    assert info["fallback_reason"] == ""
    assert info["submitted_action"] == str(action)


def test_action_without_ability_grp_id_still_matches():
    """Regression: absent `abilityGrpId` compared as None != 0, so a correctly
    matched action still fell through to the index-0 default and every land
    drop / cast was recorded as a fallback."""
    action = GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")
    payload = {
        "actions": [
            {"actionType": "ActionType_Pass"},
            {"actionType": "ActionType_Play", "grpId": 111, "instanceId": 7},
        ]
    }
    game_state = {"hand": [{"name": "Forest", "instance_id": 7, "grp_id": 111}]}
    cmd, info = map_game_action_to_pipe_command_ex(action, payload, {}, game_state)
    assert cmd["action_index"] == 1
    assert info["fallback"] is False


def test_unmatched_action_defaults_to_index_zero_and_is_tagged():
    """Index-0 substitution is a real submission the model never chose."""
    action = GameAction(action_type=ActionType.CAST_SPELL, card_name="Nonexistent Card")
    payload = {
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 999, "instanceId": 42},
            {"actionType": "ActionType_Cast", "grpId": 888, "instanceId": 43},
        ]
    }
    cmd, info = map_game_action_to_pipe_command_ex(action, payload, {}, {})
    assert cmd == {"action": "submit_action", "action_index": 0, "auto_pass": False}
    assert info["fallback"] is True
    assert info["fallback_reason"] == "action_unmatched"
    assert info["submitted_action"] != str(action)
    assert "grpId=999" in info["submitted_action"]


def test_empty_action_list_passes_and_is_tagged():
    action = GameAction(action_type=ActionType.CAST_SPELL, card_name="Shock")
    cmd, info = map_game_action_to_pipe_command_ex(action, {"actions": []}, {}, {})
    assert cmd == {"action": "submit_pass"}
    assert info["fallback"] is True
    assert info["fallback_reason"] == "no_raw_actions"
    assert info["submitted_action"] == "pass"


def test_unresolvable_attacker_names_are_tagged():
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Grizzly Bears", "Phantom Creature"],
    )
    game_state = {
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [{"name": "Grizzly Bears", "instance_id": 12}],
    }
    cmd, info = map_game_action_to_pipe_command_ex(action, {}, {}, game_state)
    assert len(cmd["attackers"]) == 1
    assert info["fallback"] is True
    assert info["fallback_reason"] == "attackers_unresolved"
    assert info["submitted_action"] == "declare_attackers(Grizzly Bears)"


def test_fully_resolved_attackers_are_not_tagged():
    action = GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["Grizzly Bears"])
    game_state = {
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2, "is_local": False}],
        "battlefield": [{"name": "Grizzly Bears", "instance_id": 12}],
    }
    _cmd, info = map_game_action_to_pipe_command_ex(action, {}, {}, game_state)
    assert info["fallback"] is False
    assert info["submitted_action"] == "declare_attackers(Grizzly Bears)"


def test_substituted_target_is_tagged():
    action = GameAction(action_type=ActionType.SELECT_TARGET, target_names=["Not On Board"])
    payload = {"qualifiedTargets": [{"targetInstanceId": 55}]}
    cmd, info = map_game_action_to_pipe_command_ex(action, payload, {}, {"battlefield": []})
    assert cmd == {"action": "submit_targets", "target_instance_id": 55}
    assert info["fallback"] is True
    assert info["fallback_reason"] == "target_unmatched"


def test_pass_and_mulligan_are_never_tagged():
    for atype, expected in (
        (ActionType.PASS_PRIORITY, "pass"),
        (ActionType.MULLIGAN_KEEP, "mulligan_keep"),
        (ActionType.MULLIGAN_MULL, "mulligan_mull"),
    ):
        _cmd, info = map_game_action_to_pipe_command_ex(GameAction(action_type=atype), {}, {}, {})
        assert info["fallback"] is False
        assert info["submitted_action"] == expected


def test_legacy_mapper_signature_is_preserved():
    """The single-return wrapper stays, so existing callers keep working."""
    cmd = map_game_action_to_pipe_command(GameAction(action_type=ActionType.PASS_PRIORITY), {}, {}, {})
    assert cmd == {"action": "submit_pass"}


# ------------------------------------------------------- planner tags


def test_plan_fallback_reason_recognises_planner_default_paths():
    assert plan_fallback_reason(ActionPlan()) == "planner_no_actions"

    auto = ActionPlan(
        actions=[GameAction(action_type=ActionType.PASS_PRIORITY)],
        overall_strategy="[auto-pick] Pass",
    )
    assert plan_fallback_reason(auto) == "planner_auto_pick"

    land = ActionPlan(
        actions=[GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")],
        overall_strategy="[land-drop-first] Play Land: Forest",
    )
    assert plan_fallback_reason(land) == "planner_preflight_land_drop"

    # A salvaged pick is still the MODEL's chosen index — not a fallback.
    salvage = ActionPlan(
        actions=[GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")],
        overall_strategy="[pick-salvage] Play Land: Forest",
    )
    assert plan_fallback_reason(salvage) == ""

    real = ActionPlan(
        actions=[GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")],
        overall_strategy="Develop mana before combat",
    )
    assert plan_fallback_reason(real) == ""


# ------------------------------------------------- end-to-end recording


class _StubPlanner:
    """Minimal ActionPlanner stand-in: no backend, no network."""

    def __init__(self, plan: ActionPlan):
        self._plan = plan

    def _build_action_prompt(self, game_state, trigger, legal_actions, context):
        return f"PROMPT trigger={trigger} legal={legal_actions}"

    def plan_actions(self, game_state, trigger, **kwargs):
        return self._plan


def _orchestrator(tmp_path: Path, plan: ActionPlan, alt_plan: ActionPlan) -> SelfPlayOrchestrator:
    orch = SelfPlayOrchestrator.__new__(SelfPlayOrchestrator)
    orch.trajectories_path = tmp_path / "traj.jsonl"
    orch.local_planner = _StubPlanner(plan)
    orch.opp_planner = _StubPlanner(alt_plan)

    class _Spec:
        label = "stub"

    orch.local_spec = _Spec()
    orch.opp_spec = _Spec()
    orch.current_match_id = None
    orch.current_match_winner = None
    orch.decisions_log = []
    import threading

    orch._log_lock = threading.Lock()
    return orch


def _request(payload: dict, players: list[dict]) -> dict:
    return {
        "seat": "local",
        "request_type": "ActionsAvailableRequest",
        "payload": payload,
        "context": {"type": "priority"},
        "game_state": {
            "match_id": "m1",
            "players": players,
            "battlefield": [],
            "hand": [{"name": "Forest", "instance_id": 7, "grp_id": 111}],
            "turn": {"turn_number": 3, "phase": "Main1"},
            "legal_actions": ["Play Land: Forest", "Pass"],
        },
    }


@pytest.fixture
def _land_plan():
    return ActionPlan(
        actions=[GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")],
        overall_strategy="Develop mana",
    )


def test_record_carries_submitted_action_and_fallback_false(tmp_path, _land_plan):
    orch = _orchestrator(tmp_path, _land_plan, ActionPlan())
    payload = {"actions": [{"actionType": "ActionType_Play", "grpId": 111, "instanceId": 7}]}
    orch.handle_decision_request(_request(payload, _players(local_seat=1, losing_seat=0)))

    rec = orch.decisions_log[0]
    assert rec["fallback"] is False
    assert rec["fallback_reason"] == ""
    assert rec["submitted_action"] == rec["planned_action"]
    # The DPO counterpart is tagged separately.
    assert rec["alt_fallback"] is True
    assert rec["alt_fallback_reason"] == "planner_no_actions"


def test_record_tags_fallback_when_submission_diverges(tmp_path, _land_plan):
    orch = _orchestrator(tmp_path, _land_plan, ActionPlan())
    # The legal actions are unrelated casts: the planned land drop cannot be
    # matched, so index 0 is submitted instead.
    payload = {
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 555, "instanceId": 9},
            {"actionType": "ActionType_Cast", "grpId": 556, "instanceId": 10},
        ]
    }
    orch.handle_decision_request(_request(payload, _players(local_seat=1, losing_seat=0)))

    rec = orch.decisions_log[0]
    assert rec["fallback"] is True
    assert rec["fallback_reason"] == "action_unmatched"
    assert rec["submitted_action"] != rec["planned_action"]
    assert "grpId=555" in rec["submitted_action"]


def test_record_tags_planner_auto_pick(tmp_path):
    auto = ActionPlan(
        actions=[GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest")],
        overall_strategy="[auto-pick] Play Land: Forest",
    )
    orch = _orchestrator(tmp_path, auto, ActionPlan())
    payload = {"actions": [{"actionType": "ActionType_Play", "grpId": 111, "instanceId": 7}]}
    orch.handle_decision_request(_request(payload, _players(local_seat=1, losing_seat=0)))

    rec = orch.decisions_log[0]
    assert rec["fallback"] is True
    assert rec["fallback_reason"] == "planner_auto_pick"


def test_flushed_trajectory_lines_are_dataset_ready(tmp_path, _land_plan):
    """Every flushed line must carry the fields build_dataset.py filters on."""
    orch = _orchestrator(tmp_path, _land_plan, ActionPlan())
    payload = {"actions": [{"actionType": "ActionType_Play", "grpId": 111, "instanceId": 7}]}
    # local is seat 2 here; seat 1 lost, so the local model won.
    orch.handle_decision_request(_request(payload, _players(local_seat=2, losing_seat=1)))
    orch.flush_match_logs()

    lines = orch.trajectories_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    for key in ("fallback", "submitted_action", "planned_action", "winner", "prompt_user"):
        assert key in rec, key
    assert rec["winner"] == "local"
    assert rec["fallback"] is False
