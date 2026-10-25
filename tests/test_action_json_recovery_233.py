import json
from unittest.mock import Mock

import pytest

from arenamcp.action_json import recover_action_json
from arenamcp.action_planner import ActionPlanner, ActionType


@pytest.mark.parametrize(
    "tail", ['"reaso', '"reasoning":"Choose X=4 because', '"reasoning":"Use "four" mana"}]}']
)
def test_numeric_payload_survives_broken_trailing_prose(tail):
    raw = '{"actions":[{"action_type":"numeric_input","numeric_value":4,' + tail
    plan = ActionPlanner(Mock())._parse_response(raw, [])
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.NUMERIC_INPUT
    assert plan.actions[0].numeric_value == 4


def test_completed_action_is_kept_before_incomplete_next_action():
    raw = '{"actions":[{"action_type":"numeric_input","numeric_value":4},{"action_type":'
    assert recover_action_json(raw) == {"actions": [{"action_type": "numeric_input", "numeric_value": 4}]}


@pytest.mark.parametrize(
    "raw",
    [
        '{"actions":[{"action_type":"numeric_input","numeric_value":',
        '{"actions":[{"action_type":"numeric_input","numeric_value":4',
        '{"actions":[{"action_type":"declare_blockers","blocker_assignments":{"Spider":"Odin",',
        '{"actions":[{"action_type":"declare_attackers","attacker_names":["Spider",',
        '{"actions":[{"action_type":"numeric_input","numeric_value":"4","reasoning":"',
    ],
)
def test_never_guesses_incomplete_action_payload(raw):
    assert recover_action_json(raw) is None


def test_complete_blocks_survive_truncated_outer_narration():
    action = {"action_type": "declare_blockers", "blocker_assignments": {"Spider": "Odin"}}
    raw = '{"actions":[' + json.dumps(action) + '],"voice_advice":"Block with'
    assert recover_action_json(raw) == {"actions": [action]}
