"""Conversation Mode controller, response identity, and match memory.

This module implements the Conversation Mode engine layer (see
``conversation-mode.md`` and the binding contract in
``conversation-mode-progress.md``):

- ``ResponseIdentity`` — canonical frozen identity stamped on every response
  so stale answers can be discarded (session/match/turn/request).
- ``MatchMemory`` — a compact, thread-safe per-match conversation memory
  (turn ring, discussed topics, revealed opponent cards, plan summary,
  proactive-speech timestamps, deferred pending questions).
- ``TopicSelector`` — Wave-3 proactive commentary: derives prioritized
  candidate topics from *meaningful* state changes (opponent developments,
  role shifts, material swings, plan drift), never from every turn/event.
- ``ConversationController`` — the engine-side session that records user
  questions, preempts speech, spawns answer threads, gates delivery, and
  (Wave 3) selects/gates/speaks proactive topics behind verbosity, cooldown,
  repetition, and user-question-priority gates.

The controller is deliberately structural about its collaborators: it never
imports ``voice_session`` (duck-typed via ``coach.voice_session``) and it
guards every attribute access on the coach so it can be unit-tested with a
fake. No PySide6 imports here.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

# The recovery daemon's grace loop must survive tests that monkeypatch
# ``time.sleep`` on the SHARED time module: keep a pristine handle.
_tool_sleep = time.sleep

from arenamcp.backend_health import is_backend_error_text, strip_health_tags
from arenamcp.settings import get_settings

logger = logging.getLogger(__name__)

# Wall-clock ``time.time`` captured at import so a test harness that
# monkeypatches ``time.time`` (the replay suite's fake clock) is detectable
# by identity at call time.
_ORIGINAL_WALL_TIME = time.time


def _gate_now() -> float:
    """Clock for suppression windows (speaking cooldown / per-topic repetition).

    Production reads ``time.monotonic()``: a wall-clock jump (NTP correction,
    suspend/resume) must never freeze or instantly expire a suppression
    window. The replay harness injects its fake clock by monkeypatching
    ``conversation.time.time``; the identity check follows such a patched
    clock so recorded-game replays stay deterministic. Tests that need full
    control inject ``now_fn`` into :class:`ConversationController` instead.
    """
    if time.time is not _ORIGINAL_WALL_TIME:  # pragma: no cover - harness path
        return time.time()
    return time.monotonic()


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

# Default minimum seconds between proactive topic utterances (settings key
# ``conversation_cooldown_seconds``). Per-topic repetition windows are 3x this.
DEFAULT_COOLDOWN_SECONDS = 90
# A deferred user question stays answerable after an urgent interrupt for at
# most this many seconds, and only within the same match.
PENDING_QUESTION_TTL_SECONDS = 60
PENDING_QUESTION_CAP = 5
# Fallback wait after an urgent topic's speech when the arbiter exposes no
# completion machinery (non-VoiceSession sinks) — recovery still must not
# preempt the topic instantly.
_RECOVERY_GRACE_SECONDS = 1.0
# Topic keys that read as "the situation just changed materially" — these are
# the STATE_SHIFT-and-above classes the Balanced verbosity tier allows.
URGENT_TOPIC_KEYS: frozenset[str] = frozenset({"threat", "urgent_decision", "low_life"})

# Instruction block prepended to every proactive-topic prompt. The
# observed-facts-vs-hypotheses rule lives HERE (in the prompt text) rather
# than in any semantic analyzer: statements about the opponent's hidden
# hand/library must be phrased as hypotheses.
TOPIC_PROMPT_PREFIX = (
    "You are the play-by-play announcer for a Magic: The Gathering broadcast, "
    "calling the game for laymen and observers. Keep it under two short "
    "sentences. Describe what is happening and why it matters: narrate the "
    "plays, board shifts, and the strategy behind them, and add brief tidbits "
    "about the cards involved when they are interesting. Your commentary is "
    "for an audience, never instructions to the player — never tell anyone "
    "what to do next. Never state or imply a win probability. Any statement "
    "about the opponent's hidden hand or library must be phrased as a "
    'hypothesis ("they might have..."), never as an observed fact. MageZero '
    "model evidence, when present below, is supporting context only: never "
    "state a win probability from it, do not imply a one-ply estimate is full "
    "rules-engine search, and never describe a policy preference as an "
    "evaluated outcome."
)

# Question path (typed/PTT): same broadcast voice, but the viewer asked a
# direct question — recommendations are delivered as commentary on the best
# line, never as commands.
ANNOUNCER_QUESTION_PREFIX = (
    "You are the broadcast announcer calling this Magic: The Gathering game "
    "for observers. Answer the viewer's question in plain language, the way a "
    "commentator would explain the situation. When they ask what to do, give "
    "your read on the best line as commentary — never as an instruction to "
    "the player."
)

# Wave 4 — the standing caveat for uncalibrated model evidence, mirrored from
# the codebase's own claim in ``MCTSTreePayload.format_for_llm_prompt``
# ("scores are not calibrated win probabilities ... preserve legal-action
# checks ... Candidate order remains the tactical heuristic ranking, not a
# neural recommendation"). The two tail clauses mirror
# ``mcts_evaluator.py``'s experimental-evidence wording verbatim in meaning —
# do not strengthen or weaken this wording in prompts: it must match what the
# code itself asserts.
UNCALIBRATED_EVIDENCE_CAVEAT = (
    "Model scores are not calibrated win probabilities: use them as "
    "supporting evidence only, distinguish evaluated one-ply states from "
    "policy-only preferences, never present a policy preference as an "
    "evaluated outcome, preserve legal-action checks, and treat candidate "
    "order as the tactical heuristic ranking, not a neural recommendation."
)


# Standing instruction for the QUESTION path whenever MageZero evidence is
# present: mirrors the topic-path rules (TOPIC_PROMPT_PREFIX) so a direct
# question ("what's my win chance?") can never parrot the uncalibrated model
# score as a real win probability (M7 grounding fix).
QUESTION_EVIDENCE_INSTRUCTIONS = (
    "Rules for using the MageZero evidence below: it is supporting context "
    "only — never state or imply a win probability from it, do not imply a "
    "one-ply estimate is full rules-engine search, and never describe a "
    "policy preference as an evaluated outcome."
)


def match_number_from_identity(identity: ResponseIdentity | None) -> int:
    try:
        return int(getattr(identity, "match_number", 0) or 0)
    except (TypeError, ValueError):
        return 0


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
        # Session-scope staleness (Wave-2 contract): a conversational response
        # is invalid when the SESSION (mode change or match boundary) or the
        # match moved underneath it. Position fields (turn_number /
        # active_player / decision_sig) are deliberately NOT compared: a live
        # LLM render takes seconds while the game advances, so position
        # equality can never hold at delivery time — every proactive topic and
        # question answer was silently dropped (live report 2026-09-16 15:41,
        # bug_20260916_154149). Position-bound gating belongs to the legacy
        # advice path's own staleness logic (standalone.py "Discarding stale
        # advice"); supersession of in-flight answers stays request-id-based
        # at the _is_stale call site.
        if other is None:
            return False
        if self.session_id != other.session_id:
            return True
        if self.match_id != other.match_id:
            return True
        if self.match_number != other.match_number:
            return True
        return False


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@dataclass
class EvidenceBlock:
    """Wave-4 MageZero evidence attached to prompts and stored on memory.

    Populated ONLY from real sources (deck gating, the evaluated tactical
    payload, client fallback/reject reasons). Every field degrades to
    None/False when the source is missing — never invented.

    Uncalibrated-by-construction: ``root_win_probability`` is set only when an
    evaluated row actually exists, and ``calibrated`` stays False (the
    codebase's own claim in ``MCTSTreePayload.format_for_llm_prompt`` is that
    these scores are NOT calibrated win probabilities).
    """

    # Model/checkpoint identity from the active MageZero selection.
    model_id: str | None = None
    checkpoint_hash: str | None = None
    # Deck gating result (``magezero_gating.is_hero_deck_gated``).
    deck_supported: bool = False
    deck_compatible: bool = False  # legacy alias of deck_supported
    similarity: float | None = None
    eval_source: str | None = None
    evaluated: bool = False  # a real evaluated row was present in the payload
    provenance: str | None = None  # e.g. MCTSBranch.score_provenance
    # Fallback/reject reasons (MageZeroClient / ModelZooClient).
    fallback_reason: str | None = None
    # Win probability ONLY when actually present in an evaluated result.
    root_win_probability: float | None = None
    calibrated: bool = False
    uncertainty_reason: str | None = None
    # Extra provenance carried alongside (kept for prompt reuse/debug).
    payload: dict[str, Any] = field(default_factory=dict)

    def is_supported(self) -> bool:
        return bool(self.deck_supported and self.deck_compatible)


# ---------------------------------------------------------------------------
# Wave 4 — MageZero evidence collection (guarded, real sources only)
# ---------------------------------------------------------------------------

# MCTSBranch.score_provenance values, strongest first for selection.
_PROVENANCE_RANK: dict[str, int] = {
    "neural_afterstate": 3,
    "prior_only": 2,
    "unsupported_fallback": 1,
    "heuristic_lookahead": 0,
}
_EVALUATED_PROVENANCE = "neural_afterstate"
_HEURISTIC_EVAL_SOURCE = "Tactical Heuristic Lookahead"

# Wave 5: payload-style labels for ``MCTSBranch.score_provenance`` values,
# mirroring ``mcts_evaluator.py``'s own provenance label map — used instead of
# a blanket "policy preference" tag so each provenance class reads as what it
# actually is. Unknown values degrade to the conservative policy-preference
# label (never an evaluated-sounding one).
_PROVENANCE_LABELS: dict[str, str] = {
    "neural_afterstate": "evaluated",
    "prior_only": "policy preference",
    "heuristic_lookahead": "heuristic",
    "unsupported_fallback": "approx lookahead",
}


def _mcts_last_payload() -> Any:
    """Read the tactical evaluator's cached payload via a guarded getattr chain.

    Read-only: never imports-fails loudly, never mutates the evaluator, and
    treats an expired cache as no payload. Returns ``None`` when MageZero /
    MCTS evaluation machinery is absent entirely.
    """
    try:
        from arenamcp.mcts_evaluator import MCTSEvaluator  # local import: optional dependency

        payload = getattr(MCTSEvaluator, "_last_payload", None)
        if payload is None:
            return None
        try:
            if not MCTSEvaluator._cache_fresh():
                return None
        except Exception:  # pragma: no cover - defensive
            pass
        return payload
    except Exception:
        return None


def _magezero_reasons() -> tuple[str | None, str | None]:
    """(fallback_reason, reject_reason) from MageZeroClient, guarded."""
    try:
        from arenamcp.magezero_client import MageZeroClient  # local import: optional dependency

        return (
            getattr(MageZeroClient, "last_fallback_reason", lambda: None)(),
            getattr(MageZeroClient, "last_reject_reason", lambda: None)(),
        )
    except Exception:
        return (None, None)


def _selection_identity(game_state: dict[str, Any]) -> tuple[str | None, str | None]:
    """model_id + checkpoint_hash of the active MageZero selection.

    Uses the same read-only selection path the evaluator uses, but with
    ``refresh=False`` so this never triggers model discovery on the
    conversation path. Returns ``(None, None)`` when unavailable.
    """
    try:
        from arenamcp.format_profile import detect_format_profile
        from arenamcp.magezero_gating import extract_hero_deck
        from arenamcp.model_zoo import ModelZooClient  # local import: optional dependency

        extraction = extract_hero_deck(game_state)
        if not extraction.is_compatible or not extraction.cards:
            return (None, None)
        selection = ModelZooClient.select(detect_format_profile(game_state), extraction.cards, refresh=False)
        spec = getattr(selection, "model_spec", None)
        if spec is None:
            return (None, None)
        model_id = getattr(spec, "model_id", None) or None
        checkpoint_hash = getattr(spec, "checkpoint_hash", None) or None
        return (model_id, checkpoint_hash)
    except Exception:
        return (None, None)


def _evidence_signature(state: dict[str, Any] | None) -> tuple[Any, ...] | None:
    """Cheap change-detection key so evidence is recomputed only when the
    inputs that could change it changed (board/hand shape, payload CONTENT,
    client fallback/reject reasons).

    Wave 5: the payload contributes a CONTENT fingerprint (eval_source +
    per-branch/trap provenance counts + score/eval reasons), not ``id()``
    which is meaningless across re-created payload objects and stable across
    real content changes.
    """
    if not isinstance(state, dict):
        return None
    battlefield = [c for c in (state.get("battlefield") or []) if isinstance(c, dict)]
    hand = [c for c in (state.get("hand") or []) if isinstance(c, dict)]
    try:
        iids = tuple(sorted(c["instance_id"] for c in battlefield if isinstance(c.get("instance_id"), int)))
    except Exception:  # pragma: no cover - defensive
        iids = ()
    zones_raw = state.get("zones")
    zones: dict[str, Any] = zones_raw if isinstance(zones_raw, dict) else {}
    try:
        payload = _mcts_last_payload()
        fb, rej = _magezero_reasons()
    except Exception:  # pragma: no cover - defensive
        payload, fb, rej = None, None, None

    payload_fp: tuple[Any, ...] | None = None
    if payload is not None:
        try:
            branches = [
                getattr(b, "score_provenance", "") or "" for b in list(getattr(payload, "branches", []) or [])
            ]
            traps = [
                getattr(b, "score_provenance", "") or ""
                for b in list(getattr(payload, "blunder_traps", []) or [])
            ]
            payload_fp = (
                str(getattr(payload, "eval_source", "") or ""),
                len(branches),
                len(traps),
                tuple(sorted(branches)),
                tuple(sorted(traps)),
                str(getattr(payload, "root_win_probability", None)),
            )
        except Exception:  # pragma: no cover - defensive
            payload_fp = None
    return (
        len(hand),
        len(battlefield),
        iids,
        zones.get("opponent_hand_count"),
        payload_fp,
        fb,
        rej,
    )


def collect_evidence_block(game_state: dict[str, Any] | None) -> EvidenceBlock:
    """Build an :class:`EvidenceBlock` from real MageZero sources.

    Never raises; every source degrades to None/False when missing, so the
    conversation keeps working with MageZero fully absent.
    """
    ev = EvidenceBlock()
    try:
        fb, rej = _magezero_reasons()
    except Exception:  # pragma: no cover - defensive
        fb, rej = None, None
    ev.eval_source = _HEURISTIC_EVAL_SOURCE

    if not isinstance(game_state, dict):
        ev.fallback_reason = fb or rej
        ev.uncertainty_reason = _unavailable_sentence(fb or rej)
        return ev

    # Deck gating: (is_active, similarity, eval_source_label)
    deck_active = False
    similarity: float | None = None
    try:
        from arenamcp.magezero_gating import is_hero_deck_gated  # local import: optional dependency

        deck_active, similarity, label = is_hero_deck_gated(game_state)
        if label:
            ev.eval_source = str(label)
    except Exception:
        logger.debug("hero-deck gating unavailable for evidence", exc_info=True)
    ev.deck_supported = bool(deck_active)
    ev.deck_compatible = bool(deck_active)
    ev.similarity = float(similarity) if isinstance(similarity, (int, float)) else None

    # Provenance / evaluated-row detection from the cached tactical payload.
    try:
        payload = _mcts_last_payload()
    except Exception:  # pragma: no cover - defensive
        payload = None
    provenance: str | None = None
    if payload is not None:
        provs = [
            str(getattr(branch, "score_provenance", "") or "")
            for branch in list(getattr(payload, "branches", []) or [])
            + list(getattr(payload, "blunder_traps", []) or [])
        ]
        provs = [p for p in provs if p]
        if provs:
            provenance = max(provs, key=lambda p: _PROVENANCE_RANK.get(p, -1))
        ev.provenance = provenance
        # ``evaluated`` requires a real neural afterstate row, not just an
        # experimental label: policy-only/heuristic branches never count.
        ev.evaluated = (
            provenance == _EVALUATED_PROVENANCE
            and str(getattr(payload, "eval_source", "") or "") != _HEURISTIC_EVAL_SOURCE
        )
        if ev.evaluated:
            try:
                ev.root_win_probability = float(payload.root_win_probability)
            except (TypeError, ValueError):
                ev.root_win_probability = None

    # Model identity only when the deck passes the gate (selection exists).
    if ev.deck_supported:
        try:
            ev.model_id, ev.checkpoint_hash = _selection_identity(game_state)
        except Exception:  # pragma: no cover - defensive
            ev.model_id, ev.checkpoint_hash = None, None

    ev.fallback_reason = fb or rej
    if ev.fallback_reason and not (ev.evaluated or ev.provenance):
        # An active fallback/reject reason explains WHY model evidence is
        # absent — surface it as the uncertainty reason.
        ev.uncertainty_reason = _unavailable_sentence(ev.fallback_reason)
    elif ev.evaluated or ev.provenance:
        ev.uncertainty_reason = UNCALIBRATED_EVIDENCE_CAVEAT
    elif ev.is_supported():
        ev.uncertainty_reason = (
            "No evaluated model evidence for this position yet; coaching continues from observed board facts."
        )
    else:
        ev.uncertainty_reason = _unavailable_sentence(None)
    ev.payload = {"similarity": ev.similarity, "fallback_reason": fb, "reject_reason": rej}
    return ev


def _unavailable_sentence(reason: str | None) -> str:
    suffix = f" ({reason})" if reason else ""
    return f"MageZero evidence is unavailable{suffix}; coaching continues from observed board facts alone."


def format_evidence_lines(evidence: EvidenceBlock | None) -> str:
    """Render the compact evidence block for prompts (identity, support,
    provenance, uncertainty). Plain and useful when MageZero is absent."""
    if evidence is None:
        return _unavailable_sentence(None)
    lines: list[str] = []
    identity: list[str] = []
    if evidence.model_id:
        identity.append(f"model={evidence.model_id}")
    if evidence.checkpoint_hash:
        identity.append(f"checkpoint={evidence.checkpoint_hash}")
    if identity:
        lines.append("MageZero evidence identity: " + ", ".join(identity))
    if evidence.is_supported():
        sim_txt = (
            f" (similarity {evidence.similarity:.0%})"
            if isinstance(evidence.similarity, (int, float))
            else ""
        )
        lines.append(f"Deck support: supported{sim_txt}, source: {evidence.eval_source or 'MageZero'}")
    else:
        lines.append(
            "Deck support: not supported — "
            f"{evidence.eval_source or _HEURISTIC_EVAL_SOURCE} remains the basis; "
            "coaching continues usefully without MageZero."
        )
    if evidence.provenance:
        label = _PROVENANCE_LABELS.get(evidence.provenance)
        if evidence.evaluated:
            score_txt = ""
            if evidence.root_win_probability is not None:
                pct = int(round(evidence.root_win_probability * 100))
                score_txt = f"; model score (uncalibrated): {pct}%"
            lines.append(
                f"Provenance: {evidence.provenance} "
                f"(evaluated one-ply afterstates, not full rules-engine search{score_txt})"
            )
        elif label is not None and label != "evaluated":
            # Wave 5: payload-style label per provenance class instead of a
            # blanket "policy preference" tag. The "evaluated" label is
            # reserved for actually-evaluated rows (branch above) so a
            # neural_afterstate provenance with a heuristic eval_source can
            # never read as an evaluated outcome.
            lines.append(f"Provenance: {evidence.provenance} ({label})")
        else:
            lines.append(f"Provenance: {evidence.provenance} (policy preference, not an evaluated outcome)")
    if evidence.uncertainty_reason:
        lines.append(f"Uncertainty: {evidence.uncertainty_reason}")
    elif evidence.fallback_reason:
        lines.append(f"Fallback/reject reason: {evidence.fallback_reason}")
    return "\n".join(lines) if lines else _unavailable_sentence(None)


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
class PendingQuestion:
    """A user question deferred by an urgent interrupt, kept for later.

    ``ts`` defaults to wall clock for direct constructions, but the
    controller stamps deferred questions with its suppression clock (see
    ``ConversationController._now``) so TTL checks share one clock base.
    """

    text: str
    ts: float = field(default_factory=time.time)
    match_id: str | None = None


@dataclass
class MatchMemory:
    """Compact per-match conversation memory. All mutation is controller-locked."""

    turns: list[ConversationTurn] = field(default_factory=list)
    discussed_topics: dict[str, float] = field(default_factory=dict)
    revealed_opponent_cards: list[str] = field(default_factory=list)
    plan_summary: str = ""
    last_evidence: EvidenceBlock | None = None
    # Wave 3: last proactive topic utterance (monotonic-free wall clock) and
    # questions deferred by urgent interrupts, newest last, capped.
    last_proactive_ts: float = 0.0
    pending_questions: list[PendingQuestion] = field(default_factory=list)
    # Previous plan summary, kept so plan-drift topics can detect change.
    plan_summary_prev: str = ""

    def append(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)
        if len(self.turns) > MEMORY_RING_SIZE:
            del self.turns[: len(self.turns) - MEMORY_RING_SIZE]

    def record_pending_question(
        self, text: str, match_id: str | None = None, ts: float | None = None
    ) -> None:
        """Defer a question for later recovery (capped, newest kept).

        ``ts`` defaults to the monotonic clock (Wave 5) so a backward
        wall-clock jump can never extend a deferral's lifetime; callers with
        their own clock base pass ``ts`` explicitly.
        """
        self.pending_questions.append(
            PendingQuestion(text=str(text), match_id=match_id, ts=time.monotonic() if ts is None else ts)
        )
        if len(self.pending_questions) > PENDING_QUESTION_CAP:
            del self.pending_questions[: len(self.pending_questions) - PENDING_QUESTION_CAP]


# ---------------------------------------------------------------------------
# Topic selection (Wave 3 — proactive game-dynamics commentary)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopicCandidate:
    """One proactive-commentary candidate produced by TopicSelector."""

    key: str
    priority: EventPriority
    evidence: str  # observed facts; hidden-info claims phrased as hypotheses


# Material-change thresholds for snapshot-diff topics (mirrors the
# GamePlanManager material-signature philosophy: only *meaningful* movement
# generates commentary, never every turn/event).
_LIFE_DELTA = 3
_CREATURE_DELTA = 2
_LAND_DELTA = 2
_HAND_DELTA = 2


def _state_players(state: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if not isinstance(state, dict):
        return {}
    out: dict[int, dict[str, Any]] = {}
    for p in state.get("players", []) or []:
        if isinstance(p, dict) and isinstance(p.get("seat_id"), int):
            out[p["seat_id"]] = p
    return out


def _local_seat(state: dict[str, Any] | None) -> int | None:
    if not isinstance(state, dict):
        return None
    seat = state.get("local_seat_id")
    if isinstance(seat, int):
        return seat
    for p in state.get("players", []) or []:
        if isinstance(p, dict) and p.get("is_local") and isinstance(p.get("seat_id"), int):
            return p["seat_id"]
    return None


def _opponent_seat(state: dict[str, Any] | None, local_seat: int | None) -> int | None:
    if not isinstance(state, dict):
        return None
    seat = state.get("opponent_seat_id")
    if isinstance(seat, int):
        return seat
    for controller in {c.get("controller_seat_id") for c in _battlefield_cards(state)}:
        if isinstance(controller, int) and controller != local_seat:
            return controller
    return None


def _battlefield_cards(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []
    return [c for c in (state.get("battlefield", []) or []) if isinstance(c, dict)]


def _is_creature(card: dict[str, Any]) -> bool:
    return "Creature" in str(card.get("type_line", ""))


def _subtype_set(card: dict[str, Any]) -> set[str]:
    """Lowercase subtype tokens for comparison (see _subtype_values)."""
    return {t.lower() for t in _subtype_values(card)}


def _subtype_values(card: dict[str, Any]) -> list[str]:
    """Normalize MTGA-style subtype values to a list of original-casing strings.

    Values arrive either as a list (["Halfling", "Citizen"]) or as a
    stringified list ("['Halfling', 'Citizen']") — tolerate both without
    leaving bracket tokens behind.
    """
    raw = card.get("subtypes")
    if isinstance(raw, str):
        cleaned = raw.replace("[", "").replace("]", "").replace("'", "")
        return [t.strip() for t in cleaned.split(",") if t.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(t).strip() for t in raw if str(t).strip()]
    return []


def _is_land(card: dict[str, Any]) -> bool:
    return "Land" in str(card.get("type_line", ""))


def _cards_for_seat(state: dict[str, Any] | None, seat: int | None, predicate) -> list[dict[str, Any]]:
    if seat is None:
        return []
    return [c for c in _battlefield_cards(state) if c.get("controller_seat_id") == seat and predicate(c)]


def _life_total(state: dict[str, Any] | None, seat: int | None) -> int | None:
    if seat is None:
        return None
    player = _state_players(state).get(seat)
    if player is None:
        return None
    try:
        return int(player.get("life_total"))
    except (TypeError, ValueError):
        return None


def _hand_size(state: dict[str, Any] | None, seat: int | None) -> int | None:
    """Local hand comes from ``hand``; opponent hand size from the public zones."""
    if not isinstance(state, dict):
        return None
    local = _local_seat(state)
    if seat is not None and seat == local:
        return len([c for c in (state.get("hand", []) or []) if isinstance(c, dict)])
    zones = state.get("zones")
    if isinstance(zones, dict):
        try:
            count = zones.get("opponent_hand_count")
            if count is not None:
                return int(count)
        except (TypeError, ValueError):
            return None
    return None


def _instance_ids(state: dict[str, Any] | None) -> set[int]:
    ids: set[int] = set()
    for card in _battlefield_cards(state):
        iid = card.get("instance_id")
        if isinstance(iid, int):
            ids.add(iid)
    return ids


def _attackers_for_seat(state: dict[str, Any] | None, seat: int | None) -> bool:
    if seat is None:
        return False
    return any(c.get("is_attacking") for c in _cards_for_seat(state, seat, _is_creature))


class TopicSelector:
    """Derives prioritized proactive-commentary candidates from *meaningful*
    changes between consecutive snapshots — never from every turn or event.

    Input: ``(prev_state, curr_state, triggers, memory)``. Output: candidates
    sorted by descending :class:`EventPriority`. Every candidate carries a
    short evidence note that distinguishes observed facts (revealed cards,
    life totals) from hypotheses about hidden information.
    """

    def select(
        self,
        prev_state: dict[str, Any] | None,
        curr_state: dict[str, Any] | None,
        triggers: list[str] | None,
        memory: MatchMemory | None = None,
        threat_signature_prev: tuple[str, ...] | None = None,
    ) -> list[TopicCandidate]:
        if not isinstance(curr_state, dict):
            return []
        # Development/threat detection first so a just-revealed threat cites
        # "revealed" (the sharper observed fact) rather than "board includes".
        candidates: list[TopicCandidate] = []
        candidates.extend(self._opponent_development_topics(prev_state, curr_state, triggers))
        # Play-by-play coverage (2026-09-16): announce YOUR notable resolves
        # and combo synergies alongside the opponent developments.
        candidates.extend(self._local_play_topics(prev_state, curr_state))
        # M2 spam fix: the board-scan threat topic only fires when the threat
        # set is NEWLY detected (the trigger path already has its own
        # instance-id change detection). An unchanged threat set re-listed on
        # every batch must not produce a candidate.
        threat_sig = self._threat_signature(curr_state)
        threat_changed = threat_sig is not None and threat_sig != threat_signature_prev
        if threat_changed or (triggers and "threat_detected" in triggers):
            candidates.extend(self._threat_topics(curr_state, triggers))
        candidates.extend(self._role_shift_topics(prev_state, curr_state))
        candidates.extend(self._material_shift_topics(prev_state, curr_state))
        candidates.extend(self._material_assessment_topics(prev_state, curr_state, memory))
        candidates.extend(self._plan_drift_topics(memory))
        # Dedupe by topic key (a board threat also matches development) and
        # sort highest priority first; stable within a tier (detection order).
        deduped: dict[str, TopicCandidate] = {}
        for candidate in candidates:
            deduped.setdefault(candidate.key, candidate)
        return sorted(deduped.values(), key=lambda c: -int(c.priority))

    # -- (d) threats and interaction windows ---------------------------------

    def _threat_topics(self, curr_state: dict[str, Any], triggers: list[str] | None) -> list[TopicCandidate]:
        names = sorted({str(c.get("name")) for c in _battlefield_cards(curr_state) if _threat_card_name(c)})
        if triggers and "threat_detected" in triggers and not names:
            # The trigger machinery already carries the specific threat; the
            # snapshot names are the observed facts we can cite.
            board_names = sorted(
                {str(c.get("name")) for c in _battlefield_cards(curr_state) if c.get("name")}
            )
            if board_names:
                names = board_names[:3]
        if not names:
            return []
        joined = ", ".join(names)
        return [
            TopicCandidate(
                key="threat",
                priority=EventPriority.THREAT,
                evidence=(f"Opponent board now includes {joined} (observed)."),
            )
        ]

    def _threat_signature(self, curr_state: dict[str, Any]) -> tuple[str, ...] | None:
        """Change-detection key for the board-threat topic (M2 spam fix).

        Returns the sorted set of threat-card names currently on the opponent
        board, or ``None`` when there is no threat — so an UNCHANGED threat
        batch can be distinguished from a NEW one by the controller.
        """
        names = sorted({str(c.get("name")) for c in _battlefield_cards(curr_state) if _threat_card_name(c)})
        return tuple(names) if names else None

    # -- (a) matchup / opponent-archetype developments ------------------------

    def _opponent_development_topics(
        self,
        prev_state: dict[str, Any] | None,
        curr_state: dict[str, Any],
        triggers: list[str] | None,
    ) -> list[TopicCandidate]:
        prev_ids = _instance_ids(prev_state) if prev_state is not None else None
        local_seat = _local_seat(curr_state)
        new_cards: list[dict[str, Any]] = []
        if prev_ids is not None:
            for card in _battlefield_cards(curr_state):
                controller = card.get("controller_seat_id")
                iid = card.get("instance_id")
                if (
                    controller != local_seat
                    and isinstance(iid, int)
                    and iid not in prev_ids
                    and card.get("name")
                ):
                    new_cards.append(card)
        if not new_cards and triggers and "stack_spell_opponent" in triggers:
            # A spell is resolving/stacking for the opponent — names on the
            # stack are observed once they resolve; on the stack they are
            # still public information.
            stack = [c for c in (curr_state.get("stack", []) or []) if isinstance(c, dict)]
            opp_seat = _opponent_seat(curr_state, local_seat)
            new_cards = [
                c for c in stack if c.get("name") and c.get("controller_seat_id") in (opp_seat, None)
            ]

        if not new_cards:
            return []

        threat_names = [name for c in new_cards if (name := _threat_card_name(c))]
        names = sorted({str(c.get("name")) for c in new_cards})
        joined = ", ".join(names)
        if threat_names:
            # Threat-level development outranks a generic archetype note.
            return [
                TopicCandidate(
                    key="threat",
                    priority=EventPriority.THREAT,
                    evidence=(f"Opponent revealed {joined} (observed)."),
                )
            ]
        return [
            TopicCandidate(
                key="opponent_development",
                priority=EventPriority.STATE_SHIFT,
                evidence=(
                    f"Opponent revealed new permanent(s): {joined} (observed). "
                    "What this means for the matchup is a hypothesis."
                ),
            )
        ]

    # -- (a2) your own plays: key card entries + combo spotting ----------------

    def _local_play_topics(
        self,
        prev_state: dict[str, Any] | None,
        curr_state: dict[str, Any],
    ) -> list[TopicCandidate]:
        """Announce YOUR notable plays (play-by-play request 2026-09-16):
        a creature/planeswalker/artifact you just resolved by name, or a
        hand-drawn combo pair among your creatures (synergy tidbits).
        """
        if not prev_state:
            return []
        local_seat = _local_seat(curr_state) or _local_seat(prev_state)
        if local_seat is None:
            return []
        prev_ids = _instance_ids(prev_state)
        curr_ids = _instance_ids(curr_state)
        new_mine = [
            card
            for card in _cards_for_seat(curr_state, local_seat, lambda c: not _is_land(c))
            if isinstance(card.get("instance_id"), int)
            and card["instance_id"] not in prev_ids
            and card.get("name")
        ]
        if not new_mine:
            return []

        # Resolution disclosure gate: a card is only announced once its zone
        # (as surfaced by the snapshot) makes it public. Battlefield entries
        # in the snapshot are public — that is the gate.
        names = sorted({str(c.get("name")) for c in new_mine})
        topics: list[TopicCandidate] = [
            TopicCandidate(
                key="local_play",
                priority=EventPriority.STATE_SHIFT,
                evidence=(
                    f"You resolved: {', '.join(names[:3])} (observed). "
                    "Announce it play-by-play, with a brief card tidbit if interesting."
                ),
            )
        ]

        # Combo tidbit: creatures sharing a subtype among your NEW entries
        # and your board (synergy framing for the audience). Subtype values
        # arrive as MTGA-style stringified lists — normalize before set ops.
        # Pairs are canonicalized (name-sorted) so A+B and B+A dedupe.
        mine = _cards_for_seat(curr_state, local_seat, _is_creature)
        combo_pairs: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        for a in new_mine:
            if not _is_creature(a):
                continue
            a_subs = _subtype_set(a)
            for b in mine:
                if b is a or not b.get("name"):
                    continue
                pair = (str(a.get("name")), str(b.get("name")))
                pair = (min(pair), max(pair))
                if pair in seen_pairs:
                    continue
                shared = a_subs & _subtype_set(b)
                if shared:
                    seen_pairs.add(pair)
                    shared_lower = sorted(shared)[0]
                    # Preserve the card's original casing for the spoken label.
                    label = next(
                        (v for v in _subtype_values(b) if v.lower() == shared_lower),
                        shared_lower,
                    )
                    combo_pairs.append(f"{pair[0]} + {pair[1]} share {label}")
                if len(combo_pairs) >= 2:
                    break
            if len(combo_pairs) >= 2:
                break
        if combo_pairs:
            topics.append(
                TopicCandidate(
                    key="combo_tidbit",
                    priority=EventPriority.STATE_SHIFT,
                    evidence=(
                        "Observed synergy: " + "; ".join(dict.fromkeys(combo_pairs)) + ". "
                        "Frame it as an interesting combination for the audience."
                    ),
                )
            )
        return topics

    # -- (b) role shifts: attacking vs defending ------------------------------

    def _role_shift_topics(
        self, prev_state: dict[str, Any] | None, curr_state: dict[str, Any]
    ) -> list[TopicCandidate]:
        if not prev_state:
            return []
        local_seat = _local_seat(curr_state) or _local_seat(prev_state)
        opp_seat = _opponent_seat(curr_state, local_seat) or _opponent_seat(prev_state, local_seat)
        if local_seat is None or opp_seat is None:
            return []
        was_attacking = _attackers_for_seat(prev_state, local_seat)
        is_attacking = _attackers_for_seat(curr_state, local_seat)
        opp_was_attacking = _attackers_for_seat(prev_state, opp_seat)
        opp_is_attacking = _attackers_for_seat(curr_state, opp_seat)
        # A shift fires exactly once per flip: the next comparison starts from
        # the flipped baseline, so sustained roles never re-trigger.
        if not was_attacking and is_attacking and not opp_is_attacking:
            return [
                TopicCandidate(
                    key="role_shift",
                    priority=EventPriority.STATE_SHIFT,
                    evidence=(
                        "Your creatures were not attacking last check and now "
                        "have attacking creatures while the opponent does not "
                        "(observed) — you are on the offensive."
                    ),
                )
            ]
        if was_attacking and not is_attacking and opp_is_attacking:
            return [
                TopicCandidate(
                    key="role_shift",
                    priority=EventPriority.STATE_SHIFT,
                    evidence=(
                        "You had attacking creatures last check and now the "
                        "opponent does while yours do not (observed) — "
                        "shifting to defense."
                    ),
                )
            ]
        return []

    # -- (c) mana / card advantage / tempo / clock changes --------------------

    def _material_shift_topics(
        self, prev_state: dict[str, Any] | None, curr_state: dict[str, Any]
    ) -> list[TopicCandidate]:
        if not prev_state:
            return []
        local_seat = _local_seat(curr_state) or _local_seat(prev_state)
        opp_seat = _opponent_seat(curr_state, local_seat) or _opponent_seat(prev_state, local_seat)
        changes: list[str] = []
        card_changes: list[str] = []

        my_life = (_life_total(prev_state, local_seat), _life_total(curr_state, local_seat))
        opp_life = (_life_total(prev_state, opp_seat), _life_total(curr_state, opp_seat))
        if None not in my_life and abs(my_life[1] - my_life[0]) >= _LIFE_DELTA:
            changes.append(f"your life {my_life[0]}→{my_life[1]}")
        if None not in opp_life and abs(opp_life[1] - opp_life[0]) >= _LIFE_DELTA:
            changes.append(f"opponent life {opp_life[0]}→{opp_life[1]}")

        my_creatures = (
            len(_cards_for_seat(prev_state, local_seat, _is_creature)),
            len(_cards_for_seat(curr_state, local_seat, _is_creature)),
        )
        opp_creatures = (
            len(_cards_for_seat(prev_state, opp_seat, _is_creature)),
            len(_cards_for_seat(curr_state, opp_seat, _is_creature)),
        )
        if abs(my_creatures[1] - my_creatures[0]) >= _CREATURE_DELTA:
            changes.append(f"your creatures {my_creatures[0]}→{my_creatures[1]}")
        if abs(opp_creatures[1] - opp_creatures[0]) >= _CREATURE_DELTA:
            changes.append(f"opponent creatures {opp_creatures[0]}→{opp_creatures[1]}")

        my_lands = (
            len(_cards_for_seat(prev_state, local_seat, _is_land)),
            len(_cards_for_seat(curr_state, local_seat, _is_land)),
        )
        opp_lands = (
            len(_cards_for_seat(prev_state, opp_seat, _is_land)),
            len(_cards_for_seat(curr_state, opp_seat, _is_land)),
        )
        if abs(my_lands[1] - my_lands[0]) >= _LAND_DELTA:
            changes.append(f"your lands {my_lands[0]}→{my_lands[1]}")
        if abs(opp_lands[1] - opp_lands[0]) >= _LAND_DELTA:
            changes.append(f"opponent lands {opp_lands[0]}→{opp_lands[1]}")

        my_hand = (_hand_size(prev_state, local_seat), _hand_size(curr_state, local_seat))
        opp_hand = (_hand_size(prev_state, opp_seat), _hand_size(curr_state, opp_seat))
        if None not in my_hand and abs(my_hand[1] - my_hand[0]) >= _HAND_DELTA:
            card_changes.append(f"your hand {my_hand[0]}→{my_hand[1]}")
        if None not in opp_hand and abs(opp_hand[1] - opp_hand[0]) >= _HAND_DELTA:
            card_changes.append(f"opponent hand count {opp_hand[0]}→{opp_hand[1]}")

        if not changes and not card_changes:
            return []
        evidence = "Observed changes: " + ", ".join(changes + card_changes) + "."
        if card_changes:
            evidence += " What the opponent holds is a hypothesis."
        return [
            TopicCandidate(
                key="material_shift",
                priority=EventPriority.STATE_SHIFT,
                evidence=evidence,
            )
        ]

    # -- (e) plan success / drift ---------------------------------------------

    def _plan_drift_topics(self, memory: MatchMemory | None) -> list[TopicCandidate]:
        if memory is None:
            return []
        prev = getattr(memory, "plan_summary_prev", "") or ""
        curr = memory.plan_summary or ""
        # The controller refreshes plan_summary from GamePlanManager when
        # reachable; a *changed* non-empty plan is the drift signal. Without a
        # reachable manager, an externally-seeded plan_summary change counts.
        if prev and curr and prev != curr:
            return [
                TopicCandidate(
                    key="plan_update",
                    priority=EventPriority.STATE_SHIFT,
                    evidence=(f"The game plan changed: {curr}"),
                )
            ]
        return []

    # -- Wave 4: material assessment (MageZero evidence gate) -----------------

    def _material_assessment_topics(
        self,
        prev_state: dict[str, Any] | None,
        curr_state: dict[str, Any] | None,
        memory: MatchMemory | None,
    ) -> list[TopicCandidate]:
        """Material-assessment commentary ONLY under full evidence support.

        Requires: deck-supported evidence AND neural_afterstate provenance AND
        a valid comparison (a real evaluated row — ``evaluated`` True with a
        present root score) AND an actual material change this cycle (a score
        moving by itself NEVER triggers speech). The existing tactical
        ``material_shift`` ranking is untouched; this topic adds
        model-evidence framing and always carries the uncertainty sentence.
        """
        if not prev_state or not isinstance(curr_state, dict):
            return []
        if not self._material_shift_topics(prev_state, curr_state):
            return []
        evidence = getattr(memory, "last_evidence", None) if memory is not None else None
        if evidence is None:
            return []
        if not evidence.is_supported():
            return []
        if evidence.provenance != _EVALUATED_PROVENANCE:
            return []
        if not evidence.evaluated or evidence.root_win_probability is None:
            return []
        return [
            TopicCandidate(
                key="material_assessment",
                priority=EventPriority.STATE_SHIFT,
                evidence=(
                    f"Model evidence ({evidence.eval_source or 'MageZero'}, "
                    f"similarity {evidence.similarity:.0%}) supports assessing "
                    "how the material balance is trending. "
                    f"{UNCALIBRATED_EVIDENCE_CAVEAT}"
                ),
            )
        ]


def _threat_card_name(card: dict[str, Any]) -> str | None:
    name = card.get("name")
    if not name:
        return None
    from arenamcp.coach_triggers import GameStateTrigger  # local import: no cycle at module load

    if str(name) in GameStateTrigger.THREAT_CARDS:
        return str(name)
    return None


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
        now_fn: Callable[[], float] | None = None,
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
        # Wave 3: topic pipeline state — candidates from the last on_state
        # batch plus the selector instance (structurally simple, no coach I/O).
        self._topic_selector = TopicSelector()
        self._last_topics: list[TopicCandidate] = []
        self._last_topics_ts: float = 0.0
        # Wave 4: evidence pipeline state — the refreshed block lives on
        # memory.last_evidence; this signature detects actual input change so
        # evidence is recomputed (and prompt text rewritten) only when needed.
        self._last_evidence_sig: tuple[Any, ...] | None = None
        # Wave 5 (M2): last seen threat-card signature — a threat set that
        # does not change between batches never re-announces.
        self._threat_signature_prev: tuple[str, ...] | None = None
        # Wave 5: suppression-window clock (speaking cooldown, per-topic
        # repetition). Injectable for tests; the production default is the
        # monotonic clock (see :func:`_gate_now`) so a backward wall-clock
        # jump can never freeze or instantly expire suppression.
        # ConversationTurn.ts stays wall-clock (display timestamps) and the
        # pending-question TTL stays wall-clock too (PendingQuestion.ts is
        # constructed with wall-clock timestamps across the codebase).
        self._now: Callable[[], float] = now_fn or _gate_now

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
            had_pending = bool(self._pending)
            self._pending.clear()

        # Status lifecycle (M6): clearing a non-empty pending set orphans any
        # in-flight answer's delivery — emit idle so the UI never stays stuck
        # on "thinking" after a mode switch.
        if had_pending:
            self._emit_idle()

        # Mode transitions must be LOUD: conversation mode logs nothing on its
        # success paths, so an unlogged switch made mode state undiscoverable
        # from the log alone (2026-09-16 Mac session — mode flipped mid-match
        # and the log could not prove which mode dispatched).
        logger.info(f"Conversation mode set: {mode} (persist={persist})")

        if persist:
            try:
                get_settings().set("conversation_mode", mode)
            except Exception:
                logger.warning("Failed to persist conversation_mode", exc_info=True)

        # Confirmation only for USER-initiated switches (persist=True).
        # Boot-restore uses persist=False and must stay silent.
        if persist:
            self._speak_mode_confirmation(mode)

    def _speak_mode_confirmation(self, mode: str) -> None:
        """Short audible confirmation on user-initiated mode switches.

        A silent switch made mode state undiscoverable in live play (the
        2026-09-16 Mac session flipped mid-match unnoticed). Confirmation
        speech is identification-priority, never stale-dropped identity
        machinery — keep it tiny and unconditional except for mute.
        """
        phrase = "Conversation mode on." if mode == CONVERSATION else "Turn advice mode."
        vo = getattr(self._coach, "_voice_output", None)
        if vo is None or not hasattr(vo, "speak"):
            return
        try:
            vo.speak(phrase)
        except Exception:
            logger.debug("mode confirmation speech failed", exc_info=True)

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

    def _refresh_evidence(self, curr_state: dict[str, Any] | None) -> None:
        """Refresh ``memory.last_evidence`` only when evidence inputs changed.

        Cheap guard: the change signature covers board/hand shape, the cached
        tactical payload CONTENT, and client fallback/reject reasons — the
        things that can actually alter an EvidenceBlock. Never raises.

        Thread safety (Wave 5 TOCTOU fix): the shared signature field is
        re-read inside the write lock (compare-and-set) and the write is
        skipped when another refresh advanced it while our (potentially slow)
        evidence collection ran, so a concurrent refresh can never regress
        ``memory.last_evidence`` to an older snapshot.
        """
        try:
            sig = _evidence_signature(curr_state)
            with self._lock:
                if sig is not None and sig == self._last_evidence_sig:
                    return
                prior_sig = self._last_evidence_sig
            evidence = collect_evidence_block(curr_state)
            with self._lock:
                # Compare-and-set: if another refresh advanced the shared
                # signature while we were collecting, our snapshot is stale —
                # skip the write (the newer refresh owns the field, or the
                # next on_state will write).
                if self._last_evidence_sig != prior_sig:
                    return
                self.memory.last_evidence = evidence
                self._last_evidence_sig = sig
        except Exception:
            logger.debug("evidence refresh failed", exc_info=True)

    def _sweep_expired_pending_questions(self) -> None:
        """TTL sweep of deferred questions (M3) — expire unconditionally.

        Deferred questions older than :data:`PENDING_QUESTION_TTL_SECONDS` are
        removed regardless of any urgent topic in flight, so a stale question
        can never block topic speech forever. The sweep runs on the
        controller's suppression clock (monotonic in production, Wave 5). A
        negative elapsed time means the question's timestamp is on a
        different clock base (or predates a backward jump) — its age is
        unverifiable, so it is dropped rather than trusted.
        """
        now = self._now()
        with self._lock:
            expired = [
                pq
                for pq in self.memory.pending_questions
                if not 0 <= now - pq.ts <= PENDING_QUESTION_TTL_SECONDS
            ]
            if expired:
                self.memory.pending_questions = [
                    pq for pq in self.memory.pending_questions if pq not in expired
                ]

    def on_state(
        self,
        curr_state: dict[str, Any] | None,
        prev_state: dict[str, Any] | None,
        triggers: list[str] | None = None,
    ) -> None:
        """Record the trigger batch in memory, refresh plan info, and remember
        the candidate topics for the loop to gate and speak.

        Wave-2 slice semantics (memory-record only) are preserved; Wave 3 adds
        topic selection. No speech happens here — the coaching loop asks
        :meth:`speak_topic_if_any` after the batch is dispatched so urgency
        ordering (CRITICAL legacy dispatch first) is respected.
        """
        triggers = [str(t) for t in (triggers or [])]
        if triggers:
            with self._lock:
                self.memory.append(
                    ConversationTurn(
                        role="state",
                        text=", ".join(triggers),
                        identity=self.current_identity(request_id=self._request_counter),
                        trigger=", ".join(triggers),
                    )
                )

        self._refresh_plan_summary()
        self._refresh_evidence(curr_state)
        # M3: expire stale deferred questions on every state batch so they
        # unblock topic speech and can never recover stale answers.
        self._sweep_expired_pending_questions()

        with self._lock:
            self._last_topics = self._topic_selector.select(
                prev_state,
                curr_state,
                triggers,
                self.memory,
                threat_signature_prev=self._threat_signature_prev,
            )
            self._last_topics_ts = time.time()
            _pb = prev_state or {}
            _cb = curr_state or {}
            _pi = {c.get("instance_id") for c in (_pb.get("battlefield") or []) if isinstance(c, dict)}
            _ci = {c.get("instance_id") for c in (_cb.get("battlefield") or []) if isinstance(c, dict)}
            logger.info(
                "convo-diag: on_state topics=%s keys=%s | prev_bf=%s cur_bf=%s new_ids=%s local=%s opp=%s prev_empty=%s",
                len(self._last_topics),
                [t.key for t in self._last_topics],
                len(_pb.get("battlefield") or []),
                len(_cb.get("battlefield") or []),
                len(_ci - _pi) if _pi else "all-new",
                _local_seat(_cb),
                _opponent_seat(_cb, _local_seat(_cb)),
                not _pb,
            )
            # Advance the threat baseline so the NEXT batch sees an unchanged
            # threat set as unchanged (not newly announced).
            self._threat_signature_prev = self._topic_selector._threat_signature(
                curr_state if isinstance(curr_state, dict) else {}
            )

    # -- proactive topics (Wave 3) -------------------------------------------

    def speak_topic_if_any(
        self, match_id: str | None = None, match_number: int = 0
    ) -> tuple[str, str, TopicCandidate] | None:
        """Gate and speak the best surviving proactive topic.

        Runs the full gate ladder — user-question priority, verbosity matrix,
        speaking cooldown, per-topic repetition suppression — and, when a
        topic survives, renders it through the coaching LLM and speaks it via
        the arbiter (``urgent`` for URGENT-class topics, ``proactive``
        otherwise). Returns ``(text, speech_priority, topic)`` when something
        was spoken, else ``None``. Designed to be called from the coaching
        loop after ``on_state``; never raises.

        Parameters are delivery-context only (Wave 5 review): delivery
        gating itself comes exclusively from ``current_identity()`` —
        ``match_id`` is forwarded to the urgent-interrupt path so deferred
        questions stay same-match relevant, and ``match_number`` is accepted
        for call-site symmetry with the loop's match identity but is not used
        for gating (it is not asserted against the identity because the
        loop's counter can legitimately differ transiently across a boundary;
        the session_id bump, not the number, is what gates delivery).
        """
        try:
            return self._speak_topic_if_any(match_id, match_number)
        except Exception:
            logger.exception("speak_topic_if_any failed")
            return None

    def _speak_topic_if_any(
        self, match_id: str | None, match_number: int
    ) -> tuple[str, str, TopicCandidate] | None:
        with self._lock:
            topics = list(self._last_topics)
            self._last_topics = []
        logger.info(
            "convo-diag: speak_topic_if_any drained=%s mode=%s",
            len(topics),
            self.mode,
        )
        if not topics or self.mode != CONVERSATION:
            return None

        # USER QUESTION PRIORITY: a pending (or recoverable) user question
        # always preempts topic speech.
        with self._lock:
            has_pending = bool(self._pending)
            has_deferred = bool(self.memory.pending_questions)
        if has_pending or has_deferred:
            return None

        try:
            return self._gate_and_speak_topics(topics, match_id, match_number)
        except Exception:
            # Status lifecycle (M6): any exception in the gate/render path
            # must leave the UI idle, never stuck on "thinking".
            logger.exception("topic gate/speak failed")
            self._emit_idle()
            return None

    def _gate_and_speak_topics(
        self, topics: list[TopicCandidate], match_id: str | None, match_number: int
    ) -> tuple[str, str, TopicCandidate] | None:
        cooldown = self._cooldown_seconds()
        # Suppression windows run on the injectable monotonic clock (Wave 5):
        # a backward wall-clock jump must never freeze or instantly expire a
        # cooldown/repetition window.
        now = self._now()

        verbosity = self.verbosity
        for topic in topics:
            # VERBOSITY MATRIX (questions bypass verbosity entirely; this gate
            # is topic-only): Quiet speaks only URGENT_DECISION/THREAT-class
            # topics; Balanced speaks STATE_SHIFT and above; Detailed speaks
            # everything except FILLER.
            if verbosity == VERBOSITY_QUIET and topic.priority < EventPriority.THREAT:
                continue
            if verbosity == VERBOSITY_BALANCED and topic.priority < EventPriority.STATE_SHIFT:
                continue
            if verbosity == VERBOSITY_DETAILED and topic.priority <= EventPriority.FILLER:
                continue

            with self._lock:
                # SPEAKING COOLDOWN: no proactive speech within N seconds of
                # the last proactive utterance. URGENT-class topics bypass
                # the cooldown — they interrupt in-flight speech by design —
                # but not the per-key minimum spacing below (M2: a NEW threat
                # is announced, then not re-announced 3× within one window).
                logger.info(
                    "convo-diag: gate topic=%s prio=%s elapsed=%s cooldown=%s",
                    topic.key,
                    int(topic.priority),
                    now - self.memory.last_proactive_ts,
                    cooldown,
                )
                if topic.priority < EventPriority.THREAT and now - self.memory.last_proactive_ts < cooldown:
                    return None
                # PER-KEY MINIMUM SPACING (applies to ALL topics including
                # urgent): the same topic key may re-speak only after
                # 3×cooldown — the urgent bypass lifts the global cooldown,
                # never a per-key re-announce cap.
                last_spoken = self.memory.discussed_topics.get(topic.key, 0.0)
                if now - last_spoken < cooldown * 3:
                    continue

            identity = self.current_identity()
            self._emit("conversation_status", state="thinking")

            reply = self._render_topic(topic)

            with self._lock:
                stale = identity.is_stale_vs(self.current_identity())

            if stale or is_backend_error_text(reply):
                # Tagged backend failures are transcript-only (identical to
                # the question path) — never spoken, topic not recorded, and
                # NOT appended to memory turns: the error text must never
                # leak into later prompt digests (Wave 5). Status lifecycle
                # (M6): stale/error paths return the UI to idle — the
                # thinking status must never get stuck.
                self._emit_idle()
                if not stale:
                    payload = identity.to_payload()
                    self._emit("conversation_reply", text=reply, identity=payload)
                return None

            spoken = strip_health_tags(reply)
            if not spoken:
                # Empty render: nothing was delivered; leave the UI idle.
                self._emit_idle()
                return None

            speech_priority = "urgent" if topic.priority >= EventPriority.THREAT else "proactive"

            with self._lock:
                self.memory.discussed_topics[topic.key] = now
                self.memory.last_proactive_ts = now
                self.memory.append(
                    ConversationTurn(
                        role="coach",
                        text=spoken,
                        identity=identity,
                        trigger="proactive_topic",
                        topic=topic.key,
                    )
                )

            self._speak_topic(spoken, speech_priority, identity, topic, match_id)

            self._emit("conversation_status", state="idle")
            return (spoken, speech_priority, topic)

        return None

    def _render_topic(self, topic: TopicCandidate) -> str:
        """Render a topic through the coaching LLM (same backend as questions).

        The prompt carries the topic evidence, the current plan summary, a
        last-2-turns digest, and instructions: keep it under ~2 sentences,
        never claim win probabilities, and phrase anything about the
        opponent's hidden hand/library as a hypothesis.
        """
        with self._lock:
            recent = [turn for turn in self.memory.turns[-6:] if turn.role in ("user", "coach")][-2:]
        digest_lines = []
        for turn in recent:
            prefix = "user" if turn.role == "user" else "coach"
            digest_lines.append(f"{prefix}: {turn.text[:120]}")
        digest = "\n".join(digest_lines) or "(none)"

        with self._lock:
            plan = self.memory.plan_summary or ""

        evidence = topic.evidence
        with self._lock:
            memory_evidence = self.memory.last_evidence
        evidence_block = format_evidence_lines(memory_evidence)
        question = (
            f"{TOPIC_PROMPT_PREFIX}\n"
            f"Topic: {topic.key}\n"
            f"Evidence: {evidence}\n"
            f"MageZero evidence:\n{evidence_block}\n"
            f"Current plan: {plan or '(not yet formed)'}\n"
            f"Recent conversation:\n{digest}\n"
            "Respond with at most two short sentences of commentary."
        )

        inner = getattr(self._coach, "_coach", None)
        if inner is None or not hasattr(inner, "get_advice"):
            return "[BACKEND ERROR] coach engine unavailable"
        snapshot = self._snapshot()
        try:
            return str(inner.get_advice(snapshot, question=question, conversational=True))
        except Exception as exc:
            logger.warning("topic get_advice failed: %s", exc, exc_info=True)
            return f"[BACKEND ERROR] {type(exc).__name__}: {exc}"

    def _speak_topic(
        self,
        text: str,
        speech_priority: str,
        identity: ResponseIdentity,
        topic: TopicCandidate,
        match_id: str | None,
    ) -> None:
        """Deliver topic speech; an URGENT-class topic preempts in-flight
        speech via the arbiter and defers any in-flight answer thread's
        question for recovery."""
        # URGENT INTERRUPT: preemption is the arbiter's job; we stop current
        # speech and let the higher-priority request win the channel.
        if speech_priority == "urgent":
            self._preempt_speech("urgent_topic")
            self._defer_pending_questions(match_id)

        spoken = False
        vs = getattr(self._coach, "voice_session", None)
        if vs is not None and hasattr(vs, "speak"):
            try:
                outcome = vs.speak(text, priority=speech_priority, identity=identity)
                spoken = bool(getattr(outcome, "played", True))
            except Exception:
                logger.debug("voice_session.speak failed (topic)", exc_info=True)
                spoken = True  # conservative: assume delivered rather than re-speaking
        else:
            vo = getattr(self._coach, "_voice_output", None)
            if vo is not None and hasattr(vo, "speak"):
                try:
                    vo.speak(text)
                    spoken = True
                except Exception:
                    logger.debug("voice_output.speak failed (topic)", exc_info=True)

        # Speaking-state surfacing (deferred ledger item 3): the engine now
        # emits 'speaking' when topic audio is accepted, reverting to idle via
        # the existing lifecycle paths; desktop maps these to the status label.
        if spoken:
            self._emit("conversation_status", state="speaking")

        if speech_priority == "urgent" and spoken:
            # PENDING-QUESTION RECOVERY: after the urgent speech COMPLETES
            # (not when it starts — a recovery answer must never preempt the
            # urgent topic it follows), re-answer a still-relevant deferred
            # question on a daemon thread.
            thread = threading.Thread(
                target=self._recover_after_speech_completes,
                args=(text, speech_priority, identity, match_id, match_number_from_identity(identity)),
                daemon=True,
                name="convo-topic-recovery",
            )
            with self._lock:
                self._answer_threads.append(thread)
            thread.start()

    def _recover_after_speech_completes(
        self,
        text: str,
        speech_priority: str,
        identity: Any,
        match_id: str | None,
        match_number: int,
    ) -> None:
        """Wait for the urgent topic's speech to finish, then recover.

        Never preempts the urgent utterance it follows and never raises.
        """
        try:
            vs = getattr(self._coach, "voice_session", None)
            self._wait_for_speech_completion(vs)
            # Race guard: only recover in conversation mode (a mode switch
            # mid-utterance invalidates the deferral).
            with self._lock:
                if self.mode != CONVERSATION:
                    return
            self._recover_pending_question(match_id, match_number)
        except Exception:
            logger.exception("pending-question recovery failed")

    def _wait_for_speech_completion(self, vs: Any, timeout: float = 30.0) -> None:
        """Block until the arbiter channel is free after the urgent speech.

        Real :class:`VoiceSession` arbiters expose ``wait_for_idle`` ( backed
        by the C1 completion machinery); anything else degrades to a bounded
        grace wait so a recovery answer can never preempt the topic
        immediately. Thread-safety: called from the recovery daemon only.

        The grace loop uses ``time.tool_sleep`` (aliased at import) rather
        than ``time.sleep`` so tests that monkeypatch ``time.sleep`` on the
        shared time module (run_loop in test_standalone_conversation.py)
        cannot have a lingering recovery daemon consume their sleep budget
        and cut their coaching loop short (2026-09-16 suite-order flake).
        """
        try:
            from arenamcp.voice_session import VoiceSession  # local import: avoids cycle

            if isinstance(vs, VoiceSession):
                vs.wait_for_idle(timeout)
                return
        except Exception:  # pragma: no cover - defensive
            logger.debug("voice_session wait_for_idle unavailable", exc_info=True)
        # Non-arbiter sink: no completion signal — bounded grace period.
        grace_end = time.monotonic() + _RECOVERY_GRACE_SECONDS
        while time.monotonic() < grace_end:
            _tool_sleep(0.05)

    def _defer_pending_questions(self, match_id: str | None) -> None:
        """Snapshot pending user questions into memory for later recovery."""
        with self._lock:
            if not self._pending:
                return
            # The most recent user turn is the in-flight question thread's
            # text (on_user_question appends it before the answer spawns).
            turn = next((t for t in reversed(self.memory.turns) if t.role == "user"), None)
            if turn is None:
                return
            # Stamp with the controller's suppression clock so the TTL sweep
            # and recovery compare timestamps on ONE clock base (Wave 5).
            self.memory.record_pending_question(turn.text, match_id, ts=self._now())

    def _recover_pending_question(self, match_id: str | None, match_number: int) -> None:
        """Answer the most recent still-relevant deferred question, if any.

        TTL checks use the controller's suppression clock (monotonic in
        production, Wave 5). A negative elapsed time means the question's
        timestamp is on a different clock base (or predates a backward
        jump) — its age is unverifiable, so it is expired rather than
        trusted.
        """
        with self._lock:
            candidates = list(self.memory.pending_questions)
        now = self._now()
        for pq in reversed(candidates):  # newest first
            if not 0 <= now - pq.ts <= PENDING_QUESTION_TTL_SECONDS:
                with self._lock:
                    if pq in self.memory.pending_questions:
                        self.memory.pending_questions.remove(pq)
                continue
            if match_id is not None and pq.match_id is not None and pq.match_id != match_id:
                # Same-match relevance rule: questions from a finished match
                # are no longer relevant.
                with self._lock:
                    if pq in self.memory.pending_questions:
                        self.memory.pending_questions.remove(pq)
                continue
            with self._lock:
                if pq in self.memory.pending_questions:
                    self.memory.pending_questions.remove(pq)
            self.on_user_question(pq.text, source="deferred")
            return

    def _refresh_plan_summary(self) -> None:
        """Fold the current GamePlanManager plan into memory (guarded).

        When the coach exposes a manager, ``coach_intro()`` becomes the plan
        summary and drift detection works against real plan state; otherwise
        plan_summary stays whatever was last seeded (Wave-2 behavior).
        """
        mgr = getattr(self._coach, "_game_plan_mgr", None)
        if mgr is None:
            inner = getattr(self._coach, "_coach", None)
            mgr = getattr(inner, "_game_plan_mgr", None) if inner is not None else None
        if mgr is None or not hasattr(mgr, "coach_intro"):
            return
        try:
            intro = str(mgr.coach_intro() or "")
        except Exception:
            logger.debug("plan coach_intro failed", exc_info=True)
            return
        if not intro:
            return
        with self._lock:
            if self.memory.plan_summary and self.memory.plan_summary != intro:
                self.memory.plan_summary_prev = self.memory.plan_summary
            self.memory.plan_summary = intro

    def _cooldown_seconds(self) -> float:
        try:
            value = get_settings().get("conversation_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
            return max(0.0, float(value))
        except Exception:
            return float(DEFAULT_COOLDOWN_SECONDS)

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

    def reset_for_match(
        self, match_id: str | None, match_number: int, match_start: bool = True
    ) -> None:
        # Match boundary: clear memory (including Wave-3 proactive-timing and
        # deferred-question fields), bump session identity, drop pending.
        # ``match_start``: True when a NEW match is beginning; False when the
        # boundary is a match ENDING (match_id -> None). The audible opener
        # only belongs to match START — announcing "Match underway" as the
        # match ended was wrong (live report 2026-09-16 10:09).
        in_conversation = self._mode == CONVERSATION
        # Capture the pre-reset plan summary for the match-start opener —
        # reset_for_match replaces MatchMemory below, so this must be read
        # BEFORE the wipe (the fresh memory has no plan until the first
        # _refresh_plan_summary in on_state).
        plan_hint = ""
        if match_start:
            with contextlib.suppress(Exception):
                plan_hint = str(self.memory.plan_summary or "")
        with self._lock:
            had_pending = bool(self._pending)
            self.memory = MatchMemory()
            self._session_id += 1
            self._pending.clear()
            self._last_topics = []
            self._last_evidence_sig = None  # Wave 4: force evidence re-collection
            self._threat_signature_prev = None  # Wave 5 (M2): new board baseline
        # Status lifecycle (M6): see set_mode — orphaned answers must clear
        # the UI's "thinking" status.
        if had_pending:
            self._emit_idle()
        # Match-start handshake (user request 2026-09-16): in conversation
        # mode, announce the session audibly when a NEW match begins so mode
        # state is knowable without looking at the panel. Match-END boundaries
        # (match_id -> None) reset silently — no "Match underway" as the match
        # ends. The plan summary is seeded by the first _refresh_plan_summary
        # (GamePlanManager intro); the opener speaks immediately with whatever
        # identity exists now.
        if in_conversation and match_start:
            self._speak_match_opener(match_id, plan_hint=plan_hint)

    def _speak_match_opener(self, match_id: str | None, plan_hint: str = "") -> None:
        """Short audible match-start handshake in conversation mode.

        Doubles as the auditory mode indicator: hearing the opener IS the
        confirmation that conversation mode is live for this match. Uses the
        raw voice sink (not the arbiter) with no identity — it must never be
        stale-dropped, it always plays at the match boundary. ``plan_hint``
        carries the pre-reset plan summary (reset_for_match wipes memory
        before this runs, so the fresh MatchMemory cannot be read here).
        """
        plan = (plan_hint or "").strip()[:140]
        opener = (
            f"Conversation mode. Match underway. {plan}"
            if plan
            else "Conversation mode. Match underway. I'll call the swings as they come."
        )
        vo = getattr(self._coach, "_voice_output", None)
        if vo is None or not hasattr(vo, "speak"):
            logger.info(
                "conversation-opener: no voice sink (voice_output=%r) — opener not spoken",
                vo,
            )
            return
        try:
            vo.speak(opener)
            logger.info("conversation-opener spoken: %s", opener)
        except Exception:
            logger.warning("match-opener speech failed", exc_info=True)

    def cancel_pending(self) -> None:
        # Invalidate in-flight requests: their identities no longer match
        # pending set membership, so answers are discarded on delivery.
        with self._lock:
            had_pending = bool(self._pending)
            self._pending.clear()
        # Status lifecycle (M6): a stop/cancel that drops a non-empty pending
        # set must also release the "thinking" status.
        if had_pending:
            self._emit_idle()

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

        match_id = getattr(self._coach, "last_match_id", None) or snapshot.get("match_id") or None
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

    def _emit_idle(self) -> None:
        """Emit conversation_status idle — M6 lifecycle fix.

        ``thinking`` is emitted when a topic/question render starts; EVERY
        exit path that leaves nothing pending/speaking must return the UI to
        idle, including stale-after-cancel, backend-error, and exception
        paths. Guarded: safe to call redundantly.
        """
        self._emit("conversation_status", state="idle")

    def _augment_question(self, text: str) -> str:
        # Replay the recent conversation as a compact digest so the coaching
        # LLM can answer follow-ups without a second stateful channel, plus
        # the compact MageZero evidence block (Wave 4): identity/support/
        # uncertainty lines only — never an uncalibrated win-probability claim.
        with self._lock:
            recent = list(self.memory.turns[-6:])
            memory_evidence = self.memory.last_evidence

        evidence_block = format_evidence_lines(memory_evidence)
        evidence_useful = bool(
            memory_evidence is not None
            and (memory_evidence.is_supported() or memory_evidence.provenance or memory_evidence.model_id)
        )
        lines: list[str] = []
        for turn in recent:
            if turn.role == "user":
                lines.append(f"user: {turn.text}")
            elif turn.role == "coach":
                lines.append(f"coach: {turn.text}")

        if not lines and not evidence_useful:
            # No history and nothing meaningful from MageZero: keep the raw
            # question (conversation remains useful without MageZero), framed
            # with the broadcast-announcer voice.
            return f"{ANNOUNCER_QUESTION_PREFIX}\n\nUser question: {text}"

        if not lines:
            return (
                f"{ANNOUNCER_QUESTION_PREFIX}\n\n"
                f"{QUESTION_EVIDENCE_INSTRUCTIONS}\n"
                f"MageZero evidence:\n{evidence_block}\n\n"
                f"User question: {text}"
            )

        digest = "\n".join(lines)
        return (
            f"{ANNOUNCER_QUESTION_PREFIX}\n\n"
            f"Recent conversation:\n{digest}\n\n"
            f"{QUESTION_EVIDENCE_INSTRUCTIONS}\n"
            f"MageZero evidence:\n{evidence_block}\n\n"
            f"User question: {text}"
        )

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
            # Supersession is by REQUEST ID, not completion order: a newer
            # request that already finished (and popped itself from _pending)
            # still supersedes this one — otherwise an older answer can slip
            # through in the completion race.
            counter = self._request_counter
        if newer or counter > identity.request_id:
            return True

        live = self.current_identity(request_id=identity.request_id)
        return identity.is_stale_vs(live)

    def _answer(self, request_id: int, text: str, identity: ResponseIdentity) -> None:
        # Daemon answer thread body: never raises out of the thread.
        try:
            snapshot = self._snapshot()
            # Wave 4: ensure the evidence block reflects the live state at
            # answer time (guarded + change-gated; no-op when unchanged).
            self._refresh_evidence(snapshot)
            augmented = self._augment_question(text)

            inner = getattr(self._coach, "_coach", None)
            if inner is None or not hasattr(inner, "get_advice"):
                reply = "[BACKEND ERROR] coach engine unavailable"
            else:
                try:
                    reply = inner.get_advice(snapshot, question=augmented, conversational=True)
                except Exception as exc:
                    logger.warning("get_advice failed: %s", exc, exc_info=True)
                    reply = f"[BACKEND ERROR] {type(exc).__name__}: {exc}"

            if not isinstance(reply, str) or not reply.strip():
                reply = "[BACKEND ERROR] empty response from coach backend"

            if self._is_stale(identity):
                # Superseded / stale answers are dropped silently. Status
                # lifecycle (M6): the question's "thinking" status must still
                # resolve to idle — the answer thread owns that transition.
                with self._lock:
                    self._pending.pop(identity.request_id, None)
                self._emit_idle()
                return

            payload = identity.to_payload()
            self._emit("conversation_reply", text=reply, identity=payload)

            # Backend-error replies are transcript-only (Wave 5): they are
            # emitted for display but never appended to memory turns, so a
            # failure message can never leak into later prompt digests.
            if not is_backend_error_text(reply):
                with self._lock:
                    self.memory.append(
                        ConversationTurn(
                            role="coach",
                            text=reply,
                            identity=identity,
                        )
                    )
            with self._lock:
                self._pending.pop(identity.request_id, None)

            self._speak(reply, identity)
            self._emit("conversation_status", state="idle", request_id=request_id)
        except Exception:
            # Absolute backstop: the thread must never crash the engine.
            logger.exception("conversation answer thread crashed")
            # Status lifecycle (M6): a crashed answer must still clear the
            # "thinking" status rather than leave the UI stuck.
            self._emit_idle()

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
                # Speaking-state surfacing (deferred ledger item 3): accepted
                # question speech flips the label to 'speaking'; the desktop
                # clears it when the next reply/status event arrives.
                self._emit("conversation_status", state="speaking")
                return
            except Exception:
                logger.debug("voice_session.speak failed", exc_info=True)

        vo = getattr(self._coach, "_voice_output", None)
        if vo is not None and hasattr(vo, "speak"):
            try:
                vo.speak(spoken)
            except Exception:
                logger.debug("voice_output.speak failed", exc_info=True)
