from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

from .runtime import find_python_executable, get_app_root, get_runtime_root


class CoachProcess(QObject):
    event_received = Signal(object)
    stderr_line = Signal(str)
    exited = Signal(int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._process: Optional[QProcess] = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self.last_error = ""

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.state() != QProcess.NotRunning

    def start(self, autopilot: bool = False, dry_run: bool = False, afk: bool = False) -> None:
        if self.is_running:
            return

        app_root = Path(get_app_root())
        runtime_root = get_runtime_root()
        python_exe, python_source = find_python_executable()
        if python_exe is None:
            raise RuntimeError("Python executable not found")

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
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(env)
        process.setWorkingDirectory(str(app_root))
        process.setProgram(python_exe)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(self._on_stdout_ready)
        process.readyReadStandardError.connect(self._on_stderr_ready)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)
        process.start()

        if not process.waitForStarted(5000):
            message = process.errorString() or "Failed to start Python coach process"
            process.deleteLater()
            raise RuntimeError(message)

        self._process = process
        self._stdout_buffer = ""
        self._stderr_buffer = ""

    def stop(self) -> None:
        if self._process is None:
            return

        process = self._process
        if process.state() == QProcess.NotRunning:
            self._process = None
            process.deleteLater()
            return

        try:
            process.closeWriteChannel()
        except RuntimeError:
            pass

        process.terminate()
        if not process.waitForFinished(3000):
            process.kill()
            process.waitForFinished(2000)

    def send_command(self, command: str, text: Optional[str] = None) -> None:
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
        if self._process is not None:
            self.last_error = self._process.errorString()
