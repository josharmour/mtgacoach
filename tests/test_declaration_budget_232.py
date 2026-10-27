import concurrent.futures
import threading
import time
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionPlanner, LLMUnavailableError


def test_request_change_stops_waiting_for_a_backend_that_ignores_cancellation():
    planner = ActionPlanner(Mock(), timeout=10)
    cancel = threading.Event()
    planner.set_decision_cancel_event(cancel)
    release = threading.Event()
    started = threading.Event()
    result = []

    def backend():
        started.set()
        release.wait(2)
        return "old answer"

    def run():
        try:
            planner._bounded_call(backend)
        except LLMUnavailableError:
            result.append("cancelled")

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(1)
        cancel.set()
        worker.join(0.8)
        assert result == ["cancelled"]
    finally:
        release.set()
        worker.join(2)


def test_repair_wait_cannot_start_a_new_full_planning_budget():
    planner = ActionPlanner(Mock(), timeout=10)
    release = threading.Event()
    started = time.monotonic()
    try:
        with pytest.raises(concurrent.futures.TimeoutError):
            planner._bounded_call(lambda: release.wait(2), timeout_s=0.05)
        assert time.monotonic() - started < 0.8
    finally:
        release.set()


def test_expired_budget_does_not_send_another_request():
    planner = ActionPlanner(Mock())
    call = Mock()
    with pytest.raises(concurrent.futures.TimeoutError):
        planner._bounded_call(call, timeout_s=0)
    call.assert_not_called()


@pytest.mark.parametrize("kind", ["block", "attack"])
def test_combat_repair_receives_only_the_initial_calls_remaining_budget(monkeypatch, kind):
    budgets = []

    class Backend:
        def complete(self, *args, **kwargs):
            budgets.append(kwargs["request_timeout_s"])
            if len(budgets) == 1:
                time.sleep(0.05)
            return "not json"

    planner = ActionPlanner(Backend(), timeout=0.3, land_drop_first=False)
    monkeypatch.setattr(planner, "_parse_response", lambda *a, **k: ActionPlan())
    planner.plan_actions(
        {"turn": {"turn_number": 2}, "battlefield": [], "players": []},
        "decision_required",
        [f"{kind.title()} with: Spider", "Done"],
        {"type": f"declare_{kind}ers"},
    )
    assert len(budgets) == 2
    assert budgets[1] < budgets[0] - 0.04


@pytest.mark.parametrize("outcome", ["winner", "timeout", "request_change"])
def test_declaration_hedge_cancels_outstanding_calls(outcome):
    planner = ActionPlanner(Mock(), timeout=0.15)
    outer = threading.Event()
    planner.set_decision_cancel_event(outer)
    both_started = threading.Barrier(2)
    stopped = {"high": threading.Event(), None: threading.Event()}
    cancels = {}

    def complete(call_class, effort, *, cancel_event):
        cancels[effort] = cancel_event
        both_started.wait(timeout=2)
        if outcome == "winner" and effort == "high":
            return "high answer"
        if outcome == "request_change":
            outer.set()
        try:
            assert cancel_event.wait(2), "outstanding model call was not cancelled"
            raise LLMUnavailableError("cancelled")
        finally:
            stopped[effort].set()

    try:
        if outcome == "winner":
            assert planner._hedged_declaration(complete) == "high answer"
        else:
            expected = concurrent.futures.TimeoutError if outcome == "timeout" else LLMUnavailableError
            with pytest.raises(expected):
                planner._hedged_declaration(complete)
        assert stopped[None].wait(1)
        assert all(event.is_set() for event in cancels.values())
        if outcome != "winner":
            assert stopped["high"].wait(1)
        assert outer.is_set() == (outcome == "request_change")
    finally:
        for event in cancels.values():
            event.set()
