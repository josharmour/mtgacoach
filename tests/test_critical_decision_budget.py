"""Critical decisions hedge: the high-effort call and the normal low-effort one run at once.

2026-10-18 14:31-14:33 (standalone.log): under shared-GPU load 4 of 5
critical decision.typed calls (reasoning_effort=high, 15 s budget) ended in
"LLM streaming time budget exhausted" with no content, while low-effort
decisions in the same window finished in ~6 s. Board math then played the
turn's key spells and said "Model unavailable". A high-effort
background.game_plan shared the GPU meanwhile.
"""

import threading
import time

import pytest
from tests.jace_states import jace_engine, jace_state

from arenamcp import action_planner
from arenamcp.game_plan import GamePlanManager, critical_decision_call, critical_decision_call_inflight

HIGH = '{"option_ids": ["idx:2"], "reasoning": "high: Jace -3 draws."}'
LOW = '{"option_ids": ["idx:2"], "reasoning": "low: Jace -3."}'


class _Legs:
    """A fake backend: per effort, a delay and an answer (or an exception)."""

    def __init__(self, high=(0.05, HIGH), low=(0.01, LOW)):
        self.plan = {"high": high, None: low}
        self.calls: list = []
        self.cancels: dict = {}
        self.inflight_seen: list = []

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" not in user:
            return "{}"
        effort = kwargs.get("reasoning_effort")
        cancel = kwargs.get("cancel_event")
        self.calls.append(effort)
        self.cancels[effort] = cancel
        if effort == "high":
            self.inflight_seen.append(critical_decision_call_inflight())
        delay, answer = self.plan[effort]
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("request cancelled")
            time.sleep(0.005)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def critical(monkeypatch):
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "removal")


def _run(backend):
    engine, bridge, _ = jace_engine(HIGH)
    engine._planner._backend = backend
    started = time.monotonic()
    assert engine._try_typed_decision_path(jace_state(), "decision_required") is True
    return engine, bridge, time.monotonic() - started


def test_high_in_time_is_used_and_low_is_cancelled(critical, caplog):
    backend = _Legs()
    engine, bridge, _ = _run(backend)
    assert sorted(backend.calls, key=str) == sorted(["high", None], key=str)
    assert engine._planner._last_decision_reasoning.startswith("high:")
    assert backend.cancels[None].is_set() and not backend.cancels["high"].is_set()
    bridge.submit_action_by_index.assert_called_once()
    assert "typed-decision hedge: high won in" in caplog.text


def test_high_timing_out_uses_the_ready_low_answer_at_once(critical, monkeypatch, caplog):
    monkeypatch.setattr(action_planner.ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 0.3)
    backend = _Legs(high=(0.3, TimeoutError("LLM streaming time budget exhausted")))
    engine, bridge, elapsed = _run(backend)
    assert engine._planner._last_decision_reasoning.startswith("low:")
    assert elapsed < 0.3 + 1.0  # not the high leg's budget plus the low leg's again
    bridge.submit_action_by_index.assert_called_once()
    assert "typed-decision hedge: low won in" in caplog.text


def test_an_unparseable_high_answer_also_falls_to_low(critical):
    backend = _Legs(high=(0.02, "no json here"))
    engine, _, _ = _run(backend)
    assert engine._planner._last_decision_reasoning.startswith("low:")


def test_both_failing_falls_back_and_says_too_slow(critical):
    slow = TimeoutError("LLM streaming time budget exhausted")
    backend = _Legs(high=(0.02, slow), low=(0.02, TimeoutError("LLM streaming time budget exhausted")))
    engine, _, _ = _run(backend)
    assert engine._planner.last_llm_failure == "unavailable"  # autopilot semantics unchanged
    assert engine._planner._last_decision_reasoning.startswith("Model too slow")


def test_a_routine_decision_makes_one_low_effort_call():
    backend = _Legs()
    engine, _, _ = _run(backend)
    assert backend.calls == [None]


def test_no_game_plan_reform_starts_while_a_critical_call_is_in_flight(critical):
    backend = _Legs()
    _run(backend)
    assert backend.inflight_seen == [True]
    assert not critical_decision_call_inflight()

    class _Backend:
        def complete(self, *a, **k):
            return "{}"

    mgr = GamePlanManager(_Backend())
    with critical_decision_call():
        assert not mgr.request_reform(jace_state())
    assert mgr._last_attempt_at is None  # nothing attempted: re-requested at the next window


def test_the_in_flight_mark_counts_overlapping_calls():
    inside, release = threading.Event(), threading.Event()

    def other():
        with critical_decision_call():
            inside.set()
            release.wait(1)

    worker = threading.Thread(target=other)
    with critical_decision_call():
        worker.start()
        assert inside.wait(1)
    assert critical_decision_call_inflight()  # the other call is still in flight
    release.set()
    worker.join()
    assert not critical_decision_call_inflight()


def test_the_request_changing_cancels_both_legs(critical):
    backend = _Legs(high=(0.5, HIGH), low=(0.5, LOW))
    engine, bridge, _ = jace_engine(HIGH)
    engine._planner._backend = backend
    outer = threading.Event()
    engine._planner.set_decision_cancel_event(outer)
    threading.Timer(0.05, outer.set).start()
    from tests.jace_states import POLL

    from arenamcp.decisions import build_pending_decision

    decision = build_pending_decision(POLL, resolve_instance=lambda _iid: "")
    started = time.monotonic()
    engine._planner.plan_decision_options(decision, jace_state())
    assert time.monotonic() - started < 0.4
    assert backend.cancels["high"].is_set() and backend.cancels[None].is_set()
