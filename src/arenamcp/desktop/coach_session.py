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
    errorOccurred = Signal(str)

    # Telemetry & BrainStream signals
    telemetryUpdated = Signal(dict)
    reasoningChunk = Signal(str)
    mctsUpdated = Signal(object)

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
        self._tts.start()

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
        self._process.start(autopilot=autopilot, dry_run=dry_run, afk=afk)
        self.started.emit()

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

    def trigger_debug_report(self) -> None:
        self.send_command("debug_report")

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
                    self._autopilot_active = "ON" in val
                elif key == "MUTE":
                    self._muted = "ON" in val
                self.statusChanged.emit(key, val)

        elif ev_type in ("speak_request", "speak", "speak_audio"):
            text = str(event.get("text") or event.get("data") or "")
            if text:
                self.spokenLine.emit(text)
                if not self._muted:
                    speed = float(event.get("speed") or 1.0)
                    voice_id = str(event.get("voice_id") or "af_heart")
                    voice_name = str(event.get("voice_name") or "Auto")
                    self._tts.request_speech(
                        text=text,
                        voice_id=voice_id,
                        voice_name=voice_name,
                        speed=speed,
                    )

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

        elif ev_type in ("telemetry", "emit_telemetry"):
            data = event.get("data") if "data" in event else event.get("telemetry", {})
            if isinstance(data, dict):
                self.telemetryUpdated.emit(data)

        elif ev_type in ("reasoning", "emit_reasoning"):
            chunk = str(event.get("chunk") or event.get("text") or "")
            if chunk:
                self.reasoningChunk.emit(chunk)

        elif ev_type in ("mcts_tree", "emit_mcts"):
            payload = event.get("data") if "data" in event else event.get("mcts")
            self.mctsUpdated.emit(payload)

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
        self.stop()
        if self._tts is not None:
            self._tts.shutdown()
