"""Main application window shell for MTGA Coach."""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QGuiApplication, QShowEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from arenamcp.settings import get_settings

from .coach_session import CoachSession
from .compact_coach import CompactCoachPanel
from .hotkeys import HotkeyManager
from .performance_tab import PerformanceTab
from .repair_tab import RepairTab
from .runtime import open_url, read_version
from .theme import apply_theme, available_themes, load_saved_theme, save_theme, span
from .ui_watchdog import UiAnrWatchdog, WatchdogPingBridge

logger = logging.getLogger(__name__)


def _build_page(title: str, body: QWidget, on_back) -> QWidget:
    """A secondary page: shared header (back + title) above a scrolling body."""
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(8)

    header = QHBoxLayout()
    header.setSpacing(8)
    back = QPushButton("← Back")
    back.setToolTip("Back to the coach")
    back.clicked.connect(on_back)
    header.addWidget(back)
    heading = QLabel(title)
    heading.setProperty("role", "title")
    header.addWidget(heading, 1)
    layout.addLayout(header)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setWidget(body)
    layout.addWidget(scroll, 1)
    return page


class MainWindow(QMainWindow):
    """Top-level application window for the MTGA Coach desktop HUD."""

    _WINDOW_GEOMETRY_KEY = "desktop_window_geometry"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = get_settings()
        self._current_theme = load_saved_theme()
        self._theme_actions: dict[str, QAction] = {}
        self._debug_logging_action: QAction | None = None

        self.setWindowTitle(f"mtgacoach v{read_version()}")

        # Session & Controller
        self._session = CoachSession(self)

        # Setup & Repair sub-interface
        self.repair_tab = RepairTab()
        self.repair_tab.restart_requested.connect(self._restart_coach)

        self._build_central_widget()
        self._build_menus()
        self._apply_window_geometry()
        self._setup_hotkeys()

        # Start coach process automatically — unless the window was already
        # closed (a start after shutdown would orphan a coach process).
        self._closed = False
        QTimer.singleShot(100, self._start_session)

        # ANR Watchdog
        if WatchdogPingBridge is not None:
            self._watchdog_bridge = WatchdogPingBridge(self)
            self._ui_watchdog = UiAnrWatchdog(
                ping_fn=self._watchdog_bridge.ping_requested.emit,
                stall_threshold_s=1.5,
            )
            self._ui_watchdog.start()
        else:
            self._ui_watchdog = None

    def _build_central_widget(self) -> None:
        """Stacked pages: the coach sidebar, then Setup & Repair and Match History."""
        self.coach_panel = CompactCoachPanel(session=self._session, parent=self)
        self.coach_panel.repair_requested.connect(self._show_repair_view)
        self.coach_panel.performance_requested.connect(self._show_performance_view)
        self.coach_panel.restart_requested.connect(self._restart_coach)

        self._stack = QStackedWidget()
        self._stack.addWidget(self.coach_panel)  # Index 0
        self._stack.addWidget(_build_page("Setup & Repair", self.repair_tab, self._show_coach_view))  # 1
        self._stack.addWidget(_build_page("Match History", PerformanceTab(), self._show_coach_view))  # 2
        self.setCentralWidget(self._stack)

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()
        is_mac = sys.platform == "darwin"

        # Tools Menu. Shortcuts are registered app-wide by HotkeyManager, so the
        # menus only *display* them (text after a tab lands in the shortcut
        # column) instead of registering a second, ambiguous QAction shortcut.
        tools_menu = menu_bar.addMenu("Tools")
        repair_act = tools_menu.addAction("Setup && Repair…")
        repair_act.triggered.connect(self._show_repair_view)
        perf_act = tools_menu.addAction("Match History…")
        perf_act.triggered.connect(self._show_performance_view)
        tools_menu.addSeparator()
        advice_act = tools_menu.addAction("Get Advice Now\tF5")
        advice_act.triggered.connect(lambda: self._session.send_command("force_advice"))
        replay_act = tools_menu.addAction("Repeat Last Advice\tF10")
        replay_act.triggered.connect(lambda: self._session.send_command("replay_advice"))
        restart_act = tools_menu.addAction("Restart Coach")
        restart_act.triggered.connect(self._restart_coach)
        if is_mac:
            tools_menu.addSeparator()
            vision_act = tools_menu.addAction("Autoplay Vision Model…")
            vision_act.triggered.connect(self._choose_autoplay_vision_model)
            permissions_act = tools_menu.addAction("Autoplay Permissions…")
            permissions_act.triggered.connect(self._check_autoplay_permissions)
            stop_autoplay_act = tools_menu.addAction("Stop Autoplay\tF11")
            stop_autoplay_act.triggered.connect(lambda: self._session.send_command("force_stop"))
        tools_menu.addSeparator()
        debug_act = tools_menu.addAction("Report a Bug\tF12")
        debug_act.triggered.connect(self._session.trigger_debug_report)

        # View Menu
        view_menu = menu_bar.addMenu("View")
        debug_logging_act = view_menu.addAction("Show Debug Logging")
        debug_logging_act.setCheckable(True)
        debug_logging_act.setChecked(bool(self._settings.get("desktop_debug_logging", False)))
        debug_logging_act.toggled.connect(self.coach_panel.set_debug_logging)
        self._debug_logging_action = debug_logging_act

        theme_menu = view_menu.addMenu("Theme")
        action_group = QActionGroup(self)
        action_group.setExclusive(True)
        for theme_name, theme_label in available_themes():
            action = QAction(theme_label, self)
            action.setCheckable(True)
            action.setChecked(theme_name == self._current_theme)
            action.setData(theme_name)
            action_group.addAction(action)
            theme_menu.addAction(action)
            self._theme_actions[theme_name] = action
        action_group.triggered.connect(self._handle_theme_action)

        # Help Menu
        help_menu = menu_bar.addMenu("Help")
        keys_act = help_menu.addAction("Keyboard Shortcuts")
        keys_act.triggered.connect(self._show_shortcuts)
        docs_act = help_menu.addAction("Online Documentation")
        docs_act.triggered.connect(lambda: open_url("https://mtgacoach.com"))

    def _shortcut_rows(self) -> list[tuple[str, str]]:
        rows = [
            ("F5", "Get advice now"),
            ("F10", "Repeat the last advice"),
            ("F12 or Ctrl+Shift+D", "Report a bug (snapshot + link on the clipboard)"),
        ]
        if sys.platform == "darwin":
            rows.insert(2, ("F11", "Stop autoplay"))
        return rows

    def _show_shortcuts(self) -> None:
        rows = "".join(
            f"<tr><td style='padding:3px 16px 3px 0'>{span(key, weight=700)}</td>"
            f"<td style='padding:3px 0'>{span(action)}</td></tr>"
            for key, action in self._shortcut_rows()
        )
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard Shortcuts")
        box.setTextFormat(Qt.RichText)
        box.setText(f"<table>{rows}</table>")
        box.exec()

    def _choose_autoplay_vision_model(self) -> None:
        model, accepted = QInputDialog.getText(
            self,
            "Native Mac Autoplay",
            "Image-capable model ID served by your endpoint (blank uses the coach model).\n"
            "Autoplay sends Arena window screenshots to this model. Restart the coach after changing it.",
            text=str(self._settings.get("autopilot_vision_model") or ""),
        )
        if accepted:
            self._settings.set("autopilot_vision_model", model.strip() or None)

    def _check_autoplay_permissions(self) -> None:
        from arenamcp.native_mac_input import NativeMacInput

        try:
            NativeMacInput().check_permissions(request=True)
            message = "Accessibility and Screen Recording are allowed. This check does not send game input."
        except RuntimeError as exc:
            message = str(exc)
        QMessageBox.information(self, "Autoplay Permissions", message)

    def _setup_hotkeys(self) -> None:
        self._hotkeys = HotkeyManager(self)
        self._hotkeys.register("F5", lambda: self._session.send_command("force_advice"))
        self._hotkeys.register("F10", lambda: self._session.send_command("replay_advice"))
        self._hotkeys.register("F12", self._session.trigger_debug_report)
        self._hotkeys.register("Ctrl+Shift+D", self._session.trigger_debug_report)
        if sys.platform == "darwin":
            self._hotkeys.register("F11", lambda: self._session.send_command("force_stop"))

    def _show_coach_view(self) -> None:
        self._stack.setCurrentIndex(0)

    def _show_repair_view(self) -> None:
        self._stack.setCurrentIndex(1)

    def _show_performance_view(self) -> None:
        self._stack.setCurrentIndex(2)

    def _restart_coach(self, *args, **kwargs) -> None:
        """Perform a full UI restart, cleanly shutting down and relaunching the entire desktop app."""
        if self._closed:
            return
        self._closed = True
        self.coach_panel.restart_btn.setEnabled(False)
        self.coach_panel.restart_btn.setText("Restarting…")
        logger.info("Restart Coach requested: closing and relaunching desktop UI...")
        if not self.isMaximized() and not self.isMinimized():
            geom = self.frameGeometry()
            self._settings.set(
                self._WINDOW_GEOMETRY_KEY,
                {
                    "x": geom.x(),
                    "y": geom.y(),
                    "width": self.width(),
                    "height": self.height(),
                },
            )

        if hasattr(self, "_ui_watchdog") and self._ui_watchdog:
            self._ui_watchdog.stop()

        self._session.shutdown()
        self._hotkeys.unregister_all()

        app = QApplication.instance()
        if app is not None:
            from .app import RESTART_EXIT_CODE

            app.exit(RESTART_EXIT_CODE)
        else:
            from .app import relaunch_application

            relaunch_application()

    def _handle_theme_action(self, action: QAction) -> None:
        theme_name = str(action.data() or "")
        if not theme_name:
            return
        app = QApplication.instance()
        if app is not None:
            applied = apply_theme(app, theme_name)
            self._current_theme = save_theme(applied)
            for name, act in self._theme_actions.items():
                act.setChecked(name == self._current_theme)

    def _apply_window_geometry(self) -> None:
        self.setMinimumWidth(240)
        saved = self._settings.get(self._WINDOW_GEOMETRY_KEY)
        if isinstance(saved, dict):
            pos_x = saved.get("x")
            pos_y = saved.get("y")
            w = saved.get("width")
            h = saved.get("height")
            if w and h:
                if pos_x is not None and pos_y is not None:
                    screen = (
                        QGuiApplication.screenAt(QPoint(int(pos_x), int(pos_y)))
                        or self.screen()
                        or QGuiApplication.primaryScreen()
                    )
                    if screen is not None:
                        avail = screen.availableGeometry()
                        clamped_x = max(avail.left(), min(int(pos_x), avail.right() - 100))
                        clamped_y = max(avail.top(), min(int(pos_y), avail.bottom() - 100))
                    else:
                        clamped_x = max(0, int(pos_x))
                        clamped_y = max(0, int(pos_y))
                    self.resize(w, h)
                    self.move(clamped_x, clamped_y)
                else:
                    self.resize(w, h)
                return

        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = 300
            height = max(600, avail.height() - 60)
            self.resize(width, height)
            self.move(avail.right() - width - 16, avail.top() + 24)
        else:
            self.resize(300, 900)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._keep_window_on_screen)

    def _keep_window_on_screen(self) -> None:
        if self.isMaximized() or self.isMinimized():
            return
        frame = self.frameGeometry()
        screen = QGuiApplication.screenAt(frame.topLeft()) or self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        border_width = frame.width() - self.width()
        border_height = frame.height() - self.height()
        self.resize(
            min(self.width(), max(1, available.width() - border_width)),
            min(self.height(), max(1, available.height() - border_height)),
        )
        frame = self.frameGeometry()
        self.move(
            max(available.left(), min(frame.x(), available.right() - frame.width() + 1)),
            max(available.top(), min(frame.y(), available.bottom() - frame.height() + 1)),
        )

    def _start_session(self) -> None:
        if not self._closed:
            self._session.start()

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        self._closed = True
        if not self.isMaximized() and not self.isMinimized():
            geom = self.frameGeometry()
            self._settings.set(
                self._WINDOW_GEOMETRY_KEY,
                {
                    "x": geom.x(),
                    "y": geom.y(),
                    "width": self.width(),
                    "height": self.height(),
                },
            )

        if hasattr(self, "_ui_watchdog") and self._ui_watchdog:
            self._ui_watchdog.stop()

        self._session.shutdown()
        self._hotkeys.unregister_all()
        super().closeEvent(event)
