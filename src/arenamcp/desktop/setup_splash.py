"""First-run setup splash window.

Provides a branded PySide6 window that runs the environment/venv provisioning
step (``setup_wizard.py --setup-environment``) as a child :class:`QProcess` and
streams its progress, instead of flashing a raw console window on first launch.

The window is intentionally self-contained: it spawns and supervises the setup
subprocess, surfaces live output in an expandable "Details" pane, and emits
``setup_completed(bool success, str message)`` when the subprocess finishes (or
is cancelled). ``app.main()`` shows this before constructing the main window
when, and only when, the runtime environment is not yet provisioned.

``setup_wizard.py`` and ``repair_tab.py`` are intentionally left untouched; this
window is a wrapper around the same command builder
(:func:`runtime.get_setup_wizard_command`) used by the Repair tab.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# Heuristic step plan: (elapsed_seconds_start, percent_start, label).
# The progress bar interpolates between consecutive entries based on the
# elapsed wall-clock time of the setup subprocess. The final jump to 100% is
# driven by actual subprocess completion, not the timer.
_STEP_PLAN = (
    (0.0, 0, "Checking Python..."),
    (15.0, 25, "Creating venv..."),
    (45.0, 50, "Installing dependencies..."),
    (120.0, 90, "Verifying setup..."),
)

# Hard ceiling for the heuristic progress bar before completion so it never
# claims to be "done" while the subprocess is still running.
_HEURISTIC_CEILING = 95

# QProcess timeout (ms) after which the setup is considered hung.
_SETUP_TIMEOUT_MS = 5 * 60 * 1000

# Maximum number of failed attempts before we stop offering an in-window retry.
_MAX_RETRIES = 3


class SetupSplashWindow(QMainWindow):
    """Branded first-run setup splash with live subprocess progress.

    Emits :attr:`setup_completed` with ``(success, message)`` when the setup
    subprocess finishes or the user cancels. ``setup_success`` reflects the last
    terminal outcome and can be polled by callers driving a manual event loop.
    """

    setup_completed = Signal(bool, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._process: Optional[QProcess] = None
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self._start_time: float = 0.0
        self._finished = False
        self.setup_success = False
        self._attempts = 0

        self._app_version = self._resolve_version()

        self.setWindowTitle("mtgacoach setup")
        self.setFixedSize(520, 440)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        self._apply_window_icon()
        self._build_ui()
        self._center_on_screen()

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(500)
        self._tick_timer.timeout.connect(self._on_tick)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_version() -> str:
        try:
            from .. import __version__

            return str(__version__)
        except Exception:
            return ""

    def _apply_window_icon(self) -> None:
        try:
            from .runtime import get_app_root

            icon_path = Path(get_app_root()) / "assets" / "icon.png"
            if icon_path.exists():
                self._icon_path = icon_path
                self.setWindowIcon(QIcon(str(icon_path)))
            else:
                self._icon_path = None
        except Exception:
            self._icon_path = None

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(28, 24, 28, 20)
        root.setSpacing(12)

        # Header (optional icon + title/subtitle).
        header = QHBoxLayout()
        header.setSpacing(14)
        if getattr(self, "_icon_path", None) is not None:
            logo = QLabel()
            pixmap = QIcon(str(self._icon_path)).pixmap(56, 56)
            logo.setPixmap(pixmap)
            logo.setFixedSize(56, 56)
            header.addWidget(logo, 0, Qt.AlignTop)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("mtgacoach")
        title_font = QFont()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title.setFont(title_font)
        title_box.addWidget(title)

        subtitle = QLabel("Setting up your environment...")
        subtitle_font = QFont()
        subtitle_font.setPointSize(11)
        subtitle.setFont(subtitle_font)
        subtitle.setStyleSheet("color: palette(mid);")
        title_box.addWidget(subtitle)
        header.addLayout(title_box, 1)
        root.addLayout(header)

        # Status line + elapsed.
        status_row = QHBoxLayout()
        self.status_label = QLabel("Preparing...")
        status_font = QFont()
        status_font.setPointSize(11)
        self.status_label.setFont(status_font)
        status_row.addWidget(self.status_label, 1)

        self.elapsed_label = QLabel("")
        self.elapsed_label.setStyleSheet("color: palette(mid);")
        status_row.addWidget(self.elapsed_label, 0, Qt.AlignRight)
        root.addLayout(status_row)

        # Progress bar.
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        root.addWidget(self.progress)

        # Details toggle + pane.
        self.details_toggle = QPushButton("Show details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setFlat(True)
        self.details_toggle.toggled.connect(self._on_details_toggled)
        root.addWidget(self.details_toggle, 0, Qt.AlignLeft)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(9)
        self.details.setFont(mono)
        self.details.setVisible(False)
        root.addWidget(self.details, 1)

        root.addStretch(1)

        # Buttons.
        button_row = QHBoxLayout()
        if self._app_version:
            version_label = QLabel(f"v{self._app_version}")
            version_label.setStyleSheet("color: palette(mid);")
            version_font = QFont()
            version_font.setPointSize(9)
            version_label.setFont(version_font)
            button_row.addWidget(version_label, 0, Qt.AlignLeft)
        button_row.addStretch(1)

        self.retry_button = QPushButton("Retry")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self.start_setup)
        button_row.addWidget(self.retry_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        button_row.addWidget(self.cancel_button)
        root.addLayout(button_row)

    def _center_on_screen(self) -> None:
        try:
            screen = QApplication.primaryScreen()
            if screen is None:
                return
            geo = screen.availableGeometry()
            frame = self.frameGeometry()
            frame.moveCenter(geo.center())
            self.move(frame.topLeft())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Setup lifecycle
    # ------------------------------------------------------------------
    def start_setup(self) -> None:
        """Spawn the setup subprocess and begin streaming progress."""
        if self._process is not None:
            return

        from .runtime import get_app_root, get_setup_wizard_command

        self._finished = False
        self.setup_success = False
        self._attempts += 1
        self._stdout_buffer = ""
        self._stderr_buffer = ""
        self.details.clear()
        self.retry_button.setVisible(False)
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText("Cancel")
        self.progress.setValue(0)
        self._set_status("Checking Python...")

        try:
            program, args, env = get_setup_wizard_command("setup_environment")
        except Exception as exc:
            self._append_details(f"[!!] Unable to start setup: {exc}")
            self._fail(
                "Could not locate a Python executable. Install Python from "
                "python.org, then reopen mtgacoach."
            )
            return

        process = QProcess(self)
        proc_env = QProcessEnvironment.systemEnvironment()
        for key, value in env.items():
            proc_env.insert(key, value)
        # Ensure unbuffered, UTF-8 child output regardless of caller env.
        proc_env.insert("PYTHONUNBUFFERED", "1")
        proc_env.insert("PYTHONIOENCODING", "utf-8")
        process.setProcessEnvironment(proc_env)
        process.setWorkingDirectory(str(get_app_root()))
        process.setProgram(program)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(self._on_stdout_ready)
        process.readyReadStandardError.connect(self._on_stderr_ready)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)

        self._process = process
        self._start_time = time.monotonic()
        self._append_details(f"[..] Running: {program} {' '.join(args)}")
        process.start()
        self._tick_timer.start()

    def _on_tick(self) -> None:
        if self._process is None:
            return

        elapsed = time.monotonic() - self._start_time
        self.elapsed_label.setText(f"Elapsed: {int(elapsed)}s")

        label, percent = self._heuristic_progress(elapsed)
        self._set_status(label)
        # Never move backwards and never exceed the heuristic ceiling.
        target = min(max(percent, self.progress.value()), _HEURISTIC_CEILING)
        self.progress.setValue(target)

        if elapsed * 1000 >= _SETUP_TIMEOUT_MS:
            self._append_details("[!!] Setup timed out.")
            self._terminate_process()
            self._fail(
                "Setup timed out. Open the Repair tab to retry environment "
                "provisioning manually."
            )

    @staticmethod
    def _heuristic_progress(elapsed: float) -> tuple[str, int]:
        plan = _STEP_PLAN
        # Past the last knee: hold at its label/percent.
        last_t, last_p, last_label = plan[-1]
        if elapsed >= last_t:
            return last_label, last_p
        for i in range(len(plan) - 1):
            t0, p0, label = plan[i]
            t1, p1, _ = plan[i + 1]
            if t0 <= elapsed < t1:
                if t1 == t0:
                    return label, p0
                frac = (elapsed - t0) / (t1 - t0)
                return label, int(p0 + frac * (p1 - p0))
        return plan[0][2], plan[0][1]

    # ------------------------------------------------------------------
    # Subprocess stream handling
    # ------------------------------------------------------------------
    def _on_stdout_ready(self) -> None:
        self._drain_stream(standard_error=False)

    def _on_stderr_ready(self) -> None:
        self._drain_stream(standard_error=True)

    def _drain_stream(self, *, standard_error: bool) -> None:
        process = self._process
        if process is None:
            return

        if standard_error:
            chunk = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
            self._stderr_buffer += chunk
            buffer_name = "_stderr_buffer"
        else:
            chunk = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
            self._stdout_buffer += chunk
            buffer_name = "_stdout_buffer"

        buffer = getattr(self, buffer_name)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r").strip()
            if line:
                prefix = "[stderr] " if standard_error else ""
                self._append_details(prefix + line)
        setattr(self, buffer_name, buffer)

    def _on_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._drain_stream(standard_error=False)
        self._drain_stream(standard_error=True)
        if self._stdout_buffer.strip():
            self._append_details(self._stdout_buffer.strip())
        if self._stderr_buffer.strip():
            self._append_details("[stderr] " + self._stderr_buffer.strip())
        self._stdout_buffer = ""
        self._stderr_buffer = ""

        self._tick_timer.stop()
        process = self._process
        self._process = None
        if process is not None:
            process.deleteLater()

        if exit_code == 0:
            self._succeed()
        else:
            self._append_details(f"[!!] Setup failed with exit code {exit_code}.")
            self._fail(f"Setup failed (exit code {exit_code}).")

    def _on_error(self, error: QProcess.ProcessError) -> None:
        # FailedToStart fires before finished(); other errors usually precede
        # a finished() with a nonzero code, so only act on the start failure.
        if error == QProcess.FailedToStart:
            self._tick_timer.stop()
            process = self._process
            self._process = None
            if process is not None:
                process.deleteLater()
            self._append_details("[!!] Failed to start the setup process.")
            self._fail(
                "Could not start setup. Verify your Python installation, then "
                "retry."
            )

    # ------------------------------------------------------------------
    # Terminal states
    # ------------------------------------------------------------------
    def _succeed(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.setup_success = True
        self.progress.setValue(100)
        self._set_status("Setup complete.")
        self._append_details("[ok] Environment ready.")
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self.retry_button.setVisible(False)
        self.setup_completed.emit(True, "Setup complete.")

    def _fail(self, message: str) -> None:
        if self._finished:
            return
        self._finished = True
        self.setup_success = False
        self._set_status("Setup failed.")
        # Surface details automatically so the user can see what went wrong.
        if not self.details.isVisible():
            self.details_toggle.setChecked(True)
        self.retry_button.setVisible(self._attempts < _MAX_RETRIES)
        if self._attempts >= _MAX_RETRIES:
            self._append_details(
                "[!!] Multiple setup attempts failed. Use the Repair tab for "
                "manual provisioning."
            )
        self.cancel_button.setText("Close")
        self.cancel_button.setEnabled(True)
        self.setup_completed.emit(False, message)

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------
    def _on_cancel_clicked(self) -> None:
        if self._process is not None:
            # Setup in progress: cancel it.
            self._append_details("[..] Cancelling setup...")
            self._terminate_process()
            self._fail("Setup cancelled.")
            return
        # Already terminal: just close.
        self.close()

    def _terminate_process(self) -> None:
        self._tick_timer.stop()
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            process.readyReadStandardOutput.disconnect()
            process.readyReadStandardError.disconnect()
            process.finished.disconnect()
            process.errorOccurred.disconnect()
        except Exception:
            pass
        try:
            process.kill()
            process.waitForFinished(2000)
        except Exception:
            pass
        process.deleteLater()

    def _on_details_toggled(self, checked: bool) -> None:
        self.details.setVisible(checked)
        self.details_toggle.setText("Hide details" if checked else "Show details")

    # ------------------------------------------------------------------
    # Small UI helpers
    # ------------------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _append_details(self, message: str) -> None:
        self.details.appendPlainText(message)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        # Closing mid-setup cancels the subprocess so it never lingers.
        if self._process is not None:
            self._terminate_process()
            if not self._finished:
                self._finished = True
                self.setup_success = False
                self.setup_completed.emit(False, "Setup cancelled.")
        super().closeEvent(event)
