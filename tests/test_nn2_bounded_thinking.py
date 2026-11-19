"""NN2 regression tests: clock-safe bounded live thinking and deprecation/fallback rules."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_a3_outcome import REASON
from tests.test_a3_outcome import fixture as a3_fixture

from arenamcp.action_planner import ActionPlanner
from arenamcp.backends.proxy import (
    ProxyBackend,
    reset_thinking_budget_state,
    thinking_budget_enabled,
)
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.model_call_settings import DEEPSEEK_ROW, GLM_ROW, decision_thinking_budget
from arenamcp.selective_thinking import DEFAULT_PIVOTAL_THINKING_BUDGET, PivotalCallBudget


@pytest.fixture(autouse=True)
def _clean_thinking_env_and_state(monkeypatch):
    for var in (
        "ARENAMCP_THINKING_ROWS",
        "ARENAMCP_THINKING_BUDGET",
        "ARENAMCP_DECISION_THINKING_BUDGET",
        "ARENAMCP_PIVOTAL_THINKING",
        "ARENAMCP_PIVOTAL_THINKING_BUDGET",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_thinking_budget_state()
    yield
    reset_thinking_budget_state()


def _capture_proxy(monkeypatch, responses: list[str] | None = None):
    calls: list[dict] = []
    answers = list(responses or ["Pass."])

    def fake_complete_once(self, client, params, **kwargs):
        calls.append(json.loads(json.dumps(params)))
        if len(answers) > 1:
            return answers.pop(0)
        return answers[0]

    monkeypatch.setattr(ProxyBackend, "_complete_once", fake_complete_once, raising=True)
    return calls


def test_glm_row_cap_low_bounds_decisions_and_keeps_routine_and_deepseek_off(monkeypatch):
    assert GLM_ROW["decision"] == "cap_low"
    assert GLM_ROW["plan"] == "cap_low"
    assert DEEPSEEK_ROW["decision"] == "none"

    calls = _capture_proxy(monkeypatch)
    current = ProxyBackend(model="current", base_url="http://127.0.0.1:9/v1", api_key="test")

    current.complete("Sys", "Pivotal", call_class="decision.typed", reasoning_effort="high")
    assert calls[-1]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }
    assert calls[-1]["extra_body"]["thinking_token_budget"] == DEFAULT_PIVOTAL_THINKING_BUDGET

    current.complete("Sys", "Flat low", call_class="decision.typed", reasoning_effort="low")
    assert calls[-1]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }
    assert "thinking_token_budget" not in calls[-1]["extra_body"]

    for routine in (None, "none"):
        current.complete("Sys", "Routine", call_class="decision.typed", reasoning_effort=routine)
        assert calls[-1]["extra_body"]["chat_template_kwargs"] == {
            "thinking": False,
            "reasoning_effort": "none",
        }
        assert "thinking_token_budget" not in calls[-1]["extra_body"]

    current.complete("Plan", "Board", call_class="background.game_plan", reasoning_effort="high")
    assert calls[-1]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }
    assert "thinking_token_budget" not in calls[-1]["extra_body"]

    deepseek = ProxyBackend(
        model="deepseek-v4.1-flash-local",
        base_url="http://127.0.0.1:9/v1",
        api_key="test",
    )
    for req in ("high", "low"):
        deepseek.complete("Sys", "DS", call_class="decision.typed", reasoning_effort=req)
        assert calls[-1]["extra_body"]["chat_template_kwargs"] == {
            "thinking": False,
            "reasoning_effort": "none",
        }
        assert "thinking_token_budget" not in calls[-1]["extra_body"]


def test_thinking_rows_kill_switch_forces_decision_none_and_omits_budget(monkeypatch):
    monkeypatch.setenv("ARENAMCP_THINKING_ROWS", '{"glm":{"decision":"none"}}')
    calls = _capture_proxy(monkeypatch)
    backend = ProxyBackend(model="current", base_url="http://127.0.0.1:9/v1", api_key="test")

    backend.complete(
        "Sys",
        "Pivotal",
        call_class="decision.typed",
        reasoning_effort="high",
        thinking_token_budget=256,
    )
    extra = calls[0]["extra_body"]
    assert extra["chat_template_kwargs"] == {"thinking": False, "reasoning_effort": "none"}
    assert "thinking_token_budget" not in extra

    assert (
        decision_thinking_budget(
            SimpleNamespace(model="current", last_served_model=None, last_served_alias=None),
            "decision.typed",
            requested_effort="high",
            effective_effort="none",
            explicit_budget=256,
        )
        is None
    )


def test_separate_strategy_planning_deprecation_and_env_precedence(caplog, monkeypatch):
    backend = Mock()
    backend.complete.return_value = '{"option_ids": ["pass"], "reasoning": "ok"}'

    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        legacy = ActionPlanner(backend, separate_strategy_planning=True, pivotal_thinking=True)
    assert any("separate_strategy_planning=True is deprecated" in r.getMessage() for r in caplog.records)
    assert legacy._separate_strategy_active() is True

    legacy._call_llm("sys", "user", 256, timeout_s=5.0, call_class="decision.typed", reasoning_effort=None)
    assert backend.complete.call_args.kwargs["reasoning_effort"] == "low"

    caplog.clear()
    backend.complete.reset_mock()
    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        overridden = ActionPlanner(backend, separate_strategy_planning=True, pivotal_thinking=True)
    assert not any("separate_strategy_planning=True is deprecated" in r.getMessage() for r in caplog.records)
    assert overridden._separate_strategy_active() is False

    overridden._call_llm(
        "sys", "user", 256, timeout_s=5.0, call_class="decision.typed", reasoning_effort=None
    )
    assert backend.complete.call_args.kwargs.get("reasoning_effort") is None


def test_e4_routine_opponent_turn_and_own_stack_stay_off_while_pivotal_caps_low_256(monkeypatch):
    def run_case(state, menu, responses):
        calls = _capture_proxy(monkeypatch, responses=responses)
        backend = ProxyBackend(model="current", base_url="http://127.0.0.1:9/v1", api_key="test")
        planner = ActionPlanner(
            backend,
            separate_strategy_planning=False,
            pivotal_thinking=True,
            land_drop_first=False,
        )
        monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "unknown"}))
        monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30)
        chosen = planner.plan_decision_options(menu, state)
        return chosen, calls, planner._last_decision_trace

    pass_json = json.dumps({"option_ids": ["pass"], "reasoning": "Routine pass."})

    # 1) Routine own-turn window: no pivotal evidence -> thinking off
    routine_card = {
        "instance_id": 11,
        "name": "Bear Cub",
        "type_line": "Creature — Bear",
        "card_types": ["CardType_Creature"],
        "power": 2,
        "toughness": 2,
        "mana_cost": "{1}{G}",
        "oracle_text": "",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    routine_state = {
        "local_seat_id": 1,
        "hand": [routine_card],
        "battlefield": [],
        "turn": {"active_player": 1, "turn_number": 2, "phase": "Phase_Main1"},
    }
    routine_menu = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "cast",
                "Cast Bear Cub",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 11},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    _, routine_calls, routine_trace = run_case(routine_state, routine_menu, [pass_json])
    assert routine_trace["pivotal_thinking"]["category"] == "routine"
    assert len(routine_calls) == 1
    assert routine_calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }
    assert "thinking_token_budget" not in routine_calls[0]["extra_body"]

    # 2) Opponent-turn critical window: opponent_turn clears E4 reasons -> thinking off
    opp_state, opp_menu = a3_fixture()
    opp_state["turn"]["active_player"] = 2
    opp_menu = PendingDecision(
        request_id=(2, 1),
        request_type="SelectTargets",
        source_label="Lightning Bolt",
        options=(
            DecisionOption("t1", "Target Griselbrand", payable=True),
            DecisionOption("cancel", "Cancel", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    cancel_json = json.dumps({"option_ids": ["cancel"], "reasoning": "Hold."})
    _, opp_calls, opp_trace = run_case(opp_state, opp_menu, [cancel_json])
    assert opp_trace["pivotal_thinking"]["category"] == "routine"
    assert opp_trace["pivotal_thinking"]["reasons"] == []
    assert opp_calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }
    assert "thinking_token_budget" not in opp_calls[0]["extra_body"]

    # 3) Own-stack-only window (no GamePlan hold so LLM runs): own_stack clears E4 reasons -> thinking off
    stack_state, stack_menu = a3_fixture()
    stack_state["stack"] = [
        {
            "instance_id": 99,
            "name": "Own Creature Spell",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "card_types": ["CardType_Creature"],
        }
    ]
    _, stack_calls, stack_trace = run_case(stack_state, stack_menu, [pass_json])
    assert stack_trace["pivotal_thinking"]["category"] == "routine"
    assert stack_trace["pivotal_thinking"]["reasons"] == []
    assert stack_calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }
    assert "thinking_token_budget" not in stack_calls[0]["extra_body"]

    # 4) Pivotal own-turn window: bounded low effort + thinking_token_budget=256 on ProxyBackend
    pivotal_state, pivotal_menu = a3_fixture()
    pivotal_chosen, pivotal_calls, pivotal_trace = run_case(
        pivotal_state,
        pivotal_menu,
        [
            json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
            json.dumps({"option_ids": ["pass"], "reasoning": "Hold; blocks flyer."}),
        ],
    )
    assert pivotal_chosen == ["pass"]
    assert pivotal_trace["pivotal_thinking"]["category"] == "pivotal"
    assert len(pivotal_calls) == 2
    assert pivotal_calls[0]["extra_body"]["chat_template_kwargs"] == {
        "thinking": False,
        "reasoning_effort": "none",
    }
    assert "thinking_token_budget" not in pivotal_calls[0]["extra_body"]
    assert pivotal_calls[1]["extra_body"]["chat_template_kwargs"] == {
        "thinking": True,
        "reasoning_effort": "low",
    }
    assert pivotal_calls[1]["extra_body"]["thinking_token_budget"] == 256
    assert pivotal_trace["pivotal_thinking"]["call_usage"][1]["thinking_token_budget"] == 256


def test_http_400_rejection_disables_thinking_budget_for_session_and_retries(caplog, monkeypatch):
    seen_extras: list[dict] = []

    class _RejectedBudgetError(Exception):
        status_code = 400
        body = '{"error":"Extra inputs are not permitted: thinking_token_budget"}'

    def fake_complete_once(self, client, params, **kwargs):
        extra = dict(params.get("extra_body") or {})
        seen_extras.append(extra)
        if "thinking_token_budget" in extra:
            raise _RejectedBudgetError("BadRequest: unknown field thinking_token_budget")
        return "Play Land."

    monkeypatch.setattr(ProxyBackend, "_complete_once", fake_complete_once, raising=True)
    backend = ProxyBackend(model="current", base_url="http://127.0.0.1:9/v1", api_key="test")

    with caplog.at_level(logging.WARNING, logger="arenamcp.backends.proxy"):
        first = backend.complete("Sys", "First", call_class="decision.typed", reasoning_effort="high")
    assert first == "Play Land."
    assert len(seen_extras) == 2
    assert seen_extras[0]["thinking_token_budget"] == 256
    assert "thinking_token_budget" not in seen_extras[1]
    assert thinking_budget_enabled() is False
    assert any("token budget disabled for this session" in r.getMessage() for r in caplog.records)

    second = backend.complete("Sys", "Second", call_class="decision.typed", reasoning_effort="high")
    assert second == "Play Land."
    assert len(seen_extras) == 3
    assert "thinking_token_budget" not in seen_extras[2]


def test_pivotal_call_budget_token_cap_applies_only_to_high_effort():
    budget = PivotalCallBudget(deadline=100.0, normal_seconds=2.0, pivotal_seconds=5.0, reasons=("lethal",))
    assert budget.thinking_token_budget == DEFAULT_PIVOTAL_THINKING_BUDGET
    assert budget.token_budget_for("high") == 256
    assert budget.token_budget_for("low") is None
    assert budget.token_budget_for(None) is None


def test_env_precedence_reaches_reask_autopilot_mgr_and_plan_actions_declaration(monkeypatch):
    from unittest.mock import MagicMock, patch

    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    calls = _capture_proxy(
        monkeypatch,
        responses=[
            '{"reasoning": "Attack with Dragon for lethal.", "actions": [{"action_type": "attack", "targets": ["Dragon"]}]}'
        ],
    )
    backend = ProxyBackend(model="current", base_url="http://127.0.0.1:9/v1", api_key="test")
    planner = ActionPlanner(
        backend,
        timeout=15.0,
        land_drop_first=False,
        separate_strategy_planning=True,
        pivotal_thinking=True,
    )
    assert planner._separate_strategy_active() is False
    # Outcome reask must NOT raise due to deprecated separate_strategy_planning=True.
    planner._require_outcome_reask(yielded=True, deadline=9e9)

    with patch("arenamcp.autopilot.get_bridge", return_value=MagicMock()):
        engine = AutopilotEngine(planner=planner, config=AutopilotConfig(dry_run=False))
    assert engine._game_plan_mgr is not None
    assert engine._game_plan_mgr.separate_strategy_planning is False

    # Pivotal DeclareAttackers in plan_actions forwards bounded low + thinking_token_budget=256.
    state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 5},
        ],
        "turn": {"turn_number": 5, "active_player": 1, "phase": "Phase_Combat", "step": "Step_DeclareAttack"},
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Dragon",
                "type_line": "Creature — Dragon",
                "card_types": ["CardType_Creature"],
                "power": 5,
                "toughness": 5,
                "keywords": ["Flying"],
                "oracle_text": "Flying",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "turn_entered_battlefield": 4,
            }
        ],
        "hand": [],
        "_bridge_request_type": "DeclareAttackers",
        "_bridge_legal_attackers": [{"instance_id": 10, "name": "Dragon"}],
    }
    result = planner.plan_actions(
        state,
        "declare_attackers",
        ["attack with: Dragon (5/5)", "pass attackers"],
        {"type": "declare_attackers", "legal_attackers": [{"instance_id": 10, "name": "Dragon"}]},
    )
    assert result.actions
    assert calls, "expected at least one declaration call"
    assert any(
        c["extra_body"]["chat_template_kwargs"] == {"thinking": True, "reasoning_effort": "low"}
        and c["extra_body"].get("thinking_token_budget") == 256
        for c in calls
    )


def test_http_400_thinking_budget_rejection_scoped_per_endpoint(monkeypatch):
    class _RejectedBudgetError(Exception):
        status_code = 400
        body = '{"error":"Extra inputs are not permitted: thinking_token_budget"}'

    seen: list[tuple[str, dict]] = []

    def fake_complete_once(self, client, params, **kwargs):
        extra = dict(params.get("extra_body") or {})
        seen.append((self._base_url, extra))
        if self._base_url == "http://127.0.0.1:9/v1" and "thinking_token_budget" in extra:
            raise _RejectedBudgetError("BadRequest: unknown field thinking_token_budget")
        return "Pass."

    monkeypatch.setattr(ProxyBackend, "_complete_once", fake_complete_once, raising=True)
    bad_gateway = ProxyBackend(model="current", base_url="http://127.0.0.1:9/v1", api_key="test")
    good_gateway = ProxyBackend(model="current", base_url="http://127.0.0.1:10/v1", api_key="test")

    bad_gateway.complete("Sys", "First", call_class="decision.typed", reasoning_effort="high")
    assert thinking_budget_enabled("http://127.0.0.1:9/v1", "current") is False
    assert thinking_budget_enabled("http://127.0.0.1:10/v1", "current") is True

    good_gateway.complete("Sys", "Other", call_class="decision.typed", reasoning_effort="high")
    assert seen[-1][0] == "http://127.0.0.1:10/v1"
    assert seen[-1][1]["thinking_token_budget"] == 256
