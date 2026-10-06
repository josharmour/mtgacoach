"""Opt-in post-match navigation, isolated from strategic match decisions."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class _AutoQueueMixin:
    def set_auto_queue(self, enabled: bool) -> bool:
        self._auto_queue_enabled = bool(enabled)
        self.settings.set("auto_queue_enabled", self._auto_queue_enabled)
        self._auto_queue_since = time.time()
        self._auto_queue_last_noted = None
        self._auto_queue_observed_match = None
        self._auto_queue_faulted = False
        self._suspend_auto_queue()
        self.ui.status("AUTO_QUEUE", "ON" if enabled else "OFF")
        self.ui.status(
            "AUTO_QUEUE_DETAIL",
            "Waiting for this match to finish; Autoplay must be on." if enabled else "",
        )
        return self._auto_queue_enabled

    def _suspend_auto_queue(self) -> None:
        navigator = getattr(self, "_auto_queue_navigator", None)
        if navigator:
            navigator.set_enabled(False)
        inhibitor = getattr(self, "_auto_queue_sleep_inhibitor", None)
        if inhibitor:
            inhibitor.stop()

    def _poll_auto_queue(self, state: dict) -> bool:
        """Only an explicit match-completion event can hand UI control to navigation."""
        enabled = getattr(self, "_auto_queue_enabled", False)
        active = (
            enabled
            and self._autopilot_control_status() == "AP:ON"
            and not getattr(self, "_autopilot_dry_run", False)
            and not getattr(self, "_autopilot_afk", False)
        )
        if not active:
            self._suspend_auto_queue()
            return False
        if getattr(self, "_auto_queue_faulted", False):
            return False
        draft_driver = getattr(self, "_draft_event_driver", None)
        if (
            draft_driver is not None
            and draft_driver.enabled
            and draft_driver.run.event_name
            and not draft_driver.run.finished
        ):
            # Draft autoplay owns navigation between its event's matches.
            self._suspend_auto_queue()
            return False
        try:
            from arenamcp.auto_queue import AutoQueueNavigator
            from arenamcp.idle_sleep import SleepInhibitor
            from arenamcp.server import get_completed_match_for_navigation, get_last_queue_selection

            inhibitor = getattr(self, "_auto_queue_sleep_inhibitor", None)
            if inhibitor is None:
                inhibitor = self._auto_queue_sleep_inhibitor = SleepInhibitor()
            navigator = getattr(self, "_auto_queue_navigator", None)
            if navigator is None:
                backend = getattr(self, "_autopilot_backend", None) or getattr(self._coach, "_backend", None)
                if backend is None:
                    return False
                from arenamcp.gre_bridge import get_bridge

                bridge = get_bridge()
                navigator = self._auto_queue_navigator = AutoQueueNavigator(
                    backend=backend,
                    get_game_state=self._mcp.get_game_state,
                    bridge=bridge,
                    status_fn=lambda detail: self.ui.status("AUTO_QUEUE_DETAIL", detail),
                    queue_selection=get_last_queue_selection,
                )
            navigator.set_enabled(True)
            if not navigator.paused_reason:
                inhibitor.start()
            completed = get_completed_match_for_navigation()
            match_id = completed.get("match_id")
            current_id = state.get("match_id")
            turn = state.get("turn") or state.get("turn_info") or {}
            if (
                current_id
                and current_id != match_id
                and (turn.get("turn_number", 0) > 0 or state.get("pending_decision"))
            ):
                # Log replay during startup also parses old completion events.
                # Only queue after observing this game active in this session.
                self._auto_queue_observed_match = current_id
            if (
                match_id
                and match_id == getattr(self, "_auto_queue_observed_match", None)
                and completed.get("completed_at", 0) >= getattr(self, "_auto_queue_since", float("inf"))
                and match_id != getattr(self, "_auto_queue_last_noted", None)
            ):
                navigator.note_match_end(completed, confirmed_match_end=True)
                self._auto_queue_last_noted = match_id
            owns_input = navigator.process_tick(state)
            if navigator.paused_reason:
                inhibitor.stop()
            return owns_input
        except Exception as exc:
            self._auto_queue_faulted = True
            self._suspend_auto_queue()
            self.ui.status("AUTO_QUEUE_DETAIL", f"Auto-queue stopped: {exc}")
            logger.exception("Auto-queue failed")
            return False
