"""Match packet recorder: records gameplay trajectories for self-improvement.

Every match's decision sequence (PendingDecisions + chosen actions + outcomes)
is recorded to ~/.arenamcp/match_packets/ for post-match judge scoring and
DPO pair generation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from arenamcp.decisions import PendingDecision, decision_to_dict

logger = logging.getLogger(__name__)

PACKETS_DIR = Path.home() / ".arenamcp" / "match_packets"
MAX_PACKETS = 500  # Cap total history to avoid disk bloat


class MatchPacket:
    """Accrues the structured decision history of a single match."""

    def __init__(self, match_id: str):
        self.match_id = match_id
        self.start_time = datetime.now().isoformat()
        self.end_time: Optional[str] = None
        self.result: str = "unknown"
        self.deck_strategy: Optional[str] = None
        self.opponent_name: Optional[str] = None
        self.replay_path: Optional[str] = None
        self.decisions: list[dict[str, Any]] = []

    def add_decision(self, decision: PendingDecision, chosen_options: list[str]) -> None:
        """Log a decision faced and option chosen."""
        from arenamcp.request_tracker import decision_fingerprint

        fp = decision_fingerprint(decision)
        self.decisions.append({
            "pending_decision": decision_to_dict(decision),
            "chosen_options": list(chosen_options),
            "outcome": "pending",
            "fingerprint": fp,
            "timestamp": time.time(),
        })

    def update_outcome(self, fp: tuple, outcome: str) -> None:
        """Settle a pending decision with its final execution outcome."""
        for entry in reversed(self.decisions):
            if entry.get("fingerprint") == fp and entry.get("outcome") == "pending":
                entry["outcome"] = outcome
                logger.debug(f"MatchPacket: updated decision {fp[0]} to {outcome}")
                break

    def save(self, packets_dir: Optional[Path] = None) -> Optional[Path]:
        """Save the packet to disk as a JSON fixture."""
        try:
            target_dir = packets_dir or PACKETS_DIR
            target_dir.mkdir(parents=True, exist_ok=True)
            self.end_time = datetime.now().isoformat()

            # Clean up temporary fingerprint tracking from serialization
            cleaned_decisions = []
            for d in self.decisions:
                d_copy = d.copy()
                if "fingerprint" in d_copy:
                    del d_copy["fingerprint"]
                cleaned_decisions.append(d_copy)

            data = {
                "match_id": self.match_id,
                "start_time": self.start_time,
                "end_time": self.end_time,
                "result": self.result,
                "deck_strategy": self.deck_strategy,
                "opponent_name": self.opponent_name,
                "replay_path": self.replay_path,
                "decisions": cleaned_decisions,
            }

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = target_dir / f"packet_{ts}_{self.match_id}.json"
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            _rotate(target_dir)
            logger.info(
                f"Match packet recorded: {path.name} (decisions={len(self.decisions)}, result={self.result})"
            )
            return path
        except Exception as e:
            logger.warning(f"MatchPacket.save failed: {e}")
            return None


def _rotate(target_dir: Path) -> None:
    try:
        packets = sorted(target_dir.glob("packet_*.json"))
        excess = len(packets) - MAX_PACKETS
        for old in packets[:excess]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass


# Global singleton instance managed by standalone loop
_current_packet: Optional[MatchPacket] = None


def start_match_packet(match_id: str) -> MatchPacket:
    """Initialize a fresh match packet.

    If a previous packet is still active (its game-end event never fired —
    crash, missed boundary), salvage it to disk as result="abandoned"
    instead of silently discarding its recorded decisions.
    """
    global _current_packet
    prev = _current_packet
    if prev is not None and prev.match_id != match_id and prev.decisions:
        prev.result = "abandoned"
        prev.save()
        logger.warning(
            f"Match packet for {prev.match_id} was never finalized — "
            f"salvaged as 'abandoned' ({len(prev.decisions)} decisions)"
        )
    _current_packet = MatchPacket(match_id)
    logger.info(f"Started match packet recording for match {match_id}")
    return _current_packet


def get_current_packet() -> Optional[MatchPacket]:
    """Retrieve the current active match packet."""
    return _current_packet


def stop_match_packet() -> Optional[MatchPacket]:
    """Terminate and clear the active match packet."""
    global _current_packet
    packet = _current_packet
    _current_packet = None
    return packet
