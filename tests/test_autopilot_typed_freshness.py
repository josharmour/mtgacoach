from unittest.mock import MagicMock

import pytest

from arenamcp.action_planner import DECLINE_DECISION
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


def _poll(instance_id=10, state_id=1):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "can_pass": True,
        "can_cancel": True,
        "actions": [{"actionType": "Cast", "instanceId": instance_id, "hasAutoTap": True}],
    }


def _engine():
    planner = MagicMock()
    planner.plan_decision_options.return_value = ["idx:0"]
    engine = AutopilotEngine(planner=planner, config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = MagicMock()
    engine._gre_bridge.connected = True
    return engine


@pytest.mark.parametrize("choice", [["idx:0"], ["pass"], [DECLINE_DECISION]])
@pytest.mark.parametrize("fresh", [_poll(instance_id=11), _poll(state_id=2), {"has_pending": False}])
def test_changed_request_during_planning_is_not_answered(choice, fresh):
    engine = _engine()
    engine._planner.plan_decision_options.return_value = choice
    engine._gre_bridge.get_pending_actions.side_effect = [_poll(), fresh]
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_not_called()
    engine._gre_bridge.submit_pass.assert_not_called()
    engine._gre_bridge.cancel_action.assert_not_called()
    assert engine._actions_executed == 0


def test_unchanged_request_is_submitted():
    engine = _engine()
    engine._gre_bridge.get_pending_actions.return_value = _poll()
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_called_once()
    assert engine._actions_executed == 1


def test_stop_during_planning_prevents_submission():
    engine = _engine()
    engine._gre_bridge.get_pending_actions.return_value = _poll()

    def stop_and_choose(*args):
        engine._abort_event.set()
        return ["idx:0"]

    engine._planner.plan_decision_options.side_effect = stop_and_choose
    engine._try_typed_decision_path({}, "decision_required")
    engine._gre_bridge.submit_action_by_index.assert_not_called()


def test_failed_freshness_poll_does_not_submit_or_fall_back():
    engine = _engine()
    engine._gre_bridge.get_pending_actions.side_effect = [_poll(), OSError("disconnected")]
    assert engine._try_typed_decision_path({}, "decision_required") is True
    engine._gre_bridge.submit_action_by_index.assert_not_called()
