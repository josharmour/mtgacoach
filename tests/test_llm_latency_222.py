"""Ledger #222: LLM latency — budget cuts, cancellation, BackendHealth, the hedge.

2026-10-23 night (matches 2c397f62, dffd55b0): "LLM streaming time budget
exhausted" left BackendHealth at OK while every decision fell to the
deterministic fallback; a doubted low answer waited out the whole high budget
(~17 s stalls) and was set aside anyway; a request change discarded a leg's
completed answer.

* the proxy records a BackendHealth failure when our own streaming budget cuts
  a call that was generating (slow-but-alive stays out of the circuit breaker);
* our own budget cut is never retried;
* a doubted low answer waits a bounded ``_DOUBT_AFTER_LOW_S`` for the high leg,
  not the whole high budget;
* a request change with a completed leg returns that leg's answer.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from tests.proxy_fakes import make_backend
from tests.test_decision_layer_20261019 import _Model, _planner, _reply

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.backend_health import BackendHealth, HealthState
from arenamcp.backends.proxy import BackendError


# --- BackendHealth sees a budget-cut stream --------------------------------------------------------


def _stream_that_outlives():
    """An SSE stream: one chunk at once, the next only after the budget died."""

    def behavior():
        def gen():
            yield _chunk("partial ")
            time.sleep(0.9)
            yield _chunk("tail")

        return iter(gen())

    return behavior


def _chunk(text):
    from types import SimpleNamespace

    choice = SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=None)
    return SimpleNamespace(choices=[choice], model="glm-5.3-flash", usage=None)


def test_budget_cut_stream_records_a_backend_health_failure():
    """20:40:26, 2c397f62: 'LLM streaming time budget exhausted' kept health OK."""
    BackendHealth.reset_instance()
    health = BackendHealth.instance()
    seen: list[dict] = []
    health.add_listener(lambda snap: seen.append(snap))

    be = make_backend([_stream_that_outlives()])
    with pytest.raises(BackendError) as exc:
        be.complete("sys", "user", 16, request_timeout_s=0.4, raise_on_error=True)
    assert "time budget exhausted" in str(exc.value)
    assert exc.value.kind == "timeout"
    assert exc.value.first_token_seen is True  # a token arrived: slow, not dead
    assert health.state is HealthState.DEGRADED
    assert any("time budget exhausted" in snap["last_error"] for snap in seen)
    BackendHealth.reset_instance()


def test_our_own_budget_cut_is_never_retried():
    """A retry would start a fresh request with no budget left."""
    from arenamcp.backends.proxy import ProxyBackend

    call = type("_Call", (), {"first_token_at": 1.0, "first_token_timeout_s": None})()
    out = ProxyBackend._classify_call_error(TimeoutError("LLM streaming time budget exhausted"), call)
    assert out.kind == "timeout"
    assert out.retryable is False
    assert out.first_token_seen is True


# --- the hedge: bounded doubt wait -----------------------------------------------------------------


def _parse(response: str):
    data = json.loads(response)
    return data, [str(option_id) for option_id in data["option_ids"]]


def test_doubted_low_answer_hands_over_after_a_bounded_wait():
    """The high leg used to be waited out to its full 15 s + 2 s grace (#222)."""
    planner = _planner(_Model("{}"))
    started = time.monotonic()

    def ask(message, *, effort, budget, cancel_event=None):
        if effort == "high":
            time.sleep(12.0)  # far past the doubt window; would outlast the old wait
            raise TimeoutError("LLM streaming time budget exhausted")
        return _reply("idx:0", "cast it now")

    with pytest.raises(SecondLookError):
        planner._hedged_decision(
            ask,
            _parse,
            "msg",
            5.0,
            None,
            lambda ids, data: "face_burn: the line rates passing above this cast",
            high_budget=15.0,
        )
    elapsed = time.monotonic() - started
    # low (instant) + _DOUBT_AFTER_LOW_S (6) + slack; the old code waited ~15+.
    assert 5.0 < elapsed < 9.5, elapsed


def test_patient_window_still_waits_out_the_high_leg():
    """Within lethal reach (#100) the full high budget stays."""
    planner = _planner(_Model("{}"))
    started = time.monotonic()

    def ask(message, *, effort, budget, cancel_event=None):
        if effort == "high":
            time.sleep(7.5)
            return _reply("idx:1", "the kill")
        return _reply("idx:0", "chip")

    data, chosen = planner._hedged_decision(
        ask, _parse, "msg", 5.0, None, lambda ids, data: "face_burn: doubt", patient=True
    )
    assert chosen == ["idx:1"]
    assert time.monotonic() - started >= 7.0


# --- the hedge: a request change keeps a completed answer ------------------------------------------


def test_request_change_with_a_ready_low_answer_returns_it():
    """#226(b): the request changed under a finished leg — the answer stands."""
    planner = _planner(_Model("{}"))
    outer = threading.Event()

    def ask(message, *, effort, budget, cancel_event=None):
        if effort == "high":
            time.sleep(0.3)
            outer.set()  # the bridge shows a different request mid-hedge
            raise TimeoutError("request cancelled")
        return _reply("idx:1", "the low answer")

    planner.set_decision_cancel_event(outer)
    planner._last_decision_trace = {}  # direct _hedged_decision call: no window init
    data, chosen = planner._hedged_decision(
        ask,
        _parse,
        "msg",
        5.0,
        None,
        lambda ids, data: "face_burn: doubt",
        high_budget=15.0,
    )
    assert chosen == ["idx:1"]
    trace = planner.get_last_decision_trace()
    assert trace["hedge"]["winner"] == "low"


def test_request_change_without_any_answer_still_cancels():
    from arenamcp.action_planner import LLMUnavailableError

    planner = _planner(_Model("{}"))
    outer = threading.Event()
    blocker = threading.Event()  # never set: neither leg ever answers

    def ask(message, *, effort, budget, cancel_event=None):
        blocker.wait(3.0)
        raise TimeoutError("request cancelled")

    planner.set_decision_cancel_event(outer)
    threading.Timer(0.2, outer.set).start()
    with pytest.raises(LLMUnavailableError):
        planner._hedged_decision(
            ask,
            _parse,
            "msg",
            5.0,
            None,
            lambda ids, data: "face_burn: doubt",
            high_budget=15.0,
        )


# --- ActionPlanner import sanity for the new constants ---------------------------------------------


def test_doubt_window_constant_is_bounded():
    assert 0 < ActionPlanner._DOUBT_AFTER_LOW_S <= 8.0
