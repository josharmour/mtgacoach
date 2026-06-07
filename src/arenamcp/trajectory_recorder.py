"""Lightweight, opt-in trajectory recorder for real-match autopilot play.

Captures one record per autopilot decision in the SAME JSONL shape produced by
``arenamcp.self_play`` so ``tools/training/build_dataset.py`` can consume
real-match trajectories exactly like self-play trajectories. Winner labelling
happens once at match end via :meth:`TrajectoryRecorder.flush_match`.

Design goals:
- **Dependency-light:** stdlib only (json, time, pathlib, logging).
- **Safe:** every public method swallows its own exceptions. Recording must
  NEVER raise into the live autopilot/play loop.
- **Opt-in:** nothing records unless a recorder instance is explicitly attached
  to the autopilot engine (``engine._trajectory_recorder = recorder``).

Record shape (matches ``SelfPlayOrchestrator`` ``decision_rec``)::

    {
      "match_id", "ts", "seat", "backend", "alt_backend",
      "request_type", "turn", "phase",
      "prompt_system", "prompt_user",
      "planned_action", "alt_planned_action",
      "submit_command", "latency_ms",
      "winner"            # added at flush time
    }
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("arenamcp.trajectory_recorder")

# Default output path, matching the self-play trajectory location convention.
_REPO = Path(__file__).resolve().parents[2]
DEFAULT_TRAJECTORY_PATH = _REPO / "tools/eval/data/real_match_trajectories.jsonl"


def normalize_winner(result: Optional[str], seat: str = "local") -> Optional[str]:
    """Map a per-game result string to a self-play ``winner`` seat label.

    ``build_dataset.py`` only accepts ``winner`` values of ``"local"`` or
    ``"opp"``. ``seat`` is the seat the recorded decisions belong to (the
    autopilot always plays the local seat in real matches).

    Returns ``"local"``/``"opp"`` for a decisive result, or ``None`` for
    draws/unknowns (so the caller can drop the match rather than mislabel it).
    """
    if not result:
        return None
    r = str(result).strip().lower()
    other = "opp" if seat == "local" else "local"
    if r in ("win", "won", "local", seat):
        return seat
    if r in ("loss", "lose", "lost", "opp", "opponent", other):
        return other
    # draw / unknown / tie → no usable label.
    return None


def _stringify_action(action: Any) -> str:
    """Stringify a planned action the way self_play logs it ("pass" when None)."""
    if action is None:
        return "pass"
    try:
        return str(action)
    except Exception:
        return "pass"


class TrajectoryRecorder:
    """Buffers autopilot decisions per match and flushes them as JSONL.

    Thread-safe: ``record_decision`` may be called from the autopilot thread
    while ``flush_match`` is called from the driver thread.
    """

    def __init__(
        self,
        out_path: Optional[Path] = None,
        *,
        seat: str = "local",
        backend_label: str = "",
        alt_backend_label: str = "",
    ) -> None:
        self.out_path = Path(out_path) if out_path else DEFAULT_TRAJECTORY_PATH
        self.seat = seat
        self.backend_label = backend_label
        self.alt_backend_label = alt_backend_label
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._current_match_id: Optional[str] = None
        # Cumulative counters (diagnostics only).
        self.total_recorded = 0
        self.total_flushed = 0
        try:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover - filesystem edge case
            logger.warning("Could not create trajectory dir %s: %s", self.out_path.parent, e)

    @property
    def buffered(self) -> int:
        """Number of decisions buffered for the current (unflushed) match."""
        with self._lock:
            return len(self._buffer)

    def record_decision(
        self,
        *,
        game_state: Optional[Dict[str, Any]],
        prompt_system: str,
        prompt_user: str,
        planned_action: Any,
        alt_action: Any = None,
        request_type: Optional[str] = None,
        latency_ms: Optional[float] = None,
        submit_command: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Buffer one decision record. Never raises into the caller."""
        try:
            gs = game_state or {}
            turn = gs.get("turn", {}) or {}
            match_id = gs.get("match_id") or self._current_match_id or "unknown_match"

            rec: Dict[str, Any] = {
                "match_id": match_id,
                "ts": time.time(),
                "seat": self.seat,
                "backend": self.backend_label,
                "alt_backend": self.alt_backend_label,
                "request_type": request_type or gs.get("_bridge_request_type") or "",
                "turn": turn.get("turn_number", 0),
                "phase": turn.get("phase", ""),
                "prompt_system": prompt_system or "",
                "prompt_user": prompt_user or "",
                "planned_action": _stringify_action(planned_action),
                "alt_planned_action": _stringify_action(alt_action),
                "submit_command": submit_command,
                "latency_ms": round(float(latency_ms), 1) if latency_ms is not None else None,
            }

            with self._lock:
                self._current_match_id = match_id
                self._buffer.append(rec)
                self.total_recorded += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("record_decision failed (ignored): %s", e)

    def flush_match(self, winner: Optional[str]) -> int:
        """Append all buffered decisions to ``out_path``, labelled with winner.

        ``winner`` should be the self-play seat label (``"local"``/``"opp"``);
        ``None`` is written through as-is (build_dataset will skip those rows).
        Returns the number of records written. Never raises.
        """
        try:
            with self._lock:
                batch = self._buffer
                self._buffer = []
                match_id = self._current_match_id
                self._current_match_id = None

            if not batch:
                return 0

            for rec in batch:
                rec["winner"] = winner

            with open(self.out_path, "a", encoding="utf-8") as f:
                for rec in batch:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

            self.total_flushed += len(batch)
            logger.info(
                "Flushed %d decisions for match %s (winner=%s) -> %s",
                len(batch), match_id, winner, self.out_path,
            )
            return len(batch)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("flush_match failed (decisions lost): %s", e)
            return 0

    def discard_match(self) -> int:
        """Drop the current buffer WITHOUT writing (e.g. an abandoned match)."""
        try:
            with self._lock:
                n = len(self._buffer)
                self._buffer = []
                self._current_match_id = None
            return n
        except Exception:
            return 0
