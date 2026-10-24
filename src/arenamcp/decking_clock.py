"""Alternate win-condition clocks (ledger #221): decking (mill) as a real clock.

The life-race clocks in :mod:`arenamcp.board_assessment` count only combat
damage and drains. An opposing mill engine (The Mindskinner, a Millstone, an
"Eerie" enchantment ...) kills through our library instead, and the coach used
to reason in face-damage frames all game while it banked — one activation then
milled our last cards at 20 life. This module answers, deterministically and
from the same planner snapshot (``server.get_game_state`` shape), the questions
the life clocks leave out:

* **engines** - the opponent's battlefield permanents whose oracle text mills
  (``mill``/``mills``, or the older "put the top N cards of ... library into
  ... graveyard" templating);
* **observed mill** - cards actually milled from OUR library per turn, read
  from ``recent_events`` ``zone_transfer`` entries with ``category == "Mill"``;
* **rate** - the fastest mill observed in the events window, else the best
  per-activation amount parsed from an engine's oracle text;
* **turns to deck** - ``ceil(our library / rate)``, ``None`` when no rate is
  known (the fact is then qualitative: an engine is on board and our library
  is finite).

Our library count comes from zone membership (``zones.library_count`` with a
known ``library_count_source``), exactly like :func:`arenamcp.library_counts.
observed_library_count`; an unobserved library yields no threat. Poison counters
are not tracked yet (the snapshot carries none); battles likewise.

No LLM, no I/O; a typical board costs well under a millisecond.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# "mill", "mills it", "milled six" — and the pre-"mill"-verb templating on
# older cards ("put the top three cards of target player's library into their
# graveyard"). A word boundary keeps "millstone" out of our own names.
_MILL_WORD = re.compile(r"\bmills?\b|\bmilled\b", re.IGNORECASE)
_PUT_TOP = re.compile(
    r"puts? the top [^.]*?\bcards? of [^.]*?library into [^.]*?graveyard", re.IGNORECASE
)
MILL_TEXT = re.compile(rf"(?:{_MILL_WORD.pattern})|(?:{_PUT_TOP.pattern})")

# Per-activation amounts: "mills 16", "put the top eight cards", "mill half"
# (half stays unquantified — the fact stays qualitative).
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_MILL_AMOUNT = re.compile(
    r"\bmills? (?P<word>[a-z]+|\d+)"
    r"|put[s]? the top (?P<top_word>[a-z]+|\d+) cards?"
    r"|(?P<damaged>mills? that many)",
    re.IGNORECASE,
)


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _amount(token: str | None) -> int | None:
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token.lower())


def controller(card: dict) -> Any:
    return card.get("controller_seat_id") if card.get("controller_seat_id") is not None else card.get("owner_seat_id")


def mill_amount(oracle_text: str) -> int | None:
    """Cards per activation the text names; None when it doesn't quantify one."""
    match = _MILL_AMOUNT.search(oracle_text or "")
    if match is None:
        return None
    if match.group("damaged"):
        # "mills that many" scales with another number (damage, power); the
        # caller's observed rate covers it once it has fired.
        return None
    return _amount(match.group("word")) or _amount(match.group("top_word"))


def is_mill_text(oracle_text: str) -> bool:
    return bool(MILL_TEXT.search(oracle_text or ""))


def mill_engines(battlefield: Any, opponent_seat: int | None) -> list[str]:
    """Names of the opponent's battlefield permanents whose oracle text mills."""
    engines: list[str] = []
    for card in battlefield or []:
        if not isinstance(card, dict):
            continue
        if opponent_seat is not None and controller(card) != opponent_seat:
            continue
        text = str(card.get("oracle_text") or "")
        if is_mill_text(text):
            name = str(card.get("name") or f"Card#{card.get('grp_id') or '?'}")
            engines.append(name)
    return engines


def observed_mill_per_turn(events: Any, local_seat: int | None) -> dict[int, int]:
    """Cards milled from OUR library per turn, from ``recent_events`` transfers.

    A transfer counts when its owner or controller is us (the milled cards are
    ours); unknown seats count too — a mill we can't attribute still thins our
    library.
    """
    per_turn: dict[int, int] = {}
    for event in events or []:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") != "zone_transfer":
            continue
        if str(event.get("category") or "").lower() != "mill":
            continue
        owner = event.get("owner_seat_id")
        ctrl = event.get("controller_seat_id")
        if (
            local_seat is not None
            and owner != local_seat
            and ctrl != local_seat
            and owner is not None
            and ctrl is not None
        ):
            continue
        turn = _int(event.get("turn"))
        if turn is None:
            continue
        per_turn[turn] = per_turn.get(turn, 0) + 1
    return per_turn


@dataclass(frozen=True)
class DeckingThreat:
    """An opposing mill clock against our library, or None-shaped when absent."""

    cards_left: int
    engines: list[str] = field(default_factory=list)
    rate: int | None = None  # fastest observed mill per turn, else best engine text
    turns: int | None = None  # ceil(cards_left / rate)
    milled_recent: int = 0  # cards milled from us inside the events window
    basis: str = ""  # "observed mill" / "mill engine" / "observed mill + mill engine"

    def fact(self) -> str:
        """The plan-block fact: 'we deck in ~N turns (...)' or the qualitative form."""
        detail: list[str] = []
        if self.engines:
            detail.append(f"mill engine: {self.engines[0]} on board")
        if self.milled_recent:
            detail.append(f"milled {self.milled_recent} in recent turns")
        cause = "; ".join(detail) or "mill"
        if self.turns is not None:
            return f"we deck in ~{self.turns} turn(s) ({cause})"
        return f"their mill can deck us ({cause}); our library has {self.cards_left} cards"


def decking_threat(state: dict | None) -> DeckingThreat | None:
    """The mill clock against our library on this snapshot, None without one.

    Needs a known our-library count (zone membership), and either an opposing
    mill engine on board or observed Mill transfers out of our library.
    """
    if not isinstance(state, dict):
        return None
    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    cards = _int(zones.get("library_count", state.get("library_count"))) if zones else _int(state.get("library_count"))
    if str(zones.get("library_count_source") or state.get("library_count_source") or "") == "unknown":
        cards = None
    if cards is None:
        return None

    local = state.get("local_seat_id")
    if local is None:
        local = next(
            (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
            None,
        )
    opponent = state.get("opponent_seat_id")
    if opponent is None:
        opponent = next(
            (
                p.get("seat_id")
                for p in state.get("players") or []
                if isinstance(p, dict) and p.get("seat_id") is not None and p.get("seat_id") != local
            ),
            None,
        )

    engines = mill_engines(state.get("battlefield"), opponent)
    per_turn = observed_mill_per_turn(state.get("recent_events"), local)
    milled_recent = sum(per_turn.values())
    if not engines and not per_turn:
        return None

    observed_rate = max(per_turn.values()) if per_turn else None
    text_rates = []
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict):
            continue
        if opponent is not None and controller(card) != opponent:
            continue
        amount = mill_amount(str(card.get("oracle_text") or ""))
        if amount:
            text_rates.append(amount)
    text_rate = max(text_rates) if text_rates else None

    rate = observed_rate or text_rate
    turns = math.ceil(cards / rate) if rate else None

    bases = []
    if observed_rate:
        bases.append("observed mill")
    if engines:
        bases.append("mill engine")
    return DeckingThreat(
        cards_left=cards,
        engines=engines,
        rate=rate,
        turns=turns,
        milled_recent=milled_recent,
        basis=" + ".join(bases),
    )


def role_note(threat: DeckingThreat | None, our_clock: int | None) -> str:
    """Why the life-race framing is wrong here ('' when it isn't).

    The mill beats our clock when it decks us strictly sooner (or we have no
    clock at all): the plan must remove the engine or win before it fires, not
    chip faces.
    """
    if threat is None or threat.turns is None:
        return ""
    engine = threat.engines[0] if threat.engines else "their mill engine"
    if our_clock is None:
        return f"their mill decks us in ~{threat.turns} (we have no damage clock) — remove {engine} or win before it"
    if threat.turns < our_clock:
        return (
            f"their mill decks us in ~{threat.turns} (< our damage clock {our_clock}) — "
            f"remove {engine} or win before it"
        )
    return ""
