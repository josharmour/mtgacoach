from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QStatusBar, QTabWidget

from arenamcp.settings import get_settings

from .coach_process import CoachProcess
from .coach_tab import CoachTab
from .repair_tab import RepairTab
from .runtime import detect_runtime_state, read_version
from .theme import THEME_LABELS, apply_theme, available_themes, load_saved_theme, save_theme


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._closing = False
        self._launch_flags = (False, False, False)
        self._process: Optional[CoachProcess] = None
        self._settings = get_settings()
        self._theme_actions: dict[str, QAction] = {}
        self._debug_logging_action: Optional[QAction] = None
        self._current_theme = load_saved_theme()

        self.setWindowTitle(f"mtgacoach v{read_version()}")
        self.resize(1400, 980)

        tabs = QTabWidget()
        self.coach_tab = CoachTab()
        self.repair_tab = RepairTab()
        self.repair_tab.restart_requested.connect(self.restart_coach)
        tabs.addTab(self.coach_tab, "Coach")
        tabs.addTab(self.repair_tab, "Repair")
        self.tabs = tabs
        self.setCentralWidget(tabs)

        status_bar = QStatusBar()
        self.setStatusBar(status_bar)
        self._status_bar = status_bar

        refresh_action = QAction("Refresh Status", self)
        refresh_action.triggered.connect(self.refresh_state)
        self.menuBar().addAction(refresh_action)
        self._build_theme_menu()
        self._build_view_menu()

        self.refresh_state()
        self._auto_start()

    def refresh_state(self) -> None:
        self.repair_tab.refresh_state()

    def restart_coach(self, autopilot: bool, dry_run: bool, afk: bool) -> None:
        self._launch_flags = (autopilot, dry_run, afk)
        if self._process is not None:
            self.coach_tab.detach_process()
            self._process.stop()
            self._process.deleteLater()
            self._process = None
        self._start_coach(*self._launch_flags)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._closing = True
        self._current_theme = save_theme(self._current_theme)
        self._settings.set(
            "desktop_debug_logging",
            bool(self._debug_logging_action.isChecked()) if self._debug_logging_action else False,
        )
        if self._process is not None:
            self.coach_tab.detach_process()
            self._process.stop()
            self._process.deleteLater()
            self._process = None
        self.coach_tab.shutdown()
        super().closeEvent(event)

    def _auto_start(self) -> None:
        state = detect_runtime_state()
        if state.python_exe is None:
            self._status_bar.showMessage("Python not found. Go to Repair to set up.")
            self.tabs.setCurrentIndex(1)
            return
        self._start_coach(False, False, False)

    def _start_coach(self, autopilot: bool, dry_run: bool, afk: bool) -> None:
        process = CoachProcess(self)
        process.exited.connect(self._on_process_exited)
        self._process = process
        self.coach_tab.attach_process(process)

        try:
            process.start(autopilot=autopilot, dry_run=dry_run, afk=afk)
            self._sync_runtime_preferences(process)
            self._status_bar.showMessage("Coach is running.")
            self.tabs.setCurrentIndex(0)
        except Exception as exc:
            self.coach_tab.detach_process()
            self._process = None
            self._status_bar.showMessage(f"Coach failed to start: {exc}")
            self.tabs.setCurrentIndex(1)
            QMessageBox.critical(
                self,
                "Coach Launch Failed",
                f"{exc}\n\n{process.last_error}",
            )

    def _on_process_exited(self, exit_code: int) -> None:
        if self._closing:
            return

        self._status_bar.showMessage(f"Coach exited ({exit_code}). Restarting...")
        if self._process is not None:
            self.coach_tab.detach_process()
            self._process.deleteLater()
            self._process = None
        QTimer.singleShot(250, lambda: self._start_coach(*self._launch_flags))

    def _build_theme_menu(self) -> None:
        theme_menu = self.menuBar().addMenu("Theme")
        action_group = QActionGroup(self)
        action_group.setExclusive(True)

        current_theme = self._current_theme
        for theme_name, label in available_themes():
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(theme_name == current_theme)
            action.setData(theme_name)
            action_group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[theme_name] = action
        action_group.triggered.connect(self._handle_theme_action)

    def _handle_theme_action(self, action: QAction) -> None:
        theme_name = str(action.data() or "")
        if theme_name:
            self._apply_theme_choice(theme_name)

    def _build_view_menu(self) -> None:
        view_menu = self.menuBar().addMenu("View")
        debug_action = QAction("Show Debug Logging", self)
        debug_action.setCheckable(True)
        debug_action.setChecked(bool(self._settings.get("desktop_debug_logging", False)))
        debug_action.toggled.connect(self._set_debug_logging)
        view_menu.addAction(debug_action)
        self._debug_logging_action = debug_action
        self._set_debug_logging(debug_action.isChecked())

    def _set_debug_logging(self, enabled: bool) -> None:
        self.coach_tab.set_debug_logging(enabled)
        self._settings.set("desktop_debug_logging", bool(enabled))

    def _apply_theme_choice(self, theme_name: str) -> None:
        app = QApplication.instance()
        if app is None:
            return

        applied = apply_theme(app, theme_name)
        self._current_theme = save_theme(applied)
        for name, action in self._theme_actions.items():
            action.setChecked(name == self._current_theme)
        self.coach_tab.refresh_game_state_view()
        self._status_bar.showMessage(f"Theme: {THEME_LABELS[self._current_theme]}", 3000)

    def _sync_runtime_preferences(self, process: CoachProcess) -> None:
        process.send_payload(
            {
                "cmd": "sync_voice_preferences",
                "voice": self._settings.get("voice"),
                "voice_speed": self._settings.get("voice_speed", 1.0),
                "muted": bool(self._settings.get("muted", False)),
            }
        )
