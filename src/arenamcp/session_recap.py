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
        # Oldest qualifying streak first, so every unseen review is eventually offered.
        for qualifying in _qualifying(reviews, known):
            identity = _identity(qualifying)
            if identity not in self.data.get("acknowledged", []):
                break
        else:
            return None
        baseline = set(active.get("baseline", []))
        streak_ids = set(qualifying["matches"])
        games = [
            r
            for r in records
            if r.match_id
            and (
                r.match_id in streak_ids
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
