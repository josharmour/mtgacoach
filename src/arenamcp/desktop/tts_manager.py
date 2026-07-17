from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .audio import AudioPlayback
from .runtime import find_python_executable, get_app_root, get_runtime_root


class TtsManager(QObject):
    log_line = Signal(str)
    status_line = Signal(str)
    error_line = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._process: Optional[QProcess] = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._ready = False
        self._busy = False
        self._closing = False
        self._generation = 0
        self._pending_request: Optional[dict[str, Any]] = None
        self._current_audio_path: Optional[Path] = None
        # When the Kokoro worker can't serve (init failure, dead process),
        # macOS falls back to the built-in `say` voice instead of silence —
        # the coach's speech must never silently disappear.
        self._worker_failed = False
        self._say_process: Optional[subprocess.Popen] = None
        self._last_text: str = ""
        self._last_speed: float = 1.0

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def start(self) -> None:
        if self.is_running:
            return

        self._closing = False
        self._ready = False
        self._busy = False
        self._stdout_buffer = ""
        self._stderr_buffer = ""

        app_root = Path(get_app_root())
        runtime_root = get_runtime_root()
        python_exe, python_source = find_python_executable()
        if python_exe is None:
            raise RuntimeError("Python executable not found for Kokoro worker")

        process = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONPATH", str(app_root / "src"))
        env.insert("MTGACOACH_RUNTIME_ROOT", runtime_root)
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(env)
        process.setWorkingDirectory(str(app_root))
        process.setProgram(python_exe)
        process.setArguments(["-u", "-m", "arenamcp.desktop.tts_worker"])
        process.readyReadStandardOutput.connect(self._on_stdout_ready)
        process.readyReadStandardError.connect(self._on_stderr_ready)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        process.start()

        if not process.waitForStarted(5000):
            message = process.errorString() or "Failed to start Kokoro worker"
            process.deleteLater()
            raise RuntimeError(message)

        self._process = process
        self.status_line.emit(f"Starting Kokoro worker ({python_source})...")

    def request_speech(
        self,
        *,
        text: str,
        voice_id: str,
        voice_name: str,
        speed: float,
    ) -> None:
        if not text or not text.strip():
            return

        self._last_text = text
        self._last_speed = float(speed)

        if self._worker_failed and sys.platform == "darwin":
            self._speak_via_say(text, speed)
            return

        if not self.is_running:
            try:
                self.start()
            except Exception as exc:
                self._worker_failed = True
                self.error_line.emit(str(exc))
                if sys.platform == "darwin":
                    self.status_line.emit("Kokoro unavailable — using macOS voice.")
                    self._speak_via_say(text, speed)
                return

        self._generation += 1
        self._pending_request = {
            "cmd": "render",
            "generation": self._generation,
            "text": text,
            "voice_id": voice_id,
            "voice_name": voice_name,
            "speed": float(speed),
        }
        self._stop_playback()
        self._dispatch_pending()

    def stop_speech(self) -> None:
        self._generation += 1
        self._pending_request = None
        self._stop_playback()
        self._stop_say()

    def _speak_via_say(self, text: str, speed: float) -> None:
        """macOS built-in voice fallback (darwin only)."""
        self._stop_say()
        try:
            wpm = max(90, min(450, int(175 * (speed or 1.0))))
            self._say_process = subprocess.Popen(
                ["say", "-r", str(wpm)], stdin=subprocess.PIPE
            )
            assert self._say_process.stdin is not None
            self._say_process.stdin.write(text.encode("utf-8"))
            self._say_process.stdin.close()
        except Exception as exc:
            self.error_line.emit(f"macOS say fallback failed: {exc}")

    def _stop_say(self) -> None:
        proc = self._say_process
        self._say_process = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def shutdown(self) -> None:
        self._closing = True
        self._pending_request = None
        self._stop_playback()
        self._stop_say()

        process = self._process
        if process is None:
            return

        if process.state() != QProcess.NotRunning:
            try:
                process.write(json.dumps({"cmd": "shutdown"}).encode("utf-8") + b"\n")
                process.closeWriteChannel()
            except RuntimeError:
                pass
            process.terminate()
            if not process.waitForFinished(1500):
                process.kill()
                process.waitForFinished(1500)

        self._process = None
        process.deleteLater()

    def _dispatch_pending(self) -> None:
        if self._process is None or self._process.state() == QProcess.NotRunning:
            return
        if not self._ready or self._busy or self._pending_request is None:
            return

        payload = self._pending_request
        self._pending_request = None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._process.write(line.encode("utf-8", errors="replace"))
        self._busy = True

    def _on_stdout_ready(self) -> None:
        if self._process is None:
            return

        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stdout_buffer += chunk
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._handle_stdout_line(line.rstrip("\r"))

    def _on_stderr_ready(self) -> None:
        if self._process is None:
            return

        chunk = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")
        self._stderr_buffer += chunk
        while "\n" in self._stderr_buffer:
            line, self._stderr_buffer = self._stderr_buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self.log_line.emit(f"[tts] {line}")

    def _handle_stdout_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            self.log_line.emit(f"[tts raw] {line}")
            return

        event_type = str(payload.get("type", "")).strip().lower()
        if event_type == "ready":
            self._ready = True
            self._busy = False
            voice_name = str(payload.get("voice_name", "")).strip()
            speed = payload.get("speed")
            self.status_line.emit(
                f"Kokoro ready: {voice_name or 'voice'} @ {speed}x"
                if speed is not None
                else f"Kokoro ready: {voice_name or 'voice'}"
            )
            self._dispatch_pending()
            return

        if event_type == "rendered":
            self._busy = False
            generation = int(payload.get("generation", 0))
            path = str(payload.get("path", "")).strip()
            if generation == self._generation and path:
                if AudioPlayback.play_file(path):
                    self._current_audio_path = Path(path)
                else:
                    self.error_line.emit(f"Kokoro audio playback failed: {path}")
                    self._cleanup_path(Path(path))
            else:
                self._cleanup_path(Path(path))
            self._dispatch_pending()
            return

        if event_type == "error":
            self._busy = False
            generation = int(payload.get("generation", 0) or 0)
            message = str(payload.get("message", "Kokoro worker error")).strip()
            if generation == 0 or generation == self._generation:
                self.error_line.emit(message)
            trace = str(payload.get("traceback", "")).strip()
            if trace:
                self.log_line.emit(trace)
            if "init failed" in message:
                # Kokoro can't come up at all (usually missing model files).
                # Downgrade permanently for this session and voice the advice
                # that just failed rather than dropping it.
                self._worker_failed = True
                if sys.platform == "darwin" and self._last_text:
                    self.status_line.emit("Kokoro unavailable — using macOS voice.")
                    self._speak_via_say(self._last_text, self._last_speed)
                return
            if sys.platform == "darwin" and self._last_text:
                # Transient render failure: keep Kokoro for next time, but
                # don't lose this utterance.
                self._speak_via_say(self._last_text, self._last_speed)
            self._dispatch_pending()
            return

        if event_type == "log":
            self.log_line.emit(str(payload.get("message", "")).strip())

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if self._stdout_buffer.strip():
            self._handle_stdout_line(self._stdout_buffer.strip())
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._ready = False
        self._busy = False

        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()

        if not self._closing:
            self.error_line.emit(f"Kokoro worker exited ({exit_code}).")
            if exit_code != 0:
                self._worker_failed = True
                if sys.platform == "darwin" and self._last_text:
                    self.status_line.emit("Kokoro unavailable — using macOS voice.")
                    self._speak_via_say(self._last_text, self._last_speed)

    def _on_error(self, _error: QProcess.ProcessError) -> None:
        if self._process is not None:
            self.error_line.emit(self._process.errorString())

    def _stop_playback(self) -> None:
        AudioPlayback.stop()
        if self._current_audio_path is not None:
            self._cleanup_path(self._current_audio_path)
            self._current_audio_path = None

    @staticmethod
    def _cleanup_path(path: Path) -> None:
        try:
            if path and path.exists():
                path.unlink()
        except OSError:
            pass
