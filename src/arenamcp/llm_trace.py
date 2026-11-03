"""Decision-scoped LLM request trace (#231).

A 15-31 s whole-loop stall cannot be attributed from per-request log lines
alone: one decision can issue a hedged pair, a repair retry and a second
opinion, and those lines interleave with background work. This module keeps one
process-wide trace that every finished request appends to while a decision is
open, so the decision's own log line names the requests that made it up --
queue wait, first token, generation, cancellation -- next to what was finally
submitted.

Instrumentation only: recording never raises into a request or a plan, and the
trace holds counts and timings, never prompts or model output.
"""

from __future__ import annotations

import threading
import time
from typing import Any

# The metrics fields worth keeping per request: enough to tell a queue wait
# from a slow first token from slow generation, and to see a cancellation.
_FIELDS = (
    "call_class",
    "status",
    "lane_wait_ms",
    "client_setup_ms",
    "ttft_ms",
    "first_content_ms",
    "total_ms",
    "attempts",
    "budget_s",
    "finish_reason",
)

_LOCK = threading.Lock()
_ACTIVE: RequestTrace | None = None


class RequestTrace:
    """The requests made while one decision was being planned."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        # A request that began before this decision opened belongs to an earlier
        # one: its metrics can land after this trace is active (a cancelled leg
        # finishing late), and attributing it here would misreport the decision.
        self.started_unix_s = time.time()
        self.requests: list[dict[str, Any]] = []

    def add(self, metrics: dict[str, Any]) -> None:
        started_unix_s = metrics.get("request_started_unix_s")
        if started_unix_s is not None and started_unix_s < self.started_unix_s:
            return
        entry = {name: metrics.get(name) for name in _FIELDS}
        with _LOCK:
            self.requests.append(entry)

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)

    def summary(self) -> dict[str, Any]:
        """A JSON-safe record of the decision: its wall clock and each request."""
        with _LOCK:
            requests = [dict(entry) for entry in self.requests]
        return {
            "elapsed_ms": self.elapsed_ms(),
            "requests": requests,
            "cancelled": sum(1 for entry in requests if entry["status"] in ("cancelled", "dropped")),
        }


def start() -> RequestTrace:
    """Open a trace; requests recorded until finish() append to it."""
    global _ACTIVE
    trace = RequestTrace()
    with _LOCK:
        _ACTIVE = trace
    return trace


def finish(trace: RequestTrace) -> None:
    """Close this trace. A newer trace that replaced it is left alone."""
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is trace:
            _ACTIVE = None


def record(metrics: dict[str, Any]) -> None:
    """Append one finished request's metrics to the open trace, if any."""
    with _LOCK:
        trace = _ACTIVE
    if trace is None:
        return
    try:
        trace.add(metrics)
    except Exception:  # instrumentation never breaks a request
        pass


def describe(summary: dict[str, Any]) -> str:
    """One log-line form of a summary: per-request queue/first-token/generation."""
    parts = []
    for entry in summary.get("requests") or []:
        parts.append(
            "{cls} {status} lane={lane} ttft={ttft} total={total}".format(
                cls=entry.get("call_class") or "?",
                status=entry.get("status"),
                lane=entry.get("lane_wait_ms"),
                ttft=entry.get("ttft_ms"),
                total=entry.get("total_ms"),
            )
        )
    return "elapsed={elapsed}ms requests={count} [{parts}]".format(
        elapsed=summary.get("elapsed_ms"),
        count=len(summary.get("requests") or []),
        parts="; ".join(parts),
    )
