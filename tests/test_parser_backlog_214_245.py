"""Parser backlog qualification using exact retained response prefixes.

#214(b): standalone.log 2026-10-20 17:33:38.221 (line 643468).
#245: standalone.log 2026-10-24 12:01:28.640 (line 725539), match f85b1e53.
Only 200 response characters were retained, not the full answers. The second
#245 attempt has an error offset but no separately retained raw answer.
These prefixes demonstrate complete payload recovery, not arbitrary quote repair.
"""

from unittest.mock import Mock

import pytest

from arenamcp.action_json import recover_action_json
from arenamcp.action_planner import ActionPlanner, ActionType

DAMAGE_PREFIX = '{"actions":[{"action_type":"assign_damage","distribution":{"Sneaking Guide":1,"Skirk Prospector":1},"reasoning":"Split 2 damage to kill both 1-toughness blockers"},"target_names":["Skirk Prospector","'
BLOCK_PREFIX = '{"actions":[{"action_type":"declare_blockers","blocker_assignments":{},"reasoning":"Keep both bodies for our pressure plan; 8 nonlethal"},"overall_strategy":"Preserve creatures to carry Ethereal Armor'


def test_214b_recorded_prefix_preserves_complete_damage_distribution():
    planner = ActionPlanner(Mock())
    plan = planner._parse_response(
        DAMAGE_PREFIX,
        ["Assign damage (order targets by priority)", "Done"],
        {"type": "assign_damage"},
        "AssignDamage",
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.ASSIGN_DAMAGE
    assert plan.actions[0].distribution == {"Sneaking Guide": 1, "Skirk Prospector": 1}
    assert plan.actions[0].target_names == []  # never complete the cut-off field


def test_245_recorded_prefix_recovers_explicit_empty_blocks():
    plan = ActionPlanner(Mock())._parse_response(
        BLOCK_PREFIX,
        ["Block with: Optimistic Scavenger", "Block with: Skyward Spider", "Done (confirm blockers)"],
        {"type": "declare_blockers"},
        "DeclareBlockers",
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.DECLARE_BLOCKERS
    assert plan.actions[0].blocker_assignments == {}


def test_recovery_does_not_bypass_current_request_legality():
    plan = ActionPlanner(Mock())._parse_response(
        DAMAGE_PREFIX,
        ["Block with: Skyward Spider", "Done (confirm blockers)"],
        {"type": "declare_blockers"},
        "DeclareBlockers",
    )
    assert plan.actions == []


@pytest.mark.parametrize(
    "raw",
    [
        '{"actions":[{"action_type":"declare_blockers","blocker_assignments":{"Paladin\'s Arms":"Hero",',
        '{"actions":[{"action_type":"declare_blockers","blocker_assignments":{"Skyward "Spider":"Hero"}}]}',
        '{"actions":[{"action_type":"assign_damage","distribution":{"Sneaking Guide":',
    ],
)
def test_incomplete_or_ambiguously_quoted_assignment_is_not_guessed(raw):
    assert recover_action_json(raw) is None
