"""Tempo and decision-interval tracking for Standalone Coach."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class _TempoTracker:
    def __init__(self, stall_threshold: float = 1.5, min_samples: int = 5):
        self._stall_threshold = stall_threshold
        self._min_samples = min_samples
        self._last_state_hash: str | None = None
        self._last_change_time: float = 0.0
        self._intervals: list[float] = []  # Recent inter-change intervals
        self._max_intervals = 30

    def update(self, game_state: dict) -> bool:
        """Feed a new game state snapshot.  Returns True if a stall is detected.

        A stall is when:
          - We have enough baseline samples (min_samples)
          - Time since last state change exceeds stall_threshold
          - The game is active (turn > 0)
        """
        now = time.time()

        # Cheap hash of the fields that change on every GRE update
        turn = game_state.get("turn", {})
        sig = (
            turn.get("turn_number", 0),
            turn.get("phase", ""),
            turn.get("step", ""),
            turn.get("priority_player", 0),
            game_state.get("pending_decision"),
            len(game_state.get("hand", [])),
            len(game_state.get("battlefield", [])),
            len(game_state.get("stack", [])),
        )
        state_hash = str(sig)

        if state_hash != self._last_state_hash:
            # State changed — record interval
            if self._last_change_time > 0:
                interval = now - self._last_change_time
                self._intervals.append(interval)
                if len(self._intervals) > self._max_intervals:
                    self._intervals.pop(0)
            self._last_state_hash = state_hash
            self._last_change_time = now
            return False

        # State hasn't changed — check for stall
        if self._last_change_time <= 0:
            self._last_change_time = now
            return False

        turn_num = turn.get("turn_number", 0)
        if turn_num == 0:
            return False  # Game not active

        elapsed = now - self._last_change_time
        return bool(len(self._intervals) >= self._min_samples and elapsed > self._stall_threshold)

    @property
    def avg_interval(self) -> float:
        """Average seconds between state changes."""
        if not self._intervals:
            return 0.0
        return sum(self._intervals) / len(self._intervals)

    @property
    def stall_duration(self) -> float:
        """How long the current stall has lasted."""
        if self._last_change_time <= 0:
            return 0.0
        return time.time() - self._last_change_time

    def reset(self) -> None:
        """Reset tracker for a new match."""
        self._last_state_hash = None
        self._last_change_time = 0.0
        self._intervals.clear()
