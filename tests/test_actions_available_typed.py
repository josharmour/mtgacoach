from unittest.mock import MagicMock

from arenamcp.action_planner import ActionPlanner
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.decisions import build_pending_decision


class DummyBackend:
    def complete(self, system, user, *args, **kwargs):
        # Fake a JSON response from LLM picking the first idx option
        return '{"option_ids": ["idx:0"], "reasoning": "cast a spell"}'


def test_actions_available_decision_builder():
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 123, "autoTapSolution": {}},
            {"actionType": "ActionType_Pass"},
        ],
    }

    def resolve_name(grp_id: int) -> str:
        return "Lightning Bolt"

    decision = build_pending_decision(poll, resolve_name=resolve_name)
    assert decision is not None
    assert decision.request_type == "ActionsAvailable"
    assert len(decision.options) == 2
    assert decision.options[0].option_id == "idx:0"
    assert decision.options[1].option_id == "pass"


def test_actions_available_planner():
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 123, "autoTapSolution": {}},
            {"actionType": "ActionType_Pass"},
        ],
    }

    def resolve_name(grp_id: int) -> str:
        return "Lightning Bolt"

    decision = build_pending_decision(poll, resolve_name=resolve_name)
    planner = ActionPlanner(backend=DummyBackend())

    # Check that LLM can choose the typed decision options
    chosen = planner.plan_decision_options(decision, {})
    assert chosen == ["idx:0"]


def test_try_typed_decision_path():
    # Test that _try_typed_decision_path handles ActionsAvailable
    planner = ActionPlanner(backend=DummyBackend())

    mock_bridge = MagicMock()
    mock_bridge.connected = True
    mock_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 123, "autoTapSolution": {}},
            {"actionType": "ActionType_Pass"},
        ],
    }

    engine = AutopilotEngine(
        planner=planner,
        mapper=MagicMock(),
        controller=MagicMock(),
        get_game_state=lambda: {"turn": {"turn_number": 1, "phase": "Main1"}},
        config=AutopilotConfig(dry_run=False),
    )
    # Inject our mock bridge
    engine._gre_bridge = mock_bridge
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False

    result = engine._try_typed_decision_path({}, "decision_required")
    # Autopilot handles it, submits option, and returns True
    assert result is True
    # The bridge should have been called to submit the option (idx:0)
    mock_bridge.submit_action_by_index.assert_called_once_with(0)
