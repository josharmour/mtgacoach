"""E4's one high promotion must carry already-known A3 factual concerns."""

import json
from unittest.mock import Mock

import pytest
from tests.test_a3_outcome import REASON, fixture

from arenamcp.action_planner import ActionPlanner


@pytest.mark.parametrize("revised", ["pass", "cast"])
def test_promotion_includes_actual_concerns_without_extra_high(monkeypatch, revised):
    state, menu = fixture()
    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
        json.dumps({"option_ids": [revised], "reasoning": REASON if revised == "cast" else "Hold"}),
        json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(
        planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "effect not modelled"})
    )
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30.0)
    assert planner.plan_decision_options(menu, state) != ["cast"]
    promotion = backend.complete.call_args_list[1]
    message = promotion.args[1]
    assert "PIVOTAL UNEVALUATED CHOICE" in message
    assert "SECOND LOOK" in message
    concerns = planner._unevaluated_claim_concerns(
        menu, state, ["cast"], REASON, {"cast": "effect not modelled"}
    )
    assert concerns
    assert all(text in message for _, text in concerns)
    assert promotion.kwargs["reasoning_effort"] == "high"
    assert sum(call.kwargs.get("reasoning_effort") == "high" for call in backend.complete.call_args_list) == 1


@pytest.mark.parametrize("failure", ["evaluator", "prompt_join"])
def test_preparation_failure_excludes_preferred_action(monkeypatch, failure):
    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    state, menu = fixture()
    backend = Mock()
    backend.complete.return_value = json.dumps({"option_ids": ["cast"], "reasoning": REASON})
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "unknown"}))
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30)

    def broken(*args, **kwargs):
        if failure == "evaluator":
            raise RuntimeError("synthetic concern preparation failure")
        return [("synthetic", object())]

    monkeypatch.setattr(planner, "_second_look_concerns", broken)
    monkeypatch.setattr(
        planner, "_board_math_fallback", lambda d, s: ["cast"] if d.find("cast") else ["pass"]
    )
    assert planner.plan_decision_options(menu, state) != ["cast"]
    assert "cast" in planner.get_last_decision_trace()["outcome_rejected_ids"]
    assert backend.complete.call_count == 1
