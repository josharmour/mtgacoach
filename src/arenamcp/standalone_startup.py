"""Visible startup stages and a short-lived, same-match engine reload handoff."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)
ENGINE_RESUME_PATH = Path.home() / ".arenamcp" / "engine_resume.json"

# Serialises the reload decision (arm / cancel / begin-completion) so a
# cancellation arriving from the pipe-command thread can never interleave
# with the completion path's own decision point.
_ENGINE_RELOAD_DECISION_INIT = threading.Lock()


class _StartupMixin:
    def _engine_reload_decision_lock(self) -> threading.RLock:
        """Lazily create the per-instance reload decision lock."""
        lock = getattr(self, "_engine_reload_decision_lock_instance", None)
        if lock is None:
            with _ENGINE_RELOAD_DECISION_INIT:
                lock = getattr(self, "_engine_reload_decision_lock_instance", None)
                if lock is None:
                    lock = threading.RLock()
                    self._engine_reload_decision_lock_instance = lock
        return lock

    def _publish_arena_connection_status(self) -> None:
        """Explain bridge acquisition without ever restarting a running game."""
        if sys.platform != "darwin":
            return
        now = time.monotonic()
        if now - getattr(self, "_last_arena_check", 0.0) < 5.0:
            return
        self._last_arena_check = now
        from arenamcp.android_link import game_device
        from arenamcp.desktop.runtime import is_mtga_running
        from arenamcp.platform_integration import mac_bridge_installed

        if game_device() != "desktop":
            return
        poller = getattr(self, "_bridge_poller", None)
        bridge = getattr(poller, "_bridge", None)
        if getattr(bridge, "connected", False) or getattr(poller, "connected", False):
            message = ""
            self._arena_bridge_wait_started = now
        elif not is_mtga_running():
            message = "Waiting for Arena to open…"
            self._arena_bridge_wait_started = now
        elif not mac_bridge_installed():
            # No bridge library to wait for: coaching runs from the log, and
            # autoplay reports the missing bridge itself (MANUAL REQUIRED).
            message = ""
        else:
            started = getattr(self, "_arena_bridge_wait_started", now)
            self._arena_bridge_wait_started = started
            message = "Connecting to Arena’s bridge…"
            if now - started >= 10:
                message = (
                    "Arena is running, but its bridge has not connected. If Arena was opened separately, "
                    "finish your match, then close Arena and reopen it through the coach to load the bridge. "
                    "Reload engine reconnects an existing bridge; it cannot add one to a running game."
                )
        if message != getattr(self, "_last_arena_connection_message", None):
            self._last_arena_connection_message = message
            self.ui.status("ARENA", message)

    def _startup_status(self, phase: str, message: str, *, ready: bool = False) -> None:
        started = getattr(self, "_startup_started", None)
        if started is None:
            self._startup_started = started = time.monotonic()
        payload = {
            "phase": phase,
            "message": message,
            "ready": ready,
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        emit = getattr(self.ui, "emit_startup_status", None)
        if callable(emit):
            emit(payload)
        else:
            self.ui.status("STARTUP", message)

    def _startup_complete(self) -> None:
        self._startup_finished = True
        error = getattr(self, "_startup_connection_error", "")
        if error:
            lower = error.lower()
            if (
                "key is expired" in lower
                or "trial_expired" in lower
                or "trial expired" in lower
                or "error code: 401" in lower
                or "error code: 403" in lower
                or "http 401" in lower
                or "http 403" in lower
            ):
                self._startup_status(
                    "error",
                    "Trial or license key expired — subscribe at mtgacoach.com/subscribe and enter your key in Setup & Repair.",
                )
            else:
                self._startup_status(
                    "error",
                    f"Coach loaded. {error}. Advice requests will retry automatically.",
                )
        else:
            self._startup_status(
                "ready", "Coach ready — waiting for an actionable game decision.", ready=True
            )

    def prepare_engine_reload(self) -> None:
        """Arm an engine reload; it completes at the next safe boundary.

        Arming never aborts an active decision: the main loop checkpoints and
        exits only between matches with nothing in flight (see
        ``_engine_reload_checkpoint_due``). ``cancel_engine_reload`` disarms
        the request and leaves the current engine running.
        """
        if getattr(self, "_engine_reload_armed", False):
            return
        with self._engine_reload_decision_lock():
            self._engine_reload_armed = True
            self._engine_reload_cancelled = False
            # Holds auto-queue/auto-reload navigation while a swap is pending.
            self._engine_reload_preparing = True
        self._startup_status(
            "resuming_match",
            "Engine reload armed — it will apply at the next safe break between matches…",
        )

    def cancel_engine_reload(self) -> None:
        """Disarm a pending engine reload; the current engine keeps running.

        Acknowledged cancel/commit ordering: once
        ``_complete_engine_reload`` has taken the decision at a safe boundary,
        a cancellation is acknowledged as too late instead of flipping the
        outcome — the desktop retains the pending restart until the commit's
        definitive ``reload_ready`` disposition, so the handoff can never be
        stranded between "cancelled" and "exiting".
        """
        with self._engine_reload_decision_lock():
            if getattr(self, "_engine_reload_completing", False):
                self._startup_status(
                    "resuming_match",
                    "Engine reload already committing at the safe boundary — waiting for the new engine…",
                )
                return
            if not getattr(self, "_engine_reload_armed", False):
                return
            self._engine_reload_armed = False
            self._engine_reload_cancelled = True
            self._engine_reload_preparing = False
            self._last_engine_reload_wait = None
        self._startup_status(
            "ready", "Engine reload cancelled — the current engine keeps running.", ready=True
        )
        # Distinct, unfiltered disposition event: the desktop releases its
        # retained pending restart only on this definitive acknowledgement.
        emit = getattr(self.ui, "_emit", None)
        if callable(emit):
            emit({"type": "engine_reload_cancelled"})

    def _engine_reload_checkpoint_due(self, state: dict) -> bool:
        """True when an armed reload may checkpoint and exit right now."""
        if not getattr(self, "_engine_reload_armed", False):
            return False
        if getattr(self, "_engine_reload_cancelled", False):
            self._engine_reload_armed = False
            self._engine_reload_preparing = False
            return False
        # A manual reload is not rate limited; every other piece of in-flight
        # work and the between-matches requirement gate it exactly like an
        # automatic one (standalone_auto_reload.py).
        blocker_fn = getattr(self, "_auto_reload_blocker", None)
        between_fn = getattr(self, "_auto_reload_between_matches", None)
        blocker = blocker_fn(respect_rate_limit=False) if callable(blocker_fn) else ""
        if not blocker and callable(between_fn):
            blocker = between_fn(state)
        if blocker:
            message = f"Engine reload waiting for a safe boundary: {blocker}."
            if message != getattr(self, "_last_engine_reload_wait", None):
                self._last_engine_reload_wait = message
                logger.info("%s", message)
                self._startup_status("resuming_match", message)
            return False
        self._last_engine_reload_wait = None
        return True

    def _complete_engine_reload(self) -> None:
        """Checkpoint observations and strategy at a safe boundary, then exit."""
        with self._engine_reload_decision_lock():
            # Late-response safety: the desktop may have cancelled while we
            # were waiting; honour it even at the last moment and keep this
            # engine up.
            if getattr(self, "_engine_reload_cancelled", False):
                self.cancel_engine_reload()
                return
            # Take the decision: from here until the commit below, a
            # cancellation is acknowledged as too late (see
            # ``cancel_engine_reload``) rather than racing the commit — the
            # desktop retains the pending restart until this definitive
            # ``reload_ready`` disposition.
            self._engine_reload_completing = True
        try:
            self._finish_engine_reload()
        finally:
            with self._engine_reload_decision_lock():
                self._engine_reload_completing = False
                self._engine_reload_armed = False
                self._engine_reload_preparing = False

    def _finish_engine_reload(self) -> None:
        """Write the checkpoint, announce readiness and request the restart."""
        # Stop a running countdown without declining the game; the record of
        # what was recommended, declined or conceded rides along below.
        abort_concede = getattr(self, "abort_concede", None)
        if callable(abort_concede):
            abort_concede("the engine is reloading")
        concede_export = getattr(self, "_concede_export", None)
        concede_record = concede_export() if callable(concede_export) else None
        oops_export = getattr(self, "_oops_export", None)
        oops_record = oops_export() if callable(oops_export) else None
        # Armed post-match navigation, read before suspending it below.
        queue_export = getattr(self, "_auto_queue_export", None)
        queue_record = queue_export() if callable(queue_export) else None
        reload_export = getattr(self, "_auto_reload_export", None)
        reload_record = reload_export() if callable(reload_export) else None
        suspend_queue = getattr(self, "_suspend_auto_queue", None)
        if callable(suspend_queue):
            suspend_queue()
        finish_capture = getattr(self, "_finish_autopilot_recovery", None)
        if callable(finish_capture):
            finish_capture("engine_reloaded")
        with contextlib.suppress(OSError):
            ENGINE_RESUME_PATH.unlink()
        was_paused = self._autopilot_control_status() == "AP:PAUSED"
        self._autopilot_enabled = False
        if self._autopilot is not None:
            with contextlib.suppress(Exception):
                self._autopilot.on_abort()
        self._startup_status("resuming_match", "Saving current match and strategy before reloading engine…")
        payload = {
            "saved_at": time.time(),
            "autopilot_paused": was_paused,
            "concede": concede_record,
            "oops": oops_record,
            "auto_queue": queue_record,
            "auto_reload": reload_record,
        }
        try:
            from arenamcp import server
            from arenamcp.gamestate_persistence import save_match_state

            state = self._normalize_turn_snapshot(server.game_state.get_published_snapshot())
            watcher = server.watcher
            save_match_state(
                server.game_state,
                log_offset=watcher.file_position if watcher else 0,
                log_path=str(watcher.log_path) if watcher else None,
            )
            manager = getattr(self._coach, "_game_plan_mgr", None)
            payload.update(
                {
                    "match_id": state.get("match_id"),
                    "turn": (state.get("turn") or {}).get("turn_number", 0),
                    "deck_strategy": getattr(self._coach, "_deck_strategy", None),
                    "deck_playbook": self._coach._deck_playbook.export()
                    if getattr(self._coach, "_deck_playbook", None)
                    else None,
                    "game_plan": manager.export_for_reload() if manager else None,
                }
            )
        except Exception as exc:
            logger.warning("Engine reload checkpoint failed; live state will be reacquired: %s", exc)
        # Written even without the match part: between matches the navigation
        # record is what lets the new engine requeue.
        try:
            ENGINE_RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = ENGINE_RESUME_PATH.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(ENGINE_RESUME_PATH)
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Engine reload checkpoint could not be written: %s", exc)
        emit = getattr(self.ui, "_emit", None)
        if callable(emit):
            emit({"type": "reload_ready"})
        self._restart_requested = True
        # run_forever exits; pipe-mode entrypoint tears down this interpreter.
        self._running = False

    def _load_engine_resume(self) -> None:
        self._engine_resume = None
        self._auto_queue_resume = None
        if os.environ.get("ARENAMCP_ENGINE_RELOAD") != "1":
            return
        try:
            data = json.loads(ENGINE_RESUME_PATH.read_text(encoding="utf-8"))
            age = time.time() - data.get("saved_at", 0)
            if 0 <= age <= 120:
                # Between matches there is no match part; navigation and the
                # automatic-reload rate limit still carry over.
                if isinstance(data.get("auto_queue"), dict):
                    self._auto_queue_resume = data["auto_queue"]
                resume_reload = getattr(self, "_auto_reload_resume_from", None)
                if callable(resume_reload):
                    resume_reload(data.get("auto_reload"))
            if 0 <= age <= 120 and data.get("match_id"):
                self._engine_resume = data
                resume_concede = getattr(self, "_concede_resume_from", None)
                if callable(resume_concede):
                    resume_concede(data.get("concede"))
                resume_oops = getattr(self, "_oops_resume_from", None)
                if callable(resume_oops):
                    resume_oops(data.get("oops"))
                if data.get("autopilot_paused"):
                    # Do not resume inputs while restoring a previously paused engine.
                    self._autopilot_enabled = False
                    self.ui.log("Autopilot remains inactive because it was paused before reload.")
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        finally:
            with contextlib.suppress(OSError):
                ENGINE_RESUME_PATH.unlink()

    def _try_restore_engine_resume(self, state: dict) -> None:
        data = getattr(self, "_engine_resume", None)
        if not data or not self._coach:
            return
        match_id = state.get("match_id")
        if not match_id:
            return
        self._engine_resume = None
        turn = (state.get("turn") or {}).get("turn_number", 0)
        if (
            match_id != data["match_id"]
            or state.get("game_over")
            or turn < data.get("turn", 0)
            or time.time() - data["saved_at"] > 120
        ):
            logger.info("Engine reload strategy discarded: match changed or handoff expired")
            return
        from arenamcp.coach import CoachEngine
        from arenamcp.deck_strategy import DeckPlaybook, deck_identity
        from arenamcp.laelia_strategy import validate_generated_plan

        saved = CoachEngine.read_saved_strategy(deck_identity(state), state)
        checkpoint = data.get("deck_playbook") or {}
        strategy_changed = saved is not None and saved.export()["data"] != checkpoint.get("data")
        try:
            playbook = saved or DeckPlaybook.restore(checkpoint, state)
            if saved is None:
                validate_generated_plan(playbook)
        except (ValueError, KeyError, TypeError):
            # Old unstructured summaries omitted commander policies and can
            # contain unsupported combos. Rebuild rather than grandfathering
            # them into the new strategy pipeline after an engine reload.
            self._deck_analyzed = False
            logger.info("Engine reload needs current deck playbook analysis")
            self.ui.log("Rebuilding deck strategy from card rules after reload.")
            return
        self._coach._deck_playbook = playbook
        self._coach._deck_strategy = playbook.render()
        self._coach._deck_analysis_identity = playbook.identity
        self._deck_analyzed = True
        manager = self._coach._ensure_game_plan_mgr()
        if saved is not None:
            logger.info(
                "Reloaded saved strategy %s (SHA-256 %s)", saved.identity, saved.saved_strategy_sha256
            )
        if manager and strategy_changed:
            manager.seed(playbook.render())
        elif manager and data.get("game_plan"):
            manager.restore_after_reload(data["game_plan"], state)
        logger.info("Restored deck playbook for engine reload in match %s", match_id)
        self.ui.log("Resumed current match strategy; no new deck-analysis warmup needed.")
