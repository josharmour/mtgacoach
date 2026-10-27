from dataclasses import replace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision


def _answered():
    planner = ActionPlanner(Mock())
    decision = PendingDecision((100, 4), "ActionsAvailable", (DecisionOption("pass", "Pass"),))
    state = {"match_id": "match-a", "turn": {"turn_number": 12}, "players": [], "battlefield": []}

    def answer(*args):
        planner._last_decision_option_ids = ["pass"]
        planner._last_decision_reasoning = "Hold W for Rescue."
        planner._last_decision_trace = {"reasoning": "Hold W for Rescue."}
        return ["pass"]

    planner._plan_decision_options = answer
    assert planner.plan_decision_options(decision, state) == ["pass"]
    return planner, decision, state


def test_current_request_keeps_its_explanation():
    planner, decision, state = _answered()
    evidence = planner.get_decision_evidence(decision, state, ["pass"])
    assert evidence["reasoning"] == "Hold W for Rescue."
    assert evidence["trace"]["reasoning"] == "Hold W for Rescue."


@pytest.mark.parametrize("change", ["turn", "match", "request", "type", "choice"])
def test_later_submission_cannot_inherit_old_trace(change):
    planner, decision, state = _answered()
    chosen = ["pass"]
    if change == "turn":
        state["turn"]["turn_number"] = 14
    elif change == "match":
        state["match_id"] = "match-b"
    elif change == "request":
        decision = replace(decision, request_id=(101, 5))
    elif change == "type":
        decision = replace(decision, request_type="DeclareBlockers")
    else:
        chosen = ["block:456:789"]
    assert planner.get_decision_evidence(decision, state, chosen) == {}


def test_unbound_legacy_trace_is_not_evidence_for_current_submission():
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {"reasoning": "Rabbit would die to Endurance"}
    decision = PendingDecision((102, 5), "DeclareBlockers", ())
    assert planner.get_decision_evidence(decision, {}, []) == {}
