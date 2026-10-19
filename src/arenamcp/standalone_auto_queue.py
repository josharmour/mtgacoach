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
        self._auto_queue_resume = None
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

    def _auto_queue_export(self) -> dict | None:
        """Armed navigation for the engine-reload checkpoint (standalone_startup.py)."""
        if not getattr(self, "_auto_queue_enabled", False):
            return None
        navigator = getattr(self, "_auto_queue_navigator", None)
        record = None
        if navigator is not None:
            try:
                record = navigator.export_for_reload()
            except Exception as exc:
                logger.warning("Auto queue: could not save navigation for the engine reload: %s", exc)
        # A record restored from an earlier reload and not yet resumed (autoplay
        # was not driving) rides along to the next engine.
        return record or getattr(self, "_auto_queue_resume", None)

    def _resume_auto_queue_after_reload(self, navigator, record: dict, state: dict) -> bool:
        """Continue the navigation the previous engine had armed (engine reload checkpoint).

        2026-10-19: the coach reloads itself between matches when its code
        changed (standalone_auto_reload.py), usually seconds after a match
        ended and navigation armed. The previous engine saved that navigation;
        it resumes here with the same finished match, queue, deck and stage,
        without re-reading Player.log. When it does not apply (expired, or a
        later match is in the state), the restart path below decides as for
        any restart.
        """
        ended = record.get("ended_match_id")
        current_id = state.get("match_id")
        try:
            age = time.time() - float(record.get("saved_at") or 0.0)
        except (TypeError, ValueError):
            age = float("inf")
        if not ended:
            return False
        if not 0 <= age <= 600:
            logger.info(
                "Auto queue after reload: the saved navigation for match %s expired (%.0fs old)", ended, age
            )
            return False
        if current_id and current_id != ended:
            logger.info(
                "Auto queue after reload: match %s started after match %s ended; autoplay is in control",
                current_id,
                ended,
            )
            return False
        if not navigator.restore_after_reload(record):
            logger.info("Auto queue after reload: navigation did not take match %s (disabled)", ended)
            return False
        self._auto_queue_observed_match = self._auto_queue_last_noted = ended
        expected = record.get("expected_queue") or {}
        logger.info(
            "Auto queue after reload: continuing navigation for match %s at stage %s; requeueing %s with %r",
            ended,
            record.get("stage"),
            expected.get("event_id") or record.get("last_event_id"),
            expected.get("deck_name"),
        )
        return True

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

        Every outcome is logged. 2026-10-19 (ledger #55): after the 11:06:23
        restart Arena sat idle on Decks (match 15e0eb20 ended 10:58:35, a
        DeckUpsertDeckV3 at 11:05) and nothing requeued: that coach still ran
        c9dfda7 (master reached this code at 11:07:14), and by the 11:07:19
        restart a human had clicked Play (EventJoin 11:06:39) and match
        73ccac27 was under way. The silent "match in progress" return left
        the coach log unable to tell the two apart.
        """
        turn = state.get("turn") or state.get("turn_info") or {}
        current_id = state.get("match_id")
        turn_number = turn.get("turn_number") or 0
        if current_id and (turn_number > 0 or state.get("pending_decision")):
            logger.info(
                "Auto queue after restart: match %s is being played (turn %s%s); requeueing after it ends",
                current_id,
                turn_number,
                ", decision pending" if state.get("pending_decision") else "",
            )
            return False
        from arenamcp.server import idle_completed_match_from_log

        completed = idle_completed_match_from_log()
        if not completed or (current_id and current_id != completed["match_id"]):
            logger.info(
                "Auto queue after restart: Arena is not idle after a recent match (%s); waiting for a match end",
                f"the log's last idle match is {completed['match_id']}, the coach holds {current_id}"
                if completed
                else "Player.log shows no completed match with only menu traffic after it",
            )
            return False
        if not navigator.note_match_end(completed, confirmed_match_end=True):
            logger.info(
                "Auto queue after restart: navigation did not take match %s (already handled or disabled)",
                completed["match_id"],
            )
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
                resume = getattr(self, "_auto_queue_resume", None)
                self._auto_queue_resume = None
                if not (resume and self._resume_auto_queue_after_reload(navigator, resume, state)):
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
