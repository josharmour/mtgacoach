from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .runtime import (
    RuntimeState,
    close_mtga,
    detect_runtime_state,
    get_setup_wizard_command,
    install_bepinex,
    install_plugin,
    open_path,
    open_url,
    read_version,
    repair_bridge_stack,
    restart_mtga,
    set_saved_mtga_dir,
    tail_text,
)


class RepairTab(QWidget):
    restart_requested = Signal(bool, bool, bool)
    provisioning_changed = Signal(bool)
    guided_setup_finished = Signal(bool, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._state: Optional[RuntimeState] = None
        self._status_labels: dict[str, QLabel] = {}
        self._setup_process: Optional[QProcess] = None
        self._setup_stdout_buffer = ""
        self._setup_stderr_buffer = ""
        self._setup_mode_label = ""
        self._setup_success_message: Optional[str] = None
        self._setup_failure_title = "Setup Failed"
        self._setup_completion: Optional[Callable[[bool], None]] = None
        self._guided_setup_active = False
        self._operation_in_progress = False
        self._busy_buttons: list[QPushButton] = []
        self._build_ui()
        self.refresh_state()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        root.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        launch_box = QGroupBox("Launch Options")
        launch_layout = QHBoxLayout(launch_box)
        self.restart_button = QPushButton("Restart Coach")
        self.restart_button.clicked.connect(lambda: self.restart_requested.emit(False, self.dry_run_check.isChecked(), self.afk_check.isChecked()))
        launch_layout.addWidget(self.restart_button)
        self.autopilot_button = QPushButton("Restart as Autopilot")
        self.autopilot_button.clicked.connect(lambda: self.restart_requested.emit(True, self.dry_run_check.isChecked(), self.afk_check.isChecked()))
        launch_layout.addWidget(self.autopilot_button)
        self.dry_run_check = QCheckBox("Dry-run")
        launch_layout.addWidget(self.dry_run_check)
        self.afk_check = QCheckBox("AFK")
        launch_layout.addWidget(self.afk_check)
        launch_layout.addStretch(1)
        layout.addWidget(launch_box)

        status_box = QGroupBox("Runtime Status")
        status_layout = QFormLayout(status_box)
        for key in (
            "Runtime Root",
            "Python Runtime",
            "MTGA Install",
            "MTGA Process",
            "BepInEx",
            "Bridge Plugin",
            "BepInEx Bundle",
            "Player.log",
            "Bridge Readiness",
        ):
            label = QLabel("...")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._status_labels[key] = label
            status_layout.addRow(f"{key}:", label)
        layout.addWidget(status_box)

        refresh_row = QHBoxLayout()
        refresh_status = QPushButton("Refresh Status")
        refresh_status.clicked.connect(self.refresh_state)
        refresh_row.addWidget(refresh_status)
        refresh_logs = QPushButton("Refresh Logs")
        refresh_logs.clicked.connect(self.refresh_log_tails)
        refresh_row.addWidget(refresh_logs)
        refresh_row.addStretch(1)
        layout.addLayout(refresh_row)

        mtga_box = QGroupBox("MTGA Location")
        mtga_layout = QHBoxLayout(mtga_box)
        self.mtga_path_box = QLineEdit()
        self.mtga_path_box.setPlaceholderText(r"C:\Program Files\Wizards of the Coast\MTGA")
        mtga_layout.addWidget(self.mtga_path_box, stretch=1)
        browse_button = QPushButton("Browse")
        browse_button.clicked.connect(self._browse_mtga)
        mtga_layout.addWidget(browse_button)
        save_button = QPushButton("Save")
        save_button.clicked.connect(self._save_mtga_path)
        mtga_layout.addWidget(save_button)
        layout.addWidget(mtga_box)

        fix_box = QGroupBox("Repair")
        fix_layout = QVBoxLayout(fix_box)
        action_row = QHBoxLayout()
        self.fix_all_button = QPushButton("Fix Everything")
        self.fix_all_button.clicked.connect(self.fix_everything)
        action_row.addWidget(self.fix_all_button)
        self.create_venv_button = QPushButton("Create venv")
        self.create_venv_button.clicked.connect(self._create_venv)
        action_row.addWidget(self.create_venv_button)
        self.setup_env_button = QPushButton("Setup environment")
        self.setup_env_button.clicked.connect(self._setup_environment)
        action_row.addWidget(self.setup_env_button)
        self.close_mtga_button = QPushButton("Close MTGA")
        self.close_mtga_button.clicked.connect(self._close_mtga)
        action_row.addWidget(self.close_mtga_button)
        self.restart_mtga_button = QPushButton("Restart MTGA")
        self.restart_mtga_button.clicked.connect(self._restart_mtga)
        action_row.addWidget(self.restart_mtga_button)
        self.repair_button = QPushButton("Repair MTGA Bridge")
        self.repair_button.clicked.connect(self._repair_bridge)
        action_row.addWidget(self.repair_button)
        self.install_bepinex_button = QPushButton("Install BepInEx")
        self.install_bepinex_button.clicked.connect(self._install_bepinex)
        action_row.addWidget(self.install_bepinex_button)
        self.install_plugin_button = QPushButton("Install Plugin")
        self.install_plugin_button.clicked.connect(self._install_plugin)
        action_row.addWidget(self.install_plugin_button)
        action_row.addStretch(1)
        fix_layout.addLayout(action_row)

        open_row = QHBoxLayout()
        open_mtga = QPushButton("Open MTGA Folder")
        open_mtga.clicked.connect(self._open_mtga)
        open_row.addWidget(open_mtga)
        open_player_log = QPushButton("Open Player.log")
        open_player_log.clicked.connect(self._open_player_log)
        open_row.addWidget(open_player_log)
        open_bepinex_log = QPushButton("Open BepInEx Log")
        open_bepinex_log.clicked.connect(self._open_bepinex_log)
        open_row.addWidget(open_bepinex_log)
        open_releases = QPushButton("GitHub Releases")
        open_releases.clicked.connect(lambda: open_url())
        open_row.addWidget(open_releases)
        open_python = QPushButton("Python Downloads")
        open_python.clicked.connect(lambda: open_url("https://www.python.org/downloads/windows/"))
        open_row.addWidget(open_python)
        open_row.addStretch(1)
        fix_layout.addLayout(open_row)

        self.fix_status = QLabel(f"mtgacoach v{read_version()}")
        fix_layout.addWidget(self.fix_status)
        self.fix_log = QPlainTextEdit()
        self.fix_log.setReadOnly(True)
        self.fix_log.setMaximumBlockCount(500)
        self.fix_log.setMinimumHeight(120)
        fix_layout.addWidget(self.fix_log)
        layout.addWidget(fix_box)

        log_box = QGroupBox("Log Tails")
        log_layout = QVBoxLayout(log_box)
        self.log_tail_view = QPlainTextEdit()
        self.log_tail_view.setReadOnly(True)
        self.log_tail_view.setMinimumHeight(260)
        log_layout.addWidget(self.log_tail_view)
        layout.addWidget(log_box)

        self._busy_buttons = [
            self.fix_all_button,
            self.create_venv_button,
            self.setup_env_button,
            self.close_mtga_button,
            self.restart_mtga_button,
            self.repair_button,
            self.install_bepinex_button,
            self.install_plugin_button,
        ]

    def refresh_state(self) -> RuntimeState:
        state = detect_runtime_state()
        self._state = state
        if not self.mtga_path_box.text().strip() and state.mtga_dir:
            self.mtga_path_box.setText(state.mtga_dir)

        self._set_status(
            "Runtime Root",
            f"{state.runtime_venv_dir} [{state.python_source}]"
            if state.runtime_venv_exists
            else f"{state.runtime_root} (setup required)",
            "ok" if state.runtime_venv_exists else "warn",
        )
        self._set_status(
            "Python Runtime",
            self._format_python_status(state),
            "ok" if state.has_ready_python_runtime else "warn" if state.python_exe else "error",
        )
        self._set_status(
            "MTGA Install",
            f"{state.mtga_dir} ({state.mtga_dir_source})" if state.mtga_dir else "Not detected",
            "ok" if state.mtga_dir else "error",
        )
        self._set_status("MTGA Process", "Running" if state.mtga_running else "Not running", "warn" if state.mtga_running else "ok")
        self._set_status("BepInEx", state.bepinex_dir or "Missing", "ok" if state.bepinex_installed else "error")
        plugin_text = (
            state.plugin_install_path
            or (f"Built at {state.plugin_build_path}" if state.plugin_built and state.plugin_build_path else "Missing")
        )
        self._set_status(
            "Bridge Plugin",
            plugin_text,
            "ok" if state.plugin_installed else "warn" if state.plugin_built else "error",
        )
        bundle_text = state.bepinex_bundle or ("Already installed in MTGA" if state.bepinex_installed else "No bundle found")
        self._set_status(
            "BepInEx Bundle",
            bundle_text,
            "ok" if state.bepinex_bundle or state.bepinex_installed else "warn",
        )
        player_log_path = state.player_log
        self._set_status(
            "Player.log",
            player_log_path if Path(player_log_path).exists() else f"Missing ({player_log_path})",
            "ok" if Path(player_log_path).exists() else "warn",
        )
        if state.is_fully_provisioned:
            readiness = "Ready"
        elif state.is_launchable:
            readiness = "Python ready; finish MTGA bridge setup"
        else:
            readiness = "Setup required"
        if state.restart_mtga_required:
            readiness += " (restart MTGA)"
        self._set_status(
            "Bridge Readiness",
            readiness,
            "ok" if state.is_fully_provisioned and not state.restart_mtga_required else "warn",
        )
        self.refresh_log_tails()
        self._update_button_states(state)
        self.provisioning_changed.emit(state.is_fully_provisioned)
        return state

    def refresh_log_tails(self) -> None:
        standalone_log = Path.home() / ".arenamcp" / "standalone.log"
        lines = [
            "[standalone.log tail]",
            tail_text(str(standalone_log), 3000),
            "",
            "[Player.log tail]",
            tail_text(self._state.player_log if self._state else None, 3000),
            "",
            "[BepInEx log tail]",
            tail_text(self._state.bepinex_log if self._state else None, 3000),
        ]
        self.log_tail_view.setPlainText("\n".join(lines))

    def fix_everything(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return

        self.fix_log.clear()
        self._operation_in_progress = True
        self._set_busy(True)
        self._guided_setup_active = self._guided_setup_active or False
        self._run_fix_sequence()

    def start_guided_setup(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return
        self._guided_setup_active = True
        self.fix_everything()

    def _browse_mtga(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select MTGA Install Folder")
        if folder:
            self.mtga_path_box.setText(folder)

    def _save_mtga_path(self) -> None:
        path = self.mtga_path_box.text().strip()
        if not path:
            QMessageBox.information(self, "mtgacoach", "Choose an MTGA folder first.")
            return
        set_saved_mtga_dir(path)
        self.refresh_state()
        QMessageBox.information(self, "mtgacoach", f"Saved MTGA folder:\n{path}")

    def _create_venv(self) -> None:
        self._start_setup_mode(
            mode="create_venv",
            step_label="Creating venv...",
            success_message="Runtime venv created and status refreshed.",
            failure_title="Create venv Failed",
        )

    def _setup_environment(self) -> None:
        self._start_setup_mode(
            mode="setup_environment",
            step_label="Setting up environment...",
            success_message="Environment setup completed and status refreshed.",
            failure_title="Setup Environment Failed",
        )

    def _repair_bridge(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return
        try:
            changed = repair_bridge_stack(self._selected_mtga_dir())
            self.refresh_state()
            QMessageBox.information(self, "mtgacoach", "\n".join(changed) if changed else "No changes needed.")
        except Exception as exc:
            QMessageBox.critical(self, "Repair Bridge Failed", str(exc))

    def _install_bepinex(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return
        try:
            target = install_bepinex(self._selected_mtga_dir())
            self.refresh_state()
            QMessageBox.information(self, "mtgacoach", f"BepInEx installed at:\n{target}")
        except Exception as exc:
            QMessageBox.critical(self, "Install BepInEx Failed", str(exc))

    def _install_plugin(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return
        try:
            target = install_plugin(self._selected_mtga_dir())
            self.refresh_state()
            QMessageBox.information(self, "mtgacoach", f"Plugin installed at:\n{target}")
        except Exception as exc:
            QMessageBox.critical(self, "Install Plugin Failed", str(exc))

    def _close_mtga(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return
        if not self._state or not self._state.mtga_running:
            QMessageBox.information(self, "mtgacoach", "MTGA is not running.")
            return
        if not self._confirm_close_mtga():
            return
        try:
            close_mtga()
            self.refresh_state()
            QMessageBox.information(self, "mtgacoach", "MTGA closed.")
        except Exception as exc:
            QMessageBox.critical(self, "Close MTGA Failed", str(exc))

    def _restart_mtga(self) -> None:
        if self._is_busy():
            self._show_busy_message()
            return
        mtga_dir = self._selected_mtga_dir(required=False)
        if not mtga_dir:
            QMessageBox.critical(self, "Restart MTGA Failed", "MTGA install folder is not set.")
            return
        if self._state and self._state.mtga_running and not self._confirm_close_mtga(action_label="restart MTGA"):
            return
        try:
            target = restart_mtga(mtga_dir)
            self.refresh_state()
            QMessageBox.information(self, "mtgacoach", f"MTGA launched from:\n{target}")
        except Exception as exc:
            QMessageBox.critical(self, "Restart MTGA Failed", str(exc))

    def _open_mtga(self) -> None:
        try:
            open_path(self._selected_mtga_dir())
        except Exception:
            pass

    def _open_player_log(self) -> None:
        if self._state:
            open_path(self._state.player_log)

    def _open_bepinex_log(self) -> None:
        if self._state and self._state.bepinex_log:
            open_path(self._state.bepinex_log)

    def _selected_mtga_dir(self, required: bool = True) -> Optional[str]:
        text = self.mtga_path_box.text().strip()
        if text:
            return text
        if self._state and self._state.mtga_dir:
            return self._state.mtga_dir
        if required:
            raise RuntimeError("MTGA install folder is not set")
        return None

    def _append_fix_log(self, message: str) -> None:
        self.fix_log.appendPlainText(message)
        QApplication.processEvents()

    def _set_fix_step(self, message: str) -> None:
        self.fix_status.setText(message)
        QApplication.processEvents()

    def _set_status(self, key: str, text: str, level: str) -> None:
        label = self._status_labels.get(key)
        if label is None:
            return
        colors = {
            "ok": "#245c3c",
            "warn": "#8a5a00",
            "error": "#8d1f1f",
            "default": "#334e68",
        }
        label.setText(text)
        label.setStyleSheet(f"color: {colors.get(level, colors['default'])};")

    def _format_python_status(self, state: RuntimeState) -> str:
        if not state.python_exe:
            return "Missing"

        text = f"{state.python_exe} [{state.python_source}]"
        if not state.python_ready:
            detail = state.python_ready_detail.strip().splitlines()[-1] if state.python_ready_detail.strip() else "run Setup environment"
            text += f" (setup required: {detail})"
        return text

    def _update_button_states(self, state: RuntimeState) -> None:
        busy = self._is_busy()
        self._set_busy(busy)
        self.restart_button.setEnabled(state.is_fully_provisioned and not busy)
        self.autopilot_button.setEnabled(state.is_fully_provisioned and not busy)
        self.close_mtga_button.setEnabled(state.mtga_running and not busy)
        self.restart_mtga_button.setEnabled(bool(state.mtga_exe_path) and not busy)

    def _set_busy(self, busy: bool) -> None:
        for button in self._busy_buttons:
            button.setEnabled(not busy)

    def _show_busy_message(self) -> None:
        QMessageBox.information(self, "mtgacoach", "Setup is already running. Wait for it to finish first.")

    def _is_busy(self) -> bool:
        return self._operation_in_progress or self._setup_process is not None

    def _confirm_close_mtga(self, action_label: str = "close MTGA") -> bool:
        return (
            QMessageBox.question(
                self,
                "Close MTGA",
                f"MTGA must close before mtgacoach can {action_label}. Close it now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            == QMessageBox.Yes
        )

    def _run_fix_sequence(self) -> None:
        try:
            self._set_fix_step("Scanning...")
            state = self.refresh_state()
            issues = ", ".join(state.issues) or "none"
            self._append_fix_log(f"Found {len(state.issues)} issue(s): {issues}")

            if state.is_fully_provisioned and not state.restart_mtga_required and not state.issues:
                self._append_fix_log("[ok] System is fully provisioned.")
                self._set_fix_step("All good")
                self._finish_fix_flow(True, "System is fully provisioned.")
                return

            if state.python_exe is None:
                self._append_fix_log("[!!] Python 3.10+ was not found.")
                self._append_fix_log("[..] Use the Python Downloads button, install Python, then retry.")
                self._set_fix_step("Blocked: install Python")
                self._finish_fix_flow(False, "Python 3.10+ is required.")
                return

            if not state.has_ready_python_runtime:
                self._append_fix_log("[..] Running Setup environment to create the runtime and install dependencies.")
                self._start_setup_mode(
                    mode="setup_environment",
                    step_label="Setting up environment...",
                    success_message=None,
                    failure_title="Setup Environment Failed",
                    completion=self._resume_fix_after_setup,
                    clear_log=False,
                )
                return

            self._finish_bridge_repair()
        except Exception as exc:
            self._set_fix_step("Error")
            self._append_fix_log(f"[!!] Fix failed: {exc}")
            self._finish_fix_flow(False, str(exc))
            QMessageBox.critical(self, "Repair Failed", str(exc))

    def _resume_fix_after_setup(self, success: bool) -> None:
        if not success:
            self._finish_fix_flow(False, "Setup environment failed.")
            return
        self._append_fix_log("[ok] Environment setup completed.")
        try:
            self._finish_bridge_repair()
        except Exception as exc:
            self._set_fix_step("Error")
            self._append_fix_log(f"[!!] Fix failed: {exc}")
            self._finish_fix_flow(False, str(exc))
            QMessageBox.critical(self, "Repair Failed", str(exc))

    def _finish_bridge_repair(self) -> None:
        state = self.refresh_state()
        mtga_dir = state.mtga_dir or self._selected_mtga_dir(required=False)
        if not mtga_dir:
            self._append_fix_log("[!!] MTGA install not detected.")
            self._set_fix_step("Blocked: no MTGA path")
            self._finish_fix_flow(False, "MTGA install folder is not set.")
            return

        if state.mtga_running:
            self._append_fix_log("[..] MTGA is running and must close before bridge repair can continue.")
            if not self._confirm_close_mtga(action_label="repair the MTGA bridge"):
                self._set_fix_step("Blocked: MTGA running")
                self._finish_fix_flow(False, "Close MTGA and retry.")
                return
            if close_mtga():
                self._append_fix_log("[ok] MTGA closed.")
            state = self.refresh_state()

        if not state.bepinex_installed:
            self._set_fix_step("Installing BepInEx...")
            target = install_bepinex(mtga_dir)
            self._append_fix_log(f"[ok] BepInEx installed at {target}")
        else:
            self._append_fix_log("[ok] BepInEx already installed.")

        state = self.refresh_state()
        plugin_needs_update = self._plugin_needs_update(state)
        if not state.plugin_installed or plugin_needs_update:
            self._set_fix_step("Installing bridge plugin...")
            target = install_plugin(mtga_dir)
            action = "updated" if plugin_needs_update else "installed"
            self._append_fix_log(f"[ok] Bridge plugin {action} at {target}")
        else:
            self._append_fix_log("[ok] Bridge plugin already installed.")

        state = self.refresh_state()
        if state.restart_mtga_required:
            self._append_fix_log("[..] Restart MTGA so BepInEx loads the updated bridge plugin.")

        if state.is_fully_provisioned:
            self._set_fix_step("Setup complete")
            message = "Setup complete. Restart MTGA before coaching." if state.restart_mtga_required else "System is fully provisioned and ready."
            self._append_fix_log(f"[ok] {message}")
            self._finish_fix_flow(True, message)
            return

        if state.is_launchable:
            self._set_fix_step("Python ready")
            message = "Python runtime is ready. Finish the MTGA bridge steps shown above."
            self._append_fix_log(f"[..] {message}")
            self._finish_fix_flow(False, message)
            return

        self._set_fix_step("Some issues remain")
        message = ", ".join(state.issues) or "Some issues remain."
        self._append_fix_log(f"[!!] {message}")
        self._finish_fix_flow(False, message)

    def _plugin_needs_update(self, state: RuntimeState) -> bool:
        if not (
            state.plugin_built
            and state.plugin_build_path
            and state.plugin_install_path
        ):
            return False
        try:
            return Path(state.plugin_build_path).stat().st_mtime > Path(state.plugin_install_path).stat().st_mtime
        except OSError:
            return False

    def _finish_fix_flow(self, success: bool, message: str) -> None:
        self._operation_in_progress = False
        self._set_busy(False)
        self.refresh_state()
        guided = self._guided_setup_active
        self._guided_setup_active = False
        if guided:
            self.guided_setup_finished.emit(success, message)

    def _start_setup_mode(
        self,
        *,
        mode: str,
        step_label: str,
        success_message: Optional[str],
        failure_title: str,
        completion: Optional[Callable[[bool], None]] = None,
        clear_log: bool = True,
    ) -> None:
        if self._setup_process is not None:
            self._show_busy_message()
            return

        if clear_log:
            self.fix_log.clear()
        self._append_fix_log(f"[..] Starting {step_label.rstrip('.').lower()}...")
        self._set_fix_step(step_label)
        self._operation_in_progress = True
        self._set_busy(True)
        self._setup_completion = completion
        self._setup_success_message = success_message
        self._setup_failure_title = failure_title
        self._setup_mode_label = step_label.rstrip(".")
        self._setup_stdout_buffer = ""
        self._setup_stderr_buffer = ""

        try:
            program, args, env = get_setup_wizard_command(mode)
        except Exception as exc:
            if completion is not None:
                completion(False)
            else:
                self._operation_in_progress = False
                self._set_busy(False)
            QMessageBox.critical(self, failure_title, str(exc))
            return

        process = QProcess(self)
        proc_env = QProcessEnvironment.systemEnvironment()
        for key, value in env.items():
            proc_env.insert(key, value)
        process.setProcessEnvironment(proc_env)
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[3]))
        process.setProgram(program)
        process.setArguments(args)
        process.readyReadStandardOutput.connect(self._on_setup_stdout_ready)
        process.readyReadStandardError.connect(self._on_setup_stderr_ready)
        process.finished.connect(self._on_setup_finished)
        process.errorOccurred.connect(self._on_setup_error)
        process.start()
        self._setup_process = process

    def _on_setup_stdout_ready(self) -> None:
        self._drain_setup_stream(standard_error=False)

    def _on_setup_stderr_ready(self) -> None:
        self._drain_setup_stream(standard_error=True)

    def _drain_setup_stream(self, *, standard_error: bool) -> None:
        process = self._setup_process
        if process is None:
            return

        if standard_error:
            chunk = bytes(process.readAllStandardError()).decode("utf-8", errors="replace")
            self._setup_stderr_buffer += chunk
            buffer_name = "_setup_stderr_buffer"
        else:
            chunk = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
            self._setup_stdout_buffer += chunk
            buffer_name = "_setup_stdout_buffer"

        buffer = getattr(self, buffer_name)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r").strip()
            if line:
                prefix = "[stderr] " if standard_error else ""
                self._append_fix_log(prefix + line)
        setattr(self, buffer_name, buffer)

    def _on_setup_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        self._drain_setup_stream(standard_error=False)
        self._drain_setup_stream(standard_error=True)

        if self._setup_stdout_buffer.strip():
            self._append_fix_log(self._setup_stdout_buffer.strip())
        if self._setup_stderr_buffer.strip():
            self._append_fix_log("[stderr] " + self._setup_stderr_buffer.strip())

        process = self._setup_process
        self._setup_process = None
        if process is not None:
            process.deleteLater()

        self._setup_stdout_buffer = ""
        self._setup_stderr_buffer = ""
        self.refresh_state()

        completion = self._setup_completion
        success_message = self._setup_success_message
        failure_title = self._setup_failure_title
        mode_label = self._setup_mode_label
        self._setup_completion = None
        self._setup_success_message = None
        self._setup_failure_title = "Setup Failed"
        self._setup_mode_label = ""

        success = exit_code == 0
        if success:
            self._append_fix_log(f"[ok] {mode_label} completed.")
            if completion is not None:
                completion(True)
                return
            self._operation_in_progress = False
            self._set_busy(False)
            if success_message:
                QMessageBox.information(self, "mtgacoach", success_message)
            return

        self._set_fix_step("Error")
        self._append_fix_log(f"[!!] {mode_label} failed with exit code {exit_code}.")
        if completion is not None:
            completion(False)
        else:
            self._operation_in_progress = False
            self._set_busy(False)
        QMessageBox.critical(self, failure_title, f"{mode_label} failed with exit code {exit_code}.")

    def _on_setup_error(self, _error: QProcess.ProcessError) -> None:
        process = self._setup_process
        if process is None:
            return
        message = process.errorString() or "Failed to start setup process"
        self._append_fix_log(f"[!!] {message}")
