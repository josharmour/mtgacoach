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
        # Turning it on never starts a match; only a restart resumes one.
        self._auto_queue_restart_pending = False
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

    def _arm_auto_queue_after_restart(self, navigator, state: dict) -> bool:
        """The coach restarted with auto-queue on and Arena idle after a match: requeue it.

        2026-10-19 09:53:35 and 10:06:23: the coach restarted while Arena sat
        on Home after a match; auto-queue only ever arms from a match it saw
        end, said "Waiting for this match to finish", and a human clicked Play
        both times. Checked once, at the first poll with autoplay driving. It
        arms only when no match is being played and Player.log shows the last
        match completed (within 30 min) with nothing after it: no queue
        request, other match, gameplay or draft. The queue and deck come from
        the log's request for that match, and navigation keeps its own rules
        (no paid or draft events; the log confirms the queue joined).
        """
        turn = state.get("turn") or state.get("turn_info") or {}
        current_id = state.get("match_id")
        if current_id and ((turn.get("turn_number") or 0) > 0 or state.get("pending_decision")):
            return False
        from arenamcp.server import idle_completed_match_from_log

        completed = idle_completed_match_from_log()
        if not completed or (current_id and current_id != completed["match_id"]):
            logger.info(
                "Auto queue after restart: Arena is not idle after a recent match; waiting for a match end"
            )
            return False
        if not navigator.note_match_end(completed, confirmed_match_end=True):
            return False
        self._auto_queue_observed_match = self._auto_queue_last_noted = completed["match_id"]
        logger.info(
            "Auto queue after restart: match %s ended %.0fs ago and Arena has been idle since;"
            " requeueing %s with %r",
            completed["match_id"],
            time.time() - completed["completed_at"],
            completed.get("event_id"),
            completed.get("deck_name"),
        )
        return True

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
            # A pause that retries by itself still needs the Mac awake.
            if not navigator.paused_reason or navigator.retry_scheduled:
                inhibitor.start()
            if getattr(self, "_auto_queue_restart_pending", False):
                self._auto_queue_restart_pending = False
                self._arm_auto_queue_after_restart(navigator, state)
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
            if navigator.paused_reason and not navigator.retry_scheduled:
                inhibitor.stop()
            return owns_input
        except Exception as exc:
            self._auto_queue_faulted = True
            self._suspend_auto_queue()
            self.ui.status("AUTO_QUEUE_DETAIL", f"Auto-queue stopped: {exc}")
            logger.exception("Auto-queue failed")
            return False
