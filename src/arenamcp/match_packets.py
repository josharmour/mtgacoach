"""Match packet recorder: the decisions the autopilot made in a match.

Every match's decision sequence (PendingDecisions + chosen actions + outcomes)
is recorded to ~/.arenamcp/match_packets/ for the post-match review
(match_review), bug reports and the uploaded match bundle.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from arenamcp.decisions import PendingDecision, decision_to_dict

logger = logging.getLogger(__name__)

PACKETS_DIR = Path.home() / ".arenamcp" / "match_packets"
MAX_PACKETS = 500  # Cap total history to avoid disk bloat
# Game ids whose packet was saved, across coach restarts (newest last).
FINALIZED_PATH = Path.home() / ".arenamcp" / "finalized_matches.json"
MAX_FINALIZED = 200
# The packet of a game in progress, one JSON record per line under
# PACKETS_DIR/live (see MatchPacket.journal). Journals no game finished are
# deleted after this long.
LIVE_SUBDIR = "live"
LIVE_MAX_AGE_S = 2 * 24 * 3600


def _journal_path(match_id: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(match_id))
    return PACKETS_DIR / LIVE_SUBDIR / f"{safe}.jsonl"


def _as_fingerprint(value: Any) -> Any:
    """A fingerprint read back from JSON (lists) as the tuple ``decision_fingerprint`` makes."""
    return tuple(_as_fingerprint(v) for v in value) if isinstance(value, list) else value


class MatchPacket:
    """Accrues the structured decision history of a single match."""

    def __init__(self, match_id: str):
        self.match_id = match_id
        self.start_time = datetime.now().isoformat()
        self.end_time: str | None = None
        self.result: str = "unknown"
        self.deck_strategy: str | None = None
        self.opponent_name: str | None = None
        self.replay_path: str | None = None
        self.decisions: list[dict[str, Any]] = []
        # The on-disk journal (None: not journaling, e.g. a packet built in a test).
        self.journal: Path | None = None

    # -- the live journal (ledger #72) ------------------------------------------
    #
    # 2026-10-19 match 115944ce: the coach restarted at 11:14:23 mid-game and
    # the packet saved at 11:16:10 started there (11 decisions, T8-T10); the
    # decisions of T1-T7 were lost with the old process, and so was the bundle's
    # view of them. Every recorded entry is appended to a journal as it happens,
    # and start_match_packet() resumes a game's journal after a restart.

    def start_journal(self) -> None:
        """Journal this packet under PACKETS_DIR/live, from scratch (never raises)."""
        try:
            path = _journal_path(self.match_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
            self.journal = path
            self._append({"meta": {"match_id": self.match_id, "start_time": self.start_time}})
        except Exception as e:
            self.journal = None
            logger.debug(f"MatchPacket journal unavailable for {self.match_id}: {e}")

    def _append(self, record: dict[str, Any]) -> None:
        path = self.journal
        if path is None:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(path, "a", encoding="utf-8") as journal:
                journal.write(line + "\n")
        except Exception as e:
            logger.debug(f"MatchPacket journal write failed for {self.match_id}: {e}")

    def _record(self, entry: dict[str, Any]) -> None:
        self.decisions.append(entry)
        self._append({"entry": entry})

    @classmethod
    def from_journal(cls, match_id: str) -> MatchPacket | None:
        """The packet a previous coach journaled for this game, or None (never raises)."""
        path = _journal_path(match_id)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        packet = cls(match_id)
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a line torn by the old process dying mid-write
            if not isinstance(record, dict):
                continue
            meta = record.get("meta")
            if isinstance(meta, dict) and meta.get("start_time"):
                packet.start_time = str(meta["start_time"])
            entry = record.get("entry")
            if isinstance(entry, dict):
                if "fingerprint" in entry:
                    entry["fingerprint"] = _as_fingerprint(entry["fingerprint"])
                packet.decisions.append(entry)
            outcome = record.get("outcome")
            if isinstance(outcome, list) and len(outcome) == 2:
                packet._settle(_as_fingerprint(outcome[0]), str(outcome[1]))
        # Rewrite it clean: a torn last line would swallow the next record appended to it.
        try:
            records = [{"meta": {"match_id": match_id, "start_time": packet.start_time}}]
            records += [{"entry": entry} for entry in packet.decisions]
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text(
                "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in records),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception as e:
            logger.debug(f"MatchPacket journal rewrite failed for {match_id}: {e}")
        packet.journal = path
        return packet

    def discard_journal(self) -> None:
        path, self.journal = self.journal, None
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    def add_decision(
        self,
        decision: PendingDecision,
        chosen_options: list[str],
        *,
        trace: dict[str, Any] | None = None,
        explanation: str = "",
        game_state: dict[str, Any] | None = None,
    ) -> None:
        """Log a decision faced and option chosen, with the planner's trace (policy, reasoning, lines)."""
        from arenamcp.request_tracker import decision_fingerprint

        fp = decision_fingerprint(decision)
        entry: dict[str, Any] = {
            "pending_decision": decision_to_dict(decision),
            "chosen_options": list(chosen_options),
            "outcome": "pending",
            "fingerprint": fp,
            "timestamp": time.time(),
        }
        if trace:
            entry["trace"] = trace
        if explanation:
            entry["explanation"] = explanation
        turn = (game_state or {}).get("turn") if isinstance(game_state, dict) else None
        if isinstance(turn, dict):
            entry["turn"] = {k: turn.get(k) for k in ("turn_number", "phase", "step", "active_player")}
        self._record(entry)

    def add_combat_decision(
        self,
        kind: str,
        *,
        chosen: list[str],
        options: list[str],
        explanation: str = "",
        trace: dict[str, Any] | None = None,
        game_state: dict[str, Any] | None = None,
    ) -> None:
        """Log an attack or block declaration (model plan or safe default) beside the typed decisions.

        2026-10-10: a bug report about an attack held only typed decisions;
        the declaration and its reasoning were in the log alone.
        """
        entry: dict[str, Any] = {
            "pending_decision": {
                "request_type": kind,
                "source_label": "",
                "options": [{"option_id": label, "label": label} for label in options],
            },
            "chosen_options": list(chosen),
            "outcome": "executed",
            "fingerprint": f"{kind}:{time.time():.3f}",
            "timestamp": time.time(),
        }
        if trace:
            entry["trace"] = trace
        if explanation:
            entry["explanation"] = explanation
        turn = (game_state or {}).get("turn") if isinstance(game_state, dict) else None
        if isinstance(turn, dict):
            entry["turn"] = {k: turn.get(k) for k in ("turn_number", "phase", "step", "active_player")}
        self._record(entry)

    def recent_decisions(self, count: int = 40) -> list[dict[str, Any]]:
        """The last decisions, compact enough for a bug report: labels, the pick, the trace."""
        out = []
        for entry in self.decisions[-count:]:
            pending = entry.get("pending_decision") or {}
            options = pending.get("options") or []
            chosen = set(entry.get("chosen_options") or [])
            out.append(
                {
                    "at": datetime.fromtimestamp(entry.get("timestamp", 0)).isoformat(timespec="seconds"),
                    "turn": entry.get("turn"),
                    "request_type": pending.get("request_type"),
                    "source": pending.get("source_label"),
                    "chosen": [o.get("label") for o in options if o.get("option_id") in chosen]
                    or list(chosen),
                    "options": [
                        f"{o.get('label')}{' [unpayable]' if o.get('payable') is False else ''}"
                        for o in options[:16]
                    ],
                    "explanation": entry.get("explanation", ""),
                    "trace": entry.get("trace"),
                    "outcome": entry.get("outcome"),
                }
            )
        return out

    def add_executed_action(
        self,
        action_type: str,
        card_name: str = "",
        detail: str = "",
        turn: int | None = None,
        path: str = "",
    ) -> None:
        """Log a plan-executed autopilot action (cast/land/attack/pass...).

        Typed decisions go through add_decision; executor actions were
        invisible before this — match 1 on 2026-07-05 saved decisions=0
        despite 8 successful bridge submissions, gutting the packet's value
        as telemetry (P0-9).
        """
        self._record(
            {
                "executed_action": {
                    "action_type": action_type,
                    "card_name": card_name,
                    "detail": detail,
                    "turn": turn,
                    "path": path,
                },
                "outcome": "executed",
                "timestamp": time.time(),
            }
        )

    def update_outcome(self, fp: tuple, outcome: str) -> None:
        """Settle a pending decision with its final execution outcome."""
        if self._settle(fp, outcome):
            self._append({"outcome": [fp, outcome]})

    def _settle(self, fp: Any, outcome: str) -> bool:
        for entry in reversed(self.decisions):
            if entry.get("fingerprint") == fp and entry.get("outcome") == "pending":
                entry["outcome"] = outcome
                logger.debug(f"MatchPacket: updated decision {fp[0]} to {outcome}")
                return True
        return False

    def save(self, packets_dir: Path | None = None) -> Path | None:
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
            mark_match_finalized(self.match_id)
            self.discard_journal()
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
            with contextlib.suppress(OSError):
                old.unlink()
    except Exception:
        pass


# Global singleton instance managed by standalone loop
_current_packet: MatchPacket | None = None

# Match ids whose packet already hit disk this session. The server's late
# "Completed match event for unseen match" re-surfaces a finished match id
# after finalization, which restarted recording and saved a junk
# decisions=0/result=unknown packet at the next boundary (P0-9,
# 2026-07-05 22:55:22).
_finalized_match_ids: set[str] = set()
# FINALIZED_PATH is read-modify-written from the coaching loop and the packet
# salvage path.
_finalized_lock = threading.Lock()


def _read_finalized_file() -> list[str]:
    try:
        data = json.loads(FINALIZED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = data.get("match_ids") if isinstance(data, dict) else None
    return [str(i) for i in ids] if isinstance(ids, list) else []


def mark_match_finalized(match_id: str) -> None:
    """Remember that this game's packet is on disk, in memory and in FINALIZED_PATH.

    2026-10-19 #29: the in-memory set was empty after the coach restarts at
    09:53:35 and 10:06:23, so the restored finished matches 92c09c7a and
    6175f002 were recorded again and their empty packets and bundles saved at
    the next match boundary.
    """
    _finalized_match_ids.add(match_id)
    try:
        with _finalized_lock:
            ids = [i for i in _read_finalized_file() if i != match_id]
            ids.append(str(match_id))
            payload = json.dumps({"match_ids": ids[-MAX_FINALIZED:]}, indent=1)
            FINALIZED_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = FINALIZED_PATH.with_name(f".{FINALIZED_PATH.name}.{threading.get_ident()}.tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(FINALIZED_PATH)
    except Exception as e:
        logger.warning(f"Could not persist finalized match {match_id}: {e}")


def is_match_finalized(match_id: str | None) -> bool:
    """This game's packet was saved, by this coach or one before a restart."""
    if not match_id:
        return False
    if match_id in _finalized_match_ids:
        return True
    with _finalized_lock:
        persisted = match_id in _read_finalized_file()
    if persisted:
        _finalized_match_ids.add(match_id)
    return persisted


def start_match_packet(match_id: str) -> MatchPacket | None:
    """Initialize a fresh match packet.

    Returns None (no recording) when this match id was already finalized —
    a late re-surfacing of a finished match, or a finished match restored
    after a coach restart, must not restart recording.

    If a previous packet is still active (its game-end event never fired —
    crash, missed boundary), salvage it to disk as result="abandoned"
    instead of silently discarding its recorded decisions.
    """
    global _current_packet
    if is_match_finalized(match_id):
        logger.info(f"Not restarting match packet for already-finalized match {match_id}")
        return None
    prev = _current_packet
    if prev is not None and prev.match_id != match_id and prev.decisions:
        prev.result = "abandoned"
        prev.save()
        logger.warning(
            f"Match packet for {prev.match_id} was never finalized — "
            f"salvaged as 'abandoned' ({len(prev.decisions)} decisions)"
        )
    _prune_journals(keep=match_id)
    resumed = MatchPacket.from_journal(match_id)
    if resumed is not None:
        _current_packet = resumed
        logger.info(
            f"Resumed match packet recording for match {match_id} after a restart "
            f"({len(resumed.decisions)} decisions since {resumed.start_time})"
        )
        return _current_packet
    _current_packet = MatchPacket(match_id)
    _current_packet.start_journal()
    logger.info(f"Started match packet recording for match {match_id}")
    return _current_packet


def _prune_journals(*, keep: str) -> None:
    """Delete live journals untouched for LIVE_MAX_AGE_S (games no coach finished)."""
    live = PACKETS_DIR / LIVE_SUBDIR
    keep_path = _journal_path(keep)
    try:
        cutoff = time.time() - LIVE_MAX_AGE_S
        for path in live.glob("*.jsonl"):
            if path != keep_path and path.stat().st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
    except OSError:
        pass


def get_current_packet() -> MatchPacket | None:
    """Retrieve the current active match packet."""
    return _current_packet


def stop_match_packet() -> MatchPacket | None:
    """Terminate and clear the active match packet."""
    global _current_packet
    packet = _current_packet
    _current_packet = None
    return packet
