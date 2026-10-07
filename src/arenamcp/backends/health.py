"""Process-wide circuit breaker for the LLM model server.

Why this exists (bug_20261006_185403): ``BackendHealth`` already knew the
model server was DOWN at 18:49:46, but nothing read it before calling the
model, so every trigger kept paying for failing calls. A gateway 500 cost
about 5 s; a saturated server cost the whole 12 s / 30 s budget. That added
up to 22 failed calls (109 s) and then 4 more (84 s), with the coaching loop
stalled for 16-37 s at a time.

The breaker is shared by every ``ProxyBackend`` that talks to the same
``(base_url, model)``. That matters because the coach, the autopilot, the
planner and each re-initialised backend all build their own instance.

Rules (one record per ``complete()`` call, never per HTTP attempt):

* **Counted failures**: connection errors; HTTP 408/429/500/502/503/504/524;
  timeouts that never received a first token.
* **Not counted**: 4xx auth/client errors; timeouts after the first token
  (slow but alive); empty or unparseable answers; cancelled or dropped calls.
* **Trip**: 3 consecutive counted failures whose first and last are at least
  5 s apart, or 5 consecutive failures. One slow call can never trip it, and
  a burst of concurrent calls failing in the same second needs 5 to trip it.
  A streak goes stale after 60 s with no further failure.
* **Open**: callers fail fast (the proxy raises ``BackendUnavailable`` or
  returns the circuit-open sentinel). A daemon thread sends a 1-token
  completion 5 s after opening, then 5 s after each failed probe (a probe
  costs the server almost nothing; the 2026-10-07 review measured 20-28 s of
  board-math-only play against a server that was already back when the
  interval backed off to 30 s). After 5 minutes open (a long outage, not a
  restart: the 2026-10-06 restart took 2.5 min) it probes every 15 s, to keep
  the log readable. A probe success, or a success from a call
  admitted while the breaker was open (a half-open trial), closes it. A call
  that started BEFORE the breaker opened proves nothing about the server now:
  its late success resets the streak but leaves the breaker open, and its late
  failure does not delay the next probe. With no live probe callable
  registered, one real call per probe interval is let through as a half-open
  trial instead.
* **Probation**: for 120 s after closing, 2 consecutive counted failures
  re-open it (one slow first token or one connection blip right after a
  restart must not). The proxy's one in-call retry stays allowed in probation.
  Failures of calls that started before the close are not counted.

Callers bracket each call with a token: ``token = breaker.admit()`` (None =
refused), then ``record_success(token=token)`` or
``record_failure(error, token=token)``. The token is the breaker generation,
which changes on every open and every close.

``GET /models`` must never close the breaker: LiteLLM answers it with vLLM
dead (the log shows "OK (208ms)" mid-outage). Only an inference probe or a
real completion closes it.

``ARENAMCP_LLM_CIRCUIT=0`` turns the gate off (failures are still counted for
the snapshot, but the breaker never opens).
"""

from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# HTTP statuses that mean "the model server is unavailable or saturated".
CIRCUIT_FAILURE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504, 524})

_CIRCUIT_ENV = "ARENAMCP_LLM_CIRCUIT"
_FALSE_VALUES = ("0", "false", "off", "no")


def circuit_enabled() -> bool:
    """False when ``ARENAMCP_LLM_CIRCUIT`` is 0/false/off/no (read on every call)."""
    return os.environ.get(_CIRCUIT_ENV, "").strip().lower() not in _FALSE_VALUES


class CircuitBreaker:
    """Thread-safe breaker for one model endpoint. Use ``get_circuit()``.

    ``clock`` must be monotonic seconds; tests inject a fake one and pass
    ``probe_thread=False`` so they can drive probes with ``probe_now()``.
    """

    TRIP_FAILURES = 3
    TRIP_SPAN_S = 5.0
    TRIP_FAILURES_ANY_SPAN = 5
    STREAK_STALE_S = 60.0
    # Background calls stop after this many fresh consecutive failures, even
    # before the span rule opens the breaker for everyone.
    BACKGROUND_STOP_FAILURES = 3
    PROBE_INTERVAL_S = 5.0
    # A long outage (open this long) is probed less often.
    PROBE_SLOW_AFTER_S = 300.0
    PROBE_SLOW_INTERVAL_S = 15.0
    PROBATION_S = 120.0
    # Consecutive counted failures that re-open the breaker during probation.
    PROBATION_REOPEN_FAILURES = 2

    def __init__(
        self,
        key: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        probe_thread: bool = True,
    ) -> None:
        self.key = key
        self._clock = clock
        self._lock = threading.Lock()
        self._open = False
        self._consecutive = 0
        self._streak_started: float | None = None
        self._last_failure_at: float | None = None
        self._last_error = ""
        self._opened_at_wall: float | None = None
        self._opened_at: float | None = None
        self._next_probe_at: float | None = None
        self._probe_interval = self.PROBE_INTERVAL_S
        self._probation_until: float | None = None
        # Bumped on every open and close; a call's token is the generation it
        # was admitted in (see admit()).
        self._generation = 0
        self._trips = 0
        self._last_detail = ""
        self._probe_ref: Callable[[], Callable[[], bool] | None] | None = None
        self._probe_thread_enabled = probe_thread
        self._probe_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    # ── probe registration ────────────────────────────────────────────

    def register_probe(self, probe: Callable[[], bool]) -> None:
        """Remember the most recent probe callable (weakly for bound methods).

        A bound method is held through ``weakref.WeakMethod`` so the breaker
        never keeps a discarded backend (and its HTTP client) alive. If the
        breaker is open with no probe thread (the previous probe owner was
        discarded), the thread is restarted with this callable.
        """
        try:
            ref: Callable[[], Callable[[], bool] | None] = weakref.WeakMethod(probe)  # type: ignore[arg-type]
        except TypeError:

            def ref(probe=probe):
                return probe

        with self._lock:
            self._probe_ref = ref
            if self._open:
                self._start_probe_thread_locked()

    def _probe_callable(self) -> Callable[[], bool] | None:
        with self._lock:
            ref = self._probe_ref
        return ref() if ref is not None else None

    # ── gate ──────────────────────────────────────────────────────────

    def available(self) -> bool:
        """True when foreground calls may go out. Pure read, no side effects."""
        if not circuit_enabled():
            return True
        with self._lock:
            return not self._open

    def allow(self) -> bool:
        """Gate for one real call; may admit a half-open trial while open."""
        return self.admit() is not None

    def admit(self, *, trial: bool = True) -> int | None:
        """Gate for one real call: its token (the current generation), or None when refused.

        While open, a half-open trial is admitted only when ``trial`` is True,
        no probe thread is running (no live probe callable), and at most once
        per probe interval. Pass the token back to record_success/record_failure.
        """
        with self._lock:
            if not circuit_enabled() or not self._open:
                return self._generation
            if not trial:
                return None
            thread = self._probe_thread
            if thread is not None and thread.is_alive():
                return None
            now = self._clock()
            if self._next_probe_at is not None and now >= self._next_probe_at:
                self._next_probe_at = now + self._probe_interval
                return self._generation
            return None

    def call_token(self) -> int:
        """The current generation, for a call that skipped the gate (tests, probes)."""
        with self._lock:
            return self._generation

    def background_blocked(self) -> bool:
        """True when background work should not call the model at all."""
        if not circuit_enabled():
            return False
        with self._lock:
            if self._open:
                return True
            return self._consecutive >= self.BACKGROUND_STOP_FAILURES and not self._streak_stale_locked(
                self._clock()
            )

    def retry_allowed(self) -> bool:
        """The proxy's one in-call retry: allowed unless the breaker is open.

        Allowed during probation too, so one connection blip right after a
        recovery is absorbed by the retry instead of counting toward a re-open.
        """
        if not circuit_enabled():
            return True
        with self._lock:
            return not self._open

    # ── outcomes ──────────────────────────────────────────────────────

    def record_success(self, detail: str = "", *, token: int | None = None) -> bool:
        """A real or probe success resets the streak; returns True when it closed the breaker.

        It closes an open breaker only when the call was admitted while it was
        open (``token`` is this open generation: a half-open trial) or carries
        no token (a probe). A call admitted before the breaker opened, such as
        a long background stream that already held a server slot when new
        requests started getting no first token, leaves it open.
        """
        with self._lock:
            now = self._clock()
            self._consecutive = 0
            self._streak_started = None
            self._last_failure_at = None
            if detail:
                self._last_detail = detail
            if not self._open:
                return False
            if token is not None and token != self._generation:
                logger.debug(
                    "[PROXY] Circuit for %s stays open: the success came from a call admitted before it opened",
                    self.key,
                )
                return False
            self._open = False
            self._generation += 1
            self._probation_until = now + self.PROBATION_S
            self._next_probe_at = None
            self._probe_interval = self.PROBE_INTERVAL_S
            snap = self._snapshot_locked(now)
        logger.info(
            "[PROXY] Circuit CLOSED for %s%s; probation %.0fs",
            self.key,
            f" ({detail})" if detail else "",
            self.PROBATION_S,
        )
        self._notify("closed", snap)
        return True

    def record_failure(self, error: str = "", *, token: int | None = None) -> bool:
        """Record one counted failure. Returns True when this call opened it.

        ``token`` is the generation the call was admitted in (None = current).
        While open, only a half-open trial's failure (a token from this open
        generation) pushes the next trial back; a call admitted before the
        breaker opened changes nothing. While closed, a failure from a call
        admitted before the latest open/close is old news and not counted.
        """
        with self._lock:
            now = self._clock()
            self._last_error = (error or "")[:300]
            current = token is None or token == self._generation
            if self._open:
                if current and token is not None and self._probe_thread is None:
                    # A half-open trial failed: the next one waits a full interval.
                    self._next_probe_at = max(self._next_probe_at or now, now + self._probe_interval)
                return False
            if not current:
                return False
            if self._streak_stale_locked(now):
                self._consecutive = 0
                self._streak_started = None
            self._consecutive += 1
            if self._streak_started is None:
                self._streak_started = now
            self._last_failure_at = now
            span = now - self._streak_started
            trip = (
                (self._in_probation_locked(now) and self._consecutive >= self.PROBATION_REOPEN_FAILURES)
                or (self._consecutive >= self.TRIP_FAILURES and span >= self.TRIP_SPAN_S)
                or self._consecutive >= self.TRIP_FAILURES_ANY_SPAN
            )
            if not trip or not circuit_enabled():
                return False
            self._open = True
            self._generation += 1
            self._trips += 1
            self._opened_at_wall = time.time()
            self._opened_at = now
            self._probation_until = None
            self._probe_interval = self.PROBE_INTERVAL_S
            self._next_probe_at = now + self._probe_interval
            consecutive = self._consecutive
            last_error = self._last_error
            interval = self._probe_interval
            snap = self._snapshot_locked(now)
            self._start_probe_thread_locked()
        logger.warning(
            "[PROXY] Circuit OPEN for %s: %d consecutive failures over %.1fs (last: %s); "
            "fast-failing calls, inference probe in %.0fs",
            self.key,
            consecutive,
            span,
            last_error[:160] or "?",
            interval,
        )
        self._notify("opened", snap)
        return True

    # ── probing ───────────────────────────────────────────────────────

    def probe_now(self, probe: Callable[[], bool] | None = None) -> bool:
        """Run one probe synchronously; success closes, failure schedules the next one."""
        fn = probe or self._probe_callable()
        if fn is None:
            return False
        started = time.perf_counter()
        try:
            ok = bool(fn())
            error = ""
        except Exception as e:  # a probe must never raise into the thread loop
            ok = False
            error = f"{type(e).__name__}: {e}"
        ms = (time.perf_counter() - started) * 1000
        if ok:
            self.record_success(detail=f"inference probe OK ({ms:.0f}ms)")
            return True
        with self._lock:
            now = self._clock()
            if self._open:
                if self._opened_at is not None and now - self._opened_at >= self.PROBE_SLOW_AFTER_S:
                    self._probe_interval = self.PROBE_SLOW_INTERVAL_S
                self._next_probe_at = now + self._probe_interval
            interval = self._probe_interval
        logger.info(
            "[PROXY] Circuit probe failed for %s after %.0fms%s; next probe in %.0fs",
            self.key,
            ms,
            f" ({error[:160]})" if error else "",
            interval,
        )
        return False

    def _start_probe_thread_locked(self) -> None:
        if not self._probe_thread_enabled or self._stop.is_set():
            return
        if self._probe_thread is not None and self._probe_thread.is_alive():
            return
        if self._probe_ref is None or self._probe_ref() is None:
            return  # no probe callable: allow() admits half-open trials instead
        thread = threading.Thread(target=self._probe_loop, name="llm-circuit-probe", daemon=True)
        self._probe_thread = thread
        thread.start()

    def _probe_loop(self) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    if not self._open:
                        return
                    delay = (self._next_probe_at or 0.0) - self._clock()
                if delay > 0:
                    self._stop.wait(min(delay, 1.0))
                    continue
                fn = self._probe_callable()
                if fn is None:
                    return
                self.probe_now(fn)
        finally:
            with self._lock:
                if self._probe_thread is threading.current_thread():
                    self._probe_thread = None

    def stop(self) -> None:
        """Stop the probe thread (registry reset / tests)."""
        self._stop.set()

    # ── observation ───────────────────────────────────────────────────

    def add_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.append(listener)  # type: ignore[arg-type]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(self._clock())

    def _snapshot_locked(self, now: float) -> dict[str, Any]:
        retry_in = None
        if self._open and self._next_probe_at is not None:
            retry_in = round(max(0.0, self._next_probe_at - now), 1)
        return {
            "key": self.key,
            "state": "open" if self._open else "closed",
            "enabled": circuit_enabled(),
            "probation": (not self._open) and self._in_probation_locked(now),
            "consecutive_failures": self._consecutive,
            "trips": self._trips,
            "last_error": self._last_error,
            "detail": self._last_detail,
            "opened_at": self._opened_at_wall if self._open else None,
            "retry_in_s": retry_in,
            "probe_interval_s": self._probe_interval,
            "timestamp": time.time(),
        }

    def _notify(self, event: str, snap: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners)
        with _REGISTRY_LOCK:
            listeners += list(_GLOBAL_LISTENERS)
        for listener in listeners:
            try:
                listener(event, dict(snap))
            except Exception as e:  # a broken listener must never break the breaker
                logger.debug("circuit listener failed: %s", e)

    def _streak_stale_locked(self, now: float) -> bool:
        return self._last_failure_at is not None and now - self._last_failure_at > self.STREAK_STALE_S

    def _in_probation_locked(self, now: float) -> bool:
        return self._probation_until is not None and now < self._probation_until


# ── registry ──────────────────────────────────────────────────────────

_REGISTRY: dict[tuple[str, str], CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()
_GLOBAL_LISTENERS: list[Callable[[str, dict[str, Any]], None]] = []


def get_circuit(base_url: str, model: str) -> CircuitBreaker:
    """The shared breaker for one ``(base_url, model)`` endpoint."""
    key = ((base_url or "").rstrip("/"), model or "")
    with _REGISTRY_LOCK:
        breaker = _REGISTRY.get(key)
        if breaker is None:
            breaker = CircuitBreaker(f"{key[1]} @ {key[0]}")
            _REGISTRY[key] = breaker
        return breaker


def circuit_snapshots() -> list[dict[str, Any]]:
    """Snapshots of every breaker created in this process."""
    with _REGISTRY_LOCK:
        breakers = list(_REGISTRY.values())
    return [b.snapshot() for b in breakers]


def add_circuit_listener(listener: Callable[[str, dict[str, Any]], None]) -> None:
    """Call ``listener(event, snapshot)`` on every open/close of any breaker.

    ``event`` is ``"opened"`` or ``"closed"``. Listeners run on the thread
    that caused the transition (a request thread or the probe thread) and
    must not block.
    """
    with _REGISTRY_LOCK:
        if listener not in _GLOBAL_LISTENERS:
            _GLOBAL_LISTENERS.append(listener)


def remove_circuit_listener(listener: Callable[[str, dict[str, Any]], None]) -> None:
    with _REGISTRY_LOCK:
        if listener in _GLOBAL_LISTENERS:
            _GLOBAL_LISTENERS.remove(listener)


def reset_circuit_breakers() -> None:
    """Forget every breaker and stop their probe threads (tests, engine reload)."""
    with _REGISTRY_LOCK:
        breakers = list(_REGISTRY.values())
        _REGISTRY.clear()
    for breaker in breakers:
        breaker.stop()
