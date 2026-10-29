"""Read bounded, correlated server evidence of a generated deck being saved.

Capture a checkpoint immediately before updating the bridge, then retry the
reader with that same checkpoint. Cached decks and outgoing requests are not
save acknowledgements. No game APIs, log writes, sleeps, or global state live
here. The wire schema is demonstrated by player_log_idle_after_match_20261019.log.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID

MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_RECORD_BYTES = 256 * 1024
_ANCHOR_BYTES = 4096
_OUTGOING = re.compile(r"^(?:\[UnityCrossThreadLogger\])?==> DeckUpsertDeckV3 (.+)$")
_INCOMING = re.compile(r"^(?:\[UnityCrossThreadLogger\])?<== DeckUpsertDeckV3\(([^()]+)\)$")
_PILES = {"MainDeck", "Sideboard", "CommandZone", "Companions", "CardSkins"}
_SUMMARY_FIELDS = {
    "DeckId",
    "Name",
    "Attributes",
    "Description",
    "DeckTileId",
    "DeckArtId",
    "PreferredCosmetics",
    "Mana",
    "IsCompanionValid",
    "NetDeckFolderId",
    "IsNetDeck",
    "checkInbox",
}


@dataclass(frozen=True)
class DeckSaveOffset:
    """Byte offset plus identity/anchors needed to reject replaced or truncated logs."""

    offset: int
    device: int
    inode: int
    birthtime: float | None
    # Hex strings keep dataclasses.asdict checkpoints JSON-journal compatible.
    prefix: str
    boundary: str


def _identity(info: os.stat_result) -> tuple[int, int, float | None]:
    return info.st_dev, info.st_ino, getattr(info, "st_birthtime", None)


def _anchors(stream: BinaryIO, offset: int) -> tuple[str, str]:
    stream.seek(0)
    prefix = stream.read(min(offset, _ANCHOR_BYTES))
    stream.seek(max(0, offset - _ANCHOR_BYTES))
    return prefix.hex(), stream.read(min(offset, _ANCHOR_BYTES)).hex()


def _same_file(path: Path, stream: BinaryIO, checkpoint: DeckSaveOffset) -> bool:
    info = os.fstat(stream.fileno())
    expected = checkpoint.device, checkpoint.inode, checkpoint.birthtime
    return (
        stat.S_ISREG(info.st_mode)
        and _identity(info) == expected == _identity(path.stat())
        and info.st_size >= checkpoint.offset
        and _anchors(stream, checkpoint.offset) == (checkpoint.prefix, checkpoint.boundary)
    )


def capture_deck_save_offset(path: Path) -> DeckSaveOffset | None:
    """Capture before the bridge update; unreadable/unstable logs return None.

    Reads at most 16 KiB, irrespective of log size. Keep the returned object,
    rather than its integer offset, so rotation before the first read is caught.
    """
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            prefix, boundary = _anchors(stream, info.st_size)
            checkpoint = DeckSaveOffset(info.st_size, *_identity(info), prefix, boundary)
            return checkpoint if _same_file(path, stream, checkpoint) else None
    except (OSError, ValueError):
        return None


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _object(raw: str) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        return value if isinstance(value, dict) else {}
    except (ValueError, RecursionError):
        return {}


def _guid(value: object) -> UUID | None:
    try:
        return UUID(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _counts(rows: object) -> dict[int, int] | None:
    if not isinstance(rows, list):
        return None
    result: dict[int, int] = {}
    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            return None
        card, quantity = row
        if type(card) is not int or type(quantity) is not int or card <= 0 or quantity <= 0:
            return None
        result[card] = result.get(card, 0) + quantity
    return result


def _summary_matches(summary: object, deck_id: UUID, name: str, deck_format: str) -> bool:
    if not isinstance(summary, dict) or set(summary) - _SUMMARY_FIELDS:
        return False
    attributes = summary.get("Attributes")
    if not isinstance(attributes, list) or any(not isinstance(item, dict) for item in attributes):
        return False
    formats = [item.get("value") for item in attributes if item.get("name") == "Format"]
    return (
        _guid(summary.get("DeckId")) == deck_id and summary.get("Name") == name and formats == [deck_format]
    )


def _deck_matches(deck: object, expected: dict[int, int]) -> bool:
    if not isinstance(deck, dict) or set(deck) != _PILES:
        return False
    if any(deck[pile] != [] for pile in {"Sideboard", "CommandZone", "Companions"}):
        return False
    # The bridge preserves clone cosmetics; these are not gameplay card piles.
    if not isinstance(deck["CardSkins"], list):
        return False
    main = deck["MainDeck"]
    if not isinstance(main, list) or any(
        not isinstance(row, dict) or set(row) != {"cardId", "quantity"} for row in main
    ):
        return False
    return _counts([(row["cardId"], row["quantity"]) for row in main]) == expected


def _has_save(
    data: bytes,
    checkpoint: DeckSaveOffset,
    deck_id: UUID,
    name: str,
    expected: dict[int, int],
    deck_format: str,
) -> bool:
    # Only complete lines count. An offset inside an old record cannot adopt it.
    lines = data.splitlines(keepends=True)
    if checkpoint.boundary and not bytes.fromhex(checkpoint.boundary).endswith(b"\n"):
        lines = lines[1:]
    request_id: UUID | None = None
    pending_response: UUID | None = None
    seen: set[UUID] = set()
    saved = False
    for raw in lines:
        if not raw.endswith(b"\n"):
            break
        if len(raw) > MAX_RECORD_BYTES:
            return False
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeDecodeError:
            return False
        if pending_response is not None:
            if pending_response == request_id:
                saved = _summary_matches(_object(line), deck_id, name, deck_format)
            pending_response = None
        outgoing = _OUTGOING.fullmatch(line)
        if outgoing:
            envelope = _object(outgoing[1])
            identifier = _guid(envelope.get("id"))
            if identifier is None or identifier in seen:
                return False
            seen.add(identifier)
            payload = envelope.get("request")
            request = _object(payload) if isinstance(payload, str) else {}
            summary = request.get("Summary")
            if not isinstance(summary, dict):
                return False
            if _guid(summary.get("DeckId")) == deck_id:
                # A later edit supersedes earlier acknowledgements for this deck.
                saved = False
                request_id = None
                if (
                    set(envelope) == {"id", "request"}
                    and set(request) == {"Summary", "Deck", "ActionType"}
                    and request["ActionType"] == "Updated"
                    and _summary_matches(summary, deck_id, name, deck_format)
                    and _deck_matches(request.get("Deck"), expected)
                ):
                    request_id = identifier
        incoming = _INCOMING.fullmatch(line)
        if incoming:
            pending_response = _guid(incoming[1])
    return saved


def read_deck_save(
    path: Path,
    *,
    after_offset: DeckSaveOffset,
    deck_id: str,
    name: str,
    main: list[tuple[int, int]],
    deck_format: str,
) -> bool:
    """Confirm the exact update's server acknowledgement after a checkpoint.

    Requires an exact main-deck multiset and empty other gameplay piles,
    and matching ID/name/Format in both request and response. Failures, partial
    records, bare integer offsets, rotation, and windows over 4 MiB fail closed.
    The caller may retry while waiting, but must retain the original checkpoint.
    Each call reads at most MAX_SCAN_BYTES + 32 KiB; it never scans the old log.
    """
    identifier = _guid(deck_id)
    expected = _counts(main)
    if (
        not isinstance(after_offset, DeckSaveOffset)
        or after_offset.offset < 0
        or identifier is None
        or not name
        or not deck_format
        or not expected
    ):
        return False
    try:
        with path.open("rb") as stream:
            if not _same_file(path, stream, after_offset):
                return False
            info = os.fstat(stream.fileno())
            size = info.st_size - after_offset.offset
            if size < 0 or size > MAX_SCAN_BYTES:
                return False
            stream.seek(after_offset.offset)
            data = stream.read(size)
            if len(data) != size or not _same_file(path, stream, after_offset):
                return False
            current = os.fstat(stream.fileno())
            if current.st_size < info.st_size or (
                current.st_size == info.st_size and current.st_mtime_ns != info.st_mtime_ns
            ):
                return False
            return _has_save(data, after_offset, identifier, name, expected, deck_format) and _same_file(
                path, stream, after_offset
            )
    except (OSError, ValueError):
        return False
