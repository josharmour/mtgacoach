"""Draft state management for tracking current pack contents.

This module provides draft state tracking by parsing MTGA log events
related to drafts (Premier, Traditional, Quick Draft, and Sealed).
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Draft type constants (matches MTGA event name prefixes)
DRAFT_TYPE_UNKNOWN = "unknown"
DRAFT_TYPE_PREMIER = "PremierDraft"
DRAFT_TYPE_QUICK = "QuickDraft"
DRAFT_TYPE_TRADITIONAL = "TradDraft"
DRAFT_TYPE_PICK_TWO = "PickTwoDraft"
DRAFT_TYPE_PICK_TWO_TRAD = "PickTwoTradDraft"
DRAFT_TYPE_PICK_TWO_QUICK = "PickTwoQuickDraft"
DRAFT_TYPE_SEALED = "Sealed"
DRAFT_TYPE_SEALED_TRAD = "TradSealed"

# Map event name prefixes → draft type.  Order matters: longer prefixes first
# so "PickTwoTradDraft" matches before "PickTwoDraft" or "TradDraft".
_DRAFT_TYPE_PREFIXES: list[tuple[str, str]] = [
    ("PickTwoQuickDraft", DRAFT_TYPE_PICK_TWO_QUICK),
    ("PickTwoTradDraft", DRAFT_TYPE_PICK_TWO_TRAD),
    ("PickTwoDraft", DRAFT_TYPE_PICK_TWO),
    ("PremierDraft", DRAFT_TYPE_PREMIER),
    ("QuickDraft", DRAFT_TYPE_QUICK),
    ("TradDraft", DRAFT_TYPE_TRADITIONAL),
    ("TradSealed", DRAFT_TYPE_SEALED_TRAD),
    ("Sealed", DRAFT_TYPE_SEALED),
]

_PICK_TWO_TYPES = {DRAFT_TYPE_PICK_TWO, DRAFT_TYPE_PICK_TWO_TRAD, DRAFT_TYPE_PICK_TWO_QUICK}


def detect_draft_type(event_name: str) -> str:
    """Detect draft type from an MTGA event name string.

    Matches known prefix patterns (e.g. "PremierDraft_MH3_20240101").
    Falls back to heuristic substring checks for special events
    (Arena Open, Qualifier, MWM, etc.).
    """
    for prefix, dtype in _DRAFT_TYPE_PREFIXES:
        if prefix in event_name:
            return dtype

    # Heuristic fallback for special/custom events
    lower = event_name.lower()
    if "sealed" in lower:
        return DRAFT_TYPE_SEALED
    if "draft" in lower:
        return DRAFT_TYPE_PREMIER  # Safe default for unknown draft variants
    return DRAFT_TYPE_UNKNOWN


@dataclass
class DraftState:
    """Tracks the current state of an active draft or sealed event.

    Attributes:
        event_name: The draft event name (e.g., "PremierDraft_MH3_20240101")
        draft_type: Detected draft type constant (e.g., DRAFT_TYPE_PREMIER)
        set_code: The set being drafted (e.g., "MH3")
        pack_number: Current pack number (1-indexed)
        pick_number: Current pick number (1-indexed)
        cards_in_pack: List of grpIds (arena_ids) for cards in current pack
        picked_cards: List of grpIds for cards already picked
        is_active: Whether a draft is currently in progress
        is_sealed: Whether this is a sealed event (not draft)
        sealed_pool: List of grpIds for all cards in sealed pool
        sealed_analyzed: Whether sealed pool has been analyzed this session
        picks_per_pack: Number of cards picked per pack (1 for normal, 2 for PickTwo)
    """

    event_name: str = ""
    draft_type: str = DRAFT_TYPE_UNKNOWN
    set_code: str = ""
    pack_number: int = 0
    pick_number: int = 0
    cards_in_pack: list[int] = field(default_factory=list)
    picked_cards: list[int] = field(default_factory=list)
    is_active: bool = False
    is_sealed: bool = False
    sealed_pool: list[int] = field(default_factory=list)
    sealed_analyzed: bool = False
    picks_per_pack: int = 1

    def reset(self) -> None:
        """Reset draft state for a new draft."""
        self.event_name = ""
        self.draft_type = DRAFT_TYPE_UNKNOWN
        self.set_code = ""
        self.pack_number = 0
        self.pick_number = 0
        self.cards_in_pack = []
        self.picked_cards = []
        self.is_active = False
        self.is_sealed = False
        self.sealed_pool = []
        self.sealed_analyzed = False
        self.picks_per_pack = 1


def extract_set_code(event_name: str) -> str:
    """Extract set code from draft event name.

    Event names typically look like:
    - PremierDraft_MH3_20240101
    - QuickDraft_BLB_20240815
    - Trad_Sealed_DSK_20241001

    Args:
        event_name: The full event name string

    Returns:
        The extracted set code (e.g., "MH3"), or empty string if not found.
    """
    # Try to find a 3-letter set code after underscore
    parts = event_name.split("_")
    for part in parts:
        # Set codes are typically 3 uppercase letters
        if len(part) == 3 and part.isupper():
            return part
    return ""


def create_draft_handler(draft_state: DraftState) -> Callable[[str, dict], None]:
    """Create a handler function that updates draft state from log events.

    This factory creates a handler that can be registered with the LogParser
    to process various draft-related log events.

    Args:
        draft_state: The DraftState instance to update

    Returns:
        Handler function accepting (event_type, payload) parameters.
    """

    def handle_draft_event(event_type: str, payload: dict) -> None:
        """Process draft-related log events and update state.

        This handler is called for ALL events (not just unhandled ones)
        so it must bail out quickly for non-draft events like game state.
        """

        # FAST BAIL-OUT: Skip GreToClientEvent game state messages.
        # These are the most frequent events and never contain draft data.
        # Check the dict keys directly instead of serializing to JSON.
        if "greToClientEvent" in payload:
            return

        # Convert payload to string for pattern matching
        payload_str = json.dumps(payload)

        # DRAFT-RELEVANCE CHECK: Only process payloads containing draft keywords.
        # This prevents wasted work on match/game events.
        _DRAFT_KEYWORDS = (
            "CardsInPack",
            "PackCards",
            "SelfPack",
            "DraftPack",
            "DraftStatus",
            "CardPool",
            "EventName",
            "GrpId",
        )
        if not any(kw in payload_str for kw in _DRAFT_KEYWORDS):
            return

        # DEBUG: Log draft-related events for diagnosis
        logger.debug(f"[DRAFT_DEBUG] Event: {event_type}, Keys: {list(payload.keys())}")
        if len(payload_str) < 500:
            logger.debug(f"[DRAFT_DEBUG] Payload: {payload_str}")

        # Check for sealed pool (CardPool with InternalEventName containing Sealed)
        if "CardPool" in payload_str and "InternalEventName" in payload_str:
            _handle_sealed_pool(draft_state, payload)
            return

        # Check for draft start events (CardsInPack in first pack)
        if "CardsInPack" in payload_str:
            _handle_cards_in_pack(draft_state, payload)
            return

        # Check for Draft.Notify events (Premier/Traditional pack updates)
        if "PackCards" in payload_str or "SelfPack" in payload_str:
            _handle_draft_notify(draft_state, payload)
            return

        # Check for Quick Draft DraftPack events
        if "DraftPack" in payload_str and "DraftStatus" in payload_str:
            _handle_quick_draft_pack(draft_state, payload)
            return

        # Check for pick events to track what was picked
        if "GrpId" in payload_str and ("Pick" in payload_str or "cardId" in payload_str):
            _handle_draft_pick(draft_state, payload)
            return

        # Check for event start with EventName
        if "EventName" in payload_str:
            _handle_event_start(draft_state, payload)
            return

    return handle_draft_event


def _find_nested_value(d: dict, key: str) -> Any:
    """Recursively search for a key in nested dicts and JSON-encoded strings."""
    if key in d:
        return d[key]
    for v in d.values():
        if isinstance(v, dict):
            result = _find_nested_value(v, key)
            if result is not None:
                return result
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    result = _find_nested_value(item, key)
                    if result is not None:
                        return result
        elif isinstance(v, str) and v.startswith("{"):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    result = _find_nested_value(parsed, key)
                    if result is not None:
                        return result
            except (json.JSONDecodeError, ValueError):
                pass
    return None


def _handle_event_start(draft_state: DraftState, payload: dict) -> None:
    """Handle draft/sealed event start with EventName."""
    event_name = _find_nested_value(payload, "EventName")
    if event_name and ("Draft" in event_name or "Sealed" in event_name):
        # Don't reset state if this is the same ongoing draft —
        # Quick Draft sends EventName with every pack event.
        if event_name == draft_state.event_name and draft_state.is_active:
            return

        dtype = detect_draft_type(event_name)
        draft_state.event_name = event_name
        draft_state.draft_type = dtype
        draft_state.set_code = extract_set_code(event_name)
        draft_state.is_active = True
        draft_state.is_sealed = dtype in (DRAFT_TYPE_SEALED, DRAFT_TYPE_SEALED_TRAD)
        draft_state.picks_per_pack = 2 if dtype in _PICK_TWO_TYPES else 1
        draft_state.cards_in_pack = []
        draft_state.picked_cards = []
        draft_state.sealed_pool = []
        draft_state.sealed_analyzed = False
        logger.info(
            f"{dtype} started: {event_name} "
            f"(set: {draft_state.set_code}, picks_per_pack: {draft_state.picks_per_pack})"
        )


def _handle_sealed_pool(draft_state: DraftState, payload: dict) -> None:
    """Handle sealed pool event with CardPool array.

    The sealed pool event structure is:
    {"Course": {"InternalEventName": "MWM_TLA_Sealed_...", "CardPool": [grp_ids...]}}
    """
    event_name = _find_nested_value(payload, "InternalEventName")

    # Only process if this is a sealed event
    if not event_name or "Sealed" not in event_name:
        return

    card_pool = _find_nested_value(payload, "CardPool")
    if not card_pool or not isinstance(card_pool, list):
        return

    # Set up sealed state
    draft_state.event_name = event_name
    draft_state.set_code = extract_set_code(event_name)
    draft_state.is_active = True
    draft_state.is_sealed = True
    draft_state.sealed_pool = [int(c) for c in card_pool if c]
    # Also populate picked_cards for compatibility with get_sealed_pool()
    draft_state.picked_cards = draft_state.sealed_pool.copy()
    draft_state.sealed_analyzed = False

    logger.info(
        f"Sealed pool loaded: {event_name} (set: {draft_state.set_code}) - "
        f"{len(draft_state.sealed_pool)} cards"
    )


def _handle_cards_in_pack(draft_state: DraftState, payload: dict) -> None:
    """Handle CardsInPack event (first pack in Premier/Traditional draft)."""
    cards = _find_nested_value(payload, "CardsInPack")
    pack_num = _find_nested_value(payload, "PackNumber")
    pick_num = _find_nested_value(payload, "PickNumber")

    if cards and isinstance(cards, list):
        draft_state.cards_in_pack = [int(c) for c in cards if c]
        draft_state.is_active = True

        # PackNumber/PickNumber are 0-indexed in logs
        if pack_num is not None:
            draft_state.pack_number = int(pack_num) + 1
        if pick_num is not None:
            draft_state.pick_number = int(pick_num) + 1

        logger.info(
            f"Pack {draft_state.pack_number} Pick {draft_state.pick_number}: "
            f"{len(draft_state.cards_in_pack)} cards"
        )


def _handle_draft_notify(draft_state: DraftState, payload: dict) -> None:
    """Handle Draft.Notify events (Premier/Traditional pack updates)."""
    # PackCards is comma-separated string of grpIds
    pack_cards_str = _find_nested_value(payload, "PackCards")
    self_pack = _find_nested_value(payload, "SelfPack")
    self_pick = _find_nested_value(payload, "SelfPick")

    if pack_cards_str and isinstance(pack_cards_str, str):
        # Parse comma-separated card IDs
        cards = [int(c.strip()) for c in pack_cards_str.split(",") if c.strip()]
        draft_state.cards_in_pack = cards
        draft_state.is_active = True

        if self_pack is not None:
            draft_state.pack_number = int(self_pack)
        if self_pick is not None:
            draft_state.pick_number = int(self_pick)

        logger.info(f"Pack update P{draft_state.pack_number}P{draft_state.pick_number}: {len(cards)} cards")
    elif isinstance(pack_cards_str, list):
        # Sometimes it's already a list
        draft_state.cards_in_pack = [int(c) for c in pack_cards_str if c]
        draft_state.is_active = True


def _handle_quick_draft_pack(draft_state: DraftState, payload: dict) -> None:
    """Handle Quick Draft DraftPack events."""
    draft_pack = _find_nested_value(payload, "DraftPack")
    draft_status = _find_nested_value(payload, "DraftStatus")
    pack_num = _find_nested_value(payload, "PackNumber")
    pick_num = _find_nested_value(payload, "PickNumber")

    # Only process on PickNext status
    if draft_status == "PickNext" and draft_pack:
        draft_state.cards_in_pack = [int(c) for c in draft_pack if c]
        draft_state.is_active = True

        if pack_num is not None:
            draft_state.pack_number = int(pack_num) + 1  # 0-indexed
        if pick_num is not None:
            draft_state.pick_number = int(pick_num) + 1

        logger.info(
            f"Quick Draft P{draft_state.pack_number}P{draft_state.pick_number}: "
            f"{len(draft_state.cards_in_pack)} cards"
        )


def _handle_draft_pick(draft_state: DraftState, payload: dict) -> None:
    """Handle player pick events to track picked cards."""
    # Handle PickTwo drafts: GrpIds is an array of picked card IDs
    grp_ids = _find_nested_value(payload, "GrpIds")
    if isinstance(grp_ids, list) and len(grp_ids) >= 2:
        # Dynamic PickTwo detection: if the pick payload contains 2+ cards
        # but we didn't detect PickTwo from the event name, upgrade now.
        if draft_state.picks_per_pack < 2:
            draft_state.picks_per_pack = 2
            if draft_state.draft_type not in _PICK_TWO_TYPES:
                draft_state.draft_type = DRAFT_TYPE_PICK_TWO
            logger.info("Detected PickTwo from multi-card pick payload (picks_per_pack upgraded to 2)")
        for gid in grp_ids:
            gid = int(gid)
            if gid not in draft_state.picked_cards:
                draft_state.picked_cards.append(gid)
                logger.debug(f"Picked card: {gid}")
            if gid in draft_state.cards_in_pack:
                draft_state.cards_in_pack.remove(gid)
        return
    if isinstance(grp_ids, list):
        # Single-element GrpIds array — still a normal pick
        for gid in grp_ids:
            gid = int(gid)
            if gid not in draft_state.picked_cards:
                draft_state.picked_cards.append(gid)
                logger.debug(f"Picked card: {gid}")
            if gid in draft_state.cards_in_pack:
                draft_state.cards_in_pack.remove(gid)
        return

    # Single pick: try different field names used by different draft types
    grp_id = (
        _find_nested_value(payload, "GrpId")
        or _find_nested_value(payload, "cardId")
        or _find_nested_value(payload, "CardId")
    )

    if grp_id:
        grp_id = int(grp_id)
        if grp_id not in draft_state.picked_cards:
            draft_state.picked_cards.append(grp_id)
            logger.debug(f"Picked card: {grp_id}")

        # Remove from current pack if present
        if grp_id in draft_state.cards_in_pack:
            draft_state.cards_in_pack.remove(grp_id)
