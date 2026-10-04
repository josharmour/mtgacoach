from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .runtime import find_python_executable, get_app_root, get_runtime_root


class CoachProcess(QObject):
    event_received = Signal(object)
    stderr_line = Signal(str)
    exited = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._process: QProcess | None = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self.last_error = ""
        # Serializes start()/stop() so two concurrent start() calls can't both
        # pass the is_running check, spawn two subprocesses, and leak the first
        # QProcess when the second overwrites self._process.
        self._lifecycle_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def start(
        self,
        autopilot: bool = False,
        dry_run: bool = False,
        afk: bool = False,
        *,
        engine_reload: bool = False,
    ) -> None:
        with self._lifecycle_lock:
            if self.is_running:
                return

            app_root = Path(get_app_root())
            runtime_root = get_runtime_root()

            import sys

            if getattr(sys, "frozen", False):
                python_exe = sys.executable
                python_source = "frozen_app"
                args = ["--pipe"]
            else:
                python_exe, python_source = find_python_executable()
                if python_exe is None:
                    python_exe = sys.executable
                    python_source = "sys.executable"
                args = ["-u", "-m", "arenamcp.standalone", "--pipe"]

            if autopilot:
                args.append("--autopilot")
            if dry_run:
                args.append("--dry-run")
            if afk:
                args.append("--afk")

            src_dir = str(app_root / "src")
            self.last_error = (
                f"Launching: {python_exe} ({python_source})\n"
                f"Args: {' '.join(args)}\n"
                f"WorkDir: {app_root}\n"
                f"PYTHONPATH: {src_dir}"
            )

            process = QProcess(self)
            env = QProcessEnvironment.systemEnvironment()
            env.insert("PYTHONPATH", src_dir)
            env.insert("MTGACOACH_RUNTIME_ROOT", runtime_root)
            env.insert("MTGACOACH_FRONTEND", "pyside")
            env.insert("ARENAMCP_PROACTIVE_ONLY", "1")
            env.insert("PYTHONUNBUFFERED", "1")
            env.insert("PYTHONIOENCODING", "utf-8")
            # Never inherit a stale reload flag from the desktop environment.
            if engine_reload:
                env.insert("ARENAMCP_ENGINE_RELOAD", "1")
            else:
                env.remove("ARENAMCP_ENGINE_RELOAD")
            process.setProcessEnvironment(env)
            process.setWorkingDirectory(str(app_root))
            process.setProgram(python_exe)
            process.setArguments(args)
            process.readyReadStandardOutput.connect(self._on_stdout_ready)
            process.readyReadStandardError.connect(self._on_stderr_ready)
            process.finished.connect(self._on_finished)
            process.errorOccurred.connect(self._on_error)
            self._process = process
            self._stdout_buffer = ""
            self._stderr_buffer = ""
            process.start()

            if not process.waitForStarted(5000):
                message = process.errorString() or "Failed to start Python coach process"
                if self._process is process:
                    self._process = None
                    process.deleteLater()
                raise RuntimeError(message)

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._process is None:
                return

            process = self._process
            if process.state() == QProcess.NotRunning:
                self._process = None
                process.deleteLater()
                return

            with contextlib.suppress(RuntimeError):
                process.closeWriteChannel()

            process.terminate()
            if not process.waitForFinished(3000):
                process.kill()
                if not process.waitForFinished(2000):
                    raise RuntimeError("Coach process did not exit after termination")
            if self._process is process:
                self._process = None
                process.deleteLater()

    def stop_async(self, *, command: str | None = None) -> None:
        """Stop without blocking Qt, keeping the child tracked until it exits.

        A reload command gets time to checkpoint and exit before the normal
        terminate/kill fallback, including compatibility with older engines.
        start() remains a no-op while the old child is still alive.
        """
        process = self._process
        if process is None:
            return
        if process.state() == QProcess.NotRunning:
            self._on_finished(process.exitCode(), process.exitStatus())
            return

        def _hard_kill() -> None:
            if self._process is process and process.state() != QProcess.NotRunning:
                process.kill()

        def _terminate() -> None:
            if self._process is not process or process.state() == QProcess.NotRunning:
                return
            with contextlib.suppress(RuntimeError):
                process.closeWriteChannel()
                process.terminate()
            QTimer.singleShot(2000, _hard_kill)

        if command:
            self.send_command(command)
            QTimer.singleShot(5000, _terminate)
        else:
            _terminate()

    def send_command(self, command: str, text: str | None = None) -> None:
        if self._process is None or self._process.state() == QProcess.NotRunning:
            return

        payload = {"cmd": command}
        if text is not None:
            payload["text"] = text
        self.send_payload(payload)

    def send_payload(self, payload: dict[str, object]) -> None:
        if self._process is None or self._process.state() == QProcess.NotRunning:
            return

        line = json.dumps(payload, ensure_ascii=False) + "\n"
        self._process.write(line.encode("utf-8", errors="replace"))

    def _on_stdout_ready(self) -> None:
        if self._process is None or (self.sender() is not None and self.sender() is not self._process):
            return

        chunk = bytes(self._process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._stdout_buffer += chunk
        while "\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split("\n", 1)
            self._handle_stdout_line(line.rstrip("\r"))

    def _on_stderr_ready(self) -> None:
        if self._process is None or (self.sender() is not None and self.sender() is not self._process):
            return

        chunk = bytes(self._process.readAllStandardError()).decode("utf-8", errors="replace")
        self._stderr_buffer += chunk
        while "\n" in self._stderr_buffer:
            line, self._stderr_buffer = self._stderr_buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                continue
            self.last_error = line
            self.stderr_line.emit(line)

    def _handle_stdout_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"type": "log", "message": f"[raw] {line}"}
        self.event_received.emit(payload)

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        # A late signal from a retired child must never clear a newer one.
        sender = self.sender()
        if sender is not None and sender is not self._process:
            return
        self._on_stdout_ready()
        self._on_stderr_ready()
        if self._stdout_buffer.strip():
            self._handle_stdout_line(self._stdout_buffer.strip())
        self._stdout_buffer = ""
        self._stderr_buffer = ""

        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()
        self.exited.emit(exit_code)

    def _on_error(self, _error: QProcess.ProcessError) -> None:
        if self._process is not None and (self.sender() is None or self.sender() is self._process):
            self.last_error = self._process.errorString()
