"""The sidebar's game-plan card: one emitter for the coaching loop, the autopilot and plan reforms.

The card shows ``GamePlanManager.ui_payload``: the plan, the board facts
(role, clocks, lines) and the outlook, all recomputed from a snapshot. It
used to be pushed only when the autopilot planned a decision, when the coach
made an advice call, or when a plan reform finished, and a reform painted the
snapshot it had started from. 2026-10-18, match 02fc3a1d: on the opponent's
turn 13 their spells and a land drop (Mossborn Hydra's counters doubled to
16/16 at 18:28:07, Player.log gsid 348-355) turned "AGGRESSOR, lethal on
T14" into "CONTROL/STABILIZE, dead in 2 turns". Every trigger in that turn
was quiet, so the card kept the old outlook until the autopilot planned
blocks at 18:28:30.

Now the coaching loop syncs the card from each snapshot, whoever is playing
(at most every :data:`SYNC_INTERVAL_S`), and the loop, the autopilot and
reform completions share one :class:`PlanCard`, so the card repaints only
when what it shows changes.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# The loop wakes on every new GRE message; the board facts cost 1-20 ms per
# new board (cached per board signature), so this caps the work at two
# payloads a second while still repainting well within a second.
SYNC_INTERVAL_S = 0.5


def card_payload(manager: Any, game_state: dict[str, Any] | None, *, source: str) -> dict[str, Any]:
    """``manager``'s card payload for ``game_state``, tagged with its sender; ``{}`` when there is none."""
    payload_fn = getattr(manager, "ui_payload", None)
    payload = payload_fn(game_state) if callable(payload_fn) else None
    if not isinstance(payload, dict):
        plan = getattr(manager, "current", None)
        payload = dict(plan.as_payload()) if plan is not None else {}
    return dict(payload, source=source) if payload else {}


def _shown_part(payload: dict[str, Any]) -> dict[str, Any]:
    """``payload`` without the running reform's seconds, which change every second on their own."""
    status = payload.get("plan_status")
    if isinstance(status, dict) and "inflight_s" in status:
        return dict(payload, plan_status={k: v for k, v in status.items() if k != "inflight_s"})
    return payload


class PlanCard:
    """Pushes the plan card to the UI when it changes; thread-safe (loop, autopilot, reform threads)."""

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], Any],
        *,
        clock: Callable[[], float] = time.monotonic,
        sync_interval_s: float = SYNC_INTERVAL_S,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._sync_interval_s = sync_interval_s
        self._lock = threading.Lock()
        self._shown: dict[str, Any] | None = None
        self._last_sync_at: float | None = None

    def publish(self, manager: Any, game_state: dict[str, Any] | None, *, source: str) -> bool:
        """Show the card for ``game_state`` now; False when the card already shows it.

        Never raises: the card must not break the coaching loop or a decision.
        """
        if manager is None or not callable(self._emit):
            return False
        with self._lock:
            try:
                payload = card_payload(manager, game_state, source=source)
                shown = _shown_part(payload)
                if shown == self._shown:
                    return False
                self._emit(payload)
                self._shown = shown
                return True
            except Exception as error:
                logger.debug("game-plan card emit failed: %s", error)
                return False

    def sync(self, manager: Any, game_state: dict[str, Any] | None, *, source: str) -> bool:
        """:meth:`publish` for the coaching loop's snapshots, at most once per sync interval.

        A change that arrives inside the interval is shown by the next sync
        after it, so the loop's next iteration catches it.
        """
        now = self._clock()
        with self._lock:
            if self._last_sync_at is not None and now - self._last_sync_at < self._sync_interval_s:
                return False
            self._last_sync_at = now
        return self.publish(manager, game_state, source=source)
