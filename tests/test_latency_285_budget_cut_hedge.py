"""#285: a high-effort budget cut must not discard a standing low declaration.

Historical signature (78904ea7/fd68fa31/53bdd320/10f24c09): the high-effort
declaration leg was cut at its budget while the low-effort answer stood, and the
plan degraded to a fallback; other budget failures and the model-unavailable
sentinel fell back too, and post-cast priority hops were unbounded.

On current master the bounded declaration hedge (``action_planner.py:1162``,
per-leg cancel events) returns the first usable answer, so a standing low
declaration survives a high-leg cut; both legs failing still yields the
deterministic legal hold with ``llm_unavailable``; and post-cast priority hops
are capped at one (``autopilot.py:_MAX_POST_CAST_PRIORITY_HOPS``), leaving deeper
requests to the watchdog. The retained traces are absent from this checkout, so
these are deterministic reproductions of the mechanism on current master.
"""

import json
import time

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType, LLMUnavailableError

DECLARATION = json.dumps({"actions": [{"action_type": "declare_attackers", "attacker_names": []}]})
BUDGET_CUT = "LLM streaming time budget exhausted"


def state():
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 3, "active_player": 1, "phase": "Phase_Combat"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [],
        "_bridge_request_type": "DeclareAttackers",
    }


class LegBackend:
    """High leg is cut at its budget; the low leg answers after ``low_delay``."""

    def __init__(self, high_error=None, low_error=None, low_delay=0.0):
        self.high_error = high_error
        self.low_error = low_error
        self.low_delay = low_delay
        self.efforts = []

    def complete(self, *args, **kwargs):
        effort = kwargs.get("reasoning_effort")
        self.efforts.append(effort)
        if effort == "high":
            if self.high_error is not None:
                raise self.high_error
            return DECLARATION
        if self.low_delay:
            time.sleep(self.low_delay)
        if self.low_error is not None:
            raise self.low_error
        return DECLARATION


@pytest.fixture
def high_effort(monkeypatch):
    monkeypatch.setattr("arenamcp.action_planner.declaration_effort", lambda *a, **k: ("high", "danger"))


def test_a_high_budget_cut_keeps_the_standing_low_declaration(high_effort):
    backend = LegBackend(high_error=TimeoutError(BUDGET_CUT), low_delay=0.05)
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    started = time.monotonic()
    plan = planner.plan_actions(state(), "decision_required")
    elapsed = time.monotonic() - started

    assert backend.efforts == ["high", None], "both declaration legs must be attempted"
    assert plan.fallback_reason == "", "a standing low declaration must not degrade to a fallback"
    assert [action.action_type for action in plan.actions] == [ActionType.DECLARE_ATTACKERS]
    assert elapsed < 1.0, f"the cut high leg held the plan for {elapsed:.2f}s"


def test_both_legs_cut_falls_back_to_the_legal_hold(high_effort):
    backend = LegBackend(high_error=TimeoutError(BUDGET_CUT), low_error=TimeoutError(BUDGET_CUT))
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    plan = planner.plan_actions(state(), "decision_required")

    assert plan.fallback_reason == "llm_unavailable"
    assert plan.actions == [], "the fallback must hold rather than submit an invented attack"


def test_model_unavailable_sentinel_falls_back_to_the_legal_hold(high_effort):
    backend = LegBackend(
        high_error=LLMUnavailableError("model server unavailable"),
        low_error=LLMUnavailableError("model server unavailable"),
    )
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    plan = planner.plan_actions(state(), "decision_required")

    assert plan.fallback_reason == "llm_unavailable"
    assert plan.actions == []


def test_post_cast_priority_hops_stay_capped_at_one():
    from arenamcp.autopilot import AutopilotEngine

    assert AutopilotEngine._MAX_POST_CAST_PRIORITY_HOPS == 1


def test_a_request_from_an_earlier_decision_is_not_attributed_to_this_trace():
    """A cancelled leg finishing late must not be counted in the next decision."""
    from arenamcp import llm_trace

    trace = llm_trace.start()
    try:
        llm_trace.record(
            {
                "call_class": "decision.plan",
                "status": "cancelled",
                "request_started_unix_s": trace.started_unix_s - 5.0,
            }
        )
        llm_trace.record(
            {
                "call_class": "decision.plan",
                "status": "ok",
                "request_started_unix_s": trace.started_unix_s + 0.01,
            }
        )
        llm_trace.record({"call_class": "decision.plan", "status": "ok"})
    finally:
        llm_trace.finish(trace)

    assert [entry["status"] for entry in trace.summary()["requests"]] == ["ok", "ok"]
