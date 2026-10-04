"""Fast autopilot incident capture followed by bounded recovery observations."""

from __future__ import annotations

import contextlib
import copy
import logging
import threading
import time
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone

from arenamcp.autopilot_bug_capture import AutopilotBugCapture
from arenamcp.logging_config import LOG_DIR

logger = logging.getLogger(__name__)


class _AutopilotCaptureMixin:
    def _capture_observed_state(self, state: dict | None = None) -> dict:
        if state is None:
            from arenamcp import server

            state = server.game_state.get_published_snapshot()
        state = copy.deepcopy(state or {})
        poller = getattr(self, "_bridge_poller", None)
        poll = getattr(poller, "_last_poll_result", None)
        if isinstance(poll, dict):
            state["capture_bridge_pending"] = copy.deepcopy(poll)
        return state

    def _emit_autopilot_capture_status(self) -> None:
        recorder = getattr(self, "_autopilot_bug_capture", None)
        emit = getattr(self.ui, "_emit", None)
        if recorder and callable(emit):
            emit({"type": "autopilot_bug_status", **recorder.status})

    def capture_autopilot_bug(
        self, capture_id: str, clicked_at: str = "", *, automatic: bool = False, context: dict | None = None
    ) -> None:
        lock = getattr(self, "_autopilot_capture_lock", None)
        if lock is None:
            lock = self._autopilot_capture_lock = threading.RLock()
        with lock:
            self._capture_autopilot_bug_locked(capture_id, clicked_at, automatic, context)

    def _capture_autopilot_bug_locked(
        self, capture_id: str, clicked_at: str, automatic: bool, context: dict | None
    ) -> None:
        """Copy in-memory diagnostics before abort clears the pending plan."""
        recorder = getattr(self, "_autopilot_bug_capture", None)
        if recorder and recorder.status.get("capture_id") == capture_id:
            self._emit_autopilot_capture_status()
            return
        initial = {"clicked_at": clicked_at, "captured_at": time.time(), "automatic": automatic}
        if context:
            initial["stuck_detection"] = copy.deepcopy(context)
        try:
            initial["autopilot"] = copy.deepcopy(self._collect_autopilot_info())
            engine = getattr(self, "_autopilot", None)
            plan = getattr(engine, "_current_plan", None)
            if is_dataclass(plan):
                initial["autopilot"]["committed_plan"] = asdict(plan)
            initial["game_state"] = self._capture_observed_state()
            initial["bridge"] = copy.deepcopy(self._collect_bridge_state())
            navigator = getattr(self, "_auto_queue_navigator", None)
            if navigator:
                initial["auto_queue"] = navigator.get_debug_info()
            initial["advice_history"] = copy.deepcopy(getattr(self, "_advice_history", [])[-30:])
            initial["recent_gre_log"] = list(getattr(self, "_recent_gre_log", [])[-100:])
            coach = getattr(self, "_coach", None)
            initial["deck_strategy"] = getattr(coach, "_deck_strategy", None)
            manager = getattr(coach, "_game_plan_mgr", None)
            if manager:
                initial["game_plan"] = manager.export_for_reload()
        except Exception as exc:
            initial["capture_error"] = str(exc)
        finally:
            # No disk access, screenshot or model call before relinquishing control.
            self._autopilot_enabled = False
            self._autopilot_bug_needs_reset = True
            engine = getattr(self, "_autopilot", None)
            if engine:
                with contextlib.suppress(Exception):
                    engine.on_abort()
                force_stop = getattr(engine, "force_stop", None)
                if callable(force_stop):
                    with contextlib.suppress(Exception):
                        force_stop()
            suspend_queue = getattr(self, "_suspend_auto_queue", None)
            if callable(suspend_queue):
                suspend_queue()
            sync_narration = getattr(self, "_sync_narration_mode", None)
            if callable(sync_narration):
                sync_narration()
            self.ui.status("AUTOPILOT", "AP:OFF")
            with contextlib.suppress(Exception):
                self.settings.set("autopilot_enabled", False)
        try:
            if recorder is None:
                recorder = self._autopilot_bug_capture = AutopilotBugCapture(LOG_DIR / "bug_reports")
            path = recorder.begin(capture_id, initial)
            self.ui.log(f"Autopilot paused. Recording your recovery for up to 2 minutes: {path}")
            self._emit_autopilot_capture_status()
            self._start_recovery_observer(recorder, capture_id)
        except Exception as exc:
            logger.exception("Autopilot capture failed after safely pausing")
            self.ui.error(f"Autopilot paused, but bug capture failed: {exc}")
            emit = getattr(self.ui, "_emit", None)
            if callable(emit):
                emit(
                    {
                        "type": "autopilot_bug_status",
                        "capture_id": capture_id,
                        "phase": "error",
                        "message": f"Autopilot paused; capture failed: {exc}",
                    }
                )

    def attach_autopilot_bug_screenshots(
        self, capture_id: str, screenshots: dict, screenshot_error: str = ""
    ) -> None:
        recorder = getattr(self, "_autopilot_bug_capture", None)
        if recorder and recorder.attach_screenshots(capture_id, screenshots, error=screenshot_error):
            self._emit_autopilot_capture_status()

    def _auto_capture_autopilot_stuck(self, reason: str, context: dict) -> None:
        """Local evidence for the automatic loop guard; never upload or call a model."""
        now = datetime.now(timezone.utc)
        capture_id = now.strftime("%Y%m%d_%H%M%S_%f_") + uuid.uuid4().hex[:8]
        self.capture_autopilot_bug(
            capture_id, now.isoformat(), automatic=True, context={"reason": reason, **context}
        )

        def screenshot() -> None:
            from arenamcp.autopilot_bug_capture import capture_mtga_screenshot

            try:
                images = capture_mtga_screenshot(LOG_DIR / "bug_reports", f"autopilot_bug_{capture_id}")
                error = "" if images else "Arena window unavailable for screenshot"
            except Exception as exc:
                images, error = {}, str(exc)
            self.attach_autopilot_bug_screenshots(capture_id, images, error)

        threading.Thread(target=screenshot, name="autopilot-stuck-screenshot", daemon=True).start()

    def _start_recovery_observer(self, recorder: AutopilotBugCapture, capture_id: str) -> None:
        """Recovery must remain observable while the coaching loop awaits an LLM."""

        def observe() -> None:
            while recorder.active and recorder.status.get("capture_id") == capture_id:
                try:
                    if recorder.observe(
                        self._capture_observed_state(),
                        autopilot_active=bool(getattr(self, "_autopilot_enabled", False)),
                    ):
                        self._emit_autopilot_capture_status()
                        return
                except Exception as exc:
                    logger.exception("Autopilot recovery observation failed")
                    recorder.finish("observation_failed")
                    self.ui.error(f"Recovery observation stopped: {exc}")
                    self._emit_autopilot_capture_status()
                    return
                time.sleep(0.5)

        threading.Thread(target=observe, name="autopilot-bug-recovery", daemon=True).start()

    def _finish_autopilot_recovery(self, reason: str) -> None:
        recorder = getattr(self, "_autopilot_bug_capture", None)
        if not recorder or not recorder.active:
            return
        try:
            recorder.observe(self._capture_observed_state())
            recorder.finish(reason)
            self._emit_autopilot_capture_status()
        except Exception:
            logger.exception("Could not finish autopilot recovery capture")
