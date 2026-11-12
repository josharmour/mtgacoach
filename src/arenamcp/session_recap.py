"""Local, durable desktop-session recap offers (never uploads diagnostics).

Offers are gated on the authoritative loss-streak store: a recap is only
captured when a completed, non-superseded three-loss review exists (three
finalized eligible autoplay/autoqueue match losses on the same deck in one
session). Ordinary sessions — wins, draws, one/two losses, missing or invalid
review evidence — close normally and queue nothing while an analysis is still
pending; a completed analysis may be offered on a subsequent launch.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _covers(match_id: str, game_ids: set[str]) -> bool:
    """A BO3 game-history row ("<match>-g2") maps to its match; it is not a match itself."""
    return match_id in game_ids or any(g.startswith(match_id + "-g") for g in game_ids)


def _streak_record(record_id: str, matches: set[str]) -> bool:
    """An exact match id or one of its BO3 game rows ("<match>-gN").

    Same identity semantics as _covers, so a review whose matches are covered
    only by game-history rows still renders those rows. The "-g" delimiter
    keeps a bare prefix from adopting unrelated matches ("a" must not claim
    "ab-g1").
    """
    return record_id in matches or any(record_id.startswith(m + "-g") for m in matches)


def _validated_matches(value: Any) -> list[str] | None:
    """Three distinct nonempty string match ids from a completed report, else None.

    Report evidence is trusted only when well-formed. Anything else fails
    closed: it must neither crash startup nor let stray characters (a string
    like "abc" splitting into three letters) pose as three matches.
    """
    if not isinstance(value, list) or len(value) != 3:
        return None
    if any(not isinstance(m, str) or not m for m in value):
        return None
    if len(set(value)) != 3:
        return None
    return list(value)


def _qualifying(reviews: list[dict] | None, known_ids: set[str]) -> list[dict]:
    """Completed three-loss reviews whose matches are all present in recorded history.

    The loss-streak store is the sole authority for eligibility; this only
    consumes its completed reports and never infers a streak from BO3 game
    rows, unknown results or unrelated/manual games.
    """
    found = []
    for review in reviews or []:
        if not isinstance(review, dict):
            continue
        matches = _validated_matches(review.get("matches"))
        if matches is None:
            continue
        if all(_covers(m, known_ids) for m in matches):
            found.append(
                {
                    "matches": matches,
                    "recommendation": str(review.get("recommendation", "")),
                    "analysis": str(review.get("analysis", "")),
                }
            )
    return found


def _identity(qualifying: dict) -> str:
    """Stable per-streak identity: dismissal survives text changes and restarts."""
    return "review:" + ",".join(qualifying["matches"])


def _verifiable(identity: Any, reviews: list[dict] | None) -> bool:
    """True only when the identity's streak is still a completed review.

    Legacy pre-gating ids and recaps whose review was since corrected or
    superseded fail this check and must not pop up.
    """
    if not isinstance(identity, str) or not identity.startswith("review:"):
        return False
    matches = identity[len("review:") :].split(",")
    if len(matches) != 3 or any(not m for m in matches):
        return False
    for review in reviews or []:
        if isinstance(review, dict) and _validated_matches(review.get("matches")) == matches:
            return True
    return False


def _legacy_loss_ids(entry: Any) -> set[str] | None:
    """Loss ids explicitly recorded in one well-formed legacy acknowledgement.

    Legacy schema (preserved commits d1f6c75/5bab2ad): ``<32-hex session
    uuid>:`` followed by comma-joined ``<match_or_game_id>:<result>`` pairs,
    optionally ending ``:<16-hex sha256 review digest>``. Results are plain
    words (win/loss/draw/unknown), so a trailing 16-hex segment can only be
    the digest; anything else malformed contributes nothing — a malformed
    entry never suppresses an offer.
    """
    if not isinstance(entry, str) or ":" not in entry:
        return None
    session, rest = entry.split(":", 1)
    if len(session) != 32 or any(c not in "0123456789abcdef" for c in session) or not rest:
        return None
    head, _, tail = rest.rpartition(":")
    if head and len(tail) == 16 and all(c in "0123456789abcdefABCDEF" for c in tail):
        rest = head
    ids: set[str] = set()
    for pair in rest.split(","):
        mid, sep, result = pair.partition(":")
        if not sep or not mid or ":" in result:
            return None
        if result == "loss":
            ids.add(mid)
    return ids


def _legacy_covers_match(loss_ids: set[str], match_id: str) -> bool:
    """Explicit loss evidence for one match: its own id or one of its BO3 game rows.

    Same safe ``-g`` delimiter semantics as _streak_record, so a bare prefix
    never adopts unrelated matches. A game row covers only its own match, so
    three game rows of one match can never stand in for three matches.
    """
    return match_id in loss_ids or any(i.startswith(match_id + "-g") for i in loss_ids)


def _legacy_acknowledged(acknowledged: list[Any], matches: list[str]) -> bool:
    """True only when some well-formed legacy acknowledgement lists every match as a loss.

    Partial or unrelated coverage does not suppress; every one of the streak's
    matches needs its own explicit loss evidence.
    """
    for entry in acknowledged:
        loss_ids = _legacy_loss_ids(entry)
        if loss_ids and all(_legacy_covers_match(loss_ids, m) for m in matches):
            return True
    return False


class SessionRecaps:
    """A display is not acknowledgement. Only explicit review/dismiss consumes an offer."""

    def __init__(self, path: Path, packets_dir: Path | None = None):
        self.path = path
        self.packets_dir = packets_dir or path.parent / "match_packets"
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(json.dumps(self.data), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def begin(self, records: list[Any], reviews: list[dict] | None = None) -> None:
        # Recover even when the old process was killed before its close handler.
        self.capture(records, reviews)
        self.data["active"] = {
            "id": uuid.uuid4().hex,
            "started": datetime.now(timezone.utc).isoformat(),
            "baseline": [r.match_id for r in records],
        }
        self._save()

    def capture(self, records: list[Any], reviews: list[dict] | None = None) -> dict | None:
        active = self.data.get("active")
        if not isinstance(active, dict):
            return None
        known = {r.match_id for r in records if r.match_id}
        acknowledged = self.data.get("acknowledged", [])
        # Oldest qualifying streak first, so every unseen review is eventually offered.
        migrated: list[str] = []
        selected = None
        for qualifying in _qualifying(reviews, known):
            identity = _identity(qualifying)
            if identity in acknowledged:
                continue
            if _legacy_acknowledged(acknowledged, qualifying["matches"]):
                # A pre-gating legacy dismissal already covers this streak
                # explicitly, loss by loss. Adopt it as the modern identity.
                migrated.append(identity)
                continue
            selected = qualifying
            break
        if migrated:
            # One atomic save persists the adopted modern acknowledgements and
            # drops any matching queued offers. The original legacy entries,
            # unrelated state, history and reviews are preserved untouched;
            # no blanket acknowledgement and no time-only watermark.
            self.data["acknowledged"] = acknowledged + [i for i in migrated if i not in acknowledged]
            self.data["pending"] = [
                p for p in self.data.get("pending", []) if p.get("id") not in set(migrated)
            ]
            if selected is None:
                self._save()
                return None
        if selected is None:
            return None
        qualifying = selected
        identity = _identity(qualifying)
        baseline = set(active.get("baseline", []))
        streak_ids = set(qualifying["matches"])
        games = [
            r
            for r in records
            if r.match_id
            and (
                _streak_record(r.match_id, streak_ids)
                or (r.match_id not in baseline and r.timestamp >= active.get("started", ""))
            )
        ]
        from arenamcp.session_review import read_packets, review_actions, written_review

        games.sort(key=lambda r: r.timestamp)
        packets = read_packets({r.match_id for r in games}, self.packets_dir)
        text = written_review(games, packets)
        text += (
            "\n\nThree-loss review (saved model interpretation, not verified findings):\n"
            + qualifying["recommendation"][:2000]
        )
        if qualifying["analysis"]:
            text += "\n" + qualifying["analysis"][:12000]
        text += "\nNothing has been sent to support. Review any report before sharing it."
        recap = {
            "id": identity,
            "session_id": active["id"],
            "text": text,
            "actions": review_actions(games, packets),
        }
        pending = [p for p in self.data.get("pending", []) if p.get("id") != identity]
        self.data["pending"] = (pending + [recap])[-5:]
        self._save()
        return recap

    def refresh(self, recap: dict, records: list[Any]) -> dict:
        """Refresh legacy pending offers without consuming their acknowledgement identity."""
        from arenamcp.session_review import read_packets, review_actions, written_review

        if "advice ratings average" not in str(recap.get("text", "")):
            return recap
        identity = str(recap.get("id", ""))
        games = [
            r
            for r in records
            if r.match_id and ("," + r.match_id + ":" in identity or ":" + r.match_id + ":" in identity)
        ]
        if games:
            games.sort(key=lambda r: r.timestamp)
            recap["text"] = written_review(games, read_packets({r.match_id for r in games}, self.packets_dir))
            recap["actions"] = review_actions(
                games, read_packets({r.match_id for r in games}, self.packets_dir)
            )
            self._save()
        elif "advice ratings average" in str(recap.get("text", "")):
            # History may have aged out. Do not keep showing the unsupported legacy grade.
            recap["text"] = str(recap["text"]).split("\n\nExisting Coach advice ratings")[0] + (
                "\n\nThe underlying game records are no longer available for this recap. "
                "I cannot reconstruct a decision review or assess autoplay performance."
            )
            self._save()
        return recap

    def pending(self, reviews: list[dict] | None = None) -> dict | None:
        """Latest pending offer; with reviews, only verifiable qualifying evidence survives."""
        pending = self.data.get("pending", [])
        if reviews is not None:
            pending = [p for p in pending if _verifiable(p.get("id"), reviews)]
        return pending[-1] if pending else None

    def acknowledge(self, identity: str) -> None:
        self.data["pending"] = [p for p in self.data.get("pending", []) if p.get("id") != identity]
        # An acknowledgement must outlive every identity that can reappear: the
        # loss-streak store keeps only its most recent completed jobs (~20), so
        # this bound sits far above any reappearance window. Trimming to ten
        # let the oldest dismissed streak reoffer after eleven dismissals.
        self.data["acknowledged"] = (self.data.get("acknowledged", []) + [identity])[-256:]
        self._save()
