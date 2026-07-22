"""Match history tracking from MTGA replay files.

Parses .rply replay files saved by MTGA's TimedReplayRecorder,
extracts match metadata, and maintains a searchable history database
for coaching context (win rates, archetype matchups, etc.).

Replay format (.rply):
  Line 1: #Version2
  Line 2: CosmeticReplayData JSON (player names, ranks, cosmetics)
  Lines 3+: IN-{ms}:{GREToClientMessage} or OUT-{ms}:{ClientToGREMessage}
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default storage location for match history
HISTORY_DIR = Path.home() / ".arenamcp" / "match_history"
HISTORY_FILE = HISTORY_DIR / "history.json"


@dataclass
class MatchRecord:
    """A single match record extracted from a replay or game end event."""
    match_id: str = ""
    timestamp: str = ""  # ISO format
    result: str = ""  # "win", "loss", "draw"
    opponent_name: str = ""
    opponent_rank: str = ""
    local_deck_colors: list[str] = field(default_factory=list)
    opponent_colors_seen: list[str] = field(default_factory=list)
    format_name: str = ""
    turns: int = 0
    local_life_final: int = 0
    opponent_life_final: int = 0
    replay_path: str = ""
    # Extracted from cosmetic data
    local_rank_class: int = 0
    local_rank_tier: int = 0
    opponent_rank_class: int = 0
    opponent_rank_tier: int = 0

    def to_review_prompt(
        self,
        advice_history: Optional[list[dict[str, Any]]] = None,
        opponent_cards: Optional[list[str]] = None,
        missed_decisions: Optional[list[dict[str, Any]]] = None,
        replay_summary: Optional[str] = None,
    ) -> str:
        """Generate a post-match breakdown review prompt (/analyze) for this match."""
        return generate_match_review_prompt(
            record=self,
            advice_history=advice_history,
            opponent_cards=opponent_cards,
            missed_decisions=missed_decisions,
            replay_summary=replay_summary,
        )



class MatchHistory:
    """Persistent match history database backed by a JSON file."""

    def __init__(self, history_path: Optional[Path] = None):
        self._path = history_path or HISTORY_FILE
        self._records: list[MatchRecord] = []
        self._load()

    def _load(self):
        """Load history from disk."""
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._records = [MatchRecord(**r) for r in data.get("matches", [])]
                logger.info(f"Loaded {len(self._records)} match records from {self._path}")
            except Exception as e:
                logger.warning(f"Failed to load match history: {e}")
                self._records = []
        else:
            self._records = []

    def _save(self):
        """Persist history to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"matches": [asdict(r) for r in self._records]}
        self._path.write_text(json.dumps(data, indent=2))

    def add_record(self, record: MatchRecord) -> None:
        """Add a match record and save."""
        # Deduplicate by match_id
        if record.match_id:
            self._records = [r for r in self._records if r.match_id != record.match_id]
        self._records.append(record)
        # Keep last 500 records
        if len(self._records) > 500:
            self._records = self._records[-500:]
        self._save()
        logger.info(f"Recorded match: {record.result} vs {record.opponent_name or 'unknown'}")

    def get_recent(self, n: int = 20) -> list[MatchRecord]:
        """Get the N most recent matches."""
        return self._records[-n:]

    def get_win_rate(self, last_n: int = 0) -> dict[str, Any]:
        """Calculate overall win rate, optionally limited to last N games."""
        records = self._records[-last_n:] if last_n > 0 else self._records
        if not records:
            return {"games": 0, "wins": 0, "losses": 0, "win_rate": 0.0}
        wins = sum(1 for r in records if r.result == "win")
        losses = sum(1 for r in records if r.result == "loss")
        total = wins + losses
        return {
            "games": total,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total, 3) if total > 0 else 0.0,
        }

    def get_matchup_stats(self, opponent_colors: list[str]) -> dict[str, Any]:
        """Get win rate against a specific color combination."""
        color_set = set(opponent_colors)
        matching = [r for r in self._records if set(r.opponent_colors_seen) == color_set]
        if not matching:
            return {"games": 0, "colors": opponent_colors}
        wins = sum(1 for r in matching if r.result == "win")
        return {
            "games": len(matching),
            "wins": wins,
            "losses": len(matching) - wins,
            "win_rate": round(wins / len(matching), 3),
            "colors": opponent_colors,
        }

    def get_session_stats(self, hours: float = 4.0) -> dict[str, Any]:
        """Get stats for the current session (last N hours)."""
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        session = []
        for r in self._records:
            try:
                ts = datetime.fromisoformat(r.timestamp.replace("Z", "+00:00"))
                if ts >= cutoff:
                    session.append(r)
            except (ValueError, AttributeError):
                continue
        if not session:
            return {"games": 0, "hours": hours}
        wins = sum(1 for r in session if r.result == "win")
        losses = sum(1 for r in session if r.result == "loss")
        return {
            "games": len(session),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0.0,
            "hours": hours,
        }

    @property
    def total_games(self) -> int:
        return len(self._records)


def parse_replay_cosmetics(replay_path: str) -> Optional[dict[str, Any]]:
    """Parse the cosmetic header from a .rply file.

    Returns player names, ranks, and cosmetic selections.
    """
    try:
        with open(replay_path, "r", encoding="utf-8") as f:
            line1 = f.readline().strip()
            if not line1.startswith("#Version"):
                logger.debug(f"Not a valid replay file: {replay_path}")
                return None
            line2 = f.readline().strip()
            if line2:
                return json.loads(line2)
    except Exception as e:
        logger.debug(f"Failed to parse replay cosmetics: {e}")
    return None


def parse_replay_result(replay_path: str) -> Optional[str]:
    """Scan a replay file for game result (win/loss).

    Looks for LossOfGame/WinTheGame annotations in GRE messages.
    """
    try:
        with open(replay_path, "r", encoding="utf-8") as f:
            # Skip header
            f.readline()
            f.readline()
            # Scan messages from the end (result is near the end)
            lines = f.readlines()
            for line in reversed(lines):
                if "LossOfGame" in line or "WinTheGame" in line:
                    # Determine if local player won
                    if "WinTheGame" in line:
                        return "win"
                    elif "LossOfGame" in line:
                        return "loss"
    except Exception as e:
        logger.debug(f"Failed to parse replay result: {e}")
    return None


def record_from_game_end(
    match_id: str,
    result: str,
    game_state_snapshot: dict[str, Any],
    opponent_cards: list[dict[str, Any]] = None,
    replay_path: str = "",
) -> MatchRecord:
    """Create a MatchRecord from a game end event and snapshot.

    This is the primary way to record matches — called from standalone.py
    when a game ends, using the final game state snapshot.
    """
    from datetime import datetime, timezone

    record = MatchRecord(
        match_id=match_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        result=result,
        replay_path=replay_path,
    )

    # Extract from game state snapshot
    if game_state_snapshot:
        turn_info = game_state_snapshot.get("turn", game_state_snapshot.get("turn_info", {}))
        record.turns = turn_info.get("turn_number", 0)

        players = game_state_snapshot.get("players", [])
        for p in players:
            if p.get("is_local"):
                record.local_life_final = p.get("life_total", 0)
            else:
                record.opponent_life_final = p.get("life_total", 0)

    # Extract opponent colors from their played cards
    if opponent_cards:
        colors_seen = set()
        for card in opponent_cards:
            mana = card.get("mana_cost", "")
            if "{W}" in mana or "White" in str(card.get("colors", [])):
                colors_seen.add("W")
            if "{U}" in mana or "Blue" in str(card.get("colors", [])):
                colors_seen.add("U")
            if "{B}" in mana or "Black" in str(card.get("colors", [])):
                colors_seen.add("B")
            if "{R}" in mana or "Red" in str(card.get("colors", [])):
                colors_seen.add("R")
            if "{G}" in mana or "Green" in str(card.get("colors", [])):
                colors_seen.add("G")
        record.opponent_colors_seen = sorted(colors_seen)

    return record


# Module-level singleton
_history: Optional[MatchHistory] = None


def get_history() -> MatchHistory:
    """Get or create the module-level match history singleton."""
    global _history
    if _history is None:
        _history = MatchHistory()
    return _history


def generate_match_review_prompt(
    record: MatchRecord,
    advice_history: Optional[list[dict[str, Any]]] = None,
    opponent_cards: Optional[list[str]] = None,
    missed_decisions: Optional[list[dict[str, Any]]] = None,
    replay_summary: Optional[str] = None,
) -> str:
    """Generate a post-match review prompt for /analyze match breakdowns.

    Args:
        record: MatchRecord instance with metadata.
        advice_history: Chronological log of turn decisions/advice.
        opponent_cards: List of card names revealed by the opponent.
        missed_decisions: Vision watchdog detections or unmapped decisions.
        replay_summary: Authoritative GRE replay decision summary.

    Returns:
        Formatted prompt string ready for LLM completion.
    """
    lines = [
        "POST-MATCH REVIEW REQUEST (/analyze)",
        f"Match ID: {record.match_id or 'unknown'}",
        f"Timestamp: {record.timestamp or 'unknown'}",
        f"Result: {record.result.upper() if record.result else 'UNKNOWN'}",
        f"Opponent: {record.opponent_name or 'Unknown'}",
    ]

    if record.format_name:
        lines.append(f"Format: {record.format_name}")
    if record.local_deck_colors:
        lines.append(f"Player Deck Colors: {', '.join(record.local_deck_colors)}")
    if record.opponent_colors_seen:
        lines.append(f"Opponent Colors Seen: {', '.join(record.opponent_colors_seen)}")

    if record.turns:
        lines.append(f"Match Duration: {record.turns} turns")
    if record.local_life_final or record.opponent_life_final:
        lines.append(f"Final Life Totals: Player={record.local_life_final}, Opponent={record.opponent_life_final}")

    if opponent_cards or record.opponent_colors_seen:
        opp_list = opponent_cards or []
        lines.append(f"\nOPPONENT CARDS SEEN:\n{', '.join(opp_list[:30]) if opp_list else 'Colors: ' + ', '.join(record.opponent_colors_seen)}")

    if advice_history:
        lines.append("\nCHRONOLOGICAL ADVICE LOG:")
        for entry in advice_history:
            snap = entry.get("game_snapshot") or {}
            turn = snap.get("turn_number", "?")
            phase = snap.get("phase", "?")
            trigger = entry.get("trigger", "unknown")
            advice_text = entry.get("advice", "")
            lines.append(f"Turn {turn} ({phase}) [{trigger}]: {advice_text}")

    if missed_decisions:
        lines.append(f"\nMISSED DECISION POINTS ({len(missed_decisions)} detected):")
        for md in missed_decisions:
            lines.append(
                f"Turn {md.get('turn', '?')}, {md.get('phase', '?')}: "
                f"{md.get('decision_type', 'unknown')} - {md.get('prompt_text', '')}"
            )

    if replay_summary:
        lines.append(f"\nREPLAY SUMMARY:\n{replay_summary}")

    lines.extend([
        "\nANALYSIS INSTRUCTIONS:",
        "Provide a comprehensive post-match breakdown covering:",
        "1. MATCH SUMMARY & TURNING POINTS: Key moments that decided the game.",
        "2. PLAY EVALUATION & MISTAKES: Specific plays/advice that were optimal vs suboptimal.",
        "3. MATCHUP & SIDEBOARD LESSONS: Tactical takeaways for future games against this archetype.",
        "At the end, include a 2-sentence summary line starting with 'SPOKEN:' for audio playback."
    ])

    return "\n".join(lines)
