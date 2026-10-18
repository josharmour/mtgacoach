"""Conversation Mode: proactive topic commentary, response identity, match memory.

Conversation mode is the play-by-play announcer: instead of turn advice, the
coach comments on meaningful board changes (non-critical triggers become
memory-only; CRITICAL triggers keep the legacy advice dispatch). The typed and
push-to-talk question channel was removed on 2026-10-18 (docs/DECISIONS.md).

- ``ResponseIdentity`` — canonical frozen identity stamped on topic speech so
  stale utterances can be discarded (session/match).
- ``MatchMemory`` — a compact, thread-safe per-match memory (turn ring,
  discussed topics, plan summary, proactive-speech timestamps).
- ``TopicSelector`` — derives prioritized candidate topics from *meaningful*
  state changes (opponent developments, role shifts, material swings, plan
  drift), never from every turn/event.
- ``ConversationController`` — the engine-side session: mode switching,
  match-boundary resets, and selecting/gating/speaking proactive topics
  behind cooldown and repetition gates.

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

VALID_MODES: tuple[str, ...] = (TURN_ADVICE, CONVERSATION)

# Default minimum seconds between proactive topic utterances (settings key
# ``conversation_cooldown_seconds``). Per-topic repetition windows are 3x this.
DEFAULT_COOLDOWN_SECONDS = 90

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
    'hypothesis ("they might have..."), never as an observed fact.'
)


class EventPriority(IntEnum):
    """Relative urgency of topic candidates (higher wins preemption)."""

    FILLER = 10
    STATE_SHIFT = 60
    TURN_CONTEXT = 40
    THREAT = 80
    URGENT_DECISION = 90


# ---------------------------------------------------------------------------
# Response identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResponseIdentity:
    """Immutable identity stamped on topic speech.

    ``session_id`` bumps on mode changes AND match boundaries, so an utterance
    is stale when the live identity differs in ``session_id``, ``match_id``,
    or ``match_number``. ``request_id`` has been 0 since the question channel
    went (2026-10-18); the speech arbiter still reads it as the sequence.
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
        # Session-scope staleness (Wave-2 contract): a topic utterance is
        # invalid when the SESSION (mode change or match boundary) or the
        # match moved underneath it. Position fields (turn_number /
        # active_player / decision_sig) are deliberately NOT compared: a live
        # LLM render takes seconds while the game advances, so position
        # equality can never hold at delivery time — every proactive topic
        # was silently dropped (live report 2026-09-16 15:41,
        # bug_20260916_154149). Position-bound gating belongs to the legacy
        # advice path's own staleness logic (standalone.py "Discarding stale
        # advice").
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
    # Last proactive utterance on the suppression clock. None means no
    # utterance yet; zero is a valid monotonic timestamp just after boot.
    last_proactive_ts: float | None = None
    # Previous plan summary, kept so plan-drift topics can detect change.
    plan_summary_prev: str = ""

    def append(self, turn: ConversationTurn) -> None:
        self.turns.append(turn)
        if len(self.turns) > MEMORY_RING_SIZE:
            del self.turns[: len(self.turns) - MEMORY_RING_SIZE]


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

    ``on_state`` records the trigger batch in memory and selects topics;
    ``speak_topic_if_any`` gates, renders and speaks the best one.
    """

    def __init__(
        self,
        coach: Any,
        snapshot_fn: Callable[[], dict[str, Any] | None] | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._coach = coach
        self._snapshot_fn = snapshot_fn

        self._lock = threading.RLock()
        self._mode: str = TURN_ADVICE
        self._session_id: int = 1
        self.memory = MatchMemory()
        # Topic pipeline state — candidates from the last on_state batch plus
        # the selector instance (structurally simple, no coach I/O).
        self._topic_selector = TopicSelector()
        self._last_topics: list[TopicCandidate] = []
        self._last_topics_ts: float = 0.0
        # Wave 5 (M2): last seen threat-card signature — a threat set that
        # does not change between batches never re-announces.
        self._threat_signature_prev: tuple[str, ...] | None = None
        # Wave 5: suppression-window clock (speaking cooldown, per-topic
        # repetition). Injectable for tests; the production default is the
        # monotonic clock (see :func:`_gate_now`) so a backward wall-clock
        # jump can never freeze or instantly expire suppression.
        # ConversationTurn.ts stays wall-clock (display timestamps).
        self._now: Callable[[], float] = now_fn or _gate_now

    # -- properties ---------------------------------------------------------

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    # -- mode ---------------------------------------------------------------

    def set_mode(self, mode: str, persist: bool = True) -> None:
        """Switch session mode; bumps session identity and stops current speech."""
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

    # -- state hook ---------------------------------------------------------

    def on_state(
        self,
        curr_state: dict[str, Any] | None,
        prev_state: dict[str, Any] | None,
        triggers: list[str] | None = None,
    ) -> None:
        """Record the trigger batch in memory, refresh plan info, and remember
        the candidate topics for the loop to gate and speak.

        No speech happens here — the coaching loop asks
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
                        identity=self.current_identity(),
                        trigger=", ".join(triggers),
                    )
                )

        self._refresh_plan_summary()

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

    # -- proactive topics ---------------------------------------------------

    def speak_topic_if_any(
        self, match_id: str | None = None, match_number: int = 0
    ) -> tuple[str, str, TopicCandidate] | None:
        """Gate and speak the best surviving proactive topic.

        Runs the gate ladder — priority floor, speaking cooldown, per-topic
        repetition suppression — and, when a topic survives, renders it
        through the coaching LLM and speaks it via the arbiter (``urgent``
        for URGENT-class topics, ``proactive`` otherwise). Returns ``(text,
        speech_priority, topic)`` when something was spoken, else ``None``.
        Designed to be called from the coaching loop after ``on_state``;
        never raises.

        ``match_id``/``match_number`` are the loop's match identity and are
        not used for gating: delivery is gated by ``current_identity()``
        alone (the session_id bump, not the loop's counter, is what gates
        delivery across a boundary).
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
        return self._gate_and_speak_topics(topics)

    def _gate_and_speak_topics(self, topics: list[TopicCandidate]) -> tuple[str, str, TopicCandidate] | None:
        cooldown = self._cooldown_seconds()
        # Suppression windows run on the injectable monotonic clock (Wave 5):
        # a backward wall-clock jump must never freeze or instantly expire a
        # cooldown/repetition window.
        now = self._now()

        for topic in topics:
            # Only STATE_SHIFT-and-above topics are spoken.
            if topic.priority < EventPriority.STATE_SHIFT:
                continue

            with self._lock:
                # SPEAKING COOLDOWN: no proactive speech within N seconds of
                # the last proactive utterance. URGENT-class topics bypass
                # the cooldown — they interrupt in-flight speech by design —
                # but not the per-key minimum spacing below (M2: a NEW threat
                # is announced, then not re-announced 3× within one window).
                last_proactive = self.memory.last_proactive_ts
                elapsed = now - last_proactive if last_proactive is not None else None
                logger.info(
                    "convo-diag: gate topic=%s prio=%s elapsed=%s cooldown=%s",
                    topic.key,
                    int(topic.priority),
                    elapsed,
                    cooldown,
                )
                if topic.priority < EventPriority.THREAT and elapsed is not None and elapsed < cooldown:
                    return None
                # PER-KEY MINIMUM SPACING (applies to ALL topics including
                # urgent): the same topic key may re-speak only after
                # 3×cooldown — the urgent bypass lifts the global cooldown,
                # never a per-key re-announce cap.
                last_spoken = self.memory.discussed_topics.get(topic.key)
                if last_spoken is not None and now - last_spoken < cooldown * 3:
                    continue

            identity = self.current_identity()
            reply = self._render_topic(topic)

            with self._lock:
                stale = identity.is_stale_vs(self.current_identity())

            if stale or is_backend_error_text(reply):
                # Stale renders and tagged backend failures are never spoken,
                # the topic is not recorded, and the error text never enters
                # memory turns (it would leak into later prompt digests).
                return None

            spoken = strip_health_tags(reply)
            if not spoken:
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

            self._speak_topic(spoken, speech_priority, identity)
            return (spoken, speech_priority, topic)

        return None

    def _render_topic(self, topic: TopicCandidate) -> str:
        """Render a topic through the coaching LLM.

        The prompt carries the topic evidence, the current plan summary, a
        last-2-utterances digest, and instructions: keep it under ~2
        sentences, never claim win probabilities, and phrase anything about
        the opponent's hidden hand/library as a hypothesis.
        """
        with self._lock:
            recent = [turn for turn in self.memory.turns[-6:] if turn.role == "coach"][-2:]
        digest = "\n".join(f"coach: {turn.text[:120]}" for turn in recent) or "(none)"

        with self._lock:
            plan = self.memory.plan_summary or ""

        question = (
            f"{TOPIC_PROMPT_PREFIX}\n"
            f"Topic: {topic.key}\n"
            f"Evidence: {topic.evidence}\n"
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

    def _speak_topic(self, text: str, speech_priority: str, identity: ResponseIdentity) -> None:
        """Deliver topic speech; an URGENT-class topic preempts in-flight speech."""
        # URGENT INTERRUPT: preemption is the arbiter's job; we stop current
        # speech and let the higher-priority request win the channel.
        if speech_priority == "urgent":
            self._preempt_speech("urgent_topic")

        vs = getattr(self._coach, "voice_session", None)
        if vs is not None and hasattr(vs, "speak"):
            try:
                vs.speak(text, priority=speech_priority, identity=identity)
            except Exception:
                logger.debug("voice_session.speak failed (topic)", exc_info=True)
            return
        vo = getattr(self._coach, "_voice_output", None)
        if vo is not None and hasattr(vo, "speak"):
            try:
                vo.speak(text)
            except Exception:
                logger.debug("voice_output.speak failed (topic)", exc_info=True)

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

    def reset_for_match(self, match_id: str | None, match_number: int, match_start: bool = True) -> None:
        # Match boundary: clear memory (including the proactive-timing
        # fields) and bump session identity.
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
            self.memory = MatchMemory()
            self._session_id += 1
            self._last_topics = []
            self._threat_signature_prev = None  # Wave 5 (M2): new board baseline
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

    def current_identity(self) -> ResponseIdentity:
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
            request_id=0,
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
