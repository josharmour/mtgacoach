"""Session and subprocess coordinator for MTGA Coach desktop."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from PySide6.QtCore import QObject, Signal

from .coach_process import CoachProcess
from .tts_manager import TtsManager

logger = logging.getLogger(__name__)


class CoachSession(QObject):
    """Coordinates the background standalone coach process and TTS engine."""

    # Core state & advice signals
    gameStateChanged = Signal(dict)
    turnPlanChanged = Signal(object)
    gamePlanChanged = Signal(object)
    statusChanged = Signal(str, str)
    spokenLine = Signal(str)
    logEmitted = Signal(str, str)  # message, role
    adviceReceived = Signal(str, str)  # text, label
    bugReportSaved = Signal(str, str)  # path, error
    errorOccurred = Signal(str)

    # Tactical-search result for the sidebar's tactical line
    mctsUpdated = Signal(object)

    # Conversation Mode signals
    modeChanged = Signal(str)
    conversationReply = Signal(str)
    conversationStatus = Signal(str)
    localFallbackNotice = Signal(str)

    # Lifecycle signals
    started = Signal()
    stopped = Signal()
    processExited = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process = CoachProcess(self)
        self._process.event_received.connect(self._handle_process_event)
        self._process.stderr_line.connect(self._handle_stderr)
        self._process.exited.connect(self._handle_exited)

        self._tts = TtsManager(self)
        # Lazy-start: TtsManager.request_speech() starts the Kokoro worker on
        # demand (and start() re-arms it). Eagerly spawning a worker per
        # CoachSession means every Constructed test panel owns a live Kokoro
        # subprocess that dies inside the QObject destructor cascade at GC —
        # the source of the intermittent SIGSEGV/QProcess-destroyed noise in
        # offscreen pytest runs.

        self._last_game_state: dict[str, Any] = {}
        self._statuses: dict[str, str] = {}
        self._autopilot_active = False
        self._muted = False

    @property
    def is_running(self) -> bool:
        return self._process.is_running

    @property
    def last_game_state(self) -> dict[str, Any]:
        return self._last_game_state

    def start(self, autopilot: bool = False, dry_run: bool = False, afk: bool = False) -> None:
        """Start the background coaching subprocess."""
        if self.is_running:
            return
        with contextlib.suppress(Exception):
            if not self._tts.is_running:
                self._tts.start()
        try:
            self._process.start(autopilot=autopilot, dry_run=dry_run, afk=afk)
            self.started.emit()
            self.logEmitted.emit("✅ Coach process started.", "status")
        except Exception as e:
            logger.error(f"Failed to start coach process: {e}")
            self.errorOccurred.emit(str(e))
            self.logEmitted.emit(f"❌ Failed to start coach process: {e}", "error")

    def stop(self) -> None:
        """Stop the background coaching subprocess."""
        if not self.is_running:
            return
        self._process.stop()
        self.stopped.emit()

    def restart(self, autopilot: bool = False, dry_run: bool = False, afk: bool = False) -> None:
        """Restart the background coaching process."""
        self.stop()
        self.logEmitted.emit("Restarting coach process…", "status")
        self.start(autopilot=autopilot, dry_run=dry_run, afk=afk)

    def send_command(self, command: str, *args: Any) -> None:
        """Send a JSON command to the coach subprocess."""
        self._process.send_command(command, *args)

    def toggle_autopilot(self) -> None:
        self.send_command("toggle_autopilot")

    def toggle_mute(self) -> None:
        self.send_command("toggle_mute")

    def toggle_style(self) -> None:
        self.send_command("toggle_style")

    def cycle_speed(self) -> None:
        self.send_command("cycle_speed")

    def cycle_voice(self) -> None:
        self.send_command("cycle_voice")

    def send_chat(self, text: str) -> None:
        self.send_command("chat", text)

    def set_mode(self, mode: str) -> None:
        """Switch coaching mode ("turn_advice" | "conversation")."""
        self._tts.stop_speech()
        self.send_command("set_mode", mode)

    def set_verbosity(self, verbosity: str) -> None:
        """Set conversation commentary verbosity (quiet/balanced/detailed)."""
        self.send_command("set_verbosity", verbosity)

    def stop_speaking(self) -> None:
        """Stop current TTS playback (conversation + turn advice).

        Also notifies the engine (``stop_speech`` command) so the engine-side
        arbiter channel is released and pending conversation work is cancelled
        — the UI stop button must not leave a question answer still rendering
        on the engine side.
        """
        self._tts.stop_speech()
        self.send_command("stop_speech")

    def emit_local_fallback_notice(self, message: str) -> None:
        """Surface a [LOCAL FALLBACK] notice (PTT/mic failures) on the
        conversation transcript surface."""
        self.localFallbackNotice.emit(str(message))

    def capture_screenshots(self) -> dict[str, str]:
        """Capture coach window + MTGA window screenshots into bug_reports directory."""
        from datetime import datetime
        from pathlib import Path
        import sys
        from arenamcp.logging_config import LOG_DIR

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            bug_dir = Path(LOG_DIR) / "bug_reports"
        except Exception:
            bug_dir = Path.home() / ".arenamcp" / "logs" / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        out: dict[str, str] = {}

        # 1. Coach PySide window screenshot
        try:
            from PySide6.QtWidgets import QApplication, QWidget

            active_win = QApplication.activeWindow()
            if active_win is None:
                parent = self.parent()
                if isinstance(parent, QWidget):
                    active_win = parent.window()
            if active_win is None:
                for w in QApplication.topLevelWidgets():
                    if w.isVisible():
                        active_win = w
                        break
            if active_win is not None:
                pix = active_win.grab()
                if not pix.isNull():
                    coach_path = bug_dir / f"bug_{ts}_coach.png"
                    if pix.save(str(coach_path), "PNG"):
                        out["coach"] = str(coach_path)
        except Exception as e:
            logger.debug("Coach screenshot failed: %s", e)

        # 2. MTGA window screenshot
        try:
            from PIL import ImageGrab
            from arenamcp.desktop.window_tracking import get_mtga_window_rect

            rect = get_mtga_window_rect()
            if rect is not None:
                left, top, width, height = rect
                if width > 0 and height > 0:
                    grab_kwargs = {}
                    if sys.platform == "win32":
                        grab_kwargs["all_screens"] = True
                    bbox = (left, top, left + width, top + height)
                    img = ImageGrab.grab(bbox=bbox, **grab_kwargs)
                    mtga_path = bug_dir / f"bug_{ts}_mtga.png"
                    img.save(str(mtga_path), "PNG")
                    out["mtga"] = str(mtga_path)
        except Exception as e:
            logger.debug("MTGA screenshot failed: %s", e)

        return out

    def trigger_debug_report(self) -> None:
        """Capture screenshots and request bug report creation."""
        screenshots = self.capture_screenshots()
        if self.is_running:
            self._process.send_payload(
                {
                    "cmd": "debug_report",
                    "screenshots": screenshots,
                }
            )
        else:
            self._save_offline_debug_report(screenshots)

    def _save_offline_debug_report(self, screenshots: dict[str, str]) -> None:
        """Create a local debug report directly when coach subprocess is not running."""
        import json
        import platform
        from datetime import datetime
        from pathlib import Path
        from arenamcp import __version__
        from arenamcp.logging_config import LOG_DIR

        try:
            bug_dir = Path(LOG_DIR) / "bug_reports"
        except Exception:
            bug_dir = Path.home() / ".arenamcp" / "logs" / "bug_reports"
        bug_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bug_file = bug_dir / f"bug_{ts}.json"

        try:
            report = {
                "timestamp": datetime.now().isoformat(),
                "version": __version__,
                "reason": "Desktop Bug Report (offline)",
                "system": {
                    "platform": platform.platform(),
                    "python_version": platform.python_version(),
                    "machine": platform.machine(),
                },
                "game_state": self._last_game_state,
                "statuses": self._statuses,
                "screenshots": screenshots,
                "process_running": False,
            }
            with open(bug_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, default=str)
            self.bugReportSaved.emit(str(bug_file), "")
        except Exception as e:
            logger.exception("Offline bug report failed")
            self.bugReportSaved.emit("", str(e))

    def _handle_process_event(self, event: Any) -> None:
        """Decode pipe protocol JSON event from standalone coach."""
        if not isinstance(event, dict):
            return

        ev_type = str(event.get("type") or event.get("event") or "")

        if ev_type in ("game_state", "emit_game_state"):
            state = event.get("data") if "data" in event else event.get("game_state", {})
            if isinstance(state, dict):
                self._last_game_state = state
                self.gameStateChanged.emit(state)

        elif ev_type in ("turn_plan", "emit_turn_plan"):
            plan = event.get("data") if "data" in event else event.get("turn_plan")
            self.turnPlanChanged.emit(plan)

        elif ev_type in ("game_plan", "emit_game_plan"):
            plan = event.get("data") if "data" in event else event.get("game_plan")
            self.gamePlanChanged.emit(plan)

        elif ev_type in ("status", "emit_status"):
            key = str(event.get("key") or "")
            val = str(event.get("value") or "")
            if key:
                self._statuses[key] = val
                if key == "AUTOPILOT":
                    self._autopilot_active = "ON" in val or "PAUSED" in val
                elif key == "MUTE":
                    self._muted = "ON" in val
                self.statusChanged.emit(key, val)
                if key == "MODE":
                    self.modeChanged.emit(val)
                elif key == "CONVO_STATE":
                    self.conversationStatus.emit(val)

        elif ev_type == "speak_stop":
            # Engine-initiated preemption (typed question, urgent topic): the
            # engine stopped its arbiter channel, so desktop audio must halt
            # too — otherwise the preempted utterance keeps playing for the
            # full LLM answer latency. stop_speech() is idempotent.
            self._tts.stop_speech()

        elif ev_type in ("speak_request", "speak", "speak_audio"):
            text = str(event.get("text") or event.get("data") or "")
            if text:
                self.spokenLine.emit(text)
                if not self._muted:
                    speed = float(event.get("speed") or 1.0)
                    voice_id = str(event.get("voice_id") or "af_heart")
                    voice_name = str(event.get("voice_name") or "Auto")
                    # Conversation-mode speech carries optional priority and
                    # identity (stale-response suppression); absent fields are
                    # legacy behavior.
                    extra_kwargs: dict[str, Any] = {}
                    if "priority" in event:
                        extra_kwargs["priority"] = event.get("priority")
                    if "identity" in event:
                        extra_kwargs["identity"] = event.get("identity")
                    self._tts.request_speech(
                        text=text,
                        voice_id=voice_id,
                        voice_name=voice_name,
                        speed=speed,
                        **extra_kwargs,
                    )

        elif ev_type == "conversation_reply":
            reply = str(event.get("text") or "")
            if reply:
                self.conversationReply.emit(reply)

        elif ev_type in ("advice", "emit_advice"):
            text = str(event.get("text") or event.get("advice") or "")
            label = str(event.get("seat_info") or event.get("label") or "COACH")
            if text:
                self.adviceReceived.emit(text, label)

        elif ev_type in ("log", "emit_log"):
            msg = str(event.get("message") or event.get("log") or "")
            role = str(event.get("role") or "info")
            if msg:
                self.logEmitted.emit(msg, role)

        elif ev_type in ("mcts_tree", "emit_mcts"):
            payload = event.get("data") if "data" in event else event.get("mcts")
            self.mctsUpdated.emit(payload)

        elif ev_type in ("bug_report_saved", "emit_bug_report_saved"):
            path = str(event.get("path") or "")
            err = str(event.get("error") or "")
            self.bugReportSaved.emit(path, err)

        elif ev_type in ("error", "emit_error"):
            err = str(event.get("message") or event.get("error") or "Unknown error")
            self.errorOccurred.emit(err)

    def _handle_stderr(self, line: str) -> None:
        logger.debug(f"[coach stderr] {line}")
        if "Traceback" in line or "Error:" in line or "Exception:" in line:
            self.errorOccurred.emit(line)

    def _handle_exited(self, code: int) -> None:
        logger.info(f"Coach process exited with code {code}")
        self.processExited.emit(code)
        self.stopped.emit()

    def shutdown(self) -> None:
        """Cleanly terminate subprocess and TTS manager."""
        if self._tts is not None:
            self._tts.shutdown()
        self.stop()
