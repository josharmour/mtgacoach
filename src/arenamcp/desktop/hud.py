from __future__ import annotations

import os
from typing import Any, Optional

from PySide6.QtCore import QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

try:
    import win32con
    import win32gui
except ImportError:
    win32con = None
    win32gui = None

try:
    import pygetwindow as gw
except (ImportError, NotImplementedError):
    gw = None


class HudWindow(QWidget):
    def setVisible(self, visible: bool) -> None:
        if visible and os.name != "nt":
            return
        super().setVisible(visible)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._is_click_through = False
        self._should_show = False

        # Frameless, Always on Top, Tool window (doesn't show in taskbar)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self._build_ui()

        # Timer to follow MTGA window.
        self._follow_timer = QTimer(self)
        self._follow_timer.timeout.connect(self._follow_mtga)
        self._follow_timer.start(1000)

    def closeEvent(self, event) -> None:
        if self._follow_timer.isActive():
            self._follow_timer.stop()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.container = QFrame()
        self.container.setStyleSheet(
            """
            QFrame {
                background-color: rgba(20, 20, 25, 180);
                border: 1px solid rgba(100, 100, 120, 100);
                border-radius: 8px;
            }
            """
        )
        container_layout = QVBoxLayout(self.container)

        self.title_label = QLabel("MTGA Coach HUD")
        self.title_label.setStyleSheet("color: #8a8a8a; font-size: 10px; font-weight: bold;")
        container_layout.addWidget(self.title_label)

        self.advice_label = QLabel("Waiting for game state...")
        self.advice_label.setStyleSheet("color: white; font-size: 14px;")
        self.advice_label.setWordWrap(True)
        container_layout.addWidget(self.advice_label)

        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #64c8dc; font-size: 12px;")
        container_layout.addWidget(self.stats_label)

        layout.addWidget(self.container)
        self.resize(300, 150)

    def update_advice(self, text: str) -> None:
        self.advice_label.setText(text)
        self.adjustSize()

    def update_stats(self, text: str) -> None:
        self.stats_label.setText(text)
        self.adjustSize()

    def hide_overlay(self) -> None:
        self._should_show = False
        if self.isVisible():
            self.hide()

    def set_click_through(self, enabled: bool = True) -> None:
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, enabled)
        if os.name != "nt" or win32gui is None or win32con is None:
            return

        self._is_click_through = enabled
        hwnd = int(self.winId())
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if enabled:
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                style | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED,
            )
        else:
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                style & ~win32con.WS_EX_TRANSPARENT,
            )

    def _follow_mtga(self) -> None:
        if os.name != "nt":
            if self.isVisible():
                self.hide()
            return

        if gw is None:
            return

        try:
            windows = [window for window in gw.getWindowsWithTitle("MTGA") if window.title == "MTGA"]
            if not windows:
                if self.isVisible():
                    self.hide()
                return

            mtga = windows[0]
            if mtga.isMinimized or not self._should_show:
                if self.isVisible():
                    self.hide()
                return

            if not self.isVisible():
                self.show()
                if not self._is_click_through:
                    self.set_click_through(True)

            margin = 10
            target_x = mtga.left + margin
            target_y = mtga.top + margin
            if self.pos() != QPoint(target_x, target_y):
                self.move(target_x, target_y)
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.set_click_through(True)


class DraftHudWindow(HudWindow):
    """Enhanced draft overlay with card rankings, recent advice, and controls."""

    command_requested = Signal(str)
    debug_report_requested = Signal()
    card_overlay_toggled = Signal(bool)
    calibration_toggled = Signal(bool)

    _MAX_ADVICE = 3

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        self._drag_pos: Optional[QPoint] = None
        self._user_offset: Optional[QPoint] = None
        self._recent_advice: list[str] = []
        self._ap_on = False
        super().__init__(parent)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.container = QFrame()
        self.container.setStyleSheet(
            """
            QFrame {
                background-color: rgba(20, 20, 25, 210);
                border: 1px solid rgba(100, 100, 120, 150);
                border-radius: 8px;
            }
            """
        )
        container_layout = QVBoxLayout(self.container)
        container_layout.setSpacing(4)

        # -- Title bar (draggable) --
        self.title_label = QLabel("Draft Assistant")
        self.title_label.setStyleSheet("color: #8a8a8a; font-size: 11px; font-weight: bold;")
        self.title_label.setCursor(Qt.CursorShape.OpenHandCursor)
        container_layout.addWidget(self.title_label)

        # -- Card rankings --
        self.cards_layout = QVBoxLayout()
        self.cards_layout.setSpacing(2)
        container_layout.addLayout(self.cards_layout)

        # -- Separator --
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: rgba(100, 100, 120, 80);")
        container_layout.addWidget(sep)

        # -- Recent advice --
        self.advice_label = QLabel("")
        self.advice_label.setWordWrap(True)
        self.advice_label.setStyleSheet("color: #b0e0b0; font-size: 11px;")
        self.advice_label.setMaximumHeight(80)
        container_layout.addWidget(self.advice_label)

        # -- Control buttons --
        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)

        btn_style = (
            "QPushButton { background: rgba(60,60,70,200); color: #ccc; "
            "border: 1px solid rgba(100,100,120,120); border-radius: 4px; "
            "font-size: 11px; padding: 3px 8px; } "
            "QPushButton:hover { background: rgba(80,80,95,220); color: white; } "
            "QPushButton:checked { background: rgba(50,120,60,220); color: #4ade80; "
            "border-color: #4ade80; }"
        )

        self.ap_button = QPushButton("AP")
        self.ap_button.setCheckable(True)
        self.ap_button.setToolTip("Toggle Autopilot")
        self.ap_button.setStyleSheet(btn_style)
        self.ap_button.clicked.connect(lambda: self.command_requested.emit("toggle_autopilot"))
        btn_row.addWidget(self.ap_button)

        mute_btn = QPushButton("Mute")
        mute_btn.setToolTip("Toggle Mute")
        mute_btn.setStyleSheet(btn_style)
        mute_btn.clicked.connect(lambda: self.command_requested.emit("toggle_mute"))
        btn_row.addWidget(mute_btn)

        debug_btn = QPushButton("Bug")
        debug_btn.setToolTip("Submit Debug Report")
        debug_btn.setStyleSheet(btn_style)
        debug_btn.clicked.connect(self.debug_report_requested.emit)
        btn_row.addWidget(debug_btn)

        self.cards_button = QPushButton("Cards")
        self.cards_button.setCheckable(True)
        self.cards_button.setChecked(True)  # Card overlay on by default
        self.cards_button.setToolTip("Toggle per-card score badges")
        self.cards_button.setStyleSheet(btn_style)
        self.cards_button.clicked.connect(
            lambda: self.card_overlay_toggled.emit(self.cards_button.isChecked())
        )
        btn_row.addWidget(self.cards_button)

        self.calib_button = QPushButton("Calib")
        self.calib_button.setCheckable(True)
        self.calib_button.setToolTip("Show grid outline (diagnose alignment)")
        self.calib_button.setStyleSheet(btn_style)
        self.calib_button.clicked.connect(
            lambda: self.calibration_toggled.emit(self.calib_button.isChecked())
        )
        btn_row.addWidget(self.calib_button)

        btn_row.addStretch()
        container_layout.addLayout(btn_row)

        layout.addWidget(self.container)
        self.resize(320, 450)

    # -- Public API for coach_tab integration ---------------------------

    def add_advice(self, text: str) -> None:
        """Append a new advice message (keeps last few)."""
        # Truncate long advice for the overlay
        short = text[:200] + "..." if len(text) > 200 else text
        self._recent_advice.append(short)
        if len(self._recent_advice) > self._MAX_ADVICE:
            self._recent_advice = self._recent_advice[-self._MAX_ADVICE:]
        self._refresh_advice_label()

    def update_autopilot(self, enabled: bool) -> None:
        """Sync AP button checked state."""
        self._ap_on = enabled
        self.ap_button.setChecked(enabled)

    def _refresh_advice_label(self) -> None:
        parts = []
        for i, msg in enumerate(reversed(self._recent_advice)):
            opacity = "color: #b0e0b0;" if i == 0 else "color: #708070;"
            parts.append(f"<span style='{opacity} font-size:11px;'>{msg}</span>")
        self.advice_label.setText("<br>".join(parts))

    # -- Override: draft HUD should NOT be click-through ----------------

    def showEvent(self, event) -> None:
        # Skip the parent's set_click_through(True) — draft HUD must be draggable
        QWidget.showEvent(self, event)

    # -- Drag support ---------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.title_label.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_pos is not None:
            self._drag_pos = None
            self.title_label.setCursor(Qt.CursorShape.OpenHandCursor)
            self._save_user_offset()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _save_user_offset(self) -> None:
        """Store the current position as an offset from MTGA window top-left."""
        if os.name != "nt":
            try:
                from arenamcp.desktop.runtime import get_linux_window_geometry
                geom = get_linux_window_geometry("MTGA")
                if geom:
                    self._user_offset = QPoint(
                        self.x() - geom["left"],
                        self.y() - geom["top"],
                    )
            except Exception:
                pass
            return

        if gw is None:
            return
        try:
            windows = [w for w in gw.getWindowsWithTitle("MTGA") if w.title == "MTGA"]
            if windows:
                mtga = windows[0]
                self._user_offset = QPoint(
                    self.x() - mtga.left,
                    self.y() - mtga.top,
                )
        except Exception:
            pass

    # -- Override _follow_mtga to respect user drag offset ---------------

    def _follow_mtga(self) -> None:
        if os.name != "nt":
            try:
                from arenamcp.desktop.runtime import get_linux_window_geometry
                geom = get_linux_window_geometry("MTGA")
                if not geom or not self._should_show:
                    if self.isVisible():
                        self.hide()
                    return

                if geom.get("is_minimized"):
                    if self.isVisible():
                        self.hide()
                    return

                if not self.isVisible():
                    self.show()

                if self._user_offset is not None:
                    target_x = geom["left"] + self._user_offset.x()
                    target_y = geom["top"] + self._user_offset.y()
                else:
                    margin = 10
                    target_x = geom["left"] + margin
                    target_y = geom["top"] + margin

                if self.pos() != QPoint(target_x, target_y):
                    self.move(target_x, target_y)
            except Exception:
                pass
            return

        if gw is None:
            return

        try:
            windows = [w for w in gw.getWindowsWithTitle("MTGA") if w.title == "MTGA"]
            if not windows:
                if self.isVisible():
                    self.hide()
                return

            mtga = windows[0]
            if mtga.isMinimized or not self._should_show:
                if self.isVisible():
                    self.hide()
                return

            if not self.isVisible():
                self.show()

            if self._user_offset is not None:
                target_x = mtga.left + self._user_offset.x()
                target_y = mtga.top + self._user_offset.y()
            else:
                margin = 10
                target_x = mtga.left + margin
                target_y = mtga.top + margin

            if self.pos() != QPoint(target_x, target_y):
                self.move(target_x, target_y)
        except Exception:
            pass

    # -- Draft state update ---------------------------------------------

    def update_draft_state(self, draft_info: dict[str, Any]) -> None:
        self._should_show = bool(draft_info.get("is_active")) and bool(draft_info.get("cards"))
        if not self._should_show:
            self.hide_overlay()
            return

        pack = draft_info.get("pack_number", 0)
        pick = draft_info.get("pick_number", 0)
        self.title_label.setText(f"Draft Pack {pack} Pick {pick}")

        while self.cards_layout.count():
            child = self.cards_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        cards = draft_info.get("cards", [])
        for i, card in enumerate(cards):
            c_name = card.get("name", "Unknown")
            c_wr = card.get("gih_wr")
            c_score = card.get("composite_score")
            c_reason = card.get("pick_reason", "")
            c_tier = card.get("tier")
            c_pair = card.get("best_pair")

            if c_score is not None:
                score_str = f"{c_score:.1f}"
            elif c_wr is not None:
                score_str = f"{c_wr * 100:.1f}%"
            else:
                score_str = "N/A"

            wr_str = f"{c_wr * 100:.0f}%" if c_wr is not None else ""

            # Tier-based color (replaces rank-based coloring for consistency
            # with what's shown on card overlays)
            tier_colors = {
                "FIRE": "#f97316",
                "GOLD": "#facc15",
                "SILVER": "#d1d5db",
                "BRONZE": "#a16207",
                "WEAK": "#6b7280",
            }
            color = tier_colors.get(c_tier, "white")
            if i == 0 and c_tier in ("GOLD", "FIRE"):
                color = "#4ade80"  # Highlight #1 pick extra when it's a strong card

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)

            # Tier badge (small colored chip)
            if c_tier:
                tier_badge = QLabel(c_tier[:1])  # W/B/S/G/F
                tier_badge.setFixedWidth(18)
                tier_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                tier_badge.setStyleSheet(
                    f"background: {color}; color: black; font-size: 10px; "
                    "font-weight: bold; border-radius: 3px; padding: 1px 2px;"
                )
                tier_badge.setToolTip(c_tier)
                row_layout.addWidget(tier_badge)

            name_label = QLabel(c_name)
            name_label.setStyleSheet("color: white; font-size: 13px;")

            if c_score is not None and wr_str:
                stat_text = f"{score_str}  {wr_str}"
            else:
                stat_text = score_str

            # Add best-pair hint for top picks
            if i == 0 and c_pair:
                stat_text = f"{stat_text}  → {c_pair}"
            elif i == 0 and c_reason:
                stat_text = f"{stat_text}  {c_reason}"

            stats_label = QLabel(stat_text)
            stats_label.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: bold;")
            stats_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            row_layout.addWidget(name_label, stretch=1)
            row_layout.addWidget(stats_label)
            self.cards_layout.addWidget(row)

        self.adjustSize()
        if not self.isVisible():
            self.show()
