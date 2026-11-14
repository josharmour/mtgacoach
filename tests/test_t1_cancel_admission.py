"""Synthetic pre-dispatch cancellation tests; not measured live savings."""

import threading

import pytest

from arenamcp.action_planner import ActionPlanner, LLMUnavailableError


def invoke(planner, **kwargs):
    return planner._call_llm("system", "prompt", 10, timeout_s=1, call_class="decision.plan", **kwargs)


@pytest.mark.parametrize("outer_cancel,leg_cancel", [(True, False), (False, True), (True, True)])
def test_cancelled_request_dispatches_no_backend(outer_cancel, leg_cancel):
    calls = []

    class Backend:
        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            return "ok"

    planner = ActionPlanner(Backend())
    outer, leg = threading.Event(), threading.Event()
    if outer_cancel:
        outer.set()
    if leg_cancel:
        leg.set()
    planner.set_decision_cancel_event(outer)
    with pytest.raises(LLMUnavailableError, match="request changed"):
        invoke(planner, cancel_event=leg)
    assert calls == []


def test_cancellation_during_background_yield_prevents_dispatch():
    class Backend:
        def complete(self, *args, **kwargs):
            pytest.fail("obsolete backend dispatched")

    planner = ActionPlanner(Backend())
    outer = threading.Event()
    planner.set_decision_cancel_event(outer)
    planner.set_background_yield(outer.set)
    with pytest.raises(LLMUnavailableError, match="request changed"):
        invoke(planner)


def test_cancellation_during_legacy_retry_stops_second_dispatch():
    outer = threading.Event()
    calls = []

    class Backend:
        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            outer.set()
            raise TypeError("legacy keyword unsupported")

    planner = ActionPlanner(Backend())
    planner.set_decision_cancel_event(outer)
    with pytest.raises(LLMUnavailableError, match="request changed"):
        invoke(planner)
    assert len(calls) == 1


def test_current_request_still_dispatches_and_preserves_token():
    calls = []

    class Backend:
        def complete(self, *args, **kwargs):
            calls.append(kwargs)
            return "ok"

    planner = ActionPlanner(Backend())
    outer = threading.Event()
    planner.set_decision_cancel_event(outer)
    assert invoke(planner) == "ok"
    assert len(calls) == 1 and calls[0]["cancel_event"] is outer
