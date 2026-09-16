"""Conversation Mode controller, response identity, and match memory.

This module implements the Wave-2 vertical slice of Conversation Mode (see
``conversation-mode.md`` and the binding contract in
``conversation-mode-progress.md``):

- ``ResponseIdentity`` — canonical frozen identity stamped on every response
  so stale answers can be discarded (session/match/turn/request).
- ``MatchMemory`` — a compact, thread-safe per-match conversation memory
  (turn ring, discussed topics, revealed opponent cards, plan summary).
- ``ConversationController`` — the engine-side session that records user
  questions, preempts speech, spawns answer threads, and gates delivery.

The controller is deliberately structural about its collaborators: it never
imports ``voice_session`` (duck-typed via ``coach.voice_session``) and it
guards every attribute access on the coach so it can be unit-tested with a
fake. No PySide6 imports here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from arenamcp.backend_health import is_backend_error_text, strip_health_tags
from arenamcp.settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical constants
# ---------------------------------------------------------------------------

TURN_ADVICE = "turn_advice"
CONVERSATION = "conversation"

VERBOSITY_QUIET = "quiet"
VERBOSITY_BALANCED = "balanced"
VERBOSITY_DETAILED = "detailed"

VALID_MODES: tuple[str, ...] = (TURN_ADVICE, CONVERSATION)
VALID_VERBOSITIES: tuple[str, ...] = (VERBOSITY_QUIET, VERBOSITY_BALANCED, VERBOSITY_DETAILED)


class EventPriority(IntEnum):
    """Relative urgency of conversation events (higher wins preemption)."""

    FILLER = 10
    STATE_SHIFT = 60
    TURN_CONTEXT = 40
    THREAT = 80
    URGENT_DECISION = 90
    USER_QUESTION = 100


EVENT_TO_SPEECH_PRIORITY: dict[EventPriority, str] = {
    EventPriority.USER_QUESTION: "question",
    EventPriority.URGENT_DECISION: "urgent",
    EventPriority.THREAT: "urgent",
    EventPriority.TURN_CONTEXT: "advice",
    EventPriority.STATE_SHIFT: "proactive",
    EventPriority.FILLER: "proactive",
}


# ---------------------------------------------------------------------------
# Response identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseIdentity:
    """Immutable identity stamped on a conversational response.

    ``request_id`` is monotonic per engine process. ``session_id`` bumps on
    mode changes AND match boundaries, so a response is stale when the live
    identity differs in ``session_id``, ``match_id``, or ``match_number``.
    """

    session_id: int
    match_id: str | None
    match_number: int
    turn_number: int
    active_player: int | None
    decision_sig: str | None
    mode: str
    request_id: int

    def to_payload(self) -> dict[str, Any]:
        # The 5-field JSON subset used across the pipe protocol.
        return {
            "session_id": self.session_id,
            "match_id": self.match_id,
            "turn_number": self.turn_number,
            "request_id": self.request_id,
            "mode": self.mode,
        }

    def is_stale_vs(self, other: ResponseIdentity | None) -> bool:
        # Position-bound fields additionally invalidate the response when the
        # turn, active player, or pending decision changed underneath it.
        if other is None:
            return False
        if self.session_id != other.session_id:
            return True
        if self.match_id != other.match_id:
            return True
        if self.match_number != other.match_number:
            return True
        if self.decision_sig is not None and self.decision_sig != other.decision_sig:
            return True
        if self.turn_number != other.turn_number or self.active_player != other.active_player:
            return True
        return False


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@dataclass
class EvidenceBlock:
    # Wave-4 reserved shape (MageZero evidence). Not wired anywhere yet.
    model_id: str | None = None
    checkpoint_hash: str | None = None
    deck_compatible: bool = False
    evaluated: bool = False
    provenance: str | None = None
    uncertainty_reason: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversationTurn:
    role: str
    text: str
    ts: float = field(default_factory=time.time)
    identity: ResponseIdentity | None = None
    trigger: str | None = None
    topic: str | None = None


MEMORY_RING_SIZE = 12


@dataclass
class MatchMemory:
    """Compact per-match conversation memory. All mutation is controller-locked."""

    turns: list[ConversationTurn] = field(default_factory=list)
    discussed_topics: dict[str, float] = field(default_factory=dict)
    revealed_opponent_cards: list[str] = field(default_factory=list)
    plan_summary: str = ""
    last_evidence: EvidenceBlock | None = None

    def append(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)
        if len(self.turns) > MEMORY_RING_SIZE:
            del self.turns[: len(self.turns) - MEMORY_RING_SIZE]


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ConversationController:
    """Engine-side conversation session (instantiated as ``coach.conversation``).

    Wave-2 slice: user questions get answered with memory-augmented context;
    ``on_state`` records memory only (proactive speech is Wave 3).
    """

    def __init__(
        self,
        coach: Any,
        emit_event: Callable[..., None] | None = None,
        snapshot_fn: Callable[[], dict[str, Any] | None] | None = None,
    ) -> None:
        self._coach = coach
        self._emit_event = emit_event
        self._snapshot_fn = snapshot_fn

        self._lock = threading.RLock()
        self._mode: str = TURN_ADVICE
        self._verbosity: str = VERBOSITY_BALANCED
        self._session_id: int = 1
        self._request_counter: int = 0
        self._pending: dict[int, ResponseIdentity] = {}
        self._answer_threads: list[threading.Thread] = []
        self.memory = MatchMemory()

    # -- properties ---------------------------------------------------------

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    @property
    def verbosity(self) -> str:
        with self._lock:
            return self._verbosity

    # -- mode / verbosity ---------------------------------------------------

    def set_mode(self, mode: str, persist: bool = True) -> None:
        """Switch session mode; bumps session identity and cancels pending work."""
        if mode not in VALID_MODES:
            raise ValueError(f"Invalid conversation mode: {mode!r}")

        vs = getattr(self._coach, "voice_session", None)
        if vs is not None and hasattr(vs, "stop_speaking"):
            try:
                vs.stop_speaking("mode_change")
            except Exception:  # pragma: no cover - defensive
                logger.debug("voice_session.stop_speaking failed", exc_info=True)

        with self._lock:
            self._mode = mode
            self._session_id += 1
            self._pending.clear()

        if persist:
            try:
                get_settings().set("conversation_mode", mode)
            except Exception:
                logger.warning("Failed to persist conversation_mode", exc_info=True)


    def set_verbosity(self, verbosity: str) -> None:
        """Set commentary verbosity; behavior is reserved for Wave 3."""
        if verbosity not in VALID_VERBOSITIES:
            raise ValueError(f"Invalid conversation verbosity: {verbosity!r}")

        with self._lock:
            self._verbosity = verbosity

        try:
            get_settings().set("conversation_verbosity", verbosity)
        except Exception:
            logger.warning("Failed to persist conversation_verbosity", exc_info=True)

    # -- state hook ---------------------------------------------------------

    def on_state(
        self,
        curr_state: dict[str, Any] | None,
        prev_state: dict[str, Any] | None,
        triggers: list[str] | None = None,
    ) -> None:
        # Memory-record only in this slice: no proactive speech (Wave 3 adds
        # the topic selector). Triggers are folded into the ring for context.
        if not triggers:
            return

        with self._lock:
            self.memory.append(
                ConversationTurn(
                    role="state",
                    text=", ".join(str(t) for t in triggers),
                    identity=self.current_identity(request_id=self._request_counter),
                    trigger=", ".join(str(t) for t in triggers),
                )
            )

    # -- user questions -----------------------------------------------------

    def on_user_question(self, text: str, source: str = "typed") -> int:
        """Record a user question, preempt speech, and spawn the answer thread.

        Returns the monotonic request id for the in-flight answer.
        """
        if not text or not str(text).strip():
            return 0

        with self._lock:
            self._request_counter += 1
            request_id = self._request_counter
            identity = self.current_identity(request_id=request_id)
            self._pending[request_id] = identity
            self.memory.append(
                ConversationTurn(
                    role="user",
                    text=str(text).strip(),
                    identity=identity,
                    trigger=source,
                )
            )

        self._preempt_speech("user_question")
        self._emit("conversation_status", state="thinking", request_id=request_id)

        thread = threading.Thread(
            target=self._answer,
            args=(request_id, str(text), identity),
            daemon=True,
            name=f"convo-answer-{request_id}",
        )
        with self._lock:
            self._answer_threads.append(thread)
        thread.start()
        return request_id

    def cancel_pending(self) -> None:
        # Invalidate in-flight requests: their identities no longer match
        # pending set membership, so answers are discarded on delivery.
        with self._lock:
            self._pending.clear()

    def reset_for_match(self, match_id: str | None, match_number: int) -> None:
        # Match boundary: clear memory, bump session identity, drop pending.
        with self._lock:
            self.memory = MatchMemory()
            self._session_id += 1
            self._pending.clear()

    def current_identity(self, request_id: int | None = None) -> ResponseIdentity:
        # Built from coach state with guarded attribute access everywhere;
        # unavailable fields degrade to None/0 rather than raising.
        snapshot = self._snapshot() or {}
        turn = snapshot.get("turn") if isinstance(snapshot, dict) else None
        if not isinstance(turn, dict):
            turn = {}

        turn_number = turn.get("turn_number") or snapshot.get("turn_number") or 0
        active_player = turn.get("active_player")
        if active_player is None:
            active_player = snapshot.get("active_player")

        match_id = (
            getattr(self._coach, "last_match_id", None)
            or snapshot.get("match_id")
            or None
        )
        match_number = getattr(self._coach, "_match_number", 0) or 0

        decision_sig: str | None = None
        if isinstance(snapshot, dict) and snapshot.get("pending_decision"):
            builder = getattr(self._coach, "_build_pending_decision_signature", None)
            if callable(builder):
                try:
                    decision_sig = builder(snapshot)
                except Exception:
                    logger.debug("decision signature build failed", exc_info=True)

        with self._lock:
            rid = self._request_counter if request_id is None else request_id
            mode = self._mode
            session_id = self._session_id

        return ResponseIdentity(
            session_id=session_id,
            match_id=match_id,
            match_number=int(match_number or 0),
            turn_number=int(turn_number or 0),
            active_player=active_player if isinstance(active_player, int) else None,
            decision_sig=decision_sig,
            mode=mode,
            request_id=int(rid or 0),
        )

    # -- internals ----------------------------------------------------------

    def _snapshot(self) -> dict[str, Any] | None:
        # Live snapshot source: injected fn first, then a guarded MCP read.
        if self._snapshot_fn is not None:
            try:
                snap = self._snapshot_fn()
                return snap if isinstance(snap, dict) else None
            except Exception:
                logger.debug("snapshot_fn failed", exc_info=True)
                return None

        mcp = getattr(self._coach, "_mcp", None)
        if mcp is None or not hasattr(mcp, "get_game_state"):
            return None
        try:
            snap = mcp.get_game_state()
            return snap if isinstance(snap, dict) else None
        except Exception:
            logger.debug("get_game_state failed", exc_info=True)
            return None

    def _preempt_speech(self, reason: str) -> None:
        # Prefer the generic arbiter when wired; fall back to the raw sink.
        vs = getattr(self._coach, "voice_session", None)
        if vs is not None and hasattr(vs, "stop_speaking"):
            try:
                vs.stop_speaking(reason)
                return
            except Exception:
                logger.debug("voice_session.stop_speaking failed", exc_info=True)
        vo = getattr(self._coach, "_voice_output", None)
        if vo is not None and hasattr(vo, "stop"):
            try:
                vo.stop()
            except Exception:
                logger.debug("voice_output.stop failed", exc_info=True)

    def _emit(self, event_type: str, **fields: Any) -> None:
        if self._emit_event is None:
            return
        try:
            self._emit_event(event_type, **fields)
        except Exception:
            logger.debug("emit_event(%s) failed", event_type, exc_info=True)

    def _augment_question(self, text: str) -> str:
        # Replay the recent conversation as a compact digest so the coaching
        # LLM can answer follow-ups without a second stateful channel.
        with self._lock:
            recent = list(self.memory.turns[-6:])

        lines: list[str] = []
        for turn in recent:
            if turn.role == "user":
                lines.append(f"user: {turn.text}")
            elif turn.role == "coach":
                lines.append(f"coach: {turn.text}")

        if not lines:
            return str(text)

        digest = "\n".join(lines)
        return f"Recent conversation:\n{digest}\n\nUser question: {text}"

    def _is_stale(self, identity: ResponseIdentity) -> bool:
        # Delivery gate: session/match/mode drift, or a newer request is
        # pending. Supersession by request id, not completion order.
        with self._lock:
            if identity.request_id not in self._pending:
                return True
            if self._session_id != identity.session_id:
                return True
            if self._mode != identity.mode:
                return True
            newer = any(rid > identity.request_id for rid in self._pending)
        if newer:
            return True

        live = self.current_identity(request_id=identity.request_id)
        return identity.is_stale_vs(live)

    def _answer(self, request_id: int, text: str, identity: ResponseIdentity) -> None:
        # Daemon answer thread body: never raises out of the thread.
        try:
            snapshot = self._snapshot()
            augmented = self._augment_question(text)

            inner = getattr(self._coach, "_coach", None)
            if inner is None or not hasattr(inner, "get_advice"):
                reply = "[BACKEND ERROR] coach engine unavailable"
            else:
                try:
                    reply = inner.get_advice(snapshot, question=augmented)
                except Exception as exc:
                    logger.warning("get_advice failed: %s", exc, exc_info=True)
                    reply = f"[BACKEND ERROR] {type(exc).__name__}: {exc}"

            if not isinstance(reply, str) or not reply.strip():
                reply = "[BACKEND ERROR] empty response from coach backend"

            if self._is_stale(identity):
                # Superseded / stale answers are dropped silently.
                with self._lock:
                    self._pending.pop(identity.request_id, None)
                return

            payload = identity.to_payload()
            self._emit("conversation_reply", text=reply, identity=payload)

            with self._lock:
                self.memory.append(
                    ConversationTurn(
                        role="coach",
                        text=reply,
                        identity=identity,
                    )
                )
                self._pending.pop(identity.request_id, None)

            self._speak(reply, identity)
            self._emit("conversation_status", state="idle", request_id=request_id)
        except Exception:
            # Absolute backstop: the thread must never crash the engine.
            logger.exception("conversation answer thread crashed")

    def _speak(self, text: str, identity: ResponseIdentity) -> None:
        # TTS boundary: health tags are stripped, error-tagged text is never
        # spoken (it is only displayed via conversation_reply).
        if is_backend_error_text(text):
            return

        spoken = strip_health_tags(text)
        if not spoken:
            return

        vs = getattr(self._coach, "voice_session", None)
        if vs is not None and hasattr(vs, "speak"):
            try:
                vs.speak(spoken, priority="question", identity=identity)
                return
            except Exception:
                logger.debug("voice_session.speak failed", exc_info=True)

        vo = getattr(self._coach, "_voice_output", None)
        if vo is not None and hasattr(vo, "speak"):
            try:
                vo.speak(spoken)
            except Exception:
                logger.debug("voice_output.speak failed", exc_info=True)
