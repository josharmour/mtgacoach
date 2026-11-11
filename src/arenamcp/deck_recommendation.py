"""Evidence and cooldown for an opt-in constructed deck-building invitation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arenamcp.ladder import ranked_ladder
from arenamcp.match_history import MatchRecord

MIN_RESULTS = 20
WINDOW_RESULTS = 30
COOLDOWN = timedelta(hours=24)
ADVICE_LABEL = "DECK BUILDING"


@dataclass(frozen=True)
class DeckInvitation:
    wins: int
    losses: int
    rank: str
    threshold: float
    event: str
    deck: str = ""

    @property
    def message(self) -> str:
        rate = self.wins / (self.wins + self.losses)
        pool = f"ranked games with {self.deck}" if self.deck else "ranked results"
        return (
            "Your recent win/loss record suggests trying a new deck. "
            "Would you like help building one?\n\n"
            f"Your last {self.wins + self.losses} {pool} in {self.event}: "
            f"{self.wins} wins, {self.losses} losses ({rate:.1%}). "
            f"At {self.rank}, the baseline for gaining rank progress is above {self.threshold:.1%}. "
            "Rank protection can soften losses; a different deck is no guarantee of wins."
        )


def invitation(records: list[MatchRecord], snapshot: dict, *, now: datetime | None = None):
    """Evaluate one queue, excluding draws, old results, and per-game Bo3 records.

    The evidence is per-deck: only games the autopilot played with the SAME
    deck as the latest game count towards the sample, matched by deck
    identity. A display name alone is not an identity (two different decks
    can share one), so a record without an identity never enters the pool.
    Advisor-mode games and other decks measure the player, not this deck, so
    they never enter the pool either.
    Mythic has opponent-dependent rating gains, not a fixed climb threshold.
    Thresholds follow https://magic.wizards.com/en/mtgarena/getting-started
    (checked November 2026). These are expected pip progress, not a guarantee.
    """
    rank = snapshot.get("constructedClass")
    if not rank and snapshot.get("constructedLevel"):
        rank = "Bronze"
    threshold = {"Bronze": 1 / 3, "Silver": 1 / 3, "Gold": 1 / 3, "Platinum": 0.5, "Diamond": 0.5}.get(rank)
    if threshold is None or not records:
        return None
    latest = records[-1]
    event = latest.format_name
    # History stores games, not Bo3 match wins. Never compare those to a
    # match-level rank threshold. Limited is not a wildcard-building workflow.
    normalized = event.lower().replace("_", " ")
    if ranked_ladder(event) != "Constructed" or any(x in normalized for x in ("traditional", "trad", "bo3")):
        return None
    # Without a deck identity on the latest game there is no per-deck sample.
    # A shared display name is not an identity, so there is no name fallback:
    # identity-less records must never pool into a named-deck invitation.
    deck_key = getattr(latest, "deck_identity", "")
    if not deck_key:
        return None
    now = now or datetime.now(timezone.utc)
    recent = []
    seen = set()
    for rec in reversed(records):
        if rec.format_name != event or rec.result not in ("win", "loss") or not rec.match_id:
            continue
        # Same deck as the game just ended (by identity, never by name), and
        # the autopilot played it.
        if getattr(rec, "autopilot", None) is not True:
            continue
        if getattr(rec, "deck_identity", "") != deck_key:
            continue
        if rec.match_id in seen:
            continue
        seen.add(rec.match_id)
        try:
            timestamp = datetime.fromisoformat(rec.timestamp.replace("Z", "+00:00"))
            if timestamp.tzinfo is None or not now - timedelta(days=30) <= timestamp <= now:
                continue
        except (ValueError, TypeError):
            continue
        recent.append(rec)
        if len(recent) == WINDOW_RESULTS:
            break
    if latest not in recent or len(recent) < MIN_RESULTS:
        return None
    wins = sum(rec.result == "win" for rec in recent)
    if wins / len(recent) >= threshold:
        return None
    return DeckInvitation(wins, len(recent) - wins, rank, threshold, event, latest.deck_name)


def claim_invitation(path: Path, match_id: str, *, now: datetime | None = None) -> bool:
    """Persist the cooldown before delivery, including across Coach restarts."""
    now = now or datetime.now(timezone.utc)
    try:
        previous = json.loads(path.read_text())
        shown = datetime.fromisoformat(previous["shown_at"])
        if previous.get("match_id") == match_id or now - shown < COOLDOWN:
            return False
    except (OSError, ValueError, KeyError, TypeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"shown_at": now.isoformat(), "match_id": match_id}))
    temp.replace(path)
    return True
