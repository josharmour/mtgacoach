"""Voice speech arbiter for the standalone coach.

Sits between speech producers (coaching loop, conversation answer threads,
UI stop commands) and a pluggable speech sink, enforcing two rules:

* **Priority preemption** — QUESTION > URGENT > ADVICE > PROACTIVE. A higher
  priority always replaces what is currently speaking; the same priority
  replaces only when its identity sequence is newer; a lower priority is
  dropped (superseded by the current request).
* **Staleness** — a request whose identity is older than the floor identity
  (older session, a different match within the same session, or an older
  turn within the same match) is cancelled without touching audio.

Identities are treated STRUCTURALLY: any object exposing ``session_id``,
``match_id``, ``turn_number`` and ``seq`` (or ``request_id``) attributes
works; ``identity=None`` always speaks — the legacy path is never
stale-dropped.

``SpeechState.RENDERING`` is reserved for a future synthesis-in-progress
phase. The arbiter currently transitions IDLE -> SPEAKING -> IDLE directly
(the sink owns synthesis latency), so ``state`` never reports RENDERING —
a documented approximation, not a regression.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SpeechState(str, Enum):
    """Coarse session state of the voice arbiter."""

    IDLE = "idle"
    RENDERING = "rendering"
    SPEAKING = "speaking"
    CANCELLED = "cancelled"


class SpeechPriority(str, Enum):
    """Speech urgency classes; higher rank preempts lower."""

    QUESTION = "question"
    URGENT = "urgent"
    ADVICE = "advice"
    PROACTIVE = "proactive"

    @property
    def rank(self) -> int:
        """Preemption rank — QUESTION(3) > URGENT(2) > ADVICE(1) > PROACTIVE(0)."""
        return _PRIORITY_RANK[self]


_PRIORITY_RANK = {
    SpeechPriority.QUESTION: 3,
    SpeechPriority.URGENT: 2,
    SpeechPriority.ADVICE: 1,
    SpeechPriority.PROACTIVE: 0,
}


def coerce_priority(value: Any) -> SpeechPriority:
    """Coerce a priority value (enum or string) into :class:`SpeechPriority`.

    Unknown strings fall back to PROACTIVE so an unexpected value can never
    preempt the currently speaking request.
    """
    if isinstance(value, SpeechPriority):
        return value
    try:
        return SpeechPriority(str(value).strip().lower())
    except ValueError:
        return SpeechPriority.PROACTIVE


@dataclass(frozen=True)
class SpeechIdentity:
    """Minimal identity for speech requests.

    The arbiter also accepts foreign identity objects (e.g. the canonical
    ``ResponseIdentity`` from ``conversation.py``) as long as they expose
    ``session_id``/``match_id``/``turn_number`` plus ``seq`` or ``request_id``.
    """

    session_id: int
    match_id: str | None
    turn_number: int
    seq: int


@dataclass(frozen=True)
class SpeechRequest:
    text: str
    priority: SpeechPriority
    identity: Any
    created_ts: float


@dataclass(frozen=True)
class SpeechOutcome:
    state: SpeechState
    played: bool
    superseded_by: Any = None


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    return getattr(obj, name, default)


def _identity_seq(identity: Any) -> int | None:
    seq = _attr(identity, "seq")
    if seq is None:
        seq = _attr(identity, "request_id")
    return seq


def _seq_namespace(identity: Any) -> str | None:
    """Which counter namespace supplies this identity's sequence number.

    ``SpeechIdentity`` (advice path) carries ``seq``; the canonical
    ``ResponseIdentity`` (conversation answers) carries ``request_id``. The
    two counters are unrelated — comparing across them would falsely cancel
    same-priority speech, so the arbiter only supersedes within one class.
    """
    if identity is None:
        return None
    if _attr(identity, "seq") is not None:
        return "seq"
    if _attr(identity, "request_id") is not None:
        return "request_id"
    return None


class VoiceSession:
    """Thread-safe speech arbiter delegating playback to a sink.

    The sink is duck-typed: anything with ``speak(text)`` / ``speak(text,
    blocking=False)`` and optionally ``stop()`` works (``VoiceOutput`` from
    ``tts.py``, ``_PipeVoiceOutput`` from ``standalone_voice.py``). Sink
    calls happen OUTSIDE the lock so a blocking sink cannot serialize other
    arbitrated calls.
    """

    def __init__(
        self,
        output: Any = None,
        now: Callable[[], float] = time.monotonic,
        release_poll_interval: float = 0.05,
        release_max_wait: float = 300.0,
    ) -> None:
        self._output = output
        self._now = now
        self._lock = threading.RLock()
        self._listeners: list[Callable[[str], None]] = []
        self._state = SpeechState.IDLE
        # Floor identity: requests older than this are stale. Updated by
        # speak() on acceptance and by cancel_obsolete().
        self._floor: Any = None
        # Identity/priority of the request currently winning the channel.
        self._active: Any = None
        self._active_rank = -1
        self._active_seq: int | None = None
        self._active_seq_ns: str | None = None
        # Channel ownership token: bumped on every accepted speak() and on
        # stop_speaking(). A completion release only clears the channel when
        # its token is still current, so a slow sink's late completion can
        # never clobber a newer request.
        self._channel_token = 0
        self._completion_callbacks: dict[int, Callable[[], None]] = {}
        self._release_poll_interval = release_poll_interval
        self._release_max_wait = release_max_wait

    # ── Public API ────────────────────────────────────────────────────

    def speak(self, text: str, *, priority: Any, identity: Any = None) -> SpeechOutcome:
        """Arbitrate a speech request against the current floor and channel.

        Returns the :class:`SpeechOutcome` for the request — ``SPEAKING`` /
        ``played=True`` when it reached the sink, ``CANCELLED`` /
        ``played=False`` when it was stale or preempted by a request that
        owns the channel.
        """
        prio = coerce_priority(priority)
        request = SpeechRequest(text=text, priority=prio, identity=identity, created_ts=self._now())
        with self._lock:
            if self._is_stale_locked(request.identity):
                return SpeechOutcome(state=SpeechState.CANCELLED, played=False)
            rank = prio.rank
            seq = _identity_seq(request.identity)
            seq_ns = _seq_namespace(request.identity)
            if self._active is not None and request.identity is not None:
                if rank < self._active_rank:
                    return SpeechOutcome(
                        state=SpeechState.CANCELLED, played=False, superseded_by=self._active
                    )
                if (
                    rank == self._active_rank
                    and seq is not None
                    and self._active_seq is not None
                    # Seq comparison ONLY within the same identity class:
                    # SpeechIdentity.seq and ResponseIdentity.request_id are
                    # unrelated counters — never compare across namespaces.
                    and seq_ns is not None
                    and seq_ns == self._active_seq_ns
                    and seq <= self._active_seq
                ):
                    return SpeechOutcome(
                        state=SpeechState.CANCELLED, played=False, superseded_by=self._active
                    )
            if request.identity is not None:
                self._floor = request.identity
            self._active = request.identity
            self._active_rank = rank
            self._active_seq = seq
            self._active_seq_ns = seq_ns
            token = self._channel_token + 1
            self._channel_token = token
            notify = self._transition_locked(SpeechState.SPEAKING)
        self._sink_speak(request.text)
        self._notify(notify)
        # Release the channel when the sink finishes (or is proven done); the
        # arbiter must return to IDLE so lower-priority speech can speak again.
        self._arm_completion(token)
        return SpeechOutcome(state=SpeechState.SPEAKING, played=True)

    def stop_speaking(self, reason: str = "user") -> None:
        """Drop the current request and silence the sink."""
        del reason  # accepted for call-site compatibility; logged below
        logger.debug("VoiceSession.stop_speaking")
        with self._lock:
            self._active = None
            self._active_rank = -1
            self._active_seq = None
            self._active_seq_ns = None
            self._channel_token += 1  # invalidate any armed completion
            notify = self._transition_locked(SpeechState.IDLE)
        self._sink_stop()
        self._notify(notify)

    def cancel_obsolete(self, identity: Any) -> None:
        """Advance the staleness floor to ``identity``.

        Later ``speak()`` calls carrying an older identity (older session,
        different match, older turn) are then cancelled without touching
        audio. Current playback is left alone — preemption/stop is the
        caller's job (higher-priority speak or ``stop_speaking``).
        """
        if identity is None:
            return
        with self._lock:
            self._floor = identity

    @property
    def state(self) -> SpeechState:
        """Current state — SPEAKING while a request owns the channel, else IDLE.

        RENDERING is reserved for a future synthesis-in-progress phase and is
        never reported by this implementation.
        """
        with self._lock:
            return self._state

    def add_listener(self, callback: Callable[[str], None]) -> None:
        """Register ``callback(state_string)``; invoked on state transitions."""
        with self._lock:
            self._listeners.append(callback)

    def wait_for_idle(self, timeout: float = 30.0) -> bool:
        """Block until the channel is released (state returns to IDLE).

        Backed by the completion machinery: the release monitor or a sink
        ``on_complete`` callback transitions SPEAKING → IDLE when the current
        utterance finishes. Returns True on reaching IDLE, False on timeout.
        Never blocks indefinitely: ``timeout`` bounds the wait, and any
        ``stop_speaking`` in between releases immediately.
        """
        deadline = self._now() + max(0.0, float(timeout))
        while True:
            with self._lock:
                if self._state == SpeechState.IDLE:
                    return True
            if self._now() >= deadline:
                return False
            time.sleep(0.02)

    # ── Internals ─────────────────────────────────────────────────────

    def _release_channel(self, token: int) -> None:
        """Clear the channel owned by ``token`` — the arbiter lifecycle core.

        Called on a sink completion callback (finished audio) or, when the
        sink exposes no completion signal, by the lightweight monitor once the
        sink reports not-speaking (or never started). A token that is no
        longer current means a newer request or stop took the channel first
        and this release is a no-op. Idempotent: only the token's owner can
        clear it.
        """
        with self._lock:
            if token != self._channel_token:
                return  # superseded by a newer speak/stop — leave the channel
            self._active = None
            self._active_rank = -1
            self._active_seq = None
            self._active_seq_ns = None
            self._completion_callbacks.pop(token, None)
            notify = self._transition_locked(SpeechState.IDLE)
        self._notify(notify)

    def _arm_completion(self, token: int) -> None:
        """Detect when the just-spoken utterance finishes and release the channel.

        Priority of completion mechanisms:
        1. Sink exposes ``speak(text, on_complete=cb)`` — the callback was
           armed by ``_sink_speak`` and fires exactly when playback ends.
        2. Sink exposes ``is_speaking()`` — a daemon thread polls it and
           releases once it turns False.
        3. Neither exists — the sink cannot report state, so a short monitor
           releases the channel after one poll interval (hand-off semantics;
           stop_speaking remains authoritative for preemption).
        """
        output = self._output
        if output is None:
            self._release_channel(token)
            return

        with self._lock:
            already_armed = token in self._completion_callbacks
        if already_armed:
            return

        is_speaking = getattr(output, "is_speaking", None)
        if callable(is_speaking):
            self._start_release_monitor(token, use_is_speaking=True)
            return

        # No completion signal at all: release after one interval — the
        # arbiter considers the utterance handed off (pipe-mode sinks are
        # non-blocking on the conversation path).
        self._start_release_monitor(token, use_is_speaking=False)

    def _start_release_monitor(self, token: int, *, use_is_speaking: bool) -> None:
        def monitor() -> None:
            interval = max(0.005, float(self._release_poll_interval))
            deadline = self._now() + max(0.0, float(self._release_max_wait))
            while True:
                with self._lock:
                    if token != self._channel_token:
                        return  # superseded; stop_speaking already handled IDLE
                if use_is_speaking:
                    output = self._output
                    is_speaking = getattr(output, "is_speaking", None) if output else None
                    if is_speaking is not None:
                        try:
                            # Support both a callable probe and a property.
                            speaking = bool(is_speaking() if callable(is_speaking) else is_speaking)
                        except Exception:
                            logger.debug("sink is_speaking() failed", exc_info=True)
                            self._release_channel(token)
                            return
                        if speaking:
                            time.sleep(interval)
                            continue
                        # Finished (spoke then stopped) or never started
                        # (muted/dropped) — either way the channel is free.
                        self._release_channel(token)
                        return
                else:
                    # No completion signal: hand-off semantics — release after
                    # one poll interval so a superseding request (already
                    # arbitrating) still wins the token race.
                    time.sleep(interval)
                    self._release_channel(token)
                    return
                if self._now() >= deadline:
                    logger.debug("VoiceSession release monitor timed out; releasing channel")
                    self._release_channel(token)
                    return
                time.sleep(interval)

        thread = threading.Thread(target=monitor, daemon=True, name="voice-session-release")
        thread.start()

    def _is_stale_locked(self, identity: Any) -> bool:
        """Staleness check; caller must hold the lock.

        ``identity=None`` (legacy path) is never stale.
        """
        if identity is None or self._floor is None:
            return False
        floor = self._floor
        try:
            session_id = _attr(identity, "session_id")
            floor_session = _attr(floor, "session_id")
            if session_id is None or floor_session is None:
                return False
            if session_id < floor_session:
                return True
            if session_id != floor_session:
                return False
            match_id = _attr(identity, "match_id")
            floor_match = _attr(floor, "match_id")
            # A match_id mismatch only proves staleness when both sides are
            # known; unknown values cannot be ordered.
            if match_id is not None and floor_match is not None and match_id != floor_match:
                return True
            turn_number = _attr(identity, "turn_number")
            floor_turn = _attr(floor, "turn_number")
            if turn_number is not None and floor_turn is not None and turn_number < floor_turn:
                return True
        except TypeError:
            return False
        return False

    def _transition_locked(self, new_state: SpeechState) -> list[str]:
        previous = self._state
        if new_state == previous:
            return []
        self._state = new_state
        return [new_state.value]

    def _notify(self, states: list[str]) -> None:
        for state in states:
            for callback in tuple(self._listeners):
                with contextlib.suppress(Exception):
                    callback(state)

    def _sink_speak(self, text: str) -> None:
        output = self._output
        if output is None:
            return
        speak = getattr(output, "speak", None)
        if not callable(speak):
            logger.warning("Voice sink has no callable speak(); dropping speech")
            return
        kwargs: dict[str, Any] = {}
        on_complete: Callable[[], None] | None = None
        try:
            params = inspect.signature(speak).parameters
            if "blocking" in params and params["blocking"].kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                kwargs["blocking"] = False
            if "on_complete" in params and params["on_complete"].kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                on_complete = self._make_sink_completion_callback()
                kwargs["on_complete"] = on_complete
        except (TypeError, ValueError):
            pass  # builtins/C sinks — call bare
        try:
            speak(text, **kwargs)
        except TypeError:
            # Signature guess failed — retry with positional text only.
            with contextlib.suppress(Exception):
                speak(text)
        except Exception:
            logger.exception("Voice sink speak() failed")

    def _make_sink_completion_callback(self) -> Callable[[], None]:
        """Build the ``on_complete`` callback handed to a completion-aware sink.

        The returned callable is registered under the CURRENT channel token so
        the release only clears the channel this utterance owns; a newer
        speak/stop bumps the token first and the callback becomes a no-op.
        """
        with self._lock:
            token = self._channel_token
            self._completion_callbacks[token] = lambda: self._release_channel(token)
        return lambda: self._release_channel(token)

    def _sink_stop(self) -> None:
        output = self._output
        if output is None:
            return
        stop = getattr(output, "stop", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception:
            logger.exception("Voice sink stop() failed")
