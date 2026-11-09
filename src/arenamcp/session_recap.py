"""Local, durable desktop-session recap offers (never uploads diagnostics)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        baseline = set(active.get("baseline", []))
        games = [
            r
            for r in records
            if r.match_id and r.match_id not in baseline and r.timestamp >= active.get("started", "")
        ]
        if not games:
            return None
        # Fingerprint the reviewed contents, so additional games can produce a new offer.
        identity = active["id"] + ":" + ",".join(r.match_id + ":" + r.result for r in games)
        game_ids = {r.match_id for r in games}
        relevant = [
            review
            for review in (reviews or [])
            if any(
                game_id == mid or game_id.startswith(mid + "-g")
                for mid in review.get("matches", [])
                for game_id in game_ids
            )
        ]
        if relevant:
            identity += ":" + hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()[:16]
        if identity in self.data.get("acknowledged", []):
            return None
        from arenamcp.session_review import read_packets, review_actions, written_review

        games.sort(key=lambda r: r.timestamp)
        packets = read_packets(game_ids, self.packets_dir)
        text = written_review(games, packets)
        for review in relevant[-3:]:
            text += (
                "\n\nThree-loss review (saved model interpretation, not verified findings):\n"
                + str(review.get("recommendation", ""))[:2000]
            )
            if review.get("analysis"):
                text += "\n" + str(review["analysis"])[:12000]
            text += "\nNothing has been sent to support. Review any report before sharing it."
        recap = {
            "id": identity,
            "session_id": active["id"],
            "text": text,
            "actions": review_actions(games, packets),
        }
        pending = [p for p in self.data.get("pending", []) if p.get("session_id") != active["id"]]
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

    def pending(self) -> dict | None:
        pending = self.data.get("pending", [])
        return pending[-1] if pending else None

    def acknowledge(self, identity: str) -> None:
        self.data["pending"] = [p for p in self.data.get("pending", []) if p.get("id") != identity]
        self.data["acknowledged"] = (self.data.get("acknowledged", []) + [identity])[-10:]
        self._save()
