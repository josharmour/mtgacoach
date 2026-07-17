"""Movable, resizable advice panel that floats over the MTGA window.

Replaces the click-through painted panel previously drawn by MatchOverlayWindow.
This panel is its own top-level frameless QWidget — fully mouse-interactive —
so the user can drag it to reposition and use the bottom-right grip to resize.

Position and size are persisted to overlay_calibration.json as fractions of
the MTGA client rect, so the panel keeps the same relative position when MTGA
is resized or moved between monitors.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizeGrip, QWidget

try:
    import win32con
    import win32gui
except ImportError:
    win32con = None
    win32gui = None

logger = logging.getLogger(__name__)


# Default placement (fractions of MTGA client rect): lower-left, ~quarter wide.
_DEFAULT_FRAC_X = 0.015
_DEFAULT_FRAC_Y = 0.72
_DEFAULT_FRAC_W = 0.26
_DEFAULT_FRAC_H = 0.22

# Floor sizes in pixels so a tiny MTGA window can't shrink it to nothing.
_MIN_W_PX = 180
_MIN_H_PX = 80

_GRIP_PX = 14
_TITLE_H_PX = 18  # extends drag-target above the body for clarity


class AdvicePanelWindow(QWidget):
    """Frameless, always-on-top, movable + resizable advice panel.

    Owner (MatchOverlayWindow) calls:
      - apply_mtga_rect(rect)  every tick — repositions panel to its
        saved fraction of the new MTGA rect.
      - set_advice(text, seat) when the coach has new advice.
      - clear_advice() when the match ends.
      - hide()/show() based on MTGA visibility / user toggle.
    """

    ADVICE_TTL_SEC = 25.0

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._advice_text: str = ""
        self._advice_seat: str = ""
        self._advice_expire_at: float = 0.0

        self._frac_x: float = _DEFAULT_FRAC_X
        self._frac_y: float = _DEFAULT_FRAC_Y
        self._frac_w: float = _DEFAULT_FRAC_W
        self._frac_h: float = _DEFAULT_FRAC_H

        self._mtga_rect: Optional[QRect] = None
        self._drag_origin: Optional[QPoint] = None
        self._drag_start_pos: Optional[QPoint] = None
        # Suspend MTGA-rect-driven repositioning while the user actively drags
        # or resizes. Without this, _tick would keep snapping the panel back
        # to the saved fraction every 250 ms.
        self._user_interacting: bool = False
        self._save_pending: bool = False

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setMinimumSize(_MIN_W_PX, _MIN_H_PX)

        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(_GRIP_PX, _GRIP_PX)
        self._size_grip.setCursor(Qt.CursorShape.SizeFDiagCursor)

        # Auto-fade timer — repaint at low cadence to drive TTL expiry.
        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self.update)
        self._fade_timer.start(500)

        self._load_geometry()

    # -- Public API ---------------------------------------------------------

    def set_advice(self, text: str, seat_info: str = "") -> None:
        text = (text or "").strip()
        if not text:
            self._advice_text = ""
            self._advice_seat = ""
            self._advice_expire_at = 0.0
        else:
            self._advice_text = text
            self._advice_seat = seat_info
            self._advice_expire_at = time.time() + self.ADVICE_TTL_SEC
        self.update()

    def clear_advice(self) -> None:
        self.set_advice("")

    def reset_to_default(self) -> None:
        """Restore default fractions, reposition immediately, persist."""
        self._frac_x = _DEFAULT_FRAC_X
        self._frac_y = _DEFAULT_FRAC_Y
        self._frac_w = _DEFAULT_FRAC_W
        self._frac_h = _DEFAULT_FRAC_H
        if self._mtga_rect is not None:
            self.setGeometry(self._geom_from_fractions(self._mtga_rect))
        self._save_geometry()

    def apply_mtga_rect(self, rect: Optional[QRect]) -> None:
        """Reposition the panel to its saved fraction of the new MTGA rect.

        No-op if the user is currently dragging/resizing.
        """
        self._mtga_rect = rect
        if self._user_interacting or rect is None:
            return
        new_geom = self._geom_from_fractions(rect)
        if self.geometry() != new_geom:
            self.setGeometry(new_geom)

    # -- Persistence --------------------------------------------------------

    def _calibration_path(self) -> Path:
        try:
            from arenamcp.desktop.runtime import get_runtime_root
            root = Path(get_runtime_root())
        except Exception:
            root = Path.home() / ".mtgacoach"
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return root / "overlay_calibration.json"

    def _load_geometry(self) -> None:
        try:
            p = self._calibration_path()
            if not p.exists():
                return
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            panel = data.get("advice_panel") or {}
            self._frac_x = self._clamp01(panel.get("frac_x", self._frac_x))
            self._frac_y = self._clamp01(panel.get("frac_y", self._frac_y))
            self._frac_w = self._clamp_size(panel.get("frac_w", self._frac_w))
            self._frac_h = self._clamp_size(panel.get("frac_h", self._frac_h))
        except Exception as e:
            logger.debug(f"advice_panel: load geometry failed: {e}")

    def _save_geometry(self) -> None:
        try:
            p = self._calibration_path()
            existing: dict[str, Any] = {}
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            existing["advice_panel"] = {
                "frac_x": round(self._frac_x, 4),
                "frac_y": round(self._frac_y, 4),
                "frac_w": round(self._frac_w, 4),
                "frac_h": round(self._frac_h, 4),
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
            logger.debug(
                "advice_panel saved: x=%.3f y=%.3f w=%.3f h=%.3f",
                self._frac_x, self._frac_y, self._frac_w, self._frac_h,
            )
        except Exception as e:
            logger.debug(f"advice_panel: save geometry failed: {e}")

    @staticmethod
    def _clamp01(v: Any) -> float:
        try:
            return max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _clamp_size(v: Any) -> float:
        try:
            return max(0.05, min(1.0, float(v)))
        except (TypeError, ValueError):
            return 0.2

    # -- Geometry math ------------------------------------------------------

    def _geom_from_fractions(self, mtga: QRect) -> QRect:
        w = max(_MIN_W_PX, int(round(mtga.width() * self._frac_w)))
        h = max(_MIN_H_PX, int(round(mtga.height() * self._frac_h)))
        x = mtga.left() + int(round(mtga.width() * self._frac_x))
        y = mtga.top() + int(round(mtga.height() * self._frac_y))
        # Keep panel fully inside MTGA rect.
        x = max(mtga.left(), min(mtga.right() - w, x))
        y = max(mtga.top(), min(mtga.bottom() - h, y))
        return QRect(x, y, w, h)

    def _commit_fractions_from_geometry(self) -> None:
        """Recompute frac_* from current absolute geometry given the active
        MTGA rect. No-op if MTGA rect isn't known."""
        mtga = self._mtga_rect
        if mtga is None or mtga.width() <= 0 or mtga.height() <= 0:
            return
        cur = self.geometry()
        self._frac_x = self._clamp01(
            (cur.left() - mtga.left()) / mtga.width()
        )
        self._frac_y = self._clamp01(
            (cur.top() - mtga.top()) / mtga.height()
        )
        self._frac_w = self._clamp_size(cur.width() / mtga.width())
        self._frac_h = self._clamp_size(cur.height() / mtga.height())

    # -- Mouse interaction --------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        # Don't initiate drag if click landed on the size grip.
        if self._size_grip.geometry().contains(event.position().toPoint()):
            super().mousePressEvent(event)
            return
        self._user_interacting = True
        self._drag_origin = event.globalPosition().toPoint()
        self._drag_start_pos = self.pos()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is None or self._drag_start_pos is None:
            super().mouseMoveEvent(event)
            return
        delta = event.globalPosition().toPoint() - self._drag_origin
        new_pos = self._drag_start_pos + delta
        # Constrain inside MTGA rect.
        mtga = self._mtga_rect
        if mtga is not None:
            new_pos.setX(
                max(mtga.left(), min(mtga.right() - self.width(), new_pos.x()))
            )
            new_pos.setY(
                max(mtga.top(), min(mtga.bottom() - self.height(), new_pos.y()))
            )
        self.move(new_pos)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._drag_origin is not None:
            self._drag_origin = None
            self._drag_start_pos = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            self._commit_fractions_from_geometry()
            self._save_geometry()
        self._user_interacting = False
        event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Park the size grip in the bottom-right corner.
        self._size_grip.move(
            self.width() - _GRIP_PX,
            self.height() - _GRIP_PX,
        )
        # If the resize was driven by the user (size grip), persist.
        # We can't trivially distinguish user drag from programmatic
        # setGeometry, so we save whenever resize happens during user
        # interaction OR shortly after (debounce via _save_pending).
        if self._user_interacting or self._size_grip.underMouse():
            self._user_interacting = True
            self._commit_fractions_from_geometry()
            if not self._save_pending:
                self._save_pending = True
                QTimer.singleShot(300, self._flush_save)

    def _flush_save(self) -> None:
        self._save_pending = False
        if not self.isVisible():
            return
        self._commit_fractions_from_geometry()
        self._save_geometry()
        # Drop the interaction flag a moment after the last resize so the
        # follow tick can resume tracking MTGA.
        QTimer.singleShot(150, lambda: setattr(self, "_user_interacting", False))

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._force_topmost()

    def _force_topmost(self) -> None:
        if os.name != "nt" or win32gui is None or win32con is None:
            self.raise_()
            return
        try:
            hwnd = int(self.winId())
            HWND_TOPMOST = -1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOACTIVATE = 0x0010
            SWP_SHOWWINDOW = 0x0040
            win32gui.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        except Exception:
            try:
                self.raise_()
            except Exception:
                pass

    # -- Painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Faded background even when no advice — gives the user a target to
        # grab and drag, plus shows the panel exists.
        bg = QColor(18, 22, 28, 225)
        accent = QColor(105, 212, 108)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 8, 8)
        painter.setPen(QPen(accent, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 8, 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRect(0, 0, 3, self.height())

        # Title bar.
        title_font = QFont(); title_font.setPixelSize(11); title_font.setBold(True)
        painter.setFont(title_font)
        title_metrics = painter.fontMetrics()
        title = f"COACH{f'  ·  {self._advice_seat}' if self._advice_seat else ''}"
        painter.setPen(QColor(148, 163, 184))
        painter.drawText(12, 4 + title_metrics.height() - 3, title)

        # Body — only render text if advice present and unexpired.
        if not self._advice_text:
            painter.setPen(QColor(120, 130, 140))
            body_font = QFont(); body_font.setPixelSize(12)
            painter.setFont(body_font)
            painter.drawText(
                QRect(12, 4 + title_metrics.height() + 4,
                      self.width() - 24,
                      self.height() - (4 + title_metrics.height() + 4) - _GRIP_PX),
                int(Qt.TextFlag.TextWordWrap),
                "(no advice yet — drag me, resize via the corner grip)",
            )
            return

        if self._advice_expire_at and time.time() > self._advice_expire_at:
            return

        body_font = QFont(); body_font.setPixelSize(15)
        painter.setFont(body_font)
        painter.setPen(QColor(240, 244, 248))
        body_rect = QRect(
            12,
            4 + title_metrics.height() + 6,
            self.width() - 24,
            self.height() - (4 + title_metrics.height() + 6) - _GRIP_PX - 4,
        )
        painter.drawText(
            body_rect,
            int(Qt.TextFlag.TextWordWrap),
            self._advice_text,
        )
