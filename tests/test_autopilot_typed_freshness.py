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


def test_typed_decision_receives_plan_and_schedules_background_refresh():
    engine = _engine()
    engine._gre_bridge.get_pending_actions.return_value = _poll()
    manager = MagicMock()
    manager.plan_text.return_value = "GAME PLAN: develop commander engine"
    engine._game_plan_mgr = manager
    engine._planner._deck_strategy_fn = lambda: "Commander mana engine"
    state = {"turn": {"turn_number": 4}, "match_id": "match"}

    def choose_with_plan(*args):
        engine._planner.set_game_plan.assert_called_with("GAME PLAN: develop commander engine")
        manager.request_reform.assert_called_once()
        return ["idx:0"]

    engine._planner.plan_decision_options.side_effect = choose_with_plan
    assert engine._try_typed_decision_path(state, "decision_required") is True
    manager.observe.assert_called_once()
    observed = manager.observe.call_args.args[0]
    assert observed["turn"] == state["turn"]
    assert observed["match_id"] == state["match_id"]
    assert observed["_bridge_request_type"] == "ActionsAvailable"
    assert observed["decision_context"]["type"] == "actions_available"
    assert engine._planner.plan_decision_options.call_args.args[1] is observed
    assert "_bridge_request_type" not in state
    manager.seed.assert_called_once_with("Commander mana engine")
    manager.maybe_reform.assert_not_called()
