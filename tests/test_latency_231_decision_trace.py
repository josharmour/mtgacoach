"""#231: one decision's requests must be attributable, including its submission.

The retained trace is a 15-31 s whole-loop stall whose per-request log lines
interleave with background work, so no single line named the requests that made
up the decision or what was finally submitted. These tests pin the trace that
closes that gap: queue wait, first token, generation and cancellation per
request, and the fallback/timing suffix on the submission line.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from tests.proxy_fakes import APITimeoutError, make_backend, ok_stream
from tests.test_game_plan import _state

from arenamcp import llm_trace
from arenamcp.action_planner import ActionPlanner
from arenamcp.autopilot import AutopilotEngine


def test_a_stalled_decision_records_queue_first_token_and_generation():
    be = make_backend([ok_stream("Cast removal."), APITimeoutError("Request timed out.")])
    trace = llm_trace.start()
    try:
        assert be.complete("s", "u", call_class="decision.plan", request_timeout_s=12) == "Cast removal."
        be.complete(
            "s",
            "u",
            call_class="decision.plan",
            request_timeout_s=12,
            first_token_timeout_s=6,
        )
    finally:
        llm_trace.finish(trace)

    summary = trace.summary()
    assert [entry["status"] for entry in summary["requests"]] == ["ok", "ttft_timeout"]
    answered, stalled = summary["requests"]
    assert answered["lane_wait_ms"] == 0.0, "a decision never queues behind background work"
    assert answered["ttft_ms"] is not None and answered["total_ms"] >= answered["ttft_ms"]
    assert stalled["ttft_ms"] is None, "no first token is the stall this ID reports"
    assert stalled["total_ms"] is not None
    assert summary["cancelled"] == 0
    described = llm_trace.describe(summary)
    assert "decision.plan ok" in described and "decision.plan ttft_timeout" in described


def test_a_cancelled_request_is_recorded_with_the_decision():
    cancel = threading.Event()
    cancel.set()
    be = make_backend()
    trace = llm_trace.start()
    try:
        be.complete("s", "u", call_class="decision.plan", request_timeout_s=12, cancel_event=cancel)
    finally:
        llm_trace.finish(trace)

    summary = trace.summary()
    assert summary["cancelled"] == 1
    assert summary["requests"][0]["status"] == "cancelled"
    assert summary["requests"][0]["call_class"] == "decision.plan"


def test_the_trace_closes_with_the_decision():
    trace = llm_trace.start()
    llm_trace.finish(trace)
    make_backend().complete("s", "u", call_class="background.deck_playbook")
    assert trace.summary()["requests"] == [], "later background work must not join a closed decision"


class _Engine(AutopilotEngine):
    """Only the submission-logging state; constructing the real engine needs a bridge."""

    def __init__(self) -> None:
        self._path_stats: dict[str, int] = {}
        self.last_plan_fallback_reason = "llm_unavailable"
        self._last_decision_trace = {"elapsed_ms": 15234.0, "requests": [{}, {}], "cancelled": 1}


def test_the_submission_line_names_the_fallback_and_decision_time(caplog):
    engine = _Engine()
    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        engine._log_execution_path("GRE_AWARE", "play_land Forest")

    line = caplog.text
    assert "[GRE_AWARE] play_land Forest" in line
    assert "fallback=llm_unavailable" in line
    assert "decision=15234.0ms" in line
    assert "requests=2" in line and "cancelled=1" in line


def test_planning_records_the_trace_on_the_engine(monkeypatch, caplog):
    class Backend:
        def complete(self, *args, **kwargs):
            llm_trace.record(
                {
                    "call_class": kwargs.get("call_class") or "decision.plan",
                    "status": "ok",
                    "lane_wait_ms": 0.0,
                    "ttft_ms": 120.0,
                    "total_ms": 900.0,
                }
            )
            return '{"actions":[{"action_type":"declare_attackers","attacker_names":[]}]}'

    monkeypatch.setattr("arenamcp.action_planner.declaration_effort", lambda *a: ("", ""))
    engine = SimpleNamespace(
        _planner=ActionPlanner(Backend(), timeout=1, land_drop_first=False),
        _last_decision_trace=None,
    )
    with caplog.at_level("INFO", logger="arenamcp.autopilot"):
        plan = AutopilotEngine._plan_with_trace(
            engine,
            _state(turn=3),
            "decision_required",
            ["Attack with: Spider", "Done"],
            {"type": "declare_attackers"},
        )

    assert plan.actions, "the traced plan is still the plan that gets submitted"
    summary = engine._last_decision_trace
    assert summary is not None and summary["requests"]
    assert {entry["call_class"] for entry in summary["requests"]} == {"decision.plan"}
    assert "[AUTOPILOT] Decision trace:" in caplog.text


@pytest.mark.parametrize("status", ["dropped", "cancelled"])
def test_queue_delay_is_kept_for_a_request_that_never_generated(status):
    trace = llm_trace.start()
    try:
        llm_trace.record(
            {
                "call_class": "background.game_plan",
                "status": status,
                "lane_wait_ms": 263.2,
                "ttft_ms": None,
                "total_ms": 263.5,
            }
        )
    finally:
        llm_trace.finish(trace)

    summary = trace.summary()
    assert summary["cancelled"] == 1
    assert summary["requests"][0]["lane_wait_ms"] == pytest.approx(263.2)
