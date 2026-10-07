"""OpenAI-compatible API backend for LLM coaching.

Handles both online (mtgacoach.com) and local (Ollama/LM Studio) modes
through the same OpenAI-compatible chat completions interface.
"""

import logging
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass

from arenamcp.backend_health import BACKEND_ERROR_PREFIX, BackendHealth
from arenamcp.backends.health import (
    CIRCUIT_FAILURE_STATUS_CODES,
    CircuitBreaker,
    circuit_enabled,
    get_circuit,
)
from arenamcp.client_metadata import get_client_headers

logger = logging.getLogger(__name__)


# Closed think-tag blocks inside reasoning text (DeepSeek/Qwen style).
_THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL)

# Leading phrases that mark deliberation rather than a final answer.
_COT_MARKERS = (
    "okay,",
    "okay ",
    "ok,",
    "ok ",
    "hmm",
    "wait,",
    "let me",
    "let's",
    "i need to",
    "i should",
    "we need",
    "we should",
    "the user",
    "first,",
    "so the",
    "thinking about",
    "looking at",
)

# Phrases that make text a CONTINUATION of something the listener never heard.
#
# A different category from _COT_MARKERS above, and the reason bug
# 20260729_225652 spoke "Alternatively, we could just pass. But we have a land
# drop available and a shell we can cast." — 93 chars salvaged from 286 chars of
# chain-of-thought at 22:56:33. Nothing in _COT_MARKERS starts with
# "alternatively", so a deliberation fragment passed the guard and was spoken at
# the start of a turn, referring to an option the user was never told about.
#
# The rule is principled rather than a list of observed failures: advice whose
# first word presupposes a preceding alternative is broken by construction,
# because the user hears only the final answer. Weighing two lines out loud is
# what the reasoning channel is for and exactly what must not reach the speaker.
#
# Rejecting costs a fallback-advice turn, which is the cheaper failure — see the
# docstring below.
_ORPHAN_REFERENCE_MARKERS = (
    "alternatively",
    "conversely",
    "on the other hand",
    "then again",
    "that said",
    "that being said",
    "instead,",
    "otherwise,",
    "either way",
    "in that case",
    "however",
    "although",
    "though ",
    "but ",
    "or ",
    "and ",
    "also,",
    "besides,",
    "plus,",
    "actually,",
    "which means",
    "that means",
)


def _salvage_reasoning_answer(reasoning: str) -> str:
    """Extract a clean final answer from reasoning text, or return "".

    Some OpenAI-compatible servers put the entire response in the reasoning
    field when the visible content is empty. Speaking raw chain-of-thought
    to the user is worse than no advice, so this is deliberately
    conservative: strip closed think-tag blocks (the remainder is the real
    answer), reject truncated/unclosed thinking, and for tag-free text keep
    only a trailing paragraph that doesn't read like deliberation. An empty
    return sends the caller down the existing empty-advice fallback path.

    The asymmetry is deliberate: rejecting a salvageable answer costs one turn
    of deterministic fallback advice, while accepting deliberation speaks
    incoherent advice the user cannot act on and cannot see the premise for.
    When in doubt, return "".
    """
    text = (reasoning or "").strip()
    if not text:
        return ""
    stripped = _THINK_BLOCK_RE.sub("", text).strip()
    if "<think" in stripped.lower():
        # Unclosed think block — truncated chain-of-thought, no final answer.
        return ""
    if stripped and stripped != text:
        return stripped
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", stripped) if p.strip()]
    if not paragraphs:
        return ""
    candidate = paragraphs[-1]
    if _reads_like_deliberation(candidate):
        return ""
    if len(paragraphs) == 1 and len(candidate) > 1200:
        # One giant undifferentiated block reads as chain-of-thought.
        return ""
    return candidate


def _reads_like_deliberation(candidate: str) -> bool:
    """True when this text is thinking-out-loud rather than a final answer.

    Two independent signals, both keyed on the OPENING of the text, because that
    is where both failure modes are visible:

    * ``_COT_MARKERS`` — the text announces deliberation ("Let me check...").
    * ``_ORPHAN_REFERENCE_MARKERS`` — the text continues a comparison the user
      never heard ("Alternatively, we could just pass."). Advice cannot open by
      referring to an unstated alternative.
    """
    head = candidate.strip().lower()
    if not head:
        return True
    if any(head.startswith(m) for m in _COT_MARKERS):
        return True
    return any(head.startswith(m) for m in _ORPHAN_REFERENCE_MARKERS)


# Online mode: hardcoded API endpoint
ONLINE_BASE_URL = "https://api.mtgacoach.com/v1"

# Default local endpoint (vLLM). Ollama lives at :11434 if a user wants to fall back.
DEFAULT_LOCAL_URL = "http://localhost:8000/v1"
DEFAULT_LOCAL_MODEL = "gemma4:e2b"


class BackendError(Exception):
    """Typed API failure for consumers that must branch on error semantics.

    Replaces the "[BACKEND ERROR] ..." prose sentinel for callers
    that pass raise_on_error=True (the autopilot planner). Carries enough
    structure that retry policy lives HERE, not in string matching.
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_s: float | None = None,
        status_code: int | None = None,
        kind: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.status_code = status_code
        # "http", "connection", "timeout", "first_token_timeout",
        # "unavailable", "cancelled" or "other".
        self.kind = kind or ("http" if status_code is not None else "other")
        # For timeouts: did any reasoning/content token arrive first?
        # None when unknown (errors classified outside a request).
        self.first_token_seen: bool | None = None


class BackendUnavailable(BackendError):
    """The call was skipped without touching the network: the model server is down.

    ``reason`` is "circuit_open" (the shared circuit breaker is open; every
    call is skipped until a probe succeeds) or "backend_down" (only
    background calls are skipped: the endpoint has 3 fresh consecutive
    failures but the breaker has not opened yet). Never retryable, never
    recorded as a new failure. Callers should fall back to deterministic
    play/advice at once instead of treating this as an empty model answer.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_in_s: float | None = None,
        circuit_open: bool = True,
        reason: str = "circuit_open",
    ):
        super().__init__(message, retryable=False, kind="unavailable")
        self.circuit_open = circuit_open
        self.retry_in_s = retry_in_s
        self.reason = reason


class BackendCallCancelled(BackendError):
    """The call was cancelled by the client, not failed by the server.

    ``reason`` is "superseded" (a newer call of the same background class
    replaced it), "dropped" (the background lane stayed busy past the
    caller's wait), or "cancelled" (the caller's ``cancel_event`` was set).
    Never recorded as a backend failure.
    """

    def __init__(self, message: str, *, reason: str = "cancelled"):
        super().__init__(message, retryable=False, kind="cancelled")
        self.reason = reason


class FirstTokenTimeout(TimeoutError):
    """No reasoning or content token arrived within ``first_token_timeout_s``."""


class _CallCancelled(Exception):
    """Internal: unwinds a request whose handle was cancelled."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# Text markers inside "[BACKEND ERROR] ..." sentinels (raise_on_error=False).
UNAVAILABLE_MARKER = "model server unavailable"
CIRCUIT_OPEN_MARKER = "circuit open"
CANCELLED_MARKER = "request cancelled"


def _is_sentinel_with(value, marker: str) -> bool:
    return isinstance(value, str) and value.lstrip().startswith(BACKEND_ERROR_PREFIX) and marker in value


def is_backend_unavailable(value) -> bool:
    """True for a skipped-because-down result: the exception or its sentinel text.

    Lets callers tell "the model server is down, nothing was asked" apart
    from a real failure (a plain ``BackendError`` / sentinel) and from a real
    empty answer (``""``).
    """
    return isinstance(value, BackendUnavailable) or _is_sentinel_with(value, UNAVAILABLE_MARKER)


def is_call_cancelled(value) -> bool:
    """True for a cancelled/superseded/dropped result: the exception or its sentinel."""
    return isinstance(value, BackendCallCancelled) or _is_sentinel_with(value, CANCELLED_MARKER)


# HTTP statuses worth one bounded retry: transient gateway/origin trouble.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _classify_api_error(e: Exception) -> BackendError:
    """Wrap an SDK/network exception in a BackendError with retry semantics."""
    status = getattr(e, "status_code", None)
    retry_after: float | None = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        ra = body.get("retry_after")
        try:
            retry_after = float(ra) if ra is not None else None
        except (TypeError, ValueError):
            retry_after = None
    retryable = status in _RETRYABLE_STATUS_CODES
    kind = "http" if status is not None else "other"
    if status is None:
        # Connection-level failures (reset, refused, DNS, timeout, socket drop)
        err_type = type(e).__name__
        if isinstance(e, TimeoutError) or "Timeout" in err_type:
            kind = "timeout"
        retryable = any(
            k in err_type
            for k in (
                "Connection",
                "Connect",
                "Timeout",
                "Protocol",
                "Network",
                "RemoteDisconnected",
                "Transport",
            )
        )
        if retryable and kind != "timeout":
            kind = "connection"
    return BackendError(str(e), retryable=retryable, retry_after_s=retry_after, status_code=status, kind=kind)


def _counts_for_circuit(err: BackendError) -> bool:
    """Is this final call outcome evidence that the model server is unavailable?

    Counted: connection errors, 408/429/5xx/524, and timeouts with no first
    token. Not counted: 4xx auth/client errors, timeouts after the first
    token (slow but alive), cancellations, skips and anything unclassified.
    """
    if err.kind in ("cancelled", "unavailable"):
        return False
    if err.status_code is not None:
        return err.status_code in CIRCUIT_FAILURE_STATUS_CODES
    if err.kind in ("timeout", "first_token_timeout"):
        return not err.first_token_seen
    return err.kind == "connection"


def _litellm_already_retried(err: BackendError) -> bool:
    """LiteLLM (num_retries 1) already retried this 500: a client retry doubles the cost."""
    return "litellm retried" in str(err).lower()


def _field(value, key: str):
    """Read optional usage fields from SDK objects or compatible JSON objects."""
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _token_count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rejects_stream_usage(error: Exception) -> bool:
    """Older compatible servers can explicitly reject OpenAI's usage option."""
    if getattr(error, "status_code", None) not in (400, 422):
        return False
    detail = f"{error} {getattr(error, 'body', '')}".lower()
    return "stream_options" in detail or "include_usage" in detail


# ── Call classes ──────────────────────────────────────────────────────
#
# Every request carries a ``call_class`` label, logged in each
# "[PROXY] Request metrics" line, so load can be attributed per call site
# (2026-10-06: about 48% of model-slot time went to background calls that
# could only be attributed through log-adjacency heuristics). Callers pass
# one of these; any other string is accepted and logged as given. When a
# caller passes none, the label is "auto.<module>.<function>" of the first
# caller frame outside the proxy ("auto.unknown" from an executor thread).
#
# A class starting with "background." (or a call with background=True) is
# background work: it gets BACKGROUND_PRIORITY on the GLM/vLLM path, is
# skipped while the endpoint is down, and runs in the process-wide
# background lane (one background call in flight at a time).
BACKGROUND_CLASS_PREFIX = "background."
CALL_CLASSES: dict[str, str] = {
    "decision.typed": "autopilot typed bridge decision (action_planner._llm_decision_options)",
    "decision.plan": "autopilot/coach plan_actions",
    "decision.block_repair": "plan_actions block-repair second call",
    "decision.pay": "plan_pay_or_decline",
    "decision.mulligan": "mulligan decision",
    "decision.native_mac": "native-Mac autopilot planner",
    "coach.advice": "coach get_advice, including threat alerts",
    "coach.question": "user question / conversation mode",
    "coach.deck_brief": "on-demand deck strategy brief",
    "coach.win_prob": "win probability",
    "coach.sideboard": "sideboard advice",
    "coach.postmatch": "post-match analysis and rating calls",
    "draft.pick": "draft pick advice",
    "draft.build": "draft/sealed deck build",
    "background.game_plan": "GamePlanManager reform",
    "background.deck_playbook": "deck playbook discovery + compile",
    "background.win_plan": "automatic win-in-N worker",
    "background.set_primer": "set primer generation",
    "background.postmatch": "automatic post-match analysis",
    "vision": "image completions (complete_with_image)",
    "probe": "1-token inference probe (startup check, circuit breaker)",
}

# Frames from these modules are never the "caller" of an auto-labelled call.
_LABEL_SKIP_MODULES = (
    "arenamcp.backends.proxy",
    "arenamcp.backends.health",
    "concurrent.futures",
    "threading",
    "functools",
    "contextlib",
)


def _auto_call_class() -> str:
    """'auto.<module>.<function>' of the first caller outside the proxy."""
    try:
        frame = sys._getframe(1)
    except ValueError:  # pragma: no cover - no caller frame
        return "auto.unknown"
    while frame is not None:
        module = str(frame.f_globals.get("__name__", ""))
        if module and not module.startswith(_LABEL_SKIP_MODULES):
            short = module[len("arenamcp.") :] if module.startswith("arenamcp.") else module
            return f"auto.{short}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "auto.unknown"


def _resolve_call_class(call_class) -> str:
    if isinstance(call_class, str) and call_class.strip():
        return call_class.strip()
    return _auto_call_class()


# ── Request priority (vLLM --scheduling-policy priority) ──────────────
#
# Lower runs sooner; the server default is 0. Verified 2026-10-06 that the
# LiteLLM gateway forwards the field to vLLM (priority 10 -> HTTP 200; a
# string -> HTTP 400 with vLLM's validation error after 14 s, so only real
# ints are ever sent). Background classes default to BACKGROUND_PRIORITY so
# decisions overtake them in vLLM's waiting queue. Priority only reorders
# the waiting queue; it does not evict running requests.
#
# ARENAMCP_LLM_PRIORITY=0 (or false/off/no) stops sending the field at all.
# If the server rejects it (vLLM restarted without priority scheduling
# answers 400), priority is disabled for the rest of the session and the
# request is retried once at once without it.
BACKGROUND_PRIORITY = 10
_PRIORITY_ENV = "ARENAMCP_LLM_PRIORITY"
_FALSE_VALUES = ("0", "false", "off", "no")
_priority_lock = threading.Lock()
_priority_rejected_reason: str | None = None
_invalid_priority_warned = False


def priority_enabled() -> bool:
    """True unless disabled by env or by a server rejection this session."""
    if _priority_rejected_reason is not None:
        return False
    return os.environ.get(_PRIORITY_ENV, "").strip().lower() not in _FALSE_VALUES


def reset_priority_state() -> None:
    """Forget a session-level priority rejection (tests, engine reload)."""
    global _priority_rejected_reason, _invalid_priority_warned
    with _priority_lock:
        _priority_rejected_reason = None
        _invalid_priority_warned = False


def _disable_priority(error: Exception) -> None:
    global _priority_rejected_reason
    with _priority_lock:
        first = _priority_rejected_reason is None
        _priority_rejected_reason = str(error)[:300] or type(error).__name__
    if first:
        logger.warning(
            "[PROXY] Server rejected the 'priority' field (%s); priority disabled for this "
            "session — check vLLM --scheduling-policy priority",
            str(error)[:200],
        )


def _rejects_priority(error: Exception) -> bool:
    if getattr(error, "status_code", None) not in (400, 422):
        return False
    return "priority" in f"{error} {getattr(error, 'body', '')}".lower()


def _warn_invalid_priority(value) -> None:
    global _invalid_priority_warned
    with _priority_lock:
        if _invalid_priority_warned:
            return
        _invalid_priority_warned = True
    logger.warning("[PROXY] Ignoring non-int priority %r (only ints are ever sent)", value)


def _sent_priority(params: dict | None) -> int | None:
    extra = (params or {}).get("extra_body") or {}
    value = extra.get("priority")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ── GLM reasoning effort ──────────────────────────────────────────────
#
# GLM-5.3-Flash accepts low/high/max. "medium" is not rejected: the chat
# template silently treats it as max, the slowest setting. Map it to "high"
# (the nearest real level) and say so once.
_medium_effort_warned = False


def _normalize_reasoning_effort(model: str, effort):
    global _medium_effort_warned
    if not isinstance(effort, str) or "glm" not in (model or "").lower():
        return effort
    if effort.strip().lower() != "medium":
        return effort
    if not _medium_effort_warned:
        _medium_effort_warned = True
        logger.warning("[PROXY] reasoning_effort='medium' means max on GLM; sending 'high' instead")
    return "high"


def _effective_effort(params: dict | None):
    """The effort actually requested, for metrics ("default" = template default)."""
    if not params:
        return None
    if params.get("reasoning_effort"):
        return params["reasoning_effort"]
    ctk = ((params.get("extra_body") or {}).get("chat_template_kwargs")) or {}
    if ctk.get("reasoning_effort"):
        return ctk["reasoning_effort"]
    return "default" if ctk.get("thinking") else None


def _content_chars(content) -> int:
    """Prompt text length; image parts are not counted."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(part.get("text") or "") for part in content if isinstance(part, dict))
    return 0


# ── Per-call cancellation and the background lane ─────────────────────


class _CallHandle:
    """Cancellation state for one complete() call.

    ``cancel()`` may come from another thread: the background lane when a
    newer call of the same class arrives, or the watcher of a caller's
    ``cancel_event`` (see complete()). It shuts the call's socket down, which
    wakes a request thread blocked waiting for response headers or for the
    first chunk (2026-10-07 review: closing the openai Stream from another
    thread left the reader blocked until its 30-120 s read timeout, and the
    close itself blocked for 29 s once; a socket shutdown unblocked it in about
    1 s). The dropped connection also lets the server abort the request. The
    request thread then unwinds with the cancel reason.
    """

    def __init__(self, call_class: str, external: threading.Event | None = None):
        self.call_class = call_class
        self._external = external
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None
        self._stream = None
        # The httpcore network stream this call last read or wrote (see
        # _TrackingNetworkBackend); None outside the call.
        self._network = None

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._reason is None:
                self._reason = reason
            self._event.set()
            stream = self._stream
            network = self._network
        if network is not None and _shutdown_network_stream(network):
            # The request thread wakes with a read error, sees the cancel and
            # closes its own stream.
            return
        if stream is not None:
            # No socket to shut down (tracking unavailable): closing the SDK
            # stream may block while the reader holds it, so never on this
            # (often the lane's or a watcher's) thread.
            threading.Thread(
                target=_close_quietly, args=(stream,), name="llm-stream-close", daemon=True
            ).start()

    @property
    def is_cancelled(self) -> bool:
        """Set by cancel() (not by a caller's cancel_event until its watcher fires)."""
        return self._event.is_set()

    def cancelled(self) -> str | None:
        if self._event.is_set():
            return self._reason or "cancelled"
        if self._external is not None and self._external.is_set():
            with self._lock:
                if self._reason is None:
                    self._reason = "cancelled"
                return self._reason
        return None

    def attach_stream(self, stream) -> None:
        with self._lock:
            self._stream = stream
            already = self._event.is_set()
        if already:
            _close_quietly(stream)

    def detach_stream(self) -> None:
        with self._lock:
            self._stream = None

    def note_network(self, network) -> None:
        """Remember the connection this call is using (called on each socket read/write)."""
        if self._network is network:
            return
        with self._lock:
            self._network = network
            cancelled = self._event.is_set()
        if cancelled:
            _shutdown_network_stream(network)

    def detach_network(self) -> None:
        """The call is over: its pooled connection may now serve another call."""
        with self._lock:
            self._network = None


# The _CallHandle of the complete() call running on this thread, if any. The
# sync httpx client does all socket I/O on the calling thread, so the tracking
# network backend can attribute a connection to the call using it.
_ACTIVE_CALL = threading.local()


def _shutdown_network_stream(network) -> bool:
    """Shut a connection's socket down for reading and writing; False if it can't be."""
    try:
        sock = network.get_extra_info("socket")
    except Exception:
        sock = None
    if sock is None:
        return False
    try:
        # socket.socket.shutdown, not SSLSocket.shutdown: the latter drops the
        # SSL object under a reader that may be inside it.
        socket.socket.shutdown(sock, socket.SHUT_RDWR)
    except (OSError, TypeError, ValueError):
        return False
    return True


try:  # httpcore ships with openai/httpx
    import httpcore as _httpcore

    _NetworkStreamBase: type = _httpcore.NetworkStream
    _NetworkBackendBase: type = _httpcore.NetworkBackend
except Exception:  # pragma: no cover - only without httpcore
    _httpcore = None
    _NetworkStreamBase = object
    _NetworkBackendBase = object


class _TrackedNetworkStream(_NetworkStreamBase):
    """A pass-through httpcore stream that tells the active call which socket it uses."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def _note(self) -> None:
        handle = getattr(_ACTIVE_CALL, "handle", None)
        if handle is not None:
            handle.note_network(self._inner)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self._note()
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._note()
        return self._inner.write(buffer, timeout)

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context, server_hostname: str | None = None, timeout: float | None = None):
        return _TrackedNetworkStream(self._inner.start_tls(ssl_context, server_hostname, timeout))

    def get_extra_info(self, info: str):
        return self._inner.get_extra_info(info)


class _TrackingNetworkBackend(_NetworkBackendBase):
    """Wraps the pool's network backend so every new connection is a _TrackedNetworkStream."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def connect_tcp(self, *args, **kwargs):
        return _TrackedNetworkStream(self._inner.connect_tcp(*args, **kwargs))

    def connect_unix_socket(self, *args, **kwargs):
        return _TrackedNetworkStream(self._inner.connect_unix_socket(*args, **kwargs))

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _install_cancel_tracking(openai_client) -> bool:
    """Route the SDK's connections through _TrackingNetworkBackend (best effort).

    Reaches into httpx/httpcore internals (``Client._transport._pool
    ._network_backend``, read when each new connection is made). If they
    change shape, cancellation falls back to closing the SDK stream.
    """
    if _httpcore is None:
        return False
    http_client = getattr(openai_client, "_client", None)
    transports = [getattr(http_client, "_transport", None)]
    mounts = getattr(http_client, "_mounts", None)
    if isinstance(mounts, dict):
        transports.extend(mounts.values())
    installed = False
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            continue
        if not isinstance(backend, _TrackingNetworkBackend):
            pool._network_backend = _TrackingNetworkBackend(backend)
        installed = True
    if not installed:
        logger.debug("[PROXY] Socket-level cancellation unavailable; cancel closes the stream instead")
    return installed


def _watch_cancel_event(event: threading.Event, handle: _CallHandle, done: threading.Event) -> None:
    """Cancel ``handle`` as soon as the caller sets ``event`` (until ``done``)."""
    while not done.is_set():
        if event.wait(0.1):
            if not done.is_set():
                handle.cancel("cancelled")
            return


def _close_quietly(stream) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            logger.debug("[PROXY] Failed to close stream", exc_info=True)


@dataclass
class _CallContext:
    """Per-call settings shared by complete(), its attempts and its metrics."""

    call_class: str
    max_tokens: int | None
    budget_s: float | None
    handle: _CallHandle
    first_token_timeout_s: float | None = None
    priority: int | None = None
    first_token_at: float | None = None


# ARENAMCP_LLM_BACKGROUND_LANE=0 lets background calls run concurrently again.
_LANE_ENV = "ARENAMCP_LLM_BACKGROUND_LANE"
# Default bound on how long a background call waits for the lane, and the
# least budget worth starting a request with after waiting.
_LANE_DEFAULT_WAIT_S = 30.0
_LANE_MIN_RUN_S = 2.0


def background_lane_enabled() -> bool:
    return os.environ.get(_LANE_ENV, "").strip().lower() not in _FALSE_VALUES


class _BackgroundLane:
    """At most one background call in flight across every ProxyBackend.

    2026-10-06: decision calls overlapping one other call ran p50 3.1 s vs
    1.8 s alone (p90 5.6 vs 3.2), and background calls overlapped each other
    26 times. Deck analysis and the win-in-N worker build their own backends,
    so the lane is module-level, not per instance.

    A newer call of the SAME class supersedes the in-flight (or waiting)
    one: it is cancelled (its socket shut down) and the newer call takes the
    lane at once, without waiting for the old request thread to unwind. A
    call of a different class waits up to its lane wait, then is dropped.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._holder: _CallHandle | None = None
        self._waiting: list[_CallHandle] = []

    def acquire(self, handle: _CallHandle, wait_s: float) -> str:
        """Return "acquired", "dropped", "superseded" or "cancelled"."""
        end = time.monotonic() + max(0.0, wait_s)
        with self._cond:
            to_cancel = [
                other
                for other in ([self._holder] if self._holder is not None else []) + self._waiting
                if other.call_class == handle.call_class
            ]
        for other in to_cancel:
            logger.info("[PROXY] Background %s superseded by a newer call", other.call_class)
            other.cancel("superseded")
        with self._cond:
            self._cond.notify_all()
            self._waiting.append(handle)
            try:
                while True:
                    reason = handle.cancelled()
                    if reason:
                        return reason
                    if self._holder is None or self._holder.is_cancelled:
                        # A cancelled holder's connection is already shut
                        # down; its thread releases a lane it no longer holds.
                        self._holder = handle
                        return "acquired"
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        return "dropped"
                    # Short slices so a caller's cancel_event is noticed.
                    self._cond.wait(min(remaining, 0.25))
            finally:
                self._waiting.remove(handle)

    def release(self, handle: _CallHandle) -> None:
        with self._cond:
            if self._holder is handle:
                self._holder = None
                self._cond.notify_all()

    def holder_class(self) -> str | None:
        with self._cond:
            return self._holder.call_class if self._holder is not None else None


_BACKGROUND_LANE = _BackgroundLane()


def _lane_wait_s(lane_wait_s, deadline: float) -> float:
    wait = _LANE_DEFAULT_WAIT_S if lane_wait_s is None else max(0.0, float(lane_wait_s))
    return max(0.0, min(wait, deadline - time.perf_counter() - _LANE_MIN_RUN_S))


def _read_timeout(total: float, read: float):
    """A socket timeout whose READ limit is shorter than the overall budget.

    Before the first token nothing arrives on the socket, so a short read
    timeout detects a saturated server in seconds instead of after the whole
    budget (18:52:59 and 18:53:29 on 2026-10-06 got no token in 30 s).
    Afterwards chunks arrive every few ms, so the same limit only catches a
    stalled stream.
    """
    try:
        import httpx

        return httpx.Timeout(total, read=read)
    except Exception:  # pragma: no cover - httpx ships with openai
        return total


def _positive_or_none(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _circuit_gate(
    breaker: CircuitBreaker, is_background: bool
) -> tuple[BackendUnavailable | None, int | None]:
    """(BackendUnavailable, None) when this call must not go out, else (None, admission token)."""
    if not circuit_enabled():
        return None, breaker.call_token()
    # Background first: it must never consume a half-open trial slot.
    if is_background and breaker.background_blocked():
        snap = breaker.snapshot()
        if snap["state"] == "open":
            return BackendUnavailable(
                f"{UNAVAILABLE_MARKER} ({CIRCUIT_OPEN_MARKER}; retry in {snap['retry_in_s'] or 0:.0f}s)",
                retry_in_s=snap["retry_in_s"],
            ), None
        return BackendUnavailable(
            f"{UNAVAILABLE_MARKER} (backend down; background call skipped)",
            circuit_open=False,
            reason="backend_down",
        ), None
    token = breaker.admit()
    if token is None:
        retry_in = breaker.snapshot()["retry_in_s"]
        return BackendUnavailable(
            f"{UNAVAILABLE_MARKER} ({CIRCUIT_OPEN_MARKER}; retry in {retry_in or 0:.0f}s)",
            retry_in_s=retry_in,
        ), None
    return None, token


class ProxyBackend:
    """LLM backend using OpenAI-compatible chat completions API.

    In online mode, routes through api.mtgacoach.com with the user's
    license key. In local mode, connects to a user-configured endpoint
    (Ollama, LM Studio, or any OpenAI-compatible server).
    """

    def __init__(
        self,
        model: str = "glm-5.3-flash",
        enable_thinking: bool = False,
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.model = model
        self.enable_thinking = enable_thinking
        self._base_url = base_url
        self._api_key = api_key
        self._client = None
        self._client_lock = threading.Lock()
        # What the server said it actually ran (R4: gateway aliases lie).
        self.last_served_model: str | None = None
        self._served_model_warned = False
        self._stream_usage_supported = True
        self.last_request_metrics: dict | None = None

        # Fire-and-forget warmup for any local backend to pre-load weights/KV cache
        self._local_warmup()

    @classmethod
    def create_online(cls, model: str | None = None, license_key: str = "") -> "ProxyBackend":
        """Create a backend configured for online mode (mtgacoach.com)."""
        # Hosted service currently runs one model. Migrate saved selections from
        # retired aliases; custom providers still keep their explicit model.
        resolved_model = "glm-5.3-flash"
        return cls(
            model=resolved_model,
            base_url=ONLINE_BASE_URL,
            api_key=license_key,
        )

    @classmethod
    def create_local(
        cls,
        model: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
    ) -> "ProxyBackend":
        """Create a backend configured for local mode (vLLM/Ollama/LM Studio)."""
        return cls(
            model=model or DEFAULT_LOCAL_MODEL,
            base_url=url or DEFAULT_LOCAL_URL,
            api_key=api_key or "vllm",
        )

    # Hard ceiling applied at the SDK level. Per-call request_timeout_s in
    # complete() can tighten this. Without a finite client-level timeout,
    # the OpenAI SDK defaults to ~10 minutes — which means a hung backend
    # leaves the worker thread alive long after the future times out, and
    # that's the thread leak the autopilot+coach were paying for.
    _CLIENT_HARD_TIMEOUT_S = 60.0

    def _get_client(self):
        """Create one HTTP client even when startup and warmup race."""
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from openai import OpenAI

                url = self._base_url or DEFAULT_LOCAL_URL
                key = self._api_key or "ollama"
                client_headers = get_client_headers() if url == ONLINE_BASE_URL else None

                # max_retries=0: the SDK's default (2) honors Retry-After
                # headers between attempts, and those sleeps are NOT capped
                # by the request timeout — a gateway 502 with Retry-After: 60
                # wedged one decision call for 2+ minutes while the mulligan
                # window flew by (2026-07-01). Real-time coaching would rather
                # fail fast and let the trigger/fallback layer decide.
                client = OpenAI(
                    base_url=url,
                    api_key=key,
                    default_headers=client_headers,
                    timeout=self._CLIENT_HARD_TIMEOUT_S,
                    max_retries=0,
                )
                try:
                    _install_cancel_tracking(client)
                except Exception:  # never let an internals change break the client
                    logger.debug("[PROXY] Could not install cancel tracking", exc_info=True)
                self._client = client
            except ImportError:
                raise ImportError("openai package required: pip install openai")
        return self._client

    def prepare_client(self) -> None:
        """Initialize the HTTP SDK without inference or a readiness probe.

        Successful preparation means the local client is configured, not that
        the remote model is warm or healthy. Requests still determine health.
        """
        self._get_client()

    def probe_connection(self, timeout: float = 5.0) -> None:
        """Check model discovery using the same auth/headers as inference.

        This is a single GET, never a completion or a model warmup. It does
        not prove inference works (LiteLLM answers it with vLLM dead) and
        never touches the circuit breaker; use probe_inference() for that.
        """
        self._get_client().with_options(timeout=timeout, max_retries=0).models.list()

    def _local_warmup(self) -> None:
        """Send a minimal warmup request to a local backend in a background thread.

        Fires for any non-online endpoint (vLLM/Ollama/LM Studio/etc.) so the
        first real coach call doesn't pay the cold-start cost.
        """
        if os.environ.get("ARENAMCP_ENGINE_RELOAD") == "1":
            logger.debug("[PROXY] Engine reload: skipping synthetic model warmup")
            return
        url = self._base_url or ""
        if not url or url == ONLINE_BASE_URL:
            return
        # Only warm up obvious local URLs to avoid surprising arbitrary endpoints.
        is_local = "localhost" in url or "127.0.0.1" in url or url.startswith("http://0.0.0.0")
        if not is_local:
            return

        def _warmup():
            try:
                client = self._get_client()
                client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": "hi"}],
                    max_tokens=1,
                )
                logger.info(f"[PROXY] Local warmup complete for model {self.model}")
            except Exception as e:
                logger.debug(f"[PROXY] Local warmup failed (non-fatal): {e}")

        t = threading.Thread(target=_warmup, daemon=True)
        t.start()

    def complete(
        self,
        system_prompt: str,
        user_message: str,
        # 4096 (was 1500): thinking-variant local models (gemma-4-31b-it)
        # burn hidden reasoning tokens inside this cap before any visible
        # output — 1500 returned EMPTY advice on large game-state prompts
        # (same failure as the eval harness hit 2026-06-09). A cap, not a
        # target: online models are unaffected.
        max_tokens: int = 4096,
        temperature: float = 0.3,
        request_timeout_s: float | None = None,
        raise_on_error: bool = False,
        response_format: dict | None = None,
        background: bool = False,
        enable_thinking: bool | None = None,
        reasoning_effort: str | None = None,
        *,
        call_class: str | None = None,
        priority: int | None = None,
        first_token_timeout_s: float | None = None,
        cancel_event: threading.Event | None = None,
        lane_wait_s: float | None = None,
    ) -> str:
        """Get completion from the API endpoint.

        Args:
            temperature: Sampling temperature. Default 0.3 for flavorful
                coach advice; pass 0.0 for deterministic planner calls
                (avoids cross-priority-window flip-flops).
            request_timeout_s: Overall request budget, shared by retries and
                compatibility fallbacks, capped by the client ceiling. Each
                HTTP request gets the remaining budget as its socket timeout;
                streaming also checks elapsed time between chunks. Socket
                timeouts bound stalled reads, not an exact wall-clock cutoff.
            background: Opt into a 120-second ceiling for setup analysis;
                ordinary tactical requests retain their existing ceiling.
                Also marks the call as background work (see call_class).
            enable_thinking: Per-request reasoning override; does not alter
                the backend default for subsequent tactical requests.
            reasoning_effort: GLM effort for this request: "low", "high" or
                "max". GLM-5.3 has no "medium" (the template silently runs it
                as max), so "medium" is sent as "high". Unrestricted thinking
                can outlast a turn.
            response_format: Optional structured output schema for callers
                that also validate the returned JSON.
            raise_on_error: Re-raise API errors instead of returning the
                "[BACKEND ERROR] ..." sentinel string. The autopilot
                planner sets this — during the 2026-07-05 gateway outage the
                sentinel was fed to the JSON parser, parsed to 0 actions, and
                the fallback then SUBMITTED real passes on windows with
                castable spells. Sentinel-string returns are only safe for
                consumers that display text to a human.
            call_class: Label for metrics and policy, one of CALL_CLASSES
                (e.g. "decision.typed", "coach.advice",
                "background.game_plan"). Omitted: "auto.<module>.<function>"
                of the caller. A "background." class is background work:
                priority BACKGROUND_PRIORITY, skipped while the endpoint is
                down, and serialized in the background lane.
            priority: vLLM scheduling priority (int only; lower = sooner,
                server default 0). Overrides the class default. Sent only on
                the GLM/vLLM path and only while priority is enabled.
            first_token_timeout_s: Fail the attempt when no reasoning or
                content token arrives within this many seconds (status
                "ttft_timeout", no retry). Detects a saturated server long
                before the full budget runs out.
            cancel_event: Set it to abandon the call. A watcher thread cancels
                the request within about 0.1 s (socket shut down, so a call
                still queued on the server is dropped too). The result is
                BackendCallCancelled / its sentinel.
            lane_wait_s: Background calls only: how long to wait for the
                background lane before being dropped (default 30 s, bounded
                by the budget). 0 = drop at once when the lane is busy.

        Skips and cancellations: while the shared circuit breaker is open,
        the call returns at once without touching the network, raising
        BackendUnavailable (raise_on_error) or returning
        "[BACKEND ERROR] model server unavailable (circuit open; retry in Ns)".
        Cancelled/superseded/dropped calls raise BackendCallCancelled or
        return "[BACKEND ERROR] request cancelled (<reason>)". Neither is
        recorded as a backend failure. Use is_backend_unavailable() and
        is_call_cancelled() to recognise them.
        """
        thinking_enabled = self.enable_thinking if enable_thinking is None else enable_thinking
        request_started = time.perf_counter()
        # Full deck analysis is off the tactical path and can need longer than
        # a live action window. Opt in per request; never mutate the shared
        # client's timeout or relax the default decision budget.
        budget = 120.0 if background else self._CLIENT_HARD_TIMEOUT_S
        if request_timeout_s is not None:
            budget = min(budget, max(0.0, request_timeout_s))
        deadline = request_started + budget
        resolved_class = _resolve_call_class(call_class)
        is_background = bool(background) or resolved_class.startswith(BACKGROUND_CLASS_PREFIX)
        call = _CallContext(
            call_class=resolved_class,
            max_tokens=max_tokens,
            budget_s=budget,
            handle=_CallHandle(resolved_class, cancel_event),
            first_token_timeout_s=_positive_or_none(first_token_timeout_s),
        )
        lane_held = False
        watch_done: threading.Event | None = None
        try:
            breaker = self._circuit()
            skipped, token = _circuit_gate(breaker, is_background)
            if skipped is not None:
                return self._finish_skipped(
                    skipped,
                    call,
                    request_started=request_started,
                    input_chars=len(system_prompt or "") + len(user_message or ""),
                    raise_on_error=raise_on_error,
                )

            client = self._get_client()
            params = self._build_params(
                system_prompt,
                user_message,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                thinking_enabled=thinking_enabled,
                reasoning_effort=_normalize_reasoning_effort(self.model, reasoning_effort),
            )
            call.priority = self._resolve_priority(priority, is_background)
            if call.priority is not None:
                params.setdefault("extra_body", {})["priority"] = call.priority
            if cancel_event is not None:
                watch_done = threading.Event()
                threading.Thread(
                    target=_watch_cancel_event,
                    args=(cancel_event, call.handle, watch_done),
                    name="llm-cancel-watch",
                    daemon=True,
                ).start()

            if is_background and background_lane_enabled():
                outcome = _BACKGROUND_LANE.acquire(call.handle, _lane_wait_s(lane_wait_s, deadline))
                if outcome != "acquired":
                    return self._finish_cancelled(
                        outcome,
                        call,
                        params,
                        request_started=request_started,
                        attempt=0,
                        raise_on_error=raise_on_error,
                    )
                lane_held = True
                # The endpoint may have gone down while this call waited.
                skipped, token = _circuit_gate(breaker, is_background)
                if skipped is not None:
                    return self._finish_skipped(
                        skipped,
                        call,
                        request_started=request_started,
                        input_chars=len(system_prompt or "") + len(user_message or ""),
                        raise_on_error=raise_on_error,
                    )

            # Bounded retry: one extra attempt for transient failures
            # (429/5xx/connection). A 60s Retry-After is useless mid-match —
            # skip the retry entirely when the server asks for a long wait.
            # Also skipped when LiteLLM already retried, after a first-token
            # timeout (the server is saturated), and unless the endpoint is
            # healthy (breaker closed and out of probation).
            last_err: BackendError | None = None
            attempt = 0
            retried = False
            priority_dropped = False
            while True:
                attempt += 1
                reason = call.handle.cancelled()
                if reason:
                    return self._finish_cancelled(
                        reason,
                        call,
                        params,
                        request_started=request_started,
                        attempt=attempt - 1,
                        raise_on_error=raise_on_error,
                    )
                try:
                    result = self._complete_once(
                        client,
                        params,
                        request_started=request_started,
                        deadline=deadline,
                        attempt=attempt,
                        call=call,
                    )
                except _CallCancelled as cancelled:
                    return self._finish_cancelled(
                        cancelled.reason,
                        call,
                        params,
                        request_started=request_started,
                        attempt=attempt,
                        raise_on_error=raise_on_error,
                    )
                except Exception as e:
                    if not priority_dropped and _sent_priority(params) is not None and _rejects_priority(e):
                        # Retry once at once without the field; never again
                        # this session (vLLM without priority scheduling).
                        priority_dropped = True
                        _disable_priority(e)
                        params["extra_body"].pop("priority", None)
                        call.priority = None
                        continue
                    err = self._classify_call_error(e, call)
                    if (
                        not retried
                        and err.retryable
                        and err.kind != "first_token_timeout"
                        and (err.retry_after_s is None or err.retry_after_s <= 5.0)
                        and not _litellm_already_retried(err)
                        and breaker.retry_allowed()
                    ):
                        wait = min(err.retry_after_s or 0.5, 1.0)
                        if deadline - time.perf_counter() <= wait:
                            last_err = err
                            break
                        retried = True
                        logger.warning(f"API error (retryable): {e} — one retry in {wait:.1f}s")
                        time.sleep(wait)
                        continue
                    last_err = err
                    break
                breaker.record_success(token=token)
                BackendHealth.instance().record_success()
                return result
            last_err = last_err or BackendError("unknown API failure")
            self._record_request_metrics(
                params,
                request_started=request_started,
                attempt=attempt,
                status="ttft_timeout" if last_err.kind == "first_token_timeout" else "error",
                first_token_at=call.first_token_at,
                call=call,
            )
            logger.error(f"API error: {last_err}")
            if _counts_for_circuit(last_err):
                breaker.record_failure(str(last_err), token=token)
            BackendHealth.instance().record_failure(error=str(last_err), status_code=last_err.status_code)
            if raise_on_error:
                raise last_err
            return f"{BACKEND_ERROR_PREFIX} {last_err}"
        except BackendError:
            raise
        except Exception as e:
            # Setup failures (client init, params construction).
            logger.error(f"API error: {e}")
            BackendHealth.instance().record_failure(error=str(e), status_code=getattr(e, "status_code", None))
            if raise_on_error:
                raise _classify_api_error(e) from e
            return f"{BACKEND_ERROR_PREFIX} {e}"
        finally:
            if watch_done is not None:
                watch_done.set()
            if lane_held:
                _BACKGROUND_LANE.release(call.handle)

    def _build_params(
        self,
        system_prompt: str,
        user_message: str,
        *,
        max_tokens: int,
        temperature: float,
        response_format: dict | None,
        thinking_enabled: bool,
        reasoning_effort: str | None,
    ) -> dict:
        """Chat-completion params for this model; transport options are added per attempt."""
        params = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
        }
        if response_format is not None:
            params["response_format"] = response_format

        model_lower = self.model.lower()
        is_gpt5 = (
            "gpt-5" in model_lower or "gpt5" in model_lower or "o1" in model_lower or "o3" in model_lower
        )
        is_gemini = "gemini" in model_lower

        if is_gpt5:
            params["max_completion_tokens"] = max_tokens
        else:
            params["max_tokens"] = max_tokens

        extra = {}
        # GLM-5.3 (the engine now behind the dsv4/deepseek-v4-flash
        # aliases via serve-glm53-blackwell.sh) ALWAYS deliberates: with
        # `thinking:false` the reasoning is not separated into
        # reasoning_content and leaks inline into the spoken advice —
        # observed live 2026-08-28/29 as the model echoing the QUICK-mode
        # prompt instructions ("One sentence under 15 words") into the
        # TTS text, plus fragmented content. With thinking=true +
        # reasoning_effort=low the glm45 reasoning parser separates the
        # deliberation (the coach never reads reasoning_content) and the
        # visible content is a clean short command; interleaved bench
        # through the gateway (2026-08-29): ~0.2-0.4s warm, ~2.9s cold
        # 25k-char prompt, vs 3-11s on high-effort defaults.
        # enable_thinking=True keeps plain {"thinking": true} like before.
        # CAVEAT: if a real DeepSeek-dialect dsv4 container returns to
        # :8002, the low-effort kwargs hurt it (dsv4 slowed with
        # thinking=true — the 2026-07-29 p50 6134ms bug); re-gate on the
        # served engine then.
        if reasoning_effort:
            extra["chat_template_kwargs"] = {"thinking": True, "reasoning_effort": reasoning_effort}
        elif thinking_enabled:
            extra["chat_template_kwargs"] = {"thinking": True}
        else:
            extra["chat_template_kwargs"] = {"thinking": True, "reasoning_effort": "low"}
        if thinking_enabled:
            if "claude" in model_lower:
                extra["thinking"] = {"type": "enabled", "budget_tokens": 8000}
                if "max_completion_tokens" in params:
                    params["max_completion_tokens"] = max_tokens + 8000
                else:
                    params["max_tokens"] = max_tokens + 8000
            elif is_gemini:
                # Gemini's OpenAI-compat endpoint rejects native fields
                # like thinking_config. Use reasoning_effort instead.
                params["reasoning_effort"] = "medium"
            elif is_gpt5:
                params["reasoning_effort"] = "medium"
                params["verbosity"] = "medium"
        else:
            # (legacy `think: false` Ollama spelling removed — no alias
            # routes to Ollama anymore, and `think: false` would
            # contradict chat_template_kwargs.thinking=true on GLM)
            if "claude" in model_lower:
                extra["thinking"] = {"type": "disabled"}
            if is_gemini:
                params["reasoning_effort"] = "none"
            if is_gpt5:
                params["reasoning_effort"] = "minimal"
                params["verbosity"] = "low"
        # GPT-5 reasoning models reject any temperature other than 1.0 when
        # reasoning_effort is set. Drop the param so Azure uses the default.
        if is_gpt5:
            params.pop("temperature", None)
        if extra:
            params["extra_body"] = extra
        return params

    def _priority_supported(self) -> bool:
        """Only the gateway's GLM path runs vLLM with priority scheduling."""
        return "glm" in str(getattr(self, "model", "") or "").lower()

    def _resolve_priority(self, priority, is_background: bool) -> int | None:
        """The priority to send, or None to omit the field."""
        if not self._priority_supported() or not priority_enabled():
            return None
        if priority is not None:
            if isinstance(priority, bool) or not isinstance(priority, int):
                _warn_invalid_priority(priority)
            else:
                return priority or None  # 0 is the server default: omit it
        return BACKGROUND_PRIORITY if is_background else None

    @staticmethod
    def _classify_call_error(e: Exception, call: _CallContext) -> BackendError:
        """BackendError plus whether a token arrived (first-token timeouts are their own kind)."""
        err = _classify_api_error(e)
        token_seen = call.first_token_at is not None
        ftt = call.first_token_timeout_s
        if isinstance(e, FirstTokenTimeout) or (ftt is not None and err.kind == "timeout" and not token_seen):
            err = BackendError(
                f"no first token within {ftt or 0:.1f}s ({type(e).__name__})",
                retryable=False,
                kind="first_token_timeout",
            )
        err.first_token_seen = token_seen
        return err

    def _finish_skipped(
        self,
        err: BackendUnavailable,
        call: _CallContext,
        *,
        request_started: float,
        input_chars: int,
        raise_on_error: bool,
    ) -> str:
        """Fast-fail without touching the network or the health tracker."""
        self._record_request_metrics(
            None,
            request_started=request_started,
            attempt=0,
            status="circuit_open" if err.circuit_open else "backend_down",
            call=call,
            input_chars=input_chars,
        )
        if raise_on_error:
            raise err
        return f"{BACKEND_ERROR_PREFIX} {err}"

    def _finish_cancelled(
        self,
        reason: str,
        call: _CallContext,
        params: dict | None,
        *,
        request_started: float,
        attempt: int,
        raise_on_error: bool,
    ) -> str:
        """A cancelled/superseded/dropped call: logged, never a backend failure."""
        self._record_request_metrics(
            params,
            request_started=request_started,
            attempt=attempt,
            status="dropped" if reason == "dropped" else "cancelled",
            first_token_at=call.first_token_at,
            call=call,
        )
        err = BackendCallCancelled(f"{CANCELLED_MARKER} ({reason})", reason=reason)
        if raise_on_error:
            raise err
        return f"{BACKEND_ERROR_PREFIX} {err}"

    # ── circuit breaker ───────────────────────────────────────────────

    # A probe is one 1-token completion; this bounds a probe that hangs.
    _PROBE_TIMEOUT_S = 8.0

    def _circuit(self) -> CircuitBreaker:
        """The breaker shared by every backend on this (base_url, model)."""
        breaker = get_circuit(
            getattr(self, "_base_url", None) or DEFAULT_LOCAL_URL,
            str(getattr(self, "model", "") or ""),
        )
        breaker.register_probe(self._circuit_probe)
        return breaker

    def available(self) -> bool:
        """False while the shared breaker is open: skip prompt building and model calls."""
        return self._circuit().available()

    def background_blocked(self) -> bool:
        """True while background work would be skipped: breaker open, or 3 fresh failures."""
        return self._circuit().background_blocked()

    def circuit_snapshot(self) -> dict:
        """Breaker state for UI/logs: state, retry_in_s, consecutive_failures, last_error, ..."""
        return self._circuit().snapshot()

    def _send_probe(self, timeout: float) -> None:
        """One 1-token chat completion through the inference path (raises on failure)."""
        started = time.perf_counter()
        params = self._build_params(
            "",
            "ping",
            max_tokens=1,
            temperature=0.0,
            response_format=None,
            thinking_enabled=False,
            reasoning_effort=None,
        )
        params["messages"] = [{"role": "user", "content": "ping"}]
        call = _CallContext(call_class="probe", max_tokens=1, budget_s=timeout, handle=_CallHandle("probe"))
        try:
            response = (
                self._get_client()
                .with_options(timeout=timeout, max_retries=0)
                .chat.completions.create(**params)
            )
        except Exception:
            self._record_request_metrics(
                params, request_started=started, attempt=1, status="error", call=call
            )
            raise
        served = getattr(response, "model", None)
        self._record_request_metrics(
            params,
            request_started=started,
            attempt=1,
            usage=getattr(response, "usage", None),
            served_model=served if isinstance(served, str) else None,
            call=call,
        )

    def _circuit_probe(self) -> bool:
        """Breaker probe: True when the model server answered.

        A 4xx other than 408/429 also counts as an answer: auth or
        configuration problems surface through real calls, not the breaker.
        """
        try:
            self._send_probe(self._PROBE_TIMEOUT_S)
        except Exception as e:
            err = _classify_api_error(e)
            if err.status_code is not None and err.status_code not in CIRCUIT_FAILURE_STATUS_CODES:
                return True
            raise
        BackendHealth.instance().record_success(detail="inference probe OK")
        return True

    def probe_inference(self, timeout: float = 8.0) -> tuple[bool, str]:
        """Startup/reload health check that exercises inference, not GET /models.

        Sends one 1-token completion. Records the outcome in BackendHealth
        and the shared breaker (a success closes it). Returns (ok, detail).
        Use this instead of check_gateway_health(): LiteLLM answers
        GET /models while vLLM is dead.
        """
        breaker = self._circuit()
        started = time.perf_counter()
        try:
            self._send_probe(timeout)
        except Exception as e:
            ms = (time.perf_counter() - started) * 1000
            err = _classify_api_error(e)
            err.first_token_seen = False
            if _counts_for_circuit(err):
                breaker.record_failure(str(err))
            BackendHealth.instance().record_failure(error=str(err), status_code=err.status_code)
            return False, f"{self.model}: inference probe failed after {ms:.0f}ms: {str(err)[:200]}"
        ms = (time.perf_counter() - started) * 1000
        breaker.record_success(detail=f"inference probe OK ({ms:.0f}ms)")
        BackendHealth.instance().record_success(detail=f"inference probe OK ({ms:.0f}ms)")
        return True, f"{self.model} inference OK, {ms:.0f}ms"

    def _note_served_model(self, served: str | None) -> None:
        """Record the model the server actually ran (gateway aliases lie).

        The 2026-07-05 misroute (alias 'nemotron-3-super' → an ollama 12B)
        was invisible because [PROXY] logs printed the configured alias.
        """
        if not served:
            return
        previous = self.last_served_model
        self.last_served_model = served
        if served != previous:
            # Discoverability: one INFO line on first use and on every
            # routing change, so "what model am I talking to?" is always
            # answerable from the logs without living in the UI.
            logger.info(f"[PROXY] gateway served model: {served}")
        if served != self.model and not getattr(self, "_served_model_warned", False):
            self._served_model_warned = True
            logger.warning(
                f"[PROXY] served model {served!r} != configured alias "
                f"{self.model!r} — routing is gateway-side"
            )

    def _record_request_metrics(
        self,
        params,
        *,
        request_started: float,
        attempt: int,
        status: str = "ok",
        streamed: bool = False,
        usage=None,
        served_model: str | None = None,
        first_token_at: float | None = None,
        first_content_at: float | None = None,
        finish_reason: str | None = None,
        call: "_CallContext | None" = None,
        input_chars: int | None = None,
    ) -> None:
        """Log counts and timing only; never prompts, credentials, or reasoning.

        status: "ok", "error", "ttft_timeout" (no first token in
        first_token_timeout_s), "cancelled", "dropped" (background lane
        busy), "circuit_open" or "backend_down" (skipped, nothing sent).
        """
        cached = _token_count(_field(_field(usage, "prompt_tokens_details"), "cached_tokens"))
        if cached is None:
            cached = _token_count(_field(usage, "cache_read_input_tokens"))
        if params is not None:
            input_chars = sum(_content_chars(message.get("content")) for message in params["messages"])
        max_tokens = call.max_tokens if call is not None else None
        if max_tokens is None and params is not None:
            max_tokens = params.get("max_tokens") or params.get("max_completion_tokens")
        metrics = {
            "model": self.model,
            "served_model": served_model,
            "status": status,
            "streamed": streamed,
            "attempts": attempt,
            "input_chars": input_chars,
            "input_tokens": _token_count(_field(usage, "prompt_tokens")),
            "output_tokens": _token_count(_field(usage, "completion_tokens")),
            "cached_input_tokens": cached,
            "reasoning_tokens": _token_count(
                _field(_field(usage, "completion_tokens_details"), "reasoning_tokens")
            ),
            "ttft_ms": round((first_token_at - request_started) * 1000, 1)
            if first_token_at is not None
            else None,
            "first_content_ms": round((first_content_at - request_started) * 1000, 1)
            if first_content_at is not None
            else None,
            "total_ms": round((time.perf_counter() - request_started) * 1000, 1),
            "finish_reason": finish_reason if isinstance(finish_reason, str) else None,
            "call_class": call.call_class if call is not None else None,
            "priority": _sent_priority(params),
            "reasoning_effort": _effective_effort(params),
            "max_tokens": max_tokens,
            "budget_s": round(call.budget_s, 1) if call is not None and call.budget_s is not None else None,
        }
        self.last_request_metrics = metrics
        logger.info("[PROXY] Request metrics: %s", metrics)

    def _complete_once(
        self,
        client,
        params,
        *,
        request_started: float | None = None,
        deadline: float | None = None,
        attempt: int = 1,
        call: _CallContext | None = None,
    ) -> str:
        """One attempt, sharing the caller's budget across compatible fallbacks."""
        if request_started is None:
            request_started = time.perf_counter()
        if deadline is None:
            deadline = request_started + self._CLIENT_HARD_TIMEOUT_S
        handle = call.handle if call is not None else None
        previous = getattr(_ACTIVE_CALL, "handle", None)
        _ACTIVE_CALL.handle = handle
        try:
            return self._complete_once_tracked(
                client, params, request_started=request_started, deadline=deadline, attempt=attempt, call=call
            )
        finally:
            _ACTIVE_CALL.handle = previous
            if handle is not None:
                handle.detach_network()

    def _complete_once_tracked(
        self,
        client,
        params,
        *,
        request_started: float,
        deadline: float,
        attempt: int,
        call: _CallContext | None,
    ) -> str:
        attempt_started = time.perf_counter()
        handle = call.handle if call is not None else None
        ftt = call.first_token_timeout_s if call is not None else None
        if call is not None:
            call.first_token_at = None

        def check_cancelled():
            reason = handle.cancelled() if handle is not None else None
            if reason:
                raise _CallCancelled(reason)

        def request(**options):
            check_cancelled()
            now = time.perf_counter()
            remaining = deadline - now
            if remaining <= 0:
                raise TimeoutError("LLM request time budget exhausted")
            timeout = remaining
            if ftt is not None and options.get("stream"):
                first_token_left = ftt - (now - attempt_started)
                if first_token_left <= 0:
                    raise FirstTokenTimeout(f"no first token within {ftt:.1f}s")
                if first_token_left < remaining:
                    timeout = _read_timeout(remaining, max(first_token_left, 0.5))
            return client.with_options(timeout=timeout).chat.completions.create(**params, **options)

        # Try streaming first. Usage is delivered in an extra chunk with no
        # choices; it must be collected separately from text/finish chunks.
        stream = None
        try:
            stream_options = (
                {"stream_options": {"include_usage": True}} if self._stream_usage_supported else {}
            )
            try:
                stream = request(stream=True, **stream_options)
            except Exception as error:
                if not stream_options or not _rejects_stream_usage(error):
                    raise
                self._stream_usage_supported = False
                logger.info("[PROXY] Endpoint rejects streamed usage; retaining streaming without usage")
                stream = request(stream=True)
            if handle is not None:
                handle.attach_stream(stream)
            chunks: list[str] = []
            reasoning_chunks: list[str] = []
            served_model = None
            usage = None
            finish_reason = None
            first_token_at = None
            first_content_at = None
            for chunk in stream:
                now = time.perf_counter()
                check_cancelled()
                if now >= deadline:
                    raise TimeoutError("LLM streaming time budget exhausted")
                if ftt is not None and first_token_at is None and now - attempt_started >= ftt:
                    raise FirstTokenTimeout(f"no first token within {ftt:.1f}s")
                if served_model is None and getattr(chunk, "model", None):
                    served_model = chunk.model
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                reasoning_token = (
                    getattr(delta, "reasoning_content", None)
                    or _field(getattr(delta, "model_extra", None), "reasoning")
                    or getattr(delta, "reasoning", None)
                )
                token = getattr(delta, "content", None)
                if isinstance(reasoning_token, str) and reasoning_token:
                    reasoning_chunks.append(reasoning_token)
                    if first_token_at is None:
                        first_token_at = now
                if isinstance(token, str) and token:
                    chunks.append(token)
                    if first_token_at is None:
                        first_token_at = now
                    if first_content_at is None:
                        first_content_at = now
                if call is not None and call.first_token_at is None and first_token_at is not None:
                    call.first_token_at = first_token_at

            self._note_served_model(served_model)
            content = "".join(chunks)
            if "</think>" in content:
                content = content.split("</think>")[-1].strip()
            if not content and reasoning_chunks:
                content = _salvage_reasoning_answer("".join(reasoning_chunks))
                if content:
                    logger.warning(
                        "[PROXY] Empty streamed content — using salvaged answer (%s chars)", len(content)
                    )
                else:
                    logger.warning(
                        "[PROXY] Empty streamed content with no clean final answer — using caller fallback"
                    )
            self._record_request_metrics(
                params,
                request_started=request_started,
                attempt=attempt,
                streamed=True,
                usage=usage,
                served_model=served_model,
                first_token_at=first_token_at,
                first_content_at=first_content_at,
                finish_reason=finish_reason,
                call=call,
            )
            return content or ""
        except (_CallCancelled, FirstTokenTimeout):
            raise
        except Exception as stream_err:
            # A stream closed by cancel() surfaces as some read error.
            reason = handle.cancelled() if handle is not None else None
            if reason:
                raise _CallCancelled(reason) from stream_err
            # Real API/network errors belong to the bounded outer retry loop.
            err = _classify_api_error(stream_err)
            if err.retryable or err.status_code is not None:
                raise
            logger.debug("[PROXY] Streaming failed, falling back to non-streaming: %s", stream_err)
        finally:
            if handle is not None:
                handle.detach_stream()
            # Closing drops the connection, so an abandoned request does not
            # keep generating on the server.
            _close_quietly(stream)

        response = request()
        message = response.choices[0].message
        content = message.content or ""
        if "</think>" in content:
            content = content.split("</think>")[-1].strip()
        reasoning = (
            getattr(message, "reasoning_content", None)
            or _field(getattr(message, "model_extra", None), "reasoning")
            or getattr(message, "reasoning", None)
        )
        if not content and reasoning:
            content = _salvage_reasoning_answer(reasoning)
            if content:
                logger.warning("[PROXY] Empty content — using salvaged answer (%s chars)", len(content))
            else:
                logger.warning("[PROXY] Empty content with no clean final answer — using caller fallback")
        served_model = getattr(response, "model", None)
        self._note_served_model(served_model)
        self._record_request_metrics(
            params,
            request_started=request_started,
            attempt=attempt,
            usage=getattr(response, "usage", None),
            served_model=served_model,
            finish_reason=getattr(response.choices[0], "finish_reason", None),
            call=call,
        )
        return content or ""

    # Consecutive image-completion failures before the vision path disables
    # itself. Live 2026-07-06: the gateway model (deepseek-v4-flash) can't
    # serve vision and the tunnel chokes on MB image payloads — the vision
    # watchdog burned a failing call pair every ~40s all match.
    _VISION_DISABLE_AFTER = 3

    def reset_vision_failures(self) -> None:
        """Allow a new attempt after the user explicitly resumes visual autoplay."""
        self._vision_dead = False
        self._vision_fail_count = 0

    def complete_with_image(
        self,
        system_prompt: str,
        user_message: str,
        image_bytes: bytes,
        request_timeout_s: float | None = None,
        *,
        json_mode: bool = False,
        call_class: str | None = None,
    ) -> str:
        """Get completion with an image via the OpenAI multimodal message format.

        call_class labels the metrics line (default "auto.<module>.<function>"
        of the caller; "vision" or "vision.<site>" is the documented form).
        While the shared circuit breaker is open the call is skipped and
        returns the circuit-open sentinel. Vision failures never count toward
        the breaker: big image payloads can fail on the tunnel while text
        calls are fine (2026-07-06).
        """
        import base64
        import time

        if getattr(self, "_vision_dead", False):
            return f"{BACKEND_ERROR_PREFIX} vision endpoint disabled after repeated failures"

        call = _CallContext(
            call_class=_resolve_call_class(call_class),
            max_tokens=600,
            budget_s=request_timeout_s if request_timeout_s is not None else self._CLIENT_HARD_TIMEOUT_S,
            handle=_CallHandle("vision"),
        )
        request_start = time.perf_counter()
        breaker = self._circuit()
        token = breaker.admit(trial=False)
        if token is None:
            retry_in = breaker.snapshot()["retry_in_s"]
            skipped = BackendUnavailable(
                f"{UNAVAILABLE_MARKER} ({CIRCUIT_OPEN_MARKER}; retry in {retry_in or 0:.0f}s)",
                retry_in_s=retry_in,
            )
            return self._finish_skipped(
                skipped,
                call,
                request_started=request_start,
                input_chars=len(system_prompt or "") + len(user_message or ""),
                raise_on_error=False,
            )

        params = None
        try:
            client = self._get_client()
            if request_timeout_s is not None:
                client = client.with_options(timeout=request_timeout_s)
            b64 = base64.b64encode(image_bytes).decode("utf-8")

            params = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": user_message},
                        ],
                    },
                ],
                "max_completion_tokens": 600,
                "temperature": 0.3,
            }
            if json_mode:
                params["response_format"] = {"type": "json_object"}

            model_lower = self.model.lower()
            extra = {}
            is_gpt5 = "gpt-5" in model_lower or "gpt5" in model_lower
            is_gemini = "gemini" in model_lower
            if "glm" in model_lower:
                extra["chat_template_kwargs"] = {"thinking": True, "reasoning_effort": "low"}
            if "claude" in model_lower:
                extra["thinking"] = {"type": "disabled"}
            if is_gemini:
                # OpenAI-compat path: use reasoning_effort, not thinking_config.
                params["reasoning_effort"] = "none"
            if is_gpt5:
                params["reasoning_effort"] = "minimal"
                params["verbosity"] = "low"
                # GPT-5 reasoning models reject temperature != 1.0 when
                # reasoning_effort is set. Drop it so Azure uses the default.
                params.pop("temperature", None)
            if extra:
                params["extra_body"] = extra

            request_start = time.perf_counter()
            response = client.chat.completions.create(**params)
            request_time = (time.perf_counter() - request_start) * 1000

            content = response.choices[0].message.content
            logger.info(f"[PROXY] Vision API: {request_time:.0f}ms, model: {self.model}")
            served = getattr(response, "model", None)
            finish = getattr(response.choices[0], "finish_reason", None)
            self._record_request_metrics(
                params,
                request_started=request_start,
                attempt=1,
                usage=getattr(response, "usage", None),
                served_model=served if isinstance(served, str) else None,
                finish_reason=finish if isinstance(finish, str) else None,
                call=call,
            )
            breaker.record_success(token=token)
            self._vision_fail_count = 0
            return content
        except Exception as e:
            logger.error(f"Vision API error: {e}")
            if params is not None:
                self._record_request_metrics(
                    params, request_started=request_start, attempt=1, status="error", call=call
                )
            self._vision_fail_count = getattr(self, "_vision_fail_count", 0) + 1
            if self._vision_fail_count >= self._VISION_DISABLE_AFTER:
                self._vision_dead = True
                logger.warning(
                    "[PROXY] Disabling image completions after "
                    f"{self._vision_fail_count} consecutive failures — the "
                    "configured backend/gateway cannot serve vision requests"
                )
            return f"{BACKEND_ERROR_PREFIX} vision analysis failed: {e}"

    def list_models(self) -> list[str]:
        """List available models from the endpoint."""
        try:
            client = self._get_client()
            models = client.models.list()
            return [m.id for m in models.data]
        except Exception as e:
            logger.error(f"Failed to list models: {e}")
            return []
