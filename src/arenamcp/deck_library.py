"""The player's deck library: every constructed deck, from Player.log and the bridge, with playbook status.

Log-first (2026-10-11): Arena writes ``<== DeckGetDeckSummariesV3`` (every
deck's name, format and id, no cards), ``<== EventGetCoursesV2`` (the full
list of each deck attached to an event course) and ``==> EventSetDeckV3``
(the full list of the deck just queued). The native Mac bridge adds the
complete library in one read (``get_decks``: ``WrapperController.Instance
.DecksManager.GetAllCachedDecks()``, which the client fills at login).

A record renders as the same ``game_state`` shape the in-match analysis
uses (``deck_cards`` + ``commander_grp_ids``), so a playbook built from the
Decks window has the identity the next match looks up in the cache.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RESPONSE_RE = re.compile(r"<== (DeckGetDeckSummariesV3|EventGetCoursesV2|EventSetDeckV3|EventJoin)\(")
_QUEUE_RE = re.compile(r"==> EventSetDeckV3 (\{.*\})\s*$")

PILES = ("main", "sideboard", "command_zone", "companions")
UNNAMED_DECK = "Unnamed deck"


@dataclass
class DeckRecord:
    deck_id: str
    name: str
    format: str = ""
    main: list[tuple[int, int]] = field(default_factory=list)  # (grp id, count)
    sideboard: list[tuple[int, int]] = field(default_factory=list)
    command_zone: list[int] = field(default_factory=list)
    companions: list[int] = field(default_factory=list)
    source: str = ""  # bridge | log | queue
    last_played: str = ""
    last_updated: str = ""
    # Built only from an event course's CourseDeckSummary: the copy taken when
    # the deck was last submitted to that event, which lags a later rename or
    # rebuild (EventGetCoursesV2 lists every course, stale ones included).
    snapshot: bool = False

    @property
    def complete(self) -> bool:
        return bool(self.main)

    def card_count(self) -> int:
        return sum(count for _gid, count in self.main) + len(self.command_zone)

    def deck_cards(self) -> list[int]:
        cards = [gid for gid, count in self.main for _ in range(count)]
        cards.extend(self.command_zone)
        return cards

    def game_state(self) -> dict[str, Any]:
        """The state shape the playbook analysis and its cache identity read."""
        return {
            "deck_cards": self.deck_cards(),
            "commander_grp_ids": list(self.command_zone),
            "players": [{"seat_id": 1, "is_local": True}],
            "local_seat_id": 1,
            "deck_name": self.name,
            "deck_format": self.format,
        }

    def identity(self) -> str:
        from arenamcp.deck_strategy import deck_identity

        return deck_identity(self.game_state()) if self.complete else ""

    def merge_from(self, other: DeckRecord, *, newer: bool = True) -> None:
        """Fold another observation of this deck into this record.

        ``newer``: ``other`` was seen after this record (a later log line or
        a newer log file), so its name, format and dates replace ours (a
        rename in Arena), and so does its card list when it was queued (a
        rebuild). An older ``other``, or an event course's snapshot, only
        fills what we lack, unless all we have is a snapshot. A bridge card
        list beats a log one either way, and a missing value (or the
        "Unnamed deck" placeholder) never erases ours.
        """
        wins = not other.snapshot and (newer or self.snapshot)
        rebuilt = newer and other.source == "queue" and self.source != "bridge"
        if other.complete and (not self.complete or other.source == "bridge" or rebuilt):
            self.main, self.sideboard = list(other.main), list(other.sideboard)
            self.command_zone, self.companions = list(other.command_zone), list(other.companions)
            self.source = other.source
        for attribute in ("name", "format", "last_played", "last_updated"):
            mine, theirs = getattr(self, attribute), getattr(other, attribute)
            if attribute == "name":
                mine = "" if mine == UNNAMED_DECK else mine
                theirs = "" if theirs == UNNAMED_DECK else theirs
            if theirs and (wins or not mine):
                setattr(self, attribute, theirs)
        self.snapshot = self.snapshot and other.snapshot


def _pairs(entries: Any) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        gid = entry.get("cardId", entry.get("grp_id", entry.get("Id")))
        count = entry.get("quantity", entry.get("count", entry.get("Quantity", 1)))
        try:
            gid, count = int(gid), int(count)
        except (TypeError, ValueError):
            continue
        if gid > 4 and count > 0:
            out.append((gid, count))
    return out


def _singles(entries: Any) -> list[int]:
    return [gid for gid, count in _pairs(entries) for _ in range(count)]


def _attributes(summary: dict) -> dict[str, str]:
    raw = summary.get("Attributes")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    out: dict[str, str] = {}
    for item in raw or []:
        if isinstance(item, dict) and item.get("name") is not None:
            out[str(item["name"])] = str(item.get("value") or "").strip('"')
    return out


def record_from_summary(summary: dict, source: str = "log") -> DeckRecord | None:
    deck_id = str(summary.get("DeckId") or summary.get("deck_id") or "")
    if not deck_id:
        return None
    attributes = _attributes(summary)
    return DeckRecord(
        deck_id=deck_id,
        name=str(summary.get("Name") or summary.get("name") or UNNAMED_DECK),
        format=attributes.get("Format", str(summary.get("format") or "")),
        source=source,
        last_played=attributes.get("LastPlayed", ""),
        last_updated=attributes.get("LastUpdated", ""),
    )


def record_from_deck(summary: dict, deck: dict, source: str) -> DeckRecord | None:
    record = record_from_summary(summary, source)
    if record is None or not isinstance(deck, dict):
        return record
    record.main = _pairs(deck.get("MainDeck") or deck.get("mainDeck"))
    record.sideboard = _pairs(deck.get("Sideboard") or deck.get("sideboard"))
    record.command_zone = _singles(deck.get("CommandZone") or deck.get("commandZone"))
    record.companions = _singles(deck.get("Companions") or deck.get("companions"))
    return record


def parse_player_log(text: str) -> dict[str, DeckRecord]:
    """Deck records from Player.log text; later messages refine earlier ones."""
    decks: dict[str, DeckRecord] = {}

    def absorb(record: DeckRecord | None, event_name: Any = None) -> None:
        if record is None:
            return
        if record.deck_id in decks:
            decks[record.deck_id].merge_from(record)
        else:
            decks[record.deck_id] = record
        # The event a deck was queued in stands in for a format only when
        # no summary named one: it is not a newer observation of the format.
        if event_name and not decks[record.deck_id].format:
            decks[record.deck_id].format = str(event_name)

    lines = text.splitlines()
    for index, line in enumerate(lines):
        queued = _QUEUE_RE.search(line)
        if queued:
            try:
                request = json.loads(json.loads(queued.group(1)).get("request") or "{}")
            except (ValueError, TypeError, AttributeError):
                continue
            record = record_from_deck(request.get("Summary") or {}, request.get("Deck") or {}, "queue")
            absorb(record, request.get("EventName"))
            continue
        match = _RESPONSE_RE.search(line)
        if not match:
            continue
        payload = None
        for candidate in lines[index + 1 : index + 3]:
            candidate = candidate.strip()
            if candidate.startswith("{"):
                try:
                    payload = json.loads(candidate)
                except ValueError:
                    payload = None
                break
        if not isinstance(payload, dict):
            continue
        for summary in payload.get("Summaries") or []:
            if isinstance(summary, dict):
                absorb(record_from_summary(summary, "log"))
        courses = payload.get("Courses")
        if courses is None and payload.get("CourseDeck"):
            courses = [payload]
        for course in courses or []:
            if not isinstance(course, dict):
                continue
            summary = course.get("CourseDeckSummary") or {}
            deck = course.get("CourseDeck") or {}
            if (
                isinstance(summary, dict)
                and summary.get("DeckId")
                and isinstance(deck, dict)
                and deck.get("MainDeck")
            ):
                record = record_from_deck(summary, deck, "log")
                if record is not None:
                    record.snapshot = True
                absorb(record, course.get("InternalEventName"))
    return decks


def default_log_paths() -> list[Path]:
    from arenamcp.watcher import _default_log_path

    try:
        current = Path(_default_log_path())
    except Exception:
        return []
    return [current, current.with_name("Player-prev.log")]


def load_log_decks(paths: list[Path] | None = None) -> dict[str, DeckRecord]:
    """Decks from the log files (newest first); the oldest is read first so newer files win."""
    decks: dict[str, DeckRecord] = {}
    for path in reversed(paths if paths is not None else default_log_paths()):
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            logger.debug("Deck library: cannot read %s: %s", path, error)
            continue
        for deck_id, record in parse_player_log(text).items():
            if deck_id in decks:
                decks[deck_id].merge_from(record, newer=True)
            else:
                decks[deck_id] = record
    return decks


def records_from_bridge(response: dict | None) -> dict[str, DeckRecord]:
    """Records from the bridge's ``get_decks`` reply ({"decks": [...]})."""
    decks: dict[str, DeckRecord] = {}
    if not isinstance(response, dict) or not response.get("ok"):
        return decks
    for raw in response.get("decks") or []:
        if not isinstance(raw, dict) or not raw.get("deck_id"):
            continue
        record = DeckRecord(
            deck_id=str(raw["deck_id"]),
            name=str(raw.get("name") or UNNAMED_DECK),
            format=str(raw.get("format") or ""),
            main=_pairs(raw.get("main")),
            sideboard=_pairs(raw.get("sideboard")),
            command_zone=_singles(raw.get("command_zone")),
            companions=_singles(raw.get("companions")),
            source="bridge",
        )
        decks[record.deck_id] = record
    return decks


def merge_libraries(*libraries: dict[str, DeckRecord]) -> dict[str, DeckRecord]:
    """Earlier libraries take precedence (pass the bridge first: it is the client's live library)."""
    merged: dict[str, DeckRecord] = {}
    for library in libraries:
        for deck_id, record in library.items():
            if deck_id in merged:
                merged[deck_id].merge_from(record, newer=False)
            else:
                merged[deck_id] = DeckRecord(**{**record.__dict__})
    return merged


def _names(playbook: Any, gids: list) -> list[str]:
    out = []
    for gid in gids:
        try:
            out.append(str(playbook.catalog[int(gid)].get("name") or f"Card {gid}"))
        except (KeyError, ValueError, TypeError, AttributeError):
            out.append(f"Card {gid}")
    return out


def playbook_report(playbook: Any) -> dict[str, Any]:
    """The playbook as readable sections for the Decks window ("how it pops off")."""
    data = playbook.data
    return {
        "archetype": data.get("archetype", ""),
        "summary": data.get("spoken_summary", ""),
        "primary_plan": data.get("primary_plan", ""),
        "backup_plan": data.get("backup_plan", ""),
        "mechanisms": [
            {
                "id": m.get("id", ""),
                "effect": m.get("effect", ""),
                "cards": _names(playbook, m.get("cards") or []),
                "requires": list(m.get("requires") or []),
                "avoid": list(m.get("avoid") or []),
            }
            for m in data.get("mechanisms") or []
        ],
        "decision_rules": [
            {
                "id": r.get("id", ""),
                "decisions": list(r.get("decisions") or []),
                "baseline": r.get("baseline", ""),
                "when": r.get("when", ""),
                "prefer": r.get("prefer", ""),
                "unless": r.get("unless", ""),
            }
            for r in data.get("decision_rules") or []
        ],
        "key_cards": [
            {"name": _names(playbook, [gid])[0], "role": str(role)}
            for gid, role in (data.get("card_roles") or {}).items()
        ],
        "phases": dict(data.get("phases") or {}),
        "commanders": [
            {
                "name": _names(playbook, [c.get("card")])[0],
                "deployment": c.get("deployment", ""),
                "ongoing_value": c.get("ongoing_value", ""),
                "replay_value": c.get("replay_value", ""),
                "preserve_or_reuse": c.get("preserve_or_reuse", ""),
                "constraints": c.get("constraints", ""),
            }
            for c in data.get("commanders") or []
        ],
    }


def deck_payload(record: DeckRecord, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """One row of the Decks window."""
    return {
        "deck_id": record.deck_id,
        "name": record.name,
        "format": record.format,
        "cards": record.card_count(),
        "complete": record.complete,
        "source": record.source,
        "last_played": record.last_played,
        "commanders": list(record.command_zone),
        "identity": record.identity(),
        "strategy": dict(status or {"status": "unknown"}),
    }
