"""Arena ladder standings and the queues that contribute to each ladder."""

from __future__ import annotations

import json
import os
from pathlib import Path

LADDERS = ("Constructed", "Limited")


def ranked_ladder(event_name: str) -> str | None:
    """Identify ranked queues in current and older MatchHistory format names."""
    name = "".join((event_name or "").lower().split()).replace("_", "")
    if name.startswith(("premierdraft", "quickdraft", "picktwodraft", "picktwoquickdraft", "rankeddraft")):
        return "Limited"
    if "unranked" not in name and (name.endswith("ladder") or "ranked" in name):
        return "Limited" if any(word in name for word in ("limited", "draft", "sealed")) else "Constructed"
    return None


def rank_text(snapshot: dict, ladder: str) -> str:
    prefix = ladder.lower()
    tier = snapshot.get(prefix + "Class")
    level = snapshot.get(prefix + "Level")
    if not tier and not level:
        return "Unavailable"
    # Arena omits the zero-valued Bronze class and zero progress from its JSON.
    tier = tier or "Bronze"
    if tier == "Mythic":
        place = snapshot.get(prefix + "LeaderboardPlace")
        if isinstance(place, int) and place > 0:
            return f"Mythic #{place}"
        pct = snapshot.get(prefix + "Percentile")
        return f"Mythic {pct:.1f}%" if isinstance(pct, (int, float)) else "Mythic"
    rank = f"{tier} {level}" if level else tier
    steps = snapshot.get(prefix + "Step", 0)
    return f"{rank} · {steps} {'pip' if steps == 1 else 'pips'}"


class RankLog:
    """Follow complete combined-rank replies, including those before Coach started."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self.snapshot: dict = {}
        self._identity = None
        self._offset = 0
        self._anchor = b""
        self._awaiting_rank = False

    def read(self, path: Path) -> bool:
        """Read a bounded batch. Return True when more complete lines are ready."""
        try:
            with path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (path, stat.st_dev, stat.st_ino)
                # Unity can truncate and refill the same inode between polls.
                stream.seek(self._offset - len(self._anchor))
                if identity != self._identity or stream.read(len(self._anchor)) != self._anchor:
                    self._reset()
                    self._identity = identity
                stream.seek(self._offset)
                for line in stream.readlines(512 * 1024):
                    if not line.endswith(b"\n"):
                        break  # Retry the incomplete line on the next poll.
                    self._offset += len(line)
                    if self._awaiting_rank:
                        try:
                            payload = json.loads(line)
                        except (ValueError, UnicodeError):
                            payload = None
                        if isinstance(payload, dict) and (
                            "constructedSeasonOrdinal" in payload or "limitedSeasonOrdinal" in payload
                        ):
                            self.snapshot = payload
                    self._awaiting_rank = b"<== RankGetCombinedRankInfo(" in line
                more = stream.tell() < stat.st_size
                stream.seek(max(0, self._offset - 128))
                self._anchor = stream.read(min(128, self._offset))
                return more
        except OSError:
            self._reset()
            return False
