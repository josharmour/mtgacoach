"""Opt-in post-match navigation and explicit idle Grind initiation."""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


from arenamcp.standalone_auto_quest import AutoQuestMixin

AUTO_QUEUE_LOSS_LIMIT = 3
LOSS_STOP_DETAIL = (
    f"Auto-queue stopped after {AUTO_QUEUE_LOSS_LIMIT} consecutive match losses. "
    "Turn Auto-queue on to start a fresh run."
)
LOSS_SAFEGUARD_LABEL = "LOSS SAFEGUARD"
LOSS_SAFEGUARD_MESSAGE = (
    f"Auto-queue paused automatically after {AUTO_QUEUE_LOSS_LIMIT} consecutive match losses so it "
    "doesn't keep queueing unattended during a losing streak or bad matchup run. Autoplay itself "
    "remains ready when you enter a game.\n\n"
    "Choose Clear safeguard to reset the loss counter and resume queueing right away, or "
    "Suggest decks to review decklists matched to the cards you own and the wildcards you've collected."
)


class _AutoQueueMixin(AutoQuestMixin):
    def _loss_limit(self) -> int:
        """Consecutive-loss stop threshold (0 disables the safeguard)."""
        raw = None
        if hasattr(self, "settings"):
            raw = self.settings.get("auto_queue_loss_limit")
            # Re-read from disk when backed by a real Settings instance and not
            # locally overridden, so desktop UI changes apply immediately across processes.
            try:
                from arenamcp.settings import Settings

                if isinstance(self.settings, Settings) and "auto_queue_loss_limit" not in getattr(
                    self.settings, "_dirty", ()
                ):
                    disk_val = Settings().get("auto_queue_loss_limit")
                    if disk_val is not None:
                        raw = disk_val
            except Exception:
                pass
        if raw is None:
            return AUTO_QUEUE_LOSS_LIMIT
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return AUTO_QUEUE_LOSS_LIMIT
        return max(0, val)

    def _auto_queue_mode(self) -> str:
        """Auto-queue mode: 'same' (repeat current deck) or 'winning' (queue strongest deck)."""
        raw = None
        if hasattr(self, "settings"):
            raw = self.settings.get("auto_queue_mode")
            try:
                from arenamcp.settings import Settings

                if isinstance(self.settings, Settings) and "auto_queue_mode" not in getattr(
                    self.settings, "_dirty", ()
                ):
                    disk_val = Settings().get("auto_queue_mode")
                    if disk_val is not None:
                        raw = disk_val
            except Exception:
                pass
        return "winning" if str(raw or "").strip().lower() == "winning" else "same"

    def _resolve_winning_queue_target(self) -> dict[str, object] | None:
        """Evaluate queueable constructed/Brawl decks and return the top winning target."""
        if self._auto_queue_mode() != "winning":
            return None
        try:
            from arenamcp.between_match_coach import QUEUE_PLAN_LABEL, evaluate_live_queue_plan

            plan = evaluate_live_queue_plan()
            if plan.recommended is None:
                return None
            target = plan.recommended.to_target()
            sig = (target["deck_id"], target["event_id"])
            if getattr(self, "_winning_queue_last_sig", None) != sig:
                self._winning_queue_last_sig = sig
                rec = plan.recommended
                rec_str = (
                    f"{rec.wins}W-{rec.losses}L ({rec.rate:.0%})" if rec.total > 0 else "no recent record"
                )
                strategy_note = self._winning_deck_strategy_status(rec.deck_id)
                msg = (
                    f"Auto-queue (Winning decks): targeting {rec.label} in {rec.event_id} "
                    f"({rec_str}{strategy_note}).\n{plan.summary}"
                )
                logger.info(
                    "Auto-queue winning target: %s in %s (%s%s)",
                    rec.label,
                    rec.event_id,
                    rec_str,
                    strategy_note,
                )
                advice = getattr(getattr(self, "ui", None), "advice", None)
                if callable(advice):
                    advice(msg, QUEUE_PLAN_LABEL)
            return target
        except Exception:
            logger.debug("Could not resolve winning auto-queue target", exc_info=True)
            return None

    def _winning_deck_strategy_status(self, deck_id: str) -> str:
        """Report playbook readiness for *deck_id* and kick off a background study if missing."""
        try:
            library = getattr(self, "_deck_library", None) or {}
            record = library.get(deck_id)
            if record is None or not getattr(record, "complete", False):
                return ""
            status_fn = getattr(self, "deck_strategy_status", None)
            if not callable(status_fn):
                return ""
            st = (status_fn(record) or {}).get("status")
            if st == "ready":
                return " · strategy ready"
            if st == "building":
                return " · studying deck"
            if st in ("none", "error"):
                analyze = getattr(self, "analyze_deck_async", None)
                if callable(analyze):
                    analyze(deck_id, refresh=False)
                    return " · studying deck in background"
        except Exception:
            logger.debug("Could not check winning-deck strategy status", exc_info=True)
        return ""

    def _loss_stop_detail(self, limit: int) -> str:
        if limit == AUTO_QUEUE_LOSS_LIMIT:
            return LOSS_STOP_DETAIL
        return (
            f"Auto-queue stopped after {limit} consecutive match losses. "
            "Turn Auto-queue on to start a fresh run."
        )

    def _queue_streak(self) -> dict:
        streak = getattr(self, "_auto_queue_streak", None)
        if streak is None:
            saved = self.settings.get("auto_queue_streak")
            streak = self._auto_queue_streak = dict(saved) if isinstance(saved, dict) else {}
            self._queue_restore_streak = True
        return streak

    def _reconcile_saved_loss_streak(self) -> dict:
        """Clear a saved loss streak if later confirmed wins or draws occurred."""
        if not hasattr(self, "settings"):
            return {}
        streak = self._queue_streak()
        if streak.get("losses", 0) <= 0:
            return streak
        try:
            from arenamcp.server import logged_queue_results

            rows = logged_queue_results(streak.get("rearmed_at", 0), limit=max(AUTO_QUEUE_LOSS_LIMIT + 3, 10))
        except Exception:
            logger.debug("Could not reconcile saved loss streak from log", exc_info=True)
            return streak
        through = float(streak.get("through") or 0.0)
        newer = [
            row
            for row in rows
            if row.get("outcome") in ("win", "draw", "loss")
            and float(row.get("arena_completed_at") or 0.0) > through
        ]
        if not newer or not any(row["outcome"] in ("win", "draw") for row in newer):
            return streak
        last_break = max(i for i, row in enumerate(newer) if row["outcome"] in ("win", "draw"))
        trailing_losses = sum(1 for row in newer[last_break + 1 :] if row["outcome"] == "loss")
        streak.update(
            losses=trailing_losses,
            last=newer[-1]["match_id"],
            through=float(newer[-1]["arena_completed_at"]),
        )
        streak.pop("pending", None)
        streak.setdefault("since", streak.get("rearmed_at", time.time()))
        self.settings.set("auto_queue_streak", streak)
        return streak

    def _queue_loss_guard(self, state: dict, completed: dict) -> bool:
        """Account for Arena's final match outcome before allowing any next entry."""
        limit = self._loss_limit()
        if limit <= 0:
            return True
        streak = self._queue_streak()
        if not getattr(self, "_queue_restore_streak", False) and streak.get("losses", 0) >= limit:
            return self._stop_queue_after_losses()
        if getattr(self, "_queue_restore_streak", False):
            from arenamcp.server import logged_queue_results

            # A checkpoint may resume the navigator without ever consulting
            # Recent Match. Reconcile newer Arena results before either path.
            if getattr(self, "_queue_restore_results", None) is None:
                now = time.monotonic()
                if now < getattr(self, "_queue_history_retry_at", 0):
                    return False
                rearmed = streak.get("rearmed_at", 0)
                history = logged_queue_results(rearmed, limit=limit)
                # Only a confirmed win/draw breaks the run. A newer known
                # breaker makes preceding unknown results irrelevant.
                for i in reversed(range(len(history))):
                    if history[i]["outcome"] in ("win", "draw"):
                        history = history[i:]
                        break
                for i, row in enumerate(history):
                    if row["outcome"] not in ("win", "draw", "loss"):
                        resolved = logged_queue_results(rearmed, row["match_id"])
                        if not resolved or resolved[0]["outcome"] not in ("win", "draw", "loss"):
                            self._queue_history_retry_at = now + 2
                            self.ui.status(
                                "AUTO_QUEUE_DETAIL", "Waiting for Arena to confirm the match result."
                            )
                            return False
                        history[i] = resolved[0]
                # Earlier versions reset the counter on a navigation retry. Anchor
                # the saved count to its real result before replaying newer games.
                prior_losses = 0
                for row in history:
                    prior_losses = prior_losses + 1 if row["outcome"] == "loss" else 0
                    if row["match_id"] == streak.get("last") and prior_losses > streak.get("losses", 0):
                        streak["losses"] = prior_losses
                        self.settings.set("auto_queue_streak", streak)
                newer = [row for row in history if row["arena_completed_at"] > streak.get("through", 0)]
                # A win or draw while queue was off breaks the streak, including
                # older losses that would have stopped a continuously active run.
                last_break = next(
                    (i for i in reversed(range(len(newer))) if newer[i]["outcome"] in ("win", "draw")), 0
                )
                self._queue_restore_results = newer[last_break:]
            for row in self._queue_restore_results:
                if row["match_id"] != streak.get("last") and row["arena_completed_at"] > streak.get(
                    "through", 0
                ):
                    if not self._record_queue_outcome(
                        streak, row["match_id"], row["outcome"], row["arena_completed_at"]
                    ):
                        return False
            self._queue_restore_streak = False
            self._queue_restore_results = None
            if limit > 0 and streak.get("losses", 0) >= limit:
                return self._stop_queue_after_losses()
        pending = streak.get("pending")
        current = state.get("match_id")
        turn = state.get("turn") or state.get("turn_info") or {}
        live = current and (turn.get("turn_number", 0) > 0 or state.get("pending_decision"))
        if pending and pending != completed.get("match_id") and (not live or current != pending):
            if not self._record_queue_outcome(streak, pending, None, 0):
                return False
            pending = None
        mid = completed.get("match_id")
        stamp = completed.get("arena_completed_at", 0)
        since = streak.get("since", getattr(self, "_auto_queue_since", 0))
        if (
            mid
            and mid != streak.get("last")
            and (mid == pending or (stamp > since and stamp > streak.get("through", 0)))
        ):
            if not self._record_queue_outcome(streak, mid, completed.get("outcome"), stamp):
                return False
            pending = streak.get("pending")
        if live and current not in (mid, pending, streak.get("last")):
            streak.update(pending=current, since=since)
            self.settings.set("auto_queue_streak", streak)
        return True

    def _record_queue_outcome(self, streak: dict, mid: str, outcome: str | None, stamp: float) -> bool:
        if outcome not in ("win", "loss", "draw"):
            # The watcher can receive MatchCompleted before the authoritative
            # result or miss it during reload. Retry from disk without scanning every poll.
            now = time.monotonic()
            if getattr(self, "_queue_retry_mid", None) != mid or now >= self._queue_retry_at:
                from arenamcp.server import logged_queue_results

                self._queue_retry_mid, self._queue_retry_at = mid, now + 5
                records = logged_queue_results(streak.get("since", self._auto_queue_since), mid)
                if records:
                    outcome = records[-1]["outcome"]
                    stamp = records[-1]["arena_completed_at"]
            if not outcome:
                if streak.get("pending") != mid:
                    streak["pending"] = mid
                    self.settings.set("auto_queue_streak", streak)
                self.ui.status("AUTO_QUEUE_DETAIL", "Waiting for Arena to confirm this match’s result.")
                return False
        if stamp and stamp <= streak.get("through", 0):
            # A delayed disk result for an older pending game cannot change
            # the streak after a more recent confirmed result.
            if streak.get("pending") == mid:
                streak.pop("pending")
                self.settings.set("auto_queue_streak", streak)
            return True
        streak.update(
            losses=streak.get("losses", 0) + 1 if outcome == "loss" else 0,
            last=mid,
            through=max(stamp, streak.get("through", 0)),
        )
        streak.setdefault("since", self._auto_queue_since)
        if streak.get("pending") == mid:
            streak.pop("pending", None)
        self.settings.set("auto_queue_streak", streak)
        limit = self._loss_limit()
        if limit > 0 and streak["losses"] >= limit:
            return self._stop_queue_after_losses()
        return True

    def _safeguard_brief_message(self, limit: int) -> str:
        """Build a data-backed safeguard message using local match history when available."""
        try:
            from arenamcp.between_match_coach import summarize_between_matches
            from arenamcp.match_history import MatchHistory

            history = MatchHistory()
            brief = summarize_between_matches(
                history.get_recent(history.total_games),
                consecutive_losses=limit,
            )
            if brief is not None:
                header = (
                    f"Auto-queue paused after {limit} consecutive match losses. "
                    "Autoplay remains ready when you enter a game."
                )
                return f"{header}\n\n{brief.message}"
        except Exception:
            logger.debug("Could not enrich loss safeguard with match history", exc_info=True)
        if limit == AUTO_QUEUE_LOSS_LIMIT:
            return LOSS_SAFEGUARD_MESSAGE
        return (
            f"Auto-queue paused automatically after {limit} consecutive match losses so it "
            "doesn't keep queueing unattended during a losing streak. Autoplay itself "
            "remains ready when you enter a game."
        )

    def _emit_loss_safeguard_advice(self) -> None:
        advice = getattr(self.ui, "advice", None)
        if not callable(advice):
            return
        streak = getattr(self, "_auto_queue_streak", None) or {}
        key = (streak.get("last"), streak.get("through"))
        if hasattr(self, "_loss_safeguard_notified_key") and self._loss_safeguard_notified_key == key:
            return
        self._loss_safeguard_notified_key = key
        limit = self._loss_limit() or AUTO_QUEUE_LOSS_LIMIT
        advice(self._safeguard_brief_message(limit), LOSS_SAFEGUARD_LABEL)

    def _stop_queue_after_losses(self) -> bool:
        limit = self._loss_limit() or AUTO_QUEUE_LOSS_LIMIT
        detail = self._loss_stop_detail(limit)
        self.set_auto_queue(False)
        self.ui.status("AUTO_QUEUE_DETAIL", detail)
        self.ui.log(detail)
        self._emit_loss_safeguard_advice()
        logger.info(detail)
        return False

    def clear_loss_safeguard(self, *, enable_auto_queue: bool = True) -> bool:
        """Reset any recorded loss-safeguard streak and optionally resume Auto-queue."""
        prev = (
            self._queue_streak()
            if hasattr(self, "settings")
            else (getattr(self, "_auto_queue_streak", None) or {})
        )
        latest_log_ts = 0.0
        try:
            from arenamcp.server import logged_queue_results

            latest = logged_queue_results(0, limit=1)
            if latest:
                latest_log_ts = float(latest[-1].get("arena_completed_at") or 0.0)
        except Exception:
            pass
        cut = max(time.time(), float(prev.get("through") or 0.0), latest_log_ts)
        fresh = {"since": cut, "rearmed_at": cut, "through": cut, "losses": 0}
        if prev.get("last"):
            fresh["last"] = prev["last"]
        self._auto_queue_streak = fresh
        self._queue_restore_streak = False
        self._queue_restore_results = None
        self._loss_safeguard_notified_key = None
        if hasattr(self, "settings"):
            self.settings.set("auto_queue_streak", fresh)
        if enable_auto_queue:
            return self.set_auto_queue(True)
        self.ui.status("AUTO_QUEUE_DETAIL", "")
        self.ui.log("Auto-queue loss safeguard cleared.")
        return False

    def set_auto_queue(self, enabled: bool) -> bool:
        if getattr(self, "_auto_quest", None):
            self._auto_quest = None
            self.ui.status("GRIND", "OFF")
        if enabled:
            streak = self._queue_streak() if hasattr(self, "settings") else {}
            limit = self._loss_limit()
            if limit > 0 and streak.get("losses", 0) >= limit:
                cut = max(time.time(), float(streak.get("through") or 0.0))
                fresh = {"since": cut, "rearmed_at": cut, "through": cut, "losses": 0}
                if streak.get("last"):
                    fresh["last"] = streak["last"]
                self._auto_queue_streak = fresh
                self._loss_safeguard_notified_key = None
                if hasattr(self, "settings"):
                    self.settings.set("auto_queue_streak", fresh)
        if bool(enabled) != bool(getattr(self, "_auto_queue_enabled", False)):
            if enabled:
                self._queue_restore_streak = True
                self._queue_restore_results = None
            try:
                self._loss_store().new_session()
            except Exception:
                logger.debug("Could not persist auto-queue review session", exc_info=True)
        self._auto_queue_enabled = bool(enabled)
        self.settings.set("auto_queue_enabled", self._auto_queue_enabled)
        self._auto_queue_since = time.time()
        # Explicit opt-in starts the native Recent Match path when Home is idle.
        self._auto_queue_restart_pending = bool(enabled)
        self._auto_queue_resume = None
        self._auto_queue_last_noted = None
        self._auto_queue_observed_match = None
        self._auto_queue_faulted = False
        self._winning_queue_last_sig = None
        self._suspend_auto_queue()
        # An idle Grind arm belongs to that explicit session, never ordinary
        # auto-queue or a later Grind session.
        navigator = getattr(self, "_auto_queue_navigator", None)
        if navigator is not None and getattr(navigator, "_idle_grind", False) is True:
            self._auto_queue_navigator = None
        self.ui.status("AUTO_QUEUE", "ON" if enabled else "OFF")
        if enabled:
            detail = (
                "Autoplay must be on; evaluating winning decks when Arena is idle."
                if self._auto_queue_mode() == "winning"
                else "Autoplay must be on; replaying the first Recent Match when Arena is idle."
            )
        else:
            detail = ""
        self.ui.status("AUTO_QUEUE_DETAIL", detail)
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
        if getattr(self, "_auto_quest", None):
            return None
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
        if not ended and not record.get("recent_mode"):
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
        if not record.get("idle_recent"):
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

        native = getattr(getattr(navigator, "_bridge", None), "client_runtime", "") == "il2cpp-macos"
        completed = (
            idle_completed_match_from_log(max_age_s=float("inf"))
            if native
            else idle_completed_match_from_log()
        )
        if completed and not self._queue_loss_guard(
            state, {**completed, "arena_completed_at": completed["completed_at"]}
        ):
            self._auto_queue_restart_pending = self._auto_queue_enabled
            self._suspend_auto_queue()
            return False
        if not getattr(self, "_auto_quest", None):
            winning_target = self._resolve_winning_queue_target()
            if winning_target and hasattr(navigator, "start_idle_grind"):
                if navigator.start_idle_grind(winning_target, state):
                    logger.info(
                        "Auto queue (Winning decks): queueing %s with %r from idle Home",
                        winning_target.get("event_id"),
                        winning_target.get("deck_name"),
                    )
                    return True
        if native:
            # Recover completion evidence for the persistent duplicate guard.
            # Age is immaterial here: the guard compares it with the prior
            # submission, and the scan rejects any later queue/gameplay. The
            # live bridge still proves idle Home and selects the current deck.
            completed_at = 0.0
            if completed and (not current_id or current_id == completed["match_id"]):
                completed_at = completed["completed_at"]
            return navigator.start_recent_match(state, completed_at=completed_at)
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
        """Navigate after completion, or a separately prepared explicit idle Grind."""
        if not self._check_grind_availability():
            self._suspend_auto_queue()
            return False
        # Readiness pauses recover on a fresh connection even between matches,
        # where no gameplay trigger would otherwise restart the engine.
        refresh = getattr(getattr(self, "_autopilot", None), "refresh_automation_readiness", None)
        if callable(refresh):
            refresh(state)
        enabled = getattr(self, "_auto_queue_enabled", False)
        active = (
            enabled
            and self._autopilot_control_status() == "AP:ON"
            and not getattr(self, "_autopilot_dry_run", False)
            and not getattr(self, "_autopilot_afk", False)
        )
        if not active:
            self._suspend_auto_queue()
            if not enabled and hasattr(self, "settings"):
                streak = self._queue_streak()
                if streak.get("losses", 0) > 0:
                    from arenamcp.server import get_completed_match_for_navigation

                    completed = get_completed_match_for_navigation() or {}
                    outcome = completed.get("outcome")
                    comp_mid = completed.get("match_id")
                    comp_at = float(completed.get("completed_at") or 0.0)
                    if (
                        outcome in ("win", "draw")
                        and comp_mid
                        and comp_mid != streak.get("last")
                        and comp_at > float(streak.get("through") or 0.0)
                    ):
                        streak.update(losses=0, last=comp_mid, through=comp_at)
                        streak.pop("pending", None)
                        self.settings.set("auto_queue_streak", streak)
                        self.ui.status("AUTO_QUEUE_DETAIL", "")
            return False
        from arenamcp.bridge_readiness import automation_blocker
        from arenamcp.gre_bridge import get_bridge

        bridge = getattr(getattr(self, "_autopilot", None), "_gre_bridge", None)
        if bridge is None:
            bridge = get_bridge()
        blocked = automation_blocker(bridge)
        if blocked:
            self._suspend_auto_queue()
            self.ui.status("AUTO_QUEUE_DETAIL", blocked)
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
            if getattr(self, "_auto_quest", None):
                self.ui.status(
                    "AUTO_QUEST",
                    "Grind paused: an active draft run owns navigation. Start a free constructed match after leaving the draft run.",
                )
            # Draft autoplay owns navigation between its event's matches.
            self._suspend_auto_queue()
            return False
        from arenamcp.server import get_completed_match_for_navigation

        completed = get_completed_match_for_navigation()
        if not self._queue_loss_guard(state, completed):
            self._suspend_auto_queue()
            return False
        quest_before_prepare = getattr(self, "_auto_quest", None)
        if not self._prepare_auto_quest(state):
            self._suspend_auto_queue()
            return False
        # Preparation reads the bridge and can take seconds. A user disabling
        # either control while it runs must cancel the prospective navigation.
        if (
            not getattr(self, "_auto_queue_enabled", False)
            or self._autopilot_control_status() != "AP:ON"
            or getattr(self, "_autopilot_dry_run", False)
            or getattr(self, "_autopilot_afk", False)
            or quest_before_prepare is not getattr(self, "_auto_quest", None)
        ):
            self._suspend_auto_queue()
            return False
        try:
            from arenamcp.auto_queue import AutoQueueNavigator
            from arenamcp.idle_sleep import SleepInhibitor
            from arenamcp.server import get_last_queue_selection

            inhibitor = getattr(self, "_auto_queue_sleep_inhibitor", None)
            if inhibitor is None:
                inhibitor = self._auto_queue_sleep_inhibitor = SleepInhibitor()
            navigator = getattr(self, "_auto_queue_navigator", None)
            quest = getattr(self, "_auto_quest", None)
            idle_ready = bool(quest and quest.get("idle_ready") and not quest.get("idle_started"))
            if idle_ready:
                current_id = state.get("match_id")
                if (
                    quest.get("observed")
                    or state.get("pending_decision")
                    or (current_id and current_id != completed.get("match_id"))
                    or time.time() >= min(quest.get("valid_until", 0), quest["deadline"])
                ):
                    self._suspend_auto_queue()
                    return False
                # The new session must not resume an older target retained by
                # ordinary auto-queue when Grind was explicitly enabled.
                if navigator is not None:
                    navigator.set_enabled(False)
                    if navigator.step_running:
                        return False
                    navigator = self._auto_queue_navigator = None
            if navigator is None:
                backend = getattr(self, "_autopilot_backend", None) or getattr(self._coach, "_backend", None)
                if backend is None:
                    return False
                from arenamcp.gre_bridge import get_bridge

                navigator = self._auto_queue_navigator = AutoQueueNavigator(
                    backend=backend,
                    get_game_state=self._mcp.get_game_state,
                    bridge=bridge,
                    status_fn=lambda detail: self.ui.status("AUTO_QUEUE_DETAIL", detail),
                    queue_selection=get_last_queue_selection,
                    action_allowed=(lambda: self._quest_navigation_allowed(quest)) if quest else None,
                )
            # Existing navigators may predate a Grind session started mid-match.
            # Refresh the worker gate even when no new navigator was constructed.
            navigator._action_allowed = (lambda: self._quest_navigation_allowed(quest)) if quest else None
            navigator.set_enabled(True)
            if idle_ready:
                if not navigator.start_idle_grind(quest.get("target") or {}, state):
                    self._suspend_auto_queue()
                    return False
                quest["idle_started"] = True
                quest["idle_ready"] = False
                self._auto_queue_restart_pending = False
                self._auto_queue_resume = None
            # A pause that retries by itself still needs the Mac awake.
            if not navigator.paused_reason or navigator.retry_scheduled:
                inhibitor.start()
            if getattr(self, "_auto_queue_restart_pending", False):
                self._auto_queue_restart_pending = False
                resume = getattr(self, "_auto_queue_resume", None)
                self._auto_queue_resume = None
                if not (resume and self._resume_auto_queue_after_reload(navigator, resume, state)):
                    self._arm_auto_queue_after_restart(navigator, state)
                    if not self._auto_queue_enabled or self._auto_queue_restart_pending:
                        return False
            match_id = completed.get("match_id")
            current_id = state.get("match_id")
            turn = state.get("turn") or state.get("turn_info") or {}
            queue_joined = getattr(navigator, "queue_joined", False) is True
            if (
                current_id
                and current_id != match_id
                and (
                    turn.get("turn_number", 0) > 0
                    or state.get("pending_decision")
                    or (queue_joined and not state.get("last_game_result"))
                )
            ):
                # Log replay during startup also parses old completion events.
                # Only queue after observing this game active in this session
                # (or once our own queue submission entered a live Turn-0 room).
                self._auto_queue_observed_match = current_id
            observed_id = getattr(self, "_auto_queue_observed_match", None)
            since = getattr(self, "_auto_queue_since", float("inf"))
            arena_completed_at = completed.get("arena_completed_at")
            if (
                match_id
                and completed.get("completed_at", 0) >= since
                and match_id != getattr(self, "_auto_queue_last_noted", None)
                and (
                    match_id == observed_id
                    or (queue_joined and match_id != getattr(navigator, "_ended_match_id", None))
                    or (
                        observed_id is not None
                        and isinstance(arena_completed_at, (int, float))
                        and not isinstance(arena_completed_at, bool)
                        and float(arena_completed_at) > since
                    )
                )
            ):
                quest = getattr(self, "_auto_quest", None)
                if quest and quest.get("target"):
                    completed = {**completed, **quest["target"]}
                elif not quest:
                    winning_target = self._resolve_winning_queue_target()
                    if winning_target:
                        completed = {**completed, **winning_target}
                navigator.note_match_end(completed, confirmed_match_end=True)
                self._auto_queue_last_noted = match_id
                self._auto_queue_observed_match = match_id
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
