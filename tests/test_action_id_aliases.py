"""Numeric ActionsAvailable IDs from the 2026-10-29 report retain their meaning."""

import json

import pytest
from tests.fakes import TypedBridge, make_engine, planner_with, target_state

from arenamcp.decisions import DecisionOption, PendingDecision


@pytest.mark.parametrize("answer", [0, "0"])
def test_numeric_model_action_reaches_executor_with_original_reasoning(answer):
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 123, "autoTapSolution": {}},
            {"actionType": "ActionType_Pass"},
        ],
        "can_pass": True,
    }
    bridge = TypedBridge(poll)
    bridge.submit_action_by_index = lambda index, expected=None, **kwargs: (
        bridge.submitted.append(("action", index, expected)) or True
    )
    planner = planner_with(json.dumps({"option_ids": [answer], "reasoning": "Develop the board."}))
    engine = make_engine(bridge, planner)
    assert engine._try_typed_decision_path(target_state(), "decision_required")
    assert bridge.submitted == [("action", 0, {"grpId": 123})]
    assert planner.get_decision_reasoning(["idx:0"]) == "Develop the board."


@pytest.mark.parametrize(
    ("request_type", "options", "answer", "expected"),
    [
        ("ActionsAvailable", ("idx:3", "idx:4", "pass"), 4, "idx:4"),
        ("ActionsAvailable", ("idx:3", "idx:4", "pass"), "3", "idx:3"),
        ("ActionsAvailable", ("idx:3", "idx:4", "pass"), "99", "99"),
        ("ActionsAvailable", ("idx:3", "idx:4", "pass"), "04", "04"),
        ("ActionsAvailable", ("idx:3", "idx:4", "pass"), "idx:3", "idx:3"),
        ("ActionsAvailable", ("idx:3", "idx:4", "pass"), "pass", "pass"),
        ("ActionsAvailable", ("3", "idx:3", "pass"), "3", "3"),
        ("SelectTargets", ("tgt:3", "tgt:4"), "4", "4"),
    ],
)
def test_normalization_only_repairs_exact_current_action_index(request_type, options, answer, expected):
    decision = PendingDecision(
        (1, 2), request_type, tuple(DecisionOption(oid, "Choice", payable=True) for oid in options)
    )
    planner = planner_with(json.dumps({"option_ids": [answer]}))
    assert planner._llm_decision_options(decision, target_state()) == [expected]
