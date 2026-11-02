"""BUG-248: a streaming budget cut must end in a legal play, not a stalled loop.

The retained trace (docs/backlog-triage-20261101.md, #248) is "LLM streaming
time budget exhausted" followed by 16-31 s loop delays. The controls that
answer it are already merged:

- the proxy cuts its own stream at the request budget, never retries that cut,
  keeps the circuit closed and marks health degraded (#222, d27b878);
- the planner turns any model failure into ``llm_unavailable`` inside its
  deadline, cancelling the abandoned call (F5, 70d9e2f / b352b8a);
- the autopilot replaces that plan with the board-math line and does not
  escalate the planning budget;
- #231's trace records queue wait, first token, generation and cancellation
  per request, plus what was finally submitted.

What was never pinned is the composition on this exact error: one bounded
call, a recorded timing decomposition, cancellation of the abandoned stream,
and a legal submission. The post-cast six-second waits in the same trace are
#206/#243 (1adc79d), not model duration.
"""

from __future__ import annotations

import time

import pytest
from tests.proxy_fakes import chunk, make_backend
from tests.test_game_plan import _state
from tests.test_llm_down_fallback import _run_legacy

from arenamcp import llm_trace
from arenamcp.action_planner import (
    FALLBACK_LLM_UNAVAILABLE,
    ActionPlanner,
    ActionType,
    is_llm_unavailable_error,
)
from arenamcp.backends.proxy import BackendError

CUT = "LLM streaming time budget exhausted"
LEGAL = ["Play land: Forest", "Cast Geist of Saint Thalia [OK]", "Done"]


def _stream_past_the_budget(total_s: float = 1.5):
    """A first token arrives, then generation keeps going past our own budget."""

    def gen():
        yield chunk("Cast")
        stop = time.monotonic() + total_s
        while time.monotonic() < stop:
            time.sleep(0.02)
            yield chunk(".")

    return iter(gen())


def _stream_blocked_past_the_budget():
    """A first token arrives, then next() blocks so no chunk reaches the proxy."""

    def gen():
        yield chunk("Cast the Geist")
        time.sleep(0.9)
        yield chunk(" and attack.", finish="stop")

    return iter(gen())


class _CutBackend:
    """The shape ProxyBackend raises when its own stream budget cut wins."""

    def __init__(self):
        self.calls: list[dict] = []

    def complete(self, *args, **kwargs):
        self.calls.append(kwargs)
        raise BackendError(CUT)


def _planner(backend, timeout: float = 0.4) -> ActionPlanner:
    return ActionPlanner(backend, timeout=timeout, land_drop_first=False)


def _plan(planner: ActionPlanner):
    return planner.plan_actions(
        _state(turn=3), "decision_required", list(LEGAL), {"type": "actions_available"}
    )


def test_the_proxy_cuts_its_own_stream_and_records_the_decomposition():
    be = make_backend([_stream_past_the_budget()])
    planner = _planner(be)
    trace = llm_trace.start()
    try:
        with pytest.raises(BackendError) as exc:
            planner._call_llm(
                "sys",
                "user",
                64,
                timeout_s=0.4,
                call_class="decision.plan",
                yield_background=False,
            )
    finally:
        llm_trace.finish(trace)

    assert CUT in str(exc.value)
    assert exc.value.kind == "timeout"
    assert exc.value.first_token_seen is True  # a token arrived: slow, not dead
    assert len(be._client.calls) == 1  # our own cut is never retried

    # The timing decomposition #248 asks for: no queue behind background work,
    # a first token arrived, generation was cut at the budget.
    summary = trace.summary()
    assert len(summary["requests"]) == 1
    request = summary["requests"][0]
    assert request["status"] == "error"
    assert request["attempts"] == 1
    assert request["budget_s"] == 0.4
    assert request["lane_wait_ms"] == 0.0
    assert request["ttft_ms"] is not None
    assert request["total_ms"] >= request["ttft_ms"]
    assert request["total_ms"] >= 350, "generation ran to the budget before the cut"
    assert request["total_ms"] < 3000
    assert summary["cancelled"] == 0


def test_a_stream_blocked_past_the_budget_is_one_bounded_call():
    be = make_backend([_stream_blocked_past_the_budget()])
    planner = _planner(be)
    plan = _plan(planner)

    # One attempt: a retry would start a fresh request with no budget left.
    assert len(be._client.calls) == 1
    # Benign fallback inside the deadline, not an exception and not a longer wait.
    assert plan.actions == []
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE
    # The caller's deadline wins here: generation blocks inside next(), so no
    # chunk arrives for the proxy to notice its own cut first.
    assert plan.fallback_detail == "timeout"
    diag = planner.get_recent_diagnostics()[-1]
    assert diag["failure"] == "timeout"
    assert diag["elapsed_ms"] > 0


def test_the_proxy_budget_cut_error_reaches_the_board_math_play_without_escalation(monkeypatch):
    backend = _CutBackend()
    plan = _plan(_planner(backend))
    assert len(backend.calls) == 1
    assert plan.actions == []
    assert plan.fallback_reason == FALLBACK_LLM_UNAVAILABLE
    assert CUT in plan.fallback_detail

    engine, executed = _run_legacy(monkeypatch, plan)
    assert [a.action_type for a in executed] == [ActionType.CAST_SPELL]
    assert engine._consecutive_plan_failures == 0
    assert engine._effective_planning_timeout == 15.0


@pytest.mark.parametrize("error", [TimeoutError(CUT), BackendError(CUT)])
def test_a_budget_cut_is_classified_as_the_model_being_unavailable(error):
    assert is_llm_unavailable_error(error)
