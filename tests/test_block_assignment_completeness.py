"""A blocker menu pick must not turn into an assignment with a blank attacker."""

import json
from types import SimpleNamespace

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine

MENU = ["Block with: Soldier #1 (1/1)", "Done (confirm blockers)"]
ASSIGNMENT = {"Soldier #1": "Enemy Giant"}
CONTEXT = {"type": "declare_blockers", "attackers": ["Enemy Giant", "Enemy Goblin"]}


def parse(response):
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_menu = MENU
    return planner._parse_response(response, MENU, CONTEXT, "DeclareBlockers")


@pytest.mark.parametrize(
    "response",
    [
        '{"pick": 1}',
        '{"pick": 1, "action_type": "declare_blockers", "blocker_assignments": {}}',
        '{"action_type": "declare_blockers"}',
        '{"action_type": "declare_blockers", "blocker_assignments": {"Soldier #1": ""}}',
        '{"action_type": "declare_blockers", "blocker_assignments": {"Soldier #1": null}}',
        '{"pick": 1, "reasoning": INVALID}',
    ],
)
def test_incomplete_blocks_cannot_reach_the_executor(response):
    assert parse(response).actions == []


def test_menu_pick_preserves_explicit_attacker_assignment():
    plan = parse(json.dumps({"pick": 1, "blocker_assignments": ASSIGNMENT}))
    assert len(plan.actions) == 1
    assert plan.actions[0].blocker_assignments == ASSIGNMENT


def test_intentional_no_blocks_stays_supported():
    plan = parse('{"action_type": "declare_blockers", "blocker_assignments": {}}')
    assert len(plan.actions) == 1
    assert plan.actions[0].blocker_assignments == {}


def test_missing_attacker_is_rejected_even_without_a_legal_menu():
    planner = ActionPlanner.__new__(ActionPlanner)
    action = GameAction(ActionType.DECLARE_BLOCKERS, blocker_assignments={"Soldier #1": ""})
    assert not planner._is_action_legal(action, [], CONTEXT, "DeclareBlockers")


class BlockBackend:
    def __init__(self, repaired):
        self.responses = ['{"pick": 1, "reasoning": "block the largest"}', repaired]
        self.prompts = []

    def complete(self, system, user, *args, **kwargs):
        self.prompts.append(user)
        return self.responses.pop(0)


@pytest.mark.parametrize("valid_repair", [True, False])
def test_incomplete_block_is_repaired_once_never_replaced_with_done(valid_repair):
    response = json.dumps({"action_type": "declare_blockers", "blocker_assignments": ASSIGNMENT})
    backend = BlockBackend(response if valid_repair else '{"pick": 1}')
    planner = ActionPlanner(backend, timeout=1, land_drop_first=False)
    state = {
        "turn": {"turn_number": 11, "active_player": 2, "phase": "Phase_Combat"},
        "players": [{"seat_id": 1, "is_local": True}],
        "local_seat_id": 1,
        "decision_context": CONTEXT,
        "_bridge_request_type": "DeclareBlockers",
    }
    plan = planner.plan_actions(state, "decision_required", MENU, CONTEXT)
    assert len(backend.prompts) == 2
    assert "previous response did not specify a valid complete block" in backend.prompts[-1]
    if valid_repair:
        assert plan.actions[0].blocker_assignments == ASSIGNMENT
    else:
        assert plan.actions == []


def test_garbage_blocking_answer_never_heuristically_confirms_no_blocks():
    planner = ActionPlanner.__new__(ActionPlanner)
    assert planner._fallback_plan("bad output", MENU).actions == []


def test_no_unvalidated_auto_response_after_block_repair_fails():
    pending = {
        "has_pending": True,
        "request_type": "DeclareBlockers",
        "request_class": "DeclareBlockersRequest",
    }
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._config = SimpleNamespace(dry_run=False)
    engine._gre_bridge = SimpleNamespace(
        connected=True,
        get_pending_actions=lambda: pending,
        auto_respond=lambda: pytest.fail("Must not auto-confirm no blocks"),
    )
    state = {"_bridge_request_type": "DeclareBlockers", "decision_context": CONTEXT}
    assert engine._try_interactive_safe_default(state, "decision_required") is False
    assert not engine._try_auto_respond_escape(state, "missing assignments")
