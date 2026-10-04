"""Visible startup stages and a short-lived, same-match engine reload handoff."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)
ENGINE_RESUME_PATH = Path.home() / ".arenamcp" / "engine_resume.json"


class _StartupMixin:
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
            message = ""  # Native screen/input mode does not require the bridge.
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
        warning = getattr(self, "_startup_connection_warning", "")
        if error:
            self._startup_status(
                "error",
                f"Coach loaded. {error}. Advice requests will retry automatically.",
            )
        elif warning:
            self._startup_status("connection_warning", warning, ready=True)
        else:
            self._startup_status(
                "ready", "Coach ready — waiting for an actionable game decision.", ready=True
            )

    def prepare_engine_reload(self) -> None:
        """Abort old inputs, checkpoint observations and strategy, then exit."""
        if getattr(self, "_engine_reload_preparing", False):
            return
        self._engine_reload_preparing = True
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
            payload = {
                "saved_at": time.time(),
                "match_id": state.get("match_id"),
                "turn": (state.get("turn") or {}).get("turn_number", 0),
                "deck_strategy": getattr(self._coach, "_deck_strategy", None),
                "deck_playbook": self._coach._deck_playbook.export()
                if getattr(self._coach, "_deck_playbook", None)
                else None,
                "game_plan": manager.export_for_reload() if manager else None,
                "autopilot_paused": was_paused,
            }
            ENGINE_RESUME_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = ENGINE_RESUME_PATH.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            temporary.replace(ENGINE_RESUME_PATH)
        except Exception as exc:
            logger.warning("Engine reload checkpoint failed; live state will be reacquired: %s", exc)
        emit = getattr(self.ui, "_emit", None)
        if callable(emit):
            emit({"type": "reload_ready"})
        self._restart_requested = True
        # run_forever exits; pipe-mode entrypoint tears down this interpreter.
        self._running = False

    def _load_engine_resume(self) -> None:
        self._engine_resume = None
        if os.environ.get("ARENAMCP_ENGINE_RELOAD") != "1":
            return
        try:
            data = json.loads(ENGINE_RESUME_PATH.read_text(encoding="utf-8"))
            age = time.time() - data.get("saved_at", 0)
            if 0 <= age <= 120 and data.get("match_id"):
                self._engine_resume = data
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
        from arenamcp.deck_strategy import DeckPlaybook

        try:
            playbook = DeckPlaybook.restore(data.get("deck_playbook") or {}, state)
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
        if manager and data.get("game_plan"):
            manager.restore_after_reload(data["game_plan"], state)
        logger.info("Restored deck playbook and current plan for engine reload in match %s", match_id)
        self.ui.log("Resumed current match strategy; no new deck-analysis warmup needed.")
