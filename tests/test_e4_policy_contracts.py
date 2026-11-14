"""Explicit OFF/ON contracts, not proof of low-first optimality or live savings."""

import json
from unittest.mock import Mock

import pytest
from tests.test_a3_outcome import REASON, fixture, known_fury

from arenamcp.action_planner import ActionPlanner


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
@pytest.mark.parametrize(
    "claim",
    [
        "Fury survives to attack next turn",
        "Fury kills Griselbrand with its four damage",
        "Evoking Fury leaves a double-striking 3/3 body on our battlefield",
    ],
)
def test_valid_negation_preserves_cast_with_policy_scoped_calls(monkeypatch, enabled, claim):
    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1" if enabled else "0")
    known_fury(monkeypatch)
    state, menu = fixture()
    menu.options[0].meta["alternativeCostText"] = "Evoke—Exile a red card from your hand."
    backend = Mock()
    backend.complete.return_value = json.dumps(
        {"option_ids": ["cast"], "reasoning": "It is not true that " + claim + "."}
    )
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "unknown"}))
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30)
    assert planner.plan_decision_options(menu, state) == ["cast"]
    calls = backend.complete.call_args_list
    assert len(calls) == (2 if enabled else 1)
    assert calls[0].kwargs.get("reasoning_effort") is None
    assert sum(c.kwargs.get("reasoning_effort") == "high" for c in calls) == int(enabled)
    assert all("SECOND LOOK" not in c.args[1] for c in calls)
    if enabled:
        assert "PIVOTAL UNEVALUATED CHOICE" in calls[1].args[1]
    assert not planner.last_llm_failure


@pytest.mark.parametrize("enabled", [False, True], ids=["off", "on"])
def test_false_fury_claim_corrected_with_one_high_under_both_policies(monkeypatch, enabled):
    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1" if enabled else "0")
    state, menu = fixture()
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
        json.dumps({"option_ids": ["pass"], "reasoning": "Hold; this cannot block the flyer."}),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "unknown"}))
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30)
    assert planner.plan_decision_options(menu, state) == ["pass"]
    calls = backend.complete.call_args_list
    assert len(calls) == 2
    assert sum(c.kwargs.get("reasoning_effort") == "high" for c in calls) == 1
    assert "SECOND LOOK" in calls[1].args[1]
    assert ("PIVOTAL UNEVALUATED CHOICE" in calls[1].args[1]) is enabled
