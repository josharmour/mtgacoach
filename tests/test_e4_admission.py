"""Offline E4 architecture contracts, not live performance acceptance."""

import pytest

from arenamcp.selective_thinking import PivotalCallBudget, pivotal_reasons


@pytest.mark.parametrize(
    "signal,value",
    [
        ("lethal", True),
        ("dead_soon", True),
        ("worthwhile_response_targets", 2),
        ("unresolved_mulligan", True),
        ("commander_or_engine_cast", True),
        ("close_combat", True),
        ("preferred_option_unevaluated", True),
    ],
)
def test_each_original_signal_is_pivotal(signal, value):
    assert pivotal_reasons(**{signal: value})


def test_missing_or_one_worthwhile_target_is_not_evidence():
    assert not pivotal_reasons()
    assert not pivotal_reasons(worthwhile_response_targets=1)


def budget(**kw):
    return PivotalCallBudget(deadline=100, normal_seconds=4, pivotal_seconds=15, **kw)


def test_one_high_then_correction_preserves_shared_deadline():
    b = budget(reasons=("lethal",))
    assert b.admit(now=90) == ("high", 10)
    assert b.admit(now=98, correction=True) == ("low", 2)
    assert b.high_calls == 1
    with pytest.raises(TimeoutError):
        b.admit(now=100, correction=True)


def test_routine_then_new_contradiction_can_promote_once():
    b = budget()
    assert b.admit(now=80) == ("low", 4)
    assert b.admit(now=84, correction=True) == ("high", 15)
    assert b.admit(now=99, correction=True) == ("low", 1)


@pytest.mark.parametrize("load", ["degraded", "plan_in_flight"])
def test_busy_never_adds_high_or_second_request(load):
    b = budget(reasons=("lethal",))
    assert b.admit(now=80, **{load: True}) == ("low", 4)
    with pytest.raises(TimeoutError):
        b.admit(now=81, correction=True, **{load: True})
    assert b.high_calls == 0


def test_load_rechecked_after_initial_call():
    b = budget(reasons=("lethal",))
    b.admit(now=80)
    with pytest.raises(TimeoutError):
        b.admit(now=81, degraded=True, correction=True)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "100"])
def test_nonfinite_or_non_numeric_budget_rejected(value):
    with pytest.raises(ValueError):
        PivotalCallBudget(deadline=value, normal_seconds=4, pivotal_seconds=15)


def test_e4_default_off(monkeypatch):
    from arenamcp.selective_thinking import pivotal_enabled

    monkeypatch.delenv("ARENAMCP_PIVOTAL_THINKING", raising=False)
    assert not pivotal_enabled()


def test_actual_typed_mulligan_uses_one_high_call(monkeypatch):
    from tests.test_decision_layer_20261019 import _Model, _planner, _reply
    from tests.test_planner_micro_wins_226 import _our_main

    from arenamcp.decisions import DecisionOption, PendingDecision

    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    model = _Model(_reply("keep", "keep this hand"), _reply("keep", "keep this hand"))
    planner = _planner(model)
    decision = PendingDecision(
        (1, 2),
        "Mulligan",
        (DecisionOption("keep", "Keep"), DecisionOption("mulligan", "Mulligan")),
        min_select=1,
        max_select=1,
    )
    assert planner._llm_decision_options(decision, _our_main()) == ["keep"]
    assert model.efforts == ["high"]
    assert planner.get_last_decision_trace()["pivotal_thinking"]["high_calls"] == 1


def test_adapter_does_not_promote_all_unknown_options():
    from arenamcp.decisions import DecisionOption, PendingDecision
    from arenamcp.selective_thinking import pivotal_evidence

    d = PendingDecision(
        (1, 2),
        "ActionsAvailable",
        (DecisionOption("a", "A"), DecisionOption("pass", "Pass")),
        min_select=1,
        max_select=1,
    )
    assert not pivotal_evidence(d, {}, unmodelled={"a": "unmodelled"})
    assert pivotal_evidence(d, {}, unmodelled={"a": "unmodelled"}, preferred_ids=["a"])
    assert not pivotal_evidence(d, {}, unmodelled={"a": "unmodelled"}, preferred_ids=["missing"])


def test_adapter_counts_only_current_hot_legal_response_options():
    from arenamcp.decisions import DecisionOption, PendingDecision
    from arenamcp.selective_thinking import pivotal_evidence

    d = PendingDecision(
        (1, 2),
        "SelectTargets",
        (DecisionOption("tgt:11", "A"), DecisionOption("tgt:12", "B")),
        min_select=1,
        max_select=1,
        source_instance_id=20,
    )
    state = {
        "stack": [{"instance_id": 20, "oracle_text": "Destroy target creature.", "type_line": "Instant"}]
    }
    assert pivotal_evidence(d, state, target_hints={11: "Hot", 12: "Hot"})
    assert not pivotal_evidence(d, state, target_hints={11: "Hot", 12: "Cold", 13: "Hot"})


@pytest.mark.parametrize("busy", ["degraded", "plan"])
def test_actual_pivotal_path_respects_load_without_hedge(monkeypatch, busy):
    from tests.test_decision_layer_20261019 import _Model, _planner, _reply
    from tests.test_planner_micro_wins_226 import _our_main

    from arenamcp.decisions import DecisionOption, PendingDecision

    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    model = _Model(_reply("keep", "keep"), _reply("keep", "keep"))
    planner = _planner(model)
    if busy == "degraded":
        model.degraded = lambda: True
    else:
        monkeypatch.setattr(planner, "_yield_background", lambda: False)
    d = PendingDecision(
        (1, 2),
        "Mulligan",
        (DecisionOption("keep", "Keep"), DecisionOption("mulligan", "Mulligan")),
        min_select=1,
        max_select=1,
    )
    assert planner._llm_decision_options(d, _our_main()) == ["keep"]
    assert model.efforts == [None]
    assert planner.get_last_decision_trace()["pivotal_thinking"]["high_calls"] == 0


@pytest.mark.parametrize("revised", ["pass", "cast"])
def test_enabled_policy_never_accepts_a3_false_fury_outcome(monkeypatch, revised):
    import json
    from unittest.mock import Mock

    from tests.test_a3_outcome import REASON, fixture

    from arenamcp.action_planner import ActionPlanner

    state, menu = fixture()
    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
        json.dumps({"option_ids": [revised], "reasoning": REASON if revised == "cast" else "Hold"}),
        json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "effect unmodelled"}))
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30.0)
    assert planner.plan_decision_options(menu, state) != ["cast"]
    assert sum(c.kwargs.get("reasoning_effort") == "high" for c in backend.complete.call_args_list) <= 1


def test_m1_e4_uses_complete_final_decision_totals():
    from tools.upgrade_metrics import pivotal_thinking_summary

    def row(category, values, calls=None):
        return {
            "trace": {
                "pivotal_thinking": {
                    "category": category,
                    "calls": len(values) if calls is None else calls,
                    "call_usage": [{"reasoning_tokens": v} for v in values],
                }
            }
        }

    b = {"match_id": "fixture", "packet": {"decisions": [row("pivotal", [10, 190]), row("routine", [10])]}}
    out = pivotal_thinking_summary([b])
    assert out["pivotal_routine_ratio"] == 20
    assert out["tenfold_token_gate_met"] is True
    assert out["overall_e4_gate_met"] is False
    b["packet"]["decisions"].append(row("pivotal", [None]))
    assert pivotal_thinking_summary([b])["pivotal_routine_ratio"] is None


def test_m1_e4_missing_or_failed_call_is_not_zero_thinking():
    from tools.upgrade_metrics import pivotal_thinking_summary

    assert pivotal_thinking_summary([])["pivotal_routine_ratio"] is None
    b = {
        "match_id": "fixture",
        "packet": {
            "decisions": [
                {
                    "trace": {
                        "pivotal_thinking": {
                            "category": "pivotal",
                            "calls": 2,
                            "call_usage": [{"reasoning_tokens": 200}],
                        }
                    }
                }
            ]
        },
    }
    assert pivotal_thinking_summary([b])["categories"]["pivotal"]["average_thinking_tokens"] is None


def test_enabled_combat_declaration_has_one_high_not_hedge(monkeypatch):
    from tests.test_latency_232_combat_declaration import CONTEXT, DECLARATION, MENU, board

    from arenamcp.action_planner import ActionPlanner, ActionType

    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    efforts = []

    class Backend:
        def complete(self, *args, **kw):
            efforts.append(kw.get("reasoning_effort"))
            return DECLARATION

    planner = ActionPlanner(Backend(), timeout=5, land_drop_first=False)
    plan = planner.plan_actions(board(), "decision_required", MENU, CONTEXT)
    assert efforts == ["high"]
    assert plan.actions[0].action_type is ActionType.DECLARE_BLOCKERS


def test_close_combat_requires_distinct_choice_same_deployment():
    from types import SimpleNamespace as NS

    from arenamcp.selective_thinking import close_combat_reason

    def line(attack, value, casts=()):
        return NS(
            complete=True,
            unsupported=(),
            ranked=(1, 0, value),
            block="",
            steps=[NS(land="", casts=casts, targets=(), attack=attack)],
        )

    r = NS(truncated=False, unsupported=(), choice_caveats=(), lines=[line((1,), 10), line((), 9)])
    assert close_combat_reason(r)
    r.lines[1] = line((1,), 9)
    assert not close_combat_reason(r)
    r.lines[1] = line((), 9, casts=("different deployment",))
    assert not close_combat_reason(r)


def test_engine_adapter_reads_existing_rules_not_display_name():
    from arenamcp.selective_thinking import recognised_engine_cast

    card = {
        "instance_id": 77,
        "name": "Fixture engine",
        "type_line": "Enchantment",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "oracle_text": "At the beginning of your upkeep, each opponent loses 2 life.",
    }
    assert recognised_engine_cast(card, 1, 2)
    assert not recognised_engine_cast({**card, "oracle_text": ""}, 1, 2)


def test_m1_includes_completed_declaration_metrics_without_double_counting_call_logs():
    import json

    from tools.upgrade_metrics import pivotal_thinking_summary

    event = {"category": "pivotal", "calls": 1, "call_usage": [{"reasoning_tokens": 120}]}
    b = {
        "match_id": "fixture",
        "coach_log": {
            "lines": [
                "E4 call metrics: " + json.dumps({"category": "pivotal", "reasoning_tokens": 120}),
                "E4 declaration metrics: " + json.dumps(event),
                "E4 declaration metrics: invalid",
            ]
        },
    }
    out = pivotal_thinking_summary([b])
    assert out["categories"]["pivotal"]["decisions"] == 1
    assert out["categories"]["pivotal"]["average_thinking_tokens"] == 120


@pytest.mark.parametrize("limit", ["clock", "degraded", "plan"])
@pytest.mark.parametrize("rewrite", [False, True])
def test_denied_correction_exclusion_survives_complete_fallback_pipeline(monkeypatch, limit, rewrite):
    import json
    from unittest.mock import Mock

    from tests.test_a3_outcome import REASON, fixture

    from arenamcp.action_planner import ActionPlanner

    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
    state, menu = fixture()
    backend = Mock()
    backend.degraded = lambda: limit == "degraded"
    backend.complete.return_value = json.dumps({"option_ids": ["cast"], "reasoning": REASON})
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "unknown"}))
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 2 if limit == "clock" else 30)
    if limit == "plan":
        monkeypatch.setattr(planner, "_yield_background", lambda: False)
    if rewrite:

        def sweep(decision, state, chosen):
            assert decision.find("cast") is None
            return ["cast"]  # adversarial late rewrite must still fail closed

        monkeypatch.setattr(planner, "_sequence_sweep", sweep)
    assert planner.plan_decision_options(menu, state) == ["pass"]
    assert backend.complete.call_count == (2 if limit == "clock" else 1)
    assert planner.last_llm_failure == ("set_aside" if limit == "clock" else "unavailable")


@pytest.mark.parametrize(
    "claim",
    ["Fury survives its evoke trigger", "Fury deals 7 damage to Griselbrand", "Fury can block the flyer"],
)
def test_enabled_negated_outcome_keeps_legal_development(monkeypatch, claim):
    import json
    from unittest.mock import Mock

    from tests.test_a3_outcome import fixture, known_fury

    from arenamcp.action_planner import ActionPlanner

    monkeypatch.setenv("ARENAMCP_PIVOTAL_THINKING", "1")
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
    assert sum(c.kwargs.get("reasoning_effort") == "high" for c in backend.complete.call_args_list) <= 1
    assert not planner.last_llm_failure


def test_excluded_only_option_returns_empty_without_losing_failure_class(monkeypatch):
    from dataclasses import replace
    from unittest.mock import Mock

    from tests.test_a3_outcome import fixture

    from arenamcp.action_planner import ActionPlanner

    state, menu = fixture()
    menu = replace(menu, options=(menu.options[0],))
    planner = ActionPlanner(Mock())
    planner.last_llm_failure = "unavailable"
    planner._last_decision_trace = {"outcome_rejected_ids": ["cast"]}
    assert planner._board_math_fallback(menu, state) == []
    assert planner.last_llm_failure == "unavailable"
    monkeypatch.setattr(planner, "_plan_decision_options", lambda *args: ["cast"])
    assert planner.plan_decision_options(menu, state) == []
