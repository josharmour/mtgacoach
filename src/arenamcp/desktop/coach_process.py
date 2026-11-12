from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .runtime import find_python_executable, get_app_root, get_runtime_root, is_packaged_runtime


def _coach_launch_args(python_exe: str, app_root: Path) -> tuple[list[str], bool]:
    """Return (args, packaged_runtime) without changing source-checkout behavior."""
    packaged = is_packaged_runtime(python_exe, app_root)
    if packaged:
        return (["-I", "-X", "utf8", "-u", "-m", "arenamcp.standalone", "--pipe"], True)
    return (["-u", "-m", "arenamcp.standalone", "--pipe"], False)


def engine_interpreter_for(app_root: str | Path | None = None) -> tuple[str, str]:
    """The interpreter CoachProcess.start would select for ``app_root``.

    Falls back to the running interpreter when no better candidate responds.
    Dependency-environment preparation uses this so it provisions for exactly
    the interpreter the engine will actually run on.
    """
    python_exe, python_source = find_python_executable(app_root)
    if python_exe is None:
        return sys.executable, "sys.executable"
    return python_exe, python_source


logger = logging.getLogger(__name__)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left == right


def _selected_snapshot_disagreement(app_root: Path, selected_sha: str) -> tuple[str | None, bool]:
    """Explain why the selected snapshot must NOT bind a prepared env, or None.

    Returns ``(reason, launch_root_rejected)``. Binding a prepared dependency
    environment requires agreement between the selected SHA, the selected root
    (``MTGACOACH_SNAPSHOT_ROOT`` or the launch root itself) and the manifest
    published inside that root. A merely nonempty ``MTGACOACH_SNAPSHOT_SHA``
    never unlocks dependency participation.

    ``launch_root_rejected`` is True only when the REJECTED source IS the
    launch root: that source's code must then never be executed, so the caller
    must refuse the launch outright instead of falling back to a plain launch
    of the very tree that failed validation.
    """
    from .runtime_snapshots import SnapshotValidationError, validate_snapshot_dir

    root_env = os.environ.get("MTGACOACH_SNAPSHOT_ROOT", "").strip()
    selected_root = Path(root_env) if root_env else app_root
    try:
        manifest = validate_snapshot_dir(selected_root)
    except SnapshotValidationError as exc:
        reason = f"selected snapshot {selected_sha[:7]} failed validation at {selected_root}: {exc}"
        return reason, _same_path(selected_root, app_root)
    manifest_commit = str(manifest.get("commit") or "")
    if manifest_commit != selected_sha:
        return (
            f"selected SHA {selected_sha[:7]} does not match manifest commit "
            f"{manifest_commit[:7] or '?'} in {selected_root}",
            False,
        )
    if root_env and not _same_path(Path(root_env), app_root):
        return f"selected root {root_env} does not match launch root {app_root}", False
    return None, False


def resolve_engine_launch(app_root: Path) -> tuple[str, str, list[str], bool]:
    """Resolve interpreter, source label, launch args and packaging for a
    nonfrozen source launch.

    Dependency environments participate ONLY when this launch is an
    explicitly selected snapshot (``MTGACOACH_SNAPSHOT_SHA`` present and
    snapshots not disabled) whose SHA/root/manifest all AGREE, and only as a
    PREPARED environment: a missing one raises instead of installing anything
    here. Ordinary launches and reloads — and disagreeing selections — keep
    the resolved interpreter untouched: no dependency preparation,
    installation or environment mutation.

    When the selected snapshot source ITSELF fails validation and that source
    is the launch root, the launch is REFUSED with a visible error: the engine
    never executes source that failed snapshot validation. A rejected EXTERNAL
    selection root (distinct from the launch root) only downgrades the launch
    to plain, because the checkout being launched was never rejected.
    """
    python_exe, python_source = engine_interpreter_for(app_root)

    selected_sha = os.environ.get("MTGACOACH_SNAPSHOT_SHA", "").strip()
    if selected_sha:
        from .runtime_snapshots import snapshots_disabled

        if not snapshots_disabled():
            from .dep_env import prepared_dep_env, venv_python

            disagreement, launch_root_rejected = _selected_snapshot_disagreement(app_root, selected_sha)
            if disagreement is not None:
                if launch_root_rejected:
                    # The rejected source IS the launch root: never execute it.
                    # CoachSession.start surfaces this visibly and keeps the
                    # current engine in charge.
                    raise RuntimeError(
                        f"Refusing to launch: {disagreement}. The engine will "
                        f"not execute source that failed snapshot validation "
                        f"at its launch root ({app_root}); re-validate or "
                        "re-publish the snapshot, then restart Coach."
                    )
                logger.warning(
                    "Ignoring snapshot selection %s for this launch: %s. "
                    "Launching %s with its own interpreter.",
                    selected_sha[:7],
                    disagreement,
                    app_root,
                )
                args, packaged_runtime = _coach_launch_args(python_exe, app_root)
                return python_exe, python_source, args, packaged_runtime
            prepared = prepared_dep_env(app_root, python_executable=python_exe)
            if prepared is None:
                raise RuntimeError(
                    f"Selected snapshot {selected_sha[:7]} has no prepared "
                    "dependency environment for this interpreter; use Restart "
                    "Coach from the launcher to prepare it. The current engine "
                    "version stays in charge until then."
                )
            bound = venv_python(prepared)
            if bound.exists():
                python_exe = str(bound)
                python_source = f"dep_env:{prepared.name}"

    args, packaged_runtime = _coach_launch_args(python_exe, app_root)
    return python_exe, python_source, args, packaged_runtime


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
                packaged_runtime = False
            else:
                python_exe, python_source, args, packaged_runtime = resolve_engine_launch(app_root)

            if autopilot:
                args.append("--autopilot")
            if dry_run:
                args.append("--dry-run")
            if afk:
                args.append("--afk")

            src_dir = str(app_root / "src")
            snapshot_sha = os.environ.get("MTGACOACH_SNAPSHOT_SHA", "").strip()
            self.last_error = (
                f"Launching: {python_exe} ({python_source})\n"
                f"Args: {' '.join(args)}\n"
                f"WorkDir: {app_root}\n"
                f"Snapshot: {snapshot_sha or '(checkout)'}\n"
                f"PYTHONPATH: {'(isolated; installed package)' if packaged_runtime else src_dir}"
            )

            process = QProcess(self)
            env = QProcessEnvironment.systemEnvironment()
            if packaged_runtime:
                # The bundled runtime's ._pth isolates imports; do not inject
                # the checkout src directory into an installed app.
                env.remove("PYTHONPATH")
            else:
                env.insert("PYTHONPATH", src_dir)
            env.insert("MTGACOACH_RUNTIME_ROOT", runtime_root)
            env.insert("MTGACOACH_FRONTEND", "pyside")
            # Engine events this frontend handles that older ones would ignore:
            # request_engine_reload (CoachSession -> the Reload engine slot).
            env.insert("MTGACOACH_FRONTEND_FEATURES", "request_engine_reload")
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

        Without a command this is the terminate/kill fallback. With a command
        it only *asks* the engine to reach its safe checkpoint and exit on its
        own — never terminate or kill: an engine still inside an unsafe
        decision boundary must be allowed to finish. CoachSession arms a
        deferral watchdog that cancels the reload visibly if the child does
        not exit in time; the current engine simply keeps running.
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
            # Defer, never force-terminate: the engine owns the timing of its
            # safe checkpoint. See the class docstring above.
            self.send_command(command)
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
