"""Main application window shell for MTGA Coach."""

from __future__ import annotations

import logging

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMainWindow,
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
from .theme import apply_theme, available_themes, load_saved_theme, save_theme
from .ui_watchdog import UiAnrWatchdog

logger = logging.getLogger(__name__)


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

        # Start coach process automatically
        QTimer.singleShot(100, lambda: self._session.start())

        # ANR Watchdog
        self._ui_watchdog = UiAnrWatchdog(
            ping_fn=lambda cb: QTimer.singleShot(0, cb),
            stall_threshold_s=1.5,
        )
        self._ui_watchdog.start()

    def _build_central_widget(self) -> None:
        """Construct the stacked widget (Compact HUD + Slide-over Repair & History)."""
        self.coach_panel = CompactCoachPanel(session=self._session, parent=self)
        self.coach_panel.repair_requested.connect(self._show_repair_view)
        self.coach_panel.performance_requested.connect(self._show_performance_view)
        self.coach_panel.restart_requested.connect(self._restart_coach)

        # Slide-over Setup & Repair Page
        repair_page = QWidget()
        repair_layout = QVBoxLayout(repair_page)
        repair_layout.setContentsMargins(6, 6, 6, 6)
        repair_layout.setSpacing(6)

        repair_header = QHBoxLayout()
        back_from_repair = QPushButton("← Back to Coach")
        back_from_repair.clicked.connect(self._show_coach_view)
        repair_header.addWidget(back_from_repair)
        repair_header.addStretch()
        repair_layout.addLayout(repair_header)

        repair_scroll = QScrollArea()
        repair_scroll.setWidgetResizable(True)
        repair_scroll.setFrameShape(QFrame.NoFrame)
        repair_scroll.setWidget(self.repair_tab)
        repair_layout.addWidget(repair_scroll)

        # Slide-over Match History Page
        perf_page = QWidget()
        perf_layout = QVBoxLayout(perf_page)
        perf_layout.setContentsMargins(6, 6, 6, 6)
        perf_layout.setSpacing(6)

        perf_header = QHBoxLayout()
        back_from_perf = QPushButton("← Back to Coach")
        back_from_perf.clicked.connect(self._show_coach_view)
        perf_header.addWidget(back_from_perf)
        perf_header.addStretch()
        perf_layout.addLayout(perf_header)

        perf_scroll = QScrollArea()
        perf_scroll.setWidgetResizable(True)
        perf_scroll.setFrameShape(QFrame.NoFrame)
        perf_scroll.setWidget(PerformanceTab())
        perf_layout.addWidget(perf_scroll)

        # Stacked Widget Root
        self._stack = QStackedWidget()
        self._stack.addWidget(self.coach_panel)  # Index 0
        self._stack.addWidget(repair_page)  # Index 1
        self._stack.addWidget(perf_page)  # Index 2
        self.setCentralWidget(self._stack)

    def _build_menus(self) -> None:
        menu_bar = self.menuBar()

        # Tools Menu
        tools_menu = menu_bar.addMenu("Tools")
        repair_act = tools_menu.addAction("Setup && Repair…")
        repair_act.triggered.connect(self._show_repair_view)
        perf_act = tools_menu.addAction("Match History…")
        perf_act.triggered.connect(self._show_performance_view)
        tools_menu.addSeparator()
        restart_act = tools_menu.addAction("Restart Coach")
        restart_act.triggered.connect(self._restart_coach)
        debug_act = tools_menu.addAction("Submit Bug Report (Ctrl+Shift+D)")
        debug_act.triggered.connect(self._session.trigger_debug_report)

        # View Menu
        view_menu = menu_bar.addMenu("View")
        bs_act = view_menu.addAction("Brain Stream Inspector")
        bs_act.setShortcut("Ctrl+B")
        bs_act.triggered.connect(self.coach_panel.toggle_brain_stream)

        debug_logging_act = view_menu.addAction("Show Debug Logging")
        debug_logging_act.setCheckable(True)
        debug_logging_act.setChecked(bool(self._settings.get("desktop_debug_logging", False)))
        debug_logging_act.toggled.connect(self.coach_panel.set_debug_logging)
        self._debug_logging_action = debug_logging_act

        # Theme Menu
        theme_menu = menu_bar.addMenu("Theme")
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
        docs_act = help_menu.addAction("Online Documentation")
        docs_act.triggered.connect(lambda: open_url("https://mtgacoach.com"))
        diag_act = help_menu.addAction("Run Diagnostics")
        diag_act.triggered.connect(self._show_repair_view)

    def _setup_hotkeys(self) -> None:
        self._hotkeys = HotkeyManager(self)
        self._hotkeys.register("F5", lambda: self._session.send_command("force_advice"))
        self._hotkeys.register("F10", lambda: self._session.send_command("replay_advice"))
        self._hotkeys.register("F12", self._session.trigger_debug_report)
        self._hotkeys.register("Ctrl+Shift+D", self._session.trigger_debug_report)

    def _show_coach_view(self) -> None:
        self._stack.setCurrentIndex(0)

    def _show_repair_view(self) -> None:
        self._stack.setCurrentIndex(1)

    def _show_performance_view(self) -> None:
        self._stack.setCurrentIndex(2)

    def _restart_coach(self) -> None:
        self._session.restart()

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
        saved = self._settings.get(self._WINDOW_GEOMETRY_KEY)
        if isinstance(saved, dict):
            pos_x = saved.get("x")
            pos_y = saved.get("y")
            w = saved.get("width")
            h = saved.get("height")
            if w and h:
                if pos_x is not None and pos_y is not None:
                    screen = (
                        QGuiApplication.screenAt(QPoint(int(pos_x), max(0, int(pos_y))))
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
                    self.setGeometry(clamped_x, clamped_y, w, h)
                else:
                    self.resize(w, h)
                return

        self.setMinimumWidth(240)
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            width = 300
            height = max(600, avail.height() - 60)
            self.resize(width, height)
            self.move(avail.right() - width - 16, avail.top() + 24)
        else:
            self.resize(300, 900)

    def closeEvent(self, event: QCloseEvent) -> None:  # type: ignore[override]
        if not self.isMaximized() and not self.isMinimized():
            geom = self.frameGeometry()
            self._settings.set(
                self._WINDOW_GEOMETRY_KEY,
                {
                    "x": max(0, geom.x()),
                    "y": max(0, geom.y()),
                    "width": geom.width(),
                    "height": geom.height(),
                },
            )

        if hasattr(self, "_ui_watchdog") and self._ui_watchdog:
            self._ui_watchdog.stop()

        self._session.shutdown()
        self._hotkeys.unregister_all()
        super().closeEvent(event)
