"""Event auto-queue configuration, single-use paid entry approvals, and choosers."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class EventQueueConfig:
    """User-selected configuration for Event / Draft auto-queue mode."""

    event_name: str
    display_name: str = ""
    category: str = ""
    is_free: bool = False
    entry_currency: str = "Free"
    entry_quantity: int = 0
    deck_id: str = ""
    deck_name: str = ""
    choice: str = ""
    reenter_free: bool = False
    stop_at_main_prize: bool = True
    win_no_gate_loss_limit: int = 5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> EventQueueConfig:
        return cls(
            event_name=str(raw.get("event_name") or "").strip(),
            display_name=str(raw.get("display_name") or "").strip(),
            category=str(raw.get("category") or "").strip(),
            is_free=bool(raw.get("is_free", False)),
            entry_currency=str(raw.get("entry_currency") or "Free").strip(),
            entry_quantity=max(0, int(raw.get("entry_quantity") or 0)),
            deck_id=str(raw.get("deck_id") or "").strip(),
            deck_name=str(raw.get("deck_name") or "").strip(),
            choice=str(raw.get("choice") or "").strip(),
            reenter_free=bool(raw.get("reenter_free", False)),
            stop_at_main_prize=bool(raw.get("stop_at_main_prize", True)),
            win_no_gate_loss_limit=max(0, int(raw.get("win_no_gate_loss_limit", 5))),
        )


@dataclass
class EntryApproval:
    """In-memory single-use authorization for one paid event entry."""

    token: str
    event_name: str
    currency_type: str
    quantity: int
    created_at: float
    used: bool = False


class EntryApprovalStore:
    """Thread-safe, in-memory-only approval store; never persisted to disk."""

    TTL_SECONDS = 900.0

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._approvals: dict[str, EntryApproval] = {}

    def mint(self, event_name: str, currency_type: str, quantity: int) -> str:
        """Issue a fresh single-use approval token for one exact event + fee."""
        token = uuid.uuid4().hex
        with self._lock:
            self._approvals[token] = EntryApproval(
                token=token,
                event_name=str(event_name),
                currency_type=str(currency_type),
                quantity=int(quantity),
                created_at=float(self._clock()),
            )
        return token

    def consume(self, token: str, event_name: str, currency_type: str, quantity: int) -> bool:
        """Consume *token* iff unused, unexpired, and matching event/currency/quantity."""
        if not token:
            return False
        now = float(self._clock())
        with self._lock:
            approval = self._approvals.get(token)
            if approval is None or approval.used:
                return False
            if now - approval.created_at > self.TTL_SECONDS:
                self._approvals.pop(token, None)
                return False
            if (
                approval.event_name != str(event_name)
                or approval.currency_type != str(currency_type)
                or approval.quantity != int(quantity)
            ):
                return False
            approval.used = True
            return True

    def clear(self) -> None:
        """Invalidate all unused approvals (called when mode/auto-queue stops)."""
        with self._lock:
            self._approvals.clear()


def _normalize_colors(raw_colors: Any) -> set[str]:
    out: set[str] = set()
    for ch in raw_colors or []:
        text = str(ch).strip().upper()
        for letter in ("W", "U", "B", "R", "G"):
            if letter in text:
                out.add(letter)
    return out


def choose_jumpstart_packet(
    options: list[dict[str, Any]], submitted: list[dict[str, Any]] | None = None
) -> dict[str, Any] | None:
    """Pick the best Jump In / Jumpstart packet option based on color overlap."""
    if not options:
        return None
    picked = submitted or []
    if not picked:
        # Prefer monocolor over multicolor for cleaner 2-color mana consistency.
        return min(
            options,
            key=lambda opt: (
                len(_normalize_colors(opt.get("colors"))) or 9,
                str(opt.get("name") or opt.get("packet_id") or ""),
            ),
        )
    base_colors: set[str] = set()
    for pkt in picked:
        base_colors.update(_normalize_colors(pkt.get("colors")))

    def score(opt: dict[str, Any]) -> tuple[int, int, str]:
        cand = _normalize_colors(opt.get("colors"))
        combined = base_colors | cand
        overlap = len(base_colors & cand)
        # Rank: exact same colors / <=2 total colors first, maximize overlap, then name.
        color_penalty = 0 if len(combined) <= 2 else len(combined)
        return (color_penalty, -overlap, str(opt.get("name") or opt.get("packet_id") or ""))

    return min(options, key=score)


def normalize_format_key(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


_FORMAT_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"explorer", "pioneer"}),
    frozenset({"brawl", "historicbrawl"}),
)


def format_matches(event_format: str, deck_format: str) -> bool:
    """Strictly check whether a deck's format qualifies for an event format."""
    ev_key = normalize_format_key(event_format)
    if not ev_key:
        return True
    deck_key = normalize_format_key(deck_format)
    if not deck_key:
        return False
    if ev_key == deck_key:
        return True
    return any(ev_key in group and deck_key in group for group in _FORMAT_ALIASES)


def event_needs_deck(event: dict[str, Any]) -> bool:
    """True when entering or advancing this event requires a qualifying constructed deck."""
    if event.get("in_progress") and str(event.get("module") or "") != "DeckSelect":
        return False
    if event.get("is_precon") or event.get("skip_deck_validation"):
        return False
    return str(event.get("category") or "") == "constructed_event"


def eligible_decks_for_event(
    event: dict[str, Any], decks: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Return decks eligible for *event*, preferring native validation results when present."""
    prevalidated = event.get("eligible_decks")
    if isinstance(prevalidated, list):
        return list(prevalidated)
    fmt = str(event.get("deck_select_format") or "")
    return [
        deck
        for deck in (decks or [])
        if deck.get("complete") is not False and format_matches(fmt, str(deck.get("format") or ""))
    ]


def near_decks_for_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sorted near-qualifying cached decks attached by Arena validation."""
    raw = event.get("near_decks")
    if not isinstance(raw, list):
        return []
    return [dict(d) for d in raw if isinstance(d, dict) and d.get("deck_id")]


def event_can_build_deck(event: dict[str, Any], decks: list[dict[str, Any]] | None = None) -> bool:
    """True when *event* is affordable, needs a deck, has none qualifying, and has near decks."""
    if not event_needs_deck(event):
        return False
    if not event.get("in_progress") and not event.get("is_free"):
        fees = list(event.get("entry_fees") or [])
        if not fees or not any(fee.get("affordable") for fee in fees):
            return False
    if eligible_decks_for_event(event, decks):
        return False
    return bool(near_decks_for_event(event))


def event_eligibility(event: dict[str, Any], decks: list[dict[str, Any]] | None = None) -> tuple[bool, str]:
    """Return ``(eligible, reason)`` checking balance/fees and qualifying constructed decks."""
    if not event.get("in_progress"):
        fees = list(event.get("entry_fees") or [])
        if not event.get("is_free"):
            if not fees:
                return False, "Entry fee unavailable"
            if not any(fee.get("affordable") for fee in fees):
                cost_str = " / ".join(format_entry_fee(fee) for fee in fees)
                return False, f"Insufficient funds ({cost_str})"
    if event_needs_deck(event):
        qual = eligible_decks_for_event(event, decks)
        if not qual:
            fmt = str(event.get("deck_select_format") or "Constructed").strip() or "Constructed"
            return False, f"No qualifying {fmt} deck"
    return True, ""


def choose_constructed_event_deck(
    decks: list[dict[str, Any]],
    format_name: str,
    preferred_deck_id: str = "",
    win_rates: dict[str, tuple[int, int]] | None = None,
) -> dict[str, Any] | None:
    """Choose a format-matching deck, honoring *preferred_deck_id* first, then win rate."""
    if not decks:
        return None
    eligible = [
        d
        for d in decks
        if d.get("complete") is not False and format_matches(format_name, str(d.get("format") or ""))
    ]
    if not eligible:
        return None
    if preferred_deck_id:
        for deck in eligible:
            if str(deck.get("deck_id") or "") == preferred_deck_id:
                return deck

    rates = win_rates or {}

    def deck_rank(deck: dict[str, Any]) -> tuple[float, int, str]:
        did = str(deck.get("deck_id") or "")
        wins, losses = rates.get(did, (0, 0))
        total = wins + losses
        wr = (wins / total) if total > 0 else -1.0
        return (wr, total, str(deck.get("name") or ""))

    return max(eligible, key=deck_rank)


def format_entry_fee(fee: dict[str, Any]) -> str:
    """Human-readable label for one EventEntryFeeInfo dict."""
    currency = str(fee.get("currency_type") or "None").removeprefix("CurrencyType_")
    if currency == "Gems":
        currency = "Gem"
    qty = int(fee.get("quantity") or 0)
    if currency in {"Free", "None"} and qty == 0:
        return "Free"
    if currency == "Gold":
        return f"{qty:,} Gold"
    if currency == "Gem":
        return f"{qty:,} Gems"
    if currency == "DraftToken":
        return f"{qty} Draft Token" if qty == 1 else f"{qty} Draft Tokens"
    if currency == "SealedToken":
        return f"{qty} Sealed Token" if qty == 1 else f"{qty} Sealed Tokens"
    if currency == "EventToken":
        return f"{qty} Event Token" if qty == 1 else f"{qty} Event Tokens"
    return f"{qty} {currency}" if qty else currency


def describe_event_rules(event: dict[str, Any]) -> str:
    """Short summary of an event's end condition / main prize target."""
    loss_type = str(event.get("loss_details_type") or "")
    loss_games = int(event.get("loss_details_games") or 0)
    max_wins = int(event.get("max_prize_wins") or 0)
    parts: list[str] = []
    if max_wins > 0:
        parts.append(f"{max_wins} win{'s' if max_wins != 1 else ''} prize")
    if loss_type == "LossSlots" and loss_games > 0:
        parts.append(f"{loss_games} loss{'es' if loss_games != 1 else ''} out")
    elif loss_type == "PlayMaxGames" and loss_games > 0:
        parts.append(f"{loss_games} matches max")
    elif loss_type == "PlayUntilEventEnds":
        parts.append("no loss limit")
    return " · ".join(parts)
