"""Regression coverage for the Notary Hobbits commander prompt from 2026-09-24."""

from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.decisions import build_pending_decision
from arenamcp.gamestate import GameObject, GameObjectKind, GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response
from arenamcp.rules_engine import RulesEngine


@pytest.fixture
def commander_prompt():
    return {
        "type": "GREMessageType_OptionalActionMessage",
        "systemSeatIds": [2],
        "msgId": 314,
        "gameStateId": 224,
        "prompt": {
            "promptId": 144,
            "parameters": [
                {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 0},
                {"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 695},
            ],
        },
        "optionalActionMessage": {
            "sourceId": 14454,
            "optionalActionTypes": ["CardMechanicType_ZoneTransfer"],
            "recipientIds": [695],
        },
        "allowCancel": "AllowCancel_No",
    }


@pytest.fixture
def state():
    state = GameState()
    state.local_seat_id = 2
    state._card_name_cache[103511] = "The Notary Hobbits"
    state.game_objects[695] = GameObject(
        instance_id=695,
        grp_id=103511,
        zone_id=37,
        owner_seat_id=2,
        object_kind=GameObjectKind.CARD,
    )
    return state


def test_notary_hobbits_return_is_accepted_without_llm(state, commander_prompt):
    _handle_decision_message(state, commander_prompt["type"], commander_prompt)
    context = state.decision_context
    assert context["commander_return"] is True
    assert context["recipient_ids"] == [695]
    assert context["prompt"] == "Return The Notary Hobbits to the command zone?"
    snapshot = {"decision_context": context, "pending_decision": "Optional Action"}
    enrich_snapshot_from_pending_response(
        snapshot,
        {
            "has_pending": True,
            "request_type": "OptionalAction",
            "request_class": "OptionalActionMessageRequest",
        },
        bridge_connected=True,
    )
    backend = Mock()
    plan = ActionPlanner(backend=backend).plan_actions(
        snapshot, "decision_required", RulesEngine.get_legal_actions(snapshot)
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.CLICK_BUTTON
    assert plan.actions[0].card_name == "accept"
    assert plan.spoken_actions() == "Return The Notary Hobbits to the command zone."
    assert plan.voice_advice == plan.spoken_actions()
    assert plan.fallback_reason == "planner_commander_return"
    assert not backend.mock_calls
    executor = _BridgeSubmitMixin()
    executor._gre_bridge = Mock()
    executor._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_type": "OptionalAction",
        "request_class": "OptionalActionMessageRequest",
    }
    executor._gre_bridge_failed_methods = set()
    executor._log_execution_path = Mock()
    assert executor._try_gre_bridge(plan.actions[0], snapshot) is not None
    executor._gre_bridge.submit_optional.assert_called_once_with(True)
    executor._gre_bridge.submit_pass.assert_not_called()


def _commander_poll(commander_prompt):
    return {
        "has_pending": True,
        "request_type": "OptionalAction",
        "request_class": "OptionalActionMessageRequest",
        "game_state_id": commander_prompt["gameStateId"],
        "msg_id": commander_prompt["msgId"],
        "optional_mechanics": ["ZoneTransfer"],
        "optional_recipients": [695],
        # The native bridge overlays a numeric prompt onto the log's text.
        "decision_context": {"prompt": commander_prompt["prompt"], "sourceId": 14454},
    }


@pytest.mark.parametrize("changed_request", [False, True])
def test_typed_commander_return_bypasses_portal_decline_and_revalidates_request(
    state, commander_prompt, changed_request
):
    """Replay the decision that let Portal steal the Hobbits at 09:35:52."""
    _handle_decision_message(state, commander_prompt["type"], commander_prompt)
    snapshot = {"decision_context": state.decision_context, "pending_decision": "Optional Action"}
    backend = Mock()
    backend.complete.return_value = (
        '{"option_ids": ["optional:decline"], "reasoning": "Declining stops Portal from reanimating it."}'
    )
    planner = ActionPlanner(backend=backend)
    engine = AutopilotEngine(
        planner=planner, mapper=Mock(), controller=Mock(), config=AutopilotConfig(dry_run=False)
    )
    engine._gre_bridge = Mock(connected=True)
    poll = _commander_poll(commander_prompt)
    fresh = {**poll, "msg_id": poll["msg_id"] + 1} if changed_request else poll
    engine._gre_bridge.get_pending_actions.side_effect = [poll, fresh]
    engine._notify = Mock()

    assert engine._try_typed_decision_path(snapshot, "decision_required") is True
    assert not backend.mock_calls
    if changed_request:
        engine._gre_bridge.submit_optional.assert_not_called()
    else:
        engine._gre_bridge.submit_optional.assert_called_once_with(True, expected_request_id=(224, 314))
        assert "returning The Notary Hobbits to the command zone" in engine._notify.call_args.args[1]


@pytest.mark.parametrize("case", ["unrecognized", "stale_request", "unknown_request", "other_recipient"])
def test_typed_commander_policy_does_not_accept_unrelated_optional(state, commander_prompt, case):
    _handle_decision_message(state, commander_prompt["type"], commander_prompt)
    context = state.decision_context
    poll = _commander_poll(commander_prompt)
    if case == "unrecognized":
        context["commander_return"] = False
    elif case == "stale_request":
        poll["msg_id"] += 1
    elif case == "unknown_request":
        poll["game_state_id"] = poll["msg_id"] = 0
    else:
        poll["optional_recipients"] = [999]
    backend = Mock()
    backend.complete.return_value = (
        '{"option_ids": ["optional:decline"], "reasoning": "Do not use this effect."}'
    )
    planner = ActionPlanner(backend=backend)
    decision = build_pending_decision(poll)

    assert planner.plan_decision_options(decision, {"decision_context": context}) == ["optional:decline"]
    backend.complete.assert_called_once()


@pytest.mark.parametrize(
    "case", ["other_prompt", "other_mechanic", "opponent", "token", "missing_card", "no_recipient"]
)
def test_other_optional_actions_are_not_commander_returns(state, commander_prompt, case):
    if case == "other_prompt":
        commander_prompt["prompt"]["promptId"] = 999
    elif case == "other_mechanic":
        commander_prompt["optionalActionMessage"]["optionalActionTypes"] = []
    elif case == "opponent":
        state.game_objects[695].owner_seat_id = 1
    elif case == "token":
        state.game_objects[695].object_kind = GameObjectKind.TOKEN
    elif case == "missing_card":
        state.game_objects.clear()
    else:
        commander_prompt["optionalActionMessage"]["recipientIds"] = []
    _handle_decision_message(state, commander_prompt["type"], commander_prompt)
    assert state.decision_context["commander_return"] is False


def test_optional_prompt_preserves_outer_text(state):
    message = {"prompt": {"text": "Pay 2 life?"}, "optionalActionMessage": {}}
    _handle_decision_message(state, "GREMessageType_OptionalActionMessage", message)
    assert state.decision_context["prompt"] == "Pay 2 life?"
    assert state.decision_context["commander_return"] is False


@pytest.mark.parametrize("button", ["accept", "decline"])
def test_spoken_button_action_names_the_actual_choice(button):
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name=button)])
    assert plan.spoken_actions() == f"Click {button}."
