"""Session and subprocess coordinator for MTGA Coach desktop."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .coach_process import CoachProcess
from .tts_manager import TtsManager

logger = logging.getLogger(__name__)


class CoachSession(QObject):
    """Coordinates the background standalone coach process and TTS engine."""

    # Core state & advice signals
    gameStateChanged = Signal(dict)
    turnPlanChanged = Signal(object)
    gamePlanChanged = Signal(object)
    # Auto-concede countdown: {"state": offering|armed|conceding|sent|conceded|cancelled|aborted|failed|unconfirmed, "id", ...}
    concedeCountdownChanged = Signal(dict)
    statusChanged = Signal(str, str)
    startupStatusChanged = Signal(dict)
    autopilotBugStatusChanged = Signal(dict)
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
        self._tts.speechStatus.connect(self._report_speech_status)
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
        self._last_startup_status: dict[str, Any] = {}
        self._last_autopilot_bug_status: dict[str, Any] = {}
        self._last_concede: dict[str, Any] = {}
        self._start_options = {"autopilot": False, "dry_run": False, "afk": False}
        self._pending_restart: dict[str, bool] | None = None
        self._shutting_down = False

    @property
    def is_running(self) -> bool:
        return self._process.is_running

    @property
    def last_game_state(self) -> dict[str, Any]:
        return self._last_game_state

    @property
    def last_startup_status(self) -> dict[str, Any]:
        return dict(self._last_startup_status)

    @property
    def last_autopilot_bug_status(self) -> dict[str, Any]:
        return dict(self._last_autopilot_bug_status)

    def _set_autopilot_bug_status(self, status: dict[str, Any]) -> None:
        self._last_autopilot_bug_status = dict(status)
        self.autopilotBugStatusChanged.emit(dict(status))

    def _set_startup_status(self, status: dict[str, Any]) -> None:
        self._last_startup_status = dict(status)
        self.startupStatusChanged.emit(dict(status))

    def start(
        self,
        autopilot: bool = False,
        dry_run: bool = False,
        afk: bool = False,
        *,
        engine_reload: bool = False,
    ) -> None:
        """Start the background coaching subprocess."""
        if self.is_running or self._shutting_down:
            return
        self._start_options = {"autopilot": autopilot, "dry_run": dry_run, "afk": afk}
        self._set_startup_status(
            {
                "phase": "starting",
                "message": "Reloading coaching engine…" if engine_reload else "Starting coach…",
                "ready": False,
            }
        )
        with contextlib.suppress(Exception):
            if not self._tts.is_running:
                self._tts.start()
        try:
            self._process.start(autopilot=autopilot, dry_run=dry_run, afk=afk, engine_reload=engine_reload)
            self.started.emit()
            self.logEmitted.emit("✅ Coach process started.", "status")
        except Exception as e:
            logger.error(f"Failed to start coach process: {e}")
            self._set_startup_status(
                {"phase": "error", "message": f"Coach could not start: {e}", "ready": False}
            )
            self.errorOccurred.emit(str(e))
            self.logEmitted.emit(f"❌ Failed to start coach process: {e}", "error")

    def stop(self) -> None:
        """Stop the background coaching subprocess."""
        self._pending_restart = None
        if not self.is_running:
            return
        self._process.stop()

    def restart(
        self,
        autopilot: bool | None = None,
        dry_run: bool | None = None,
        afk: bool | None = None,
    ) -> None:
        """Reload Python code after the old engine has fully exited.

        Preserve the UI and current runtime controls. The new child skips
        the optional remote warmup; the remote model server stays running.
        """
        if self._shutting_down or self._pending_restart is not None:
            return
        options = dict(self._start_options)
        if "AUTOPILOT" in self._statuses:
            options["autopilot"] = self._autopilot_active
        if "AFK" in self._statuses:
            options["afk"] = "ON" in self._statuses["AFK"]
        if "DRY_RUN" in self._statuses:
            options["dry_run"] = "ON" in self._statuses["DRY_RUN"]
        for key, value in (("autopilot", autopilot), ("dry_run", dry_run), ("afk", afk)):
            if value is not None:
                options[key] = value
        self._pending_restart = options
        self._tts.stop_speech()
        self._set_startup_status(
            {
                "phase": "reloading",
                "message": "Reloading engine — saving match context and stopping current work…",
                "ready": False,
            }
        )
        self.logEmitted.emit("Reloading coaching engine; the model server stays running.", "status")
        if self.is_running:
            self._process.stop_async(command="prepare_engine_reload")
        else:
            self._finish_restart()

    def _finish_restart(self) -> None:
        options = self._pending_restart
        if options is None or self._shutting_down or self.is_running:
            return
        self._pending_restart = None
        self.start(**options, engine_reload=True)

    def send_command(self, command: str, *args: Any) -> None:
        """Send a JSON command to the coach subprocess."""
        self._process.send_command(command, *args)

    def _report_speech_status(self, speech_id: str, state: str) -> None:
        """Tell the engine how an utterance it is waiting on ended (or started)."""
        if speech_id:
            self._process.send_payload({"cmd": "speech_status", "speech_id": speech_id, "state": state})

    def toggle_autopilot(self) -> None:
        self.send_command("toggle_autopilot")

    def set_voice(self, voice_id: str) -> None:
        """Select a Kokoro voice by id in the running engine (saved there too)."""
        self._process.send_payload({"cmd": "sync_voice_preferences", "voice": str(voice_id)})

    def set_draft_commentary(self, enabled: bool) -> None:
        """Turn spoken draft-pick explanations on or off in the running engine."""
        self._process.send_payload({"cmd": "set_draft_commentary", "enabled": bool(enabled)})

    def set_auto_concede(self, enabled: bool) -> None:
        """Choose whether autoplay concedes a game the board math says is lost."""
        self._process.send_payload({"cmd": "set_auto_concede", "enabled": bool(enabled)})

    def set_oops_emote(self, enabled: bool) -> None:
        """Choose whether autoplay sends Arena's "Oops" emote when it gets stuck or blunders."""
        self._process.send_payload({"cmd": "set_oops_emote", "enabled": bool(enabled)})

    def cancel_concede(self) -> None:
        """Stop a running auto-concede countdown and keep playing this game."""
        self._process.send_payload({"cmd": "cancel_concede"})

    def set_auto_queue(self, enabled: bool) -> None:
        """Choose whether autoplay may repeat the recent queue after a match."""
        self._process.send_payload({"cmd": "set_auto_queue", "enabled": bool(enabled)})

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
            from arenamcp.autopilot_bug_capture import capture_mtga_screenshot

            out.update(capture_mtga_screenshot(bug_dir, f"bug_{ts}"))
        except Exception as e:
            logger.debug("MTGA screenshot failed: %s", e)

        return out

    def trigger_autopilot_bug(self) -> None:
        """Pause/capture first, then attach Arena's screenshot for manual recovery."""
        from datetime import datetime, timezone
        from pathlib import Path
        from uuid import uuid4

        from arenamcp.autopilot_bug_capture import AutopilotBugCapture, capture_mtga_screenshot
        from arenamcp.logging_config import LOG_DIR

        if self._last_autopilot_bug_status.get("phase") in {"capturing", "recording"}:
            return
        clicked_at = datetime.now(timezone.utc)
        capture_id = clicked_at.strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid4().hex[:8]
        self._tts.stop_speech()
        self._set_autopilot_bug_status(
            {
                "phase": "capturing",
                "capture_id": capture_id,
                "message": "Capturing autopilot bug and requesting pause…",
            }
        )
        engine_running = self.is_running
        if engine_running:
            # Send before screenshot work so the engine snapshots its intent
            # and stops input promptly even if screen capture is slow.
            self._process.send_payload(
                {"cmd": "autopilot_bug", "capture_id": capture_id, "clicked_at": clicked_at.isoformat()}
            )
        screenshots: dict[str, str] = {}
        screenshot_error = ""
        try:
            screenshots = capture_mtga_screenshot(
                Path(LOG_DIR) / "bug_reports", f"autopilot_bug_{capture_id}"
            )
            if not screenshots:
                screenshot_error = "Arena window unavailable for screenshot"
        except Exception as exc:
            screenshot_error = str(exc)
            logger.debug("Autopilot bug screenshot failed: %s", exc)
        if screenshot_error:
            self.logEmitted.emit(f"Autopilot bug screenshot unavailable: {screenshot_error}", "error")
        if engine_running:
            self._process.send_payload(
                {
                    "cmd": "autopilot_bug_screenshots",
                    "capture_id": capture_id,
                    "screenshots": screenshots,
                    "screenshot_error": screenshot_error,
                }
            )
        else:
            try:
                recorder = AutopilotBugCapture(Path(LOG_DIR) / "bug_reports")
                path = recorder.begin(
                    capture_id,
                    {
                        "timestamp": clicked_at.isoformat(),
                        "game_state": self._last_game_state,
                        "game_state_source": "desktop_last_known_state",
                        "autopilot": {"available": False, "statuses": dict(self._statuses)},
                        "screenshots": screenshots,
                        "screenshot_error": screenshot_error,
                    },
                )
                recorder.finish("engine_unavailable")
                self.bugReportSaved.emit(str(path), "")
                self._set_autopilot_bug_status(
                    dict(
                        recorder.status,
                        message="Saved local screenshot and last known state. Engine unavailable; recovery recording could not start.",
                    )
                )
            except Exception as exc:
                self._set_autopilot_bug_status(
                    {
                        "phase": "error",
                        "capture_id": capture_id,
                        "message": f"Autopilot bug capture failed: {exc}",
                    }
                )

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
        if self._pending_restart is not None and ev_type in {
            "startup_status",
            "speak_request",
            "speak",
            "speak_audio",
            "advice",
            "emit_advice",
        }:
            # The outgoing engine can finish a model request while stopping.
            # Its advice/readiness is obsolete once a reload is requested.
            return

        if ev_type == "startup_status":
            status = event.get("data", event)
            if isinstance(status, dict):
                self._set_startup_status(status)

        elif ev_type == "autopilot_bug_status":
            status = event.get("data", event)
            if isinstance(status, dict):
                incoming_id = status.get("capture_id")
                current_id = self._last_autopilot_bug_status.get("capture_id")
                prior_finished = self._last_autopilot_bug_status.get("phase") in ("completed", "error")
                if not incoming_id or not current_id or incoming_id == current_id or prior_finished:
                    self._set_autopilot_bug_status(status)

        elif ev_type in ("game_state", "emit_game_state"):
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

        elif ev_type == "concede_countdown":
            payload = event.get("data", event)
            if isinstance(payload, dict):
                self._last_concede = dict(payload)
                self.concedeCountdownChanged.emit(dict(payload))

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
            # The engine waits on utterances that carry a speech_id (deck
            # review narration); every path must acknowledge them.
            speech_id = str(event.get("speech_id") or "")
            if not text and speech_id:
                self._report_speech_status(speech_id, "failed")
            if text:
                self.spokenLine.emit(text)
                if self._muted and speech_id:
                    self._report_speech_status(speech_id, "muted")
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
                    if speech_id:
                        extra_kwargs["speech_id"] = speech_id
                        self._report_speech_status(speech_id, "accepted")
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
        if self._last_concede.get("state") in ("offering", "armed", "conceding", "sent"):
            # The engine's countdown died with it; take the banner down.
            self._last_concede = {"state": "aborted", "reason": "the coach stopped"}
            self.concedeCountdownChanged.emit(dict(self._last_concede))
        self.processExited.emit(code)
        self.stopped.emit()
        if self._last_autopilot_bug_status.get("phase") in {"capturing", "recording"}:
            self._set_autopilot_bug_status(
                dict(
                    self._last_autopilot_bug_status,
                    phase="error",
                    message="Engine stopped during capture. Any saved recovery snapshots remain in the local report folder.",
                )
            )
        if self._pending_restart is not None and not self._shutting_down:
            # Defer until QProcess.finished has returned and cleaned up the
            # old child; never allow two engines to control Arena at once.
            QTimer.singleShot(0, self._finish_restart)
        else:
            self._set_startup_status(
                {
                    "phase": "stopped" if code == 0 or self._shutting_down else "error",
                    "message": "Coach stopped."
                    if code == 0 or self._shutting_down
                    else f"Coach exited (code {code}).",
                    "ready": False,
                }
            )

    def shutdown(self) -> None:
        """Cleanly terminate subprocess and TTS manager."""
        self._shutting_down = True
        self._pending_restart = None
        if self._tts is not None:
            self._tts.shutdown()
        self.stop()
