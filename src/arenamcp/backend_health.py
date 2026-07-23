"""Backend health architecture: never let LLM failures be silent.

Three pieces:

1. ``HealthState`` / ``BackendHealth`` — a thread-safe singleton tracker that
   records consecutive backend successes/failures and derives a coarse state
   (OK → DEGRADED → DOWN) for the UI. State transitions notify listeners so
   the desktop app can show a banner the moment the gateway starts failing.

2. Output tags — ``[BACKEND ERROR]`` marks text that came from a failed LLM
   call, ``[LOCAL FALLBACK]`` marks advice generated locally without the LLM.
   Both exist because silent failures (2026-07-05 gateway outage, 2026-07-16
   license-key 401s) produced advice-shaped text that was impossible to
   distinguish from real coaching.

3. ``check_gateway_health()`` — a startup self-check that probes
   ``GET {base_url}/models`` with the configured key so a dead gateway is
   announced at boot instead of discovered mid-match.
"""

import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Tag prepended to any text returned in place of real LLM output when the
# backend call failed. Can never be mistaken for strategic advice.
BACKEND_ERROR_PREFIX = "[BACKEND ERROR]"

# Tag prepended to advice generated locally (no LLM involved) so the user can
# tell rule-based fallback advice from model output.
LOCAL_FALLBACK_PREFIX = "[LOCAL FALLBACK]"


class HealthState(Enum):
    """Coarse backend health for UI display."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


def is_backend_error_text(text: str | None) -> bool:
    """True for strings that represent a backend failure, not real content.

    Matches the ``[BACKEND ERROR]`` tag and legacy ``Error ...`` sentinels
    (older backends, the coach timeout path, third-party wrappers) so all
    pre-existing detection sites keep working through the sentinel rename.
    """
    if not text or not isinstance(text, str):
        return False
    stripped = text.lstrip()
    return stripped.startswith(BACKEND_ERROR_PREFIX) or stripped.startswith("Error")


def is_local_fallback_text(text: str | None) -> bool:
    """True for advice tagged as locally generated (no LLM involved)."""
    if not text or not isinstance(text, str):
        return False
    return text.lstrip().startswith(LOCAL_FALLBACK_PREFIX)


def strip_health_tags(text: str) -> str:
    """Remove health tags for speech output (TTS shouldn't read them aloud).

    Displayed text keeps the tags — they exist to be seen. Only call this at
    the TTS boundary.
    """
    if not text:
        return text
    stripped = text.strip()
    for prefix in (BACKEND_ERROR_PREFIX, LOCAL_FALLBACK_PREFIX):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix) :].strip()
    return stripped


class BackendHealth:
    """Thread-safe tracker of consecutive backend successes/failures.

    Thresholds: 1-2 consecutive failures → DEGRADED, >=3 → DOWN, any success
    resets to OK. Use ``instance()`` for the process-wide singleton; construct
    directly in tests for isolation.
    """

    _instance: "BackendHealth | None" = None
    _instance_lock = threading.Lock()

    DOWN_THRESHOLD = 3

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = HealthState.OK
        self._consecutive_failures = 0
        self._total_successes = 0
        self._total_failures = 0
        self._last_success_ts: float | None = None
        self._last_failure_ts: float | None = None
        self._last_error = ""
        self._last_status_code: int | None = None
        self._last_detail = ""
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    @classmethod
    def instance(cls) -> "BackendHealth":
        """Process-wide singleton tracker."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Drop the singleton (test isolation)."""
        with cls._instance_lock:
            cls._instance = None

    @property
    def state(self) -> HealthState:
        with self._lock:
            return self._state

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback fired with snapshot() on every state transition."""
        with self._lock:
            self._listeners.append(listener)

    def record_success(self, detail: str = "") -> bool:
        """Record a successful backend call. Returns True if state changed."""
        with self._lock:
            self._consecutive_failures = 0
            self._total_successes += 1
            self._last_success_ts = time.time()
            self._last_error = ""
            self._last_status_code = None
            if detail:
                self._last_detail = detail
            old = self._state
            self._state = HealthState.OK
            transitioned = self._state != old
            snap = self._snapshot_locked()
        if transitioned:
            self._notify(snap)
        return transitioned

    def record_failure(self, error: str = "", status_code: int | None = None) -> bool:
        """Record a failed backend call. Returns True if state changed."""
        with self._lock:
            self._consecutive_failures += 1
            self._total_failures += 1
            self._last_failure_ts = time.time()
            self._last_error = (error or "")[:300]
            self._last_status_code = status_code
            old = self._state
            self._state = (
                HealthState.DOWN
                if self._consecutive_failures >= self.DOWN_THRESHOLD
                else HealthState.DEGRADED
            )
            transitioned = self._state != old
            snap = self._snapshot_locked()
        if transitioned:
            self._notify(snap)
        return transitioned

    def _snapshot_locked(self) -> dict[str, Any]:
        """snapshot() with self._lock already held."""
        if self._state is HealthState.OK:
            detail = self._last_detail or "reachable"
        else:
            detail = self._last_error or "backend failing"
        return {
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
            "last_success_ts": self._last_success_ts,
            "last_failure_ts": self._last_failure_ts,
            "last_error": self._last_error,
            "last_status_code": self._last_status_code,
            "detail": detail,
            "timestamp": time.time(),
        }

    def snapshot(self) -> dict[str, Any]:
        """Serializable health state for UI/pipe events."""
        with self._lock:
            return self._snapshot_locked()

    def _notify(self, snapshot: dict[str, Any]) -> None:
        """Fire transition listeners; a broken listener must never break the tracker."""
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception as e:
                logger.debug(f"backend-health listener failed: {e}")


# Keep in sync with backends/proxy.py defaults (importing proxy here would be
# circular: proxy imports this module to report outcomes).
_DEFAULT_BASE_URL = "http://localhost:8000/v1"


def check_gateway_health(backend: Any, timeout: float = 5.0) -> tuple[HealthState, str]:
    """Probe the LLM gateway with a minimal GET {base_url}/models request.

    Uses the backend's configured base_url and API key. Records the outcome
    to the singleton tracker and returns ``(state, detail)`` where state is
    the probe's own assessment: OK when reachable, DEGRADED when the server
    answered with an HTTP error (reachable but rejecting, e.g. auth), DOWN
    when the endpoint could not be reached at all.
    """
    base_url = str(getattr(backend, "_base_url", "") or "").rstrip("/") or _DEFAULT_BASE_URL
    api_key = str(getattr(backend, "_api_key", "") or "")
    model = str(getattr(backend, "model", "unknown") or "unknown")

    request = urllib.request.Request(f"{base_url}/models")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")

    tracker = BackendHealth.instance()
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        ms = (time.perf_counter() - start) * 1000
        tracker.record_success(detail=f"startup probe OK ({ms:.0f}ms)")
        return HealthState.OK, f"{model} reachable, {ms:.0f}ms"
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - start) * 1000
        tracker.record_failure(error=f"HTTP {e.code} from {base_url}", status_code=e.code)
        return HealthState.DEGRADED, f"{model} endpoint answered HTTP {e.code} ({ms:.0f}ms)"
    except Exception as e:
        ms = (time.perf_counter() - start) * 1000
        tracker.record_failure(error=f"{type(e).__name__}: {e}")
        return HealthState.DOWN, f"{model} unreachable: {e} ({ms:.0f}ms)"
