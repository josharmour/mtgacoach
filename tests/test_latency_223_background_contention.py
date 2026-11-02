"""#223: background contention measured as queue, setup and generation delays.

The background lane serialises model work so a game plan cannot overlap a
decision (2026-10-06 study: decisions overlapped another call 57% of the time
and ran p50 3.1 s instead of 1.8 s). The request metrics already separate the
three delays a caller can experience:

* ``client_setup_ms`` — building the HTTP client (network/setup),
* ``lane_wait_ms`` — waiting for the background lane (queue),
* ``ttft_ms``/``first_content_ms``/``total_ms`` — generation after admission.

These fixtures pin that separation and the scheduling contract around it:
urgent work never queues behind background work and keeps its budget, abandoned
background work is cancelled instead of finishing into a stale answer, and a
replacement plan still completes.
"""

from __future__ import annotations

import threading
import time

from tests.proxy_fakes import chunk, make_backend

from arenamcp.backends import proxy
from arenamcp.backends.proxy import BackendCallCancelled


class BlockingStream:
    """A stream that holds the lane until released, or fails once closed."""

    def __init__(self, text="done"):
        self.text = text
        self.release = threading.Event()
        self.started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        self.started.set()
        deadline = time.monotonic() + 10
        while not self.release.is_set():
            if self.closed.is_set():
                raise RuntimeError("stream closed by client")
            if time.monotonic() > deadline:
                raise RuntimeError("test stream never released")
            time.sleep(0.005)
        yield chunk(self.text, finish="stop")

    def close(self):
        self.closed.set()


def _start(fn, *args, **kwargs):
    result: dict = {}

    def run():
        try:
            result["value"] = fn(*args, **kwargs)
        except Exception as e:  # surfaced to the test through result
            result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, result


def _wait_queued(call_class: str) -> None:
    lane = proxy._BACKGROUND_LANE
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if [h.call_class for h in lane._waiting] == [call_class]:
            return
        time.sleep(0.005)
    raise AssertionError(f"{call_class} never queued for the lane")


def _generation_ms(metrics: dict) -> float:
    """Time from admission to first token: total TTFT minus setup and queue."""
    return metrics["ttft_ms"] - metrics["client_setup_ms"] - metrics["lane_wait_ms"]


def test_lane_wait_is_measured_separately_from_setup_and_generation():
    holder = BlockingStream("plan A")
    a = make_backend([holder])
    ta, ra = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert holder.started.wait(5)

    b = make_backend([[chunk("deck B", finish="stop")]])
    tb, rb = _start(
        b.complete,
        "s",
        "u",
        call_class="background.deck_playbook",
        request_timeout_s=10,
        lane_wait_s=5,
    )
    _wait_queued("background.deck_playbook")
    time.sleep(0.25)  # B accumulates a measurable queue delay before admission
    holder.release.set()
    ta.join(5)
    tb.join(5)

    assert ra["value"] == "plan A"
    assert rb["value"] == "deck B"
    metrics = b.last_request_metrics
    assert metrics["status"] == "ok"
    assert metrics["lane_wait_ms"] >= 150, "the queue delay is recorded on the waiting call"
    assert metrics["client_setup_ms"] < 100, "setup is measured before the lane wait"
    assert metrics["ttft_ms"] >= metrics["lane_wait_ms"], "TTFT includes the queue delay"
    assert _generation_ms(metrics) < 100, "generation is what remains after setup and queue"


def test_urgent_decision_never_queues_and_keeps_its_budget():
    holder = BlockingStream()
    a = make_backend([holder])
    ta, _ = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert holder.started.wait(5)

    b = make_backend([[chunk("attack with the flyer", finish="stop")]])
    started = time.monotonic()
    out = b.complete("s", "u", call_class="decision.combat", request_timeout_s=1.0)
    elapsed = time.monotonic() - started

    assert out == "attack with the flyer"
    assert elapsed < 1.0, "an urgent decision must not wait for background work"
    metrics = b.last_request_metrics
    assert metrics["status"] == "ok"
    assert metrics["lane_wait_ms"] == 0
    assert metrics["total_ms"] <= 1000, "the urgent call finishes inside its own budget"

    holder.release.set()
    ta.join(5)


def test_abandoned_background_work_cancels_and_the_replacement_completes():
    stale = BlockingStream("stale plan")
    a = make_backend([stale])
    ta, ra = _start(
        a.complete,
        "s",
        "u",
        call_class="background.game_plan",
        request_timeout_s=10,
        raise_on_error=True,
    )
    assert stale.started.wait(5)

    b = make_backend([[chunk("fresh plan", finish="stop")]])
    assert b.complete("s", "u", call_class="background.game_plan", request_timeout_s=10) == "fresh plan"
    ta.join(5)

    err = ra["error"]
    assert isinstance(err, BackendCallCancelled)
    assert err.reason == "superseded"
    assert stale.closed.is_set(), "the abandoned stream is closed so the server can abort it"
    assert a.last_request_metrics["status"] == "cancelled"
    replacement = b.last_request_metrics
    assert replacement["status"] == "ok"
    assert replacement["lane_wait_ms"] < 100, "a supersede takes the lane at once instead of queueing"


def test_dropped_background_call_reports_the_queue_delay_it_spent():
    holder = BlockingStream()
    a = make_backend([holder])
    ta, _ = _start(a.complete, "s", "u", call_class="background.game_plan", request_timeout_s=10)
    assert holder.started.wait(5)

    b = make_backend()
    out = b.complete("s", "u", call_class="background.deck_playbook", lane_wait_s=0.2)

    assert proxy.is_call_cancelled(out)
    metrics = b.last_request_metrics
    assert metrics["status"] == "dropped"
    assert metrics["lane_wait_ms"] >= 150, "a dropped call still reports how long it queued"
    assert b._client.calls == [], "nothing was sent to the model"

    holder.release.set()
    ta.join(5)
