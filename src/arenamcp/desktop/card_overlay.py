"""Card-position overlay — draws tier badges directly on top of MTGA draft cards.

Inspired by untapped.gg Draftsmith's approach: a borderless transparent
always-on-top window that tracks MTGA's window bounds and positions badges
using viewport-relative coordinates (vh units) based on hardcoded MTGA
draft UI layout constants.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

try:
    import win32con
    import win32gui
except ImportError:
    win32con = None
    win32gui = None

try:
    import pygetwindow as gw
except ImportError:
    gw = None

try:
    from arenamcp.input_controller import find_mtga_hwnd, get_client_rect
except Exception:
    find_mtga_hwnd = None  # type: ignore[assignment]
    get_client_rect = None  # type: ignore[assignment]


# -- Card grid layout constants (in fractions of MTGA render height/width) ----
# These are calibrated to match MTGA's draft UI. MTGA renders internally at
# 1080p, so at 1080p resolution these fractions correspond to vh units.
#
# Values derived from untapped.gg's CardPool.tsx (src/renderer/mtga/limited/draft):
# - 3x5 grid (normal draft): top:16.1vh, height:74.45vh, width:90.8vh, left offset -18.7vh
# - 2x8 grid (PickTwo / column view): top:15.7vh, height:37.1vh, width:118.45vh
#
# Fractions are of MTGA client area height (vh equivalent).

# MTGA's default draft UI is a 3×5 grid for ALL pack sizes up to 15 cards
# (including PickTwo — picked cards leave empty slots rather than reflowing).
# The 2×8 column view is a user-selectable display mode in MTGA, not a
# draft type. Default to 3×5; we can add view-mode detection later.
#
# Fractions are of MTGA client-area HEIGHT (vh equivalent from untapped).
# Format: (rows, cols, top_frac, height_frac, width_frac_of_height, left_offset_of_height)
GRID_LAYOUT_LIST_VIEW = (3, 5, 0.161, 0.7445, 0.908, -0.187)
GRID_LAYOUT_COLUMN_VIEW = (2, 8, 0.157, 0.371, 1.1845, -0.0037)


TIER_COLORS = {
    "FIRE": "#f97316",    # orange
    "GOLD": "#facc15",    # yellow
    "SILVER": "#d1d5db",  # light grey
    "BRONZE": "#a16207",  # brown
    "WEAK": "#6b7280",    # dark grey
}


class CardBadge(QLabel):
    """A single badge overlay shown on a card position."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._score: float = 0.0
        self._tier: str = ""
        self._pair: str = ""

    def set_data(self, score: Optional[float], tier: Optional[str], pair: Optional[str]) -> None:
        self._score = float(score) if score is not None else 0.0
        self._tier = tier or ""
        self._pair = pair or ""
        self._refresh()

    def _refresh(self) -> None:
        bg = TIER_COLORS.get(self._tier, "#1f2937")
        fg = "black" if self._tier in ("GOLD", "SILVER", "FIRE") else "white"
        score_text = f"{int(round(self._score))}" if self._score else "?"
        pair_text = f"<br><span style='font-size:9px;'>→ {self._pair}</span>" if self._pair else ""
        self.setText(f"<b>{score_text}</b>{pair_text}")
        self.setStyleSheet(
            f"""
            background: {bg};
            color: {fg};
            border: 1px solid rgba(0,0,0,180);
            border-radius: 4px;
            padding: 2px 4px;
            font-size: 14px;
            font-weight: bold;
            """
        )


class CardOverlayWindow(QWidget):
    """Transparent overlay window that sits on top of MTGA and draws
    per-card score badges using the hardcoded grid layout.

    Key design points (matches untapped.gg):
    - Frameless, always-on-top, translucent background
    - Click-through via Win32 WS_EX_TRANSPARENT
    - Tracks MTGA window bounds via pygetwindow; repositions every 500ms
    - Card positions hardcoded as fractions of MTGA client area
    - Aspect-ratio aware (16:9 vs 16:10)
    """

    # Refresh interval for window-follow (ms). Shorter = smoother tracking,
    # but keep reasonable since MTGA rarely moves during a draft.
    FOLLOW_INTERVAL_MS = 500

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._badges: list[CardBadge] = []
        self._last_cards_data: list[dict[str, Any]] = []
        self._should_show = False
        self._user_enabled = True
        # Calibration mode draws a red outline around the computed card grid
        # and cell boundaries, so we can see whether the hardcoded coords match
        # MTGA's actual card layout. Toggle via set_calibration(True).
        self._calibration_mode = False
        self._calibration_rects: list[QRect] = []

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # No layout — children are positioned absolutely
        self.setStyleSheet("background: transparent;")

        self._follow_timer = QTimer(self)
        self._follow_timer.timeout.connect(self._tick)
        self._follow_timer.start(self.FOLLOW_INTERVAL_MS)

    def closeEvent(self, event) -> None:
        if self._follow_timer.isActive():
            self._follow_timer.stop()
        super().closeEvent(event)

    def set_enabled(self, enabled: bool) -> None:
        """Let user toggle the card overlay on/off without destroying it."""
        self._user_enabled = enabled
        if not enabled and self.isVisible():
            self.hide()

    def set_calibration(self, enabled: bool) -> None:
        """Toggle calibration mode — draws a red grid outline so we can
        see whether the hardcoded card positions match MTGA's actual layout.
        In calibration mode the overlay is NOT click-through (so Windows
        definitely composites the painted lines) and draws a faint fill
        to make sure the outlines are visible against the game.
        """
        self._calibration_mode = bool(enabled)
        if enabled:
            # Make sure the overlay window is visible even without pack data,
            # so we can diagnose alignment before the first pack loads.
            self._should_show = True
            # Disable click-through so painted content renders reliably
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
            if os.name == "nt" and win32gui is not None and win32con is not None:
                try:
                    hwnd = int(self.winId())
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                    win32gui.SetWindowLong(
                        hwnd,
                        win32con.GWL_EXSTYLE,
                        style & ~win32con.WS_EX_TRANSPARENT,
                    )
                except Exception:
                    pass
            # Populate calibration rects with the grid geometry even if no
            # cards are loaded yet (so you always see an outline).
            self._compute_calibration_rects()
            # Force an immediate reposition/paint
            self._tick()
        else:
            # Restore click-through
            self._apply_click_through()
        self.update()

    def _compute_calibration_rects(self) -> None:
        """Compute grid outline rects from window geometry alone, without
        requiring card data. Used in calibration mode.
        """
        rect = self.geometry()
        w, h = rect.width(), rect.height()
        if w <= 0 or h <= 0:
            return
        rows, cols, top_frac, height_frac, width_frac, left_off_frac = GRID_LAYOUT_LIST_VIEW
        grid_top = int(h * top_frac)
        grid_height = int(h * height_frac)
        grid_width = int(h * width_frac)
        grid_left = int(w / 2 + h * left_off_frac - grid_width / 2)
        cell_w = grid_width / cols
        cell_h = grid_height / rows
        calib: list[QRect] = [QRect(grid_left, grid_top, grid_width, grid_height)]
        for i in range(rows * cols):
            row = i // cols
            col = i % cols
            cx = grid_left + int(col * cell_w)
            cy = grid_top + int(row * cell_h)
            calib.append(QRect(cx, cy, int(cell_w), int(cell_h)))
        self._calibration_rects = calib

    def paintEvent(self, event) -> None:
        """Draw calibration overlay if enabled."""
        super().paintEvent(event)
        if not self._calibration_mode:
            return
        painter = QPainter(self)

        # Semi-transparent dark fill so outlines are definitely visible
        painter.fillRect(self.rect(), QColor(0, 0, 0, 60))

        # Window bounds label (shows what pygetwindow reports for MTGA)
        painter.setPen(QPen(QColor("cyan"), 2))
        painter.drawRect(self.rect().adjusted(1, 1, -1, -1))
        painter.setPen(QColor("white"))
        label = (
            f"window {self.width()}x{self.height()}  "
            f"ratio={self.width() / max(1, self.height()):.3f}  "
            f"(CALIBRATION MODE — toggle off to re-enable click-through)"
        )
        painter.drawText(10, 20, label)

        if not self._calibration_rects:
            painter.drawText(10, 40, "No card data yet — waiting for draft pack…")
            return

        # Outer grid boundary (thick red)
        outer = self._calibration_rects[0]
        painter.setPen(QPen(QColor(255, 50, 50), 3))
        painter.drawRect(outer)
        painter.setPen(QColor("white"))
        painter.drawText(outer.x() + 5, outer.y() - 5, "grid bounds (red)")

        # Per-cell boundaries (yellow)
        painter.setPen(QPen(QColor(255, 220, 0), 2))
        for i, rect in enumerate(self._calibration_rects[1:]):
            painter.drawRect(rect)
            painter.setPen(QColor(255, 220, 0))
            painter.drawText(rect.x() + 4, rect.y() + 14, str(i))
            painter.setPen(QPen(QColor(255, 220, 0), 2))

    def hide_overlay(self) -> None:
        self._should_show = False
        if self.isVisible():
            self.hide()

    # -- Click-through -----------------------------------------------------

    def _apply_click_through(self) -> None:
        """Make mouse events pass through to MTGA."""
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        if os.name != "nt" or win32gui is None or win32con is None:
            return
        try:
            hwnd = int(self.winId())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_EXSTYLE,
                style | win32con.WS_EX_TRANSPARENT | win32con.WS_EX_LAYERED,
            )
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._apply_click_through()

    # -- Public update -----------------------------------------------------

    def update_draft_state(self, draft_info: dict[str, Any]) -> None:
        """Called whenever new draft data arrives."""
        if not self._user_enabled:
            self.hide_overlay()
            return

        is_active = bool(draft_info.get("is_active")) and bool(draft_info.get("cards"))
        if not is_active:
            self._last_cards_data = []
            self.hide_overlay()
            return

        cards = draft_info.get("cards", [])
        self._last_cards_data = cards
        self._should_show = True
        self._reposition_and_draw()

    # -- Geometry / MTGA tracking -----------------------------------------

    def _get_mtga_rect(self) -> Optional[QRect]:
        """Return MTGA's **client-area** rect in Qt logical pixels.

        Prefers `GetClientRect + ClientToScreen` so grid fractions (derived
        from MTGA's render area) aren't shifted by title bar / border /
        DWM-frame padding that `pygetwindow` includes.
        """
        if os.name != "nt":
            return None

        left_px = top_px = width_px = height_px = None
        if find_mtga_hwnd is not None and get_client_rect is not None:
            try:
                hwnd = find_mtga_hwnd()
                if hwnd:
                    cr = get_client_rect(hwnd)
                    if cr is not None and cr[2] > 0 and cr[3] > 0:
                        left_px, top_px, width_px, height_px = cr
            except Exception:
                left_px = None

        if left_px is None:
            if gw is None:
                return None
            try:
                windows = [w for w in gw.getWindowsWithTitle("MTGA") if w.title == "MTGA"]
                if not windows:
                    return None
                m = windows[0]
                if m.isMinimized:
                    return None
                left_px, top_px, width_px, height_px = m.left, m.top, m.width, m.height
            except Exception:
                return None

        ratio = 1.0
        try:
            from PySide6.QtGui import QGuiApplication
            center_px = (left_px + width_px // 2, top_px + height_px // 2)
            screen = QGuiApplication.primaryScreen()
            for s in QGuiApplication.screens():
                geo = s.geometry()
                if geo.contains(center_px[0], center_px[1]):
                    screen = s
                    break
            if screen:
                ratio = float(screen.devicePixelRatio() or 1.0)
        except Exception:
            ratio = 1.0
        if ratio <= 0:
            ratio = 1.0

        return QRect(
            int(left_px / ratio),
            int(top_px / ratio),
            int(width_px / ratio),
            int(height_px / ratio),
        )

    def _tick(self) -> None:
        """Periodic: match MTGA bounds and redraw if visible."""
        rect = self._get_mtga_rect()
        if rect is None or not self._should_show or not self._user_enabled:
            if self.isVisible():
                self.hide()
            return

        # Match MTGA window bounds exactly
        if self.geometry() != rect:
            self.setGeometry(rect)

        if not self.isVisible():
            self.show()
            self._apply_click_through()

        # Repaint badge positions (layout may depend on current geometry)
        self._reposition_and_draw()

    # -- Badge drawing -----------------------------------------------------

    def _reposition_and_draw(self) -> None:
        if not self._last_cards_data:
            self._clear_badges()
            return

        rect = self.geometry()
        w = rect.width()
        h = rect.height()
        if w <= 0 or h <= 0:
            return

        # Server sorts cards by composite_score but MTGA displays them in pack
        # order. Re-sort by pack_index (populated in server.py) so badges line
        # up with the actual on-screen card positions.
        cards_by_pack = sorted(
            self._last_cards_data,
            key=lambda c: c.get("pack_index", 999),
        )
        cards = cards_by_pack
        num_cards = len(cards)

        # Always use the 3×5 list view layout. MTGA keeps cards in their
        # original grid slots even after picks (leaves gaps rather than
        # reflowing), so sorting by pack_index handles the gaps naturally.
        layout = GRID_LAYOUT_LIST_VIEW
        rows, cols, top_frac, height_frac, width_frac, left_off_frac = layout

        # MTGA content may have letterbox bars. As a simple first pass,
        # assume the game fills the window (Steam launcher windowed mode).
        # TODO: subtract black bars when running fullscreen with mismatched
        # aspect ratio.

        # Determine target aspect ratio (16:9 or 16:10)
        ratio = w / h if h > 0 else 1.78
        is_16_10 = abs(ratio - 1.6) < 0.05

        # vh-equivalent: fractions are of render height
        grid_top = int(h * top_frac)
        grid_height = int(h * height_frac)
        grid_width = int(h * width_frac)
        grid_left = int(w / 2 + h * left_off_frac - grid_width / 2)

        cell_w = grid_width / cols
        cell_h = grid_height / rows

        # Ensure we have the right number of badges
        while len(self._badges) < num_cards:
            b = CardBadge(self)
            b.show()
            self._badges.append(b)
        while len(self._badges) > num_cards:
            b = self._badges.pop()
            b.hide()
            b.deleteLater()

        # Collect calibration rectangles (outer grid + per-cell)
        calib: list[QRect] = [QRect(grid_left, grid_top, grid_width, grid_height)]

        for i, card in enumerate(cards):
            if i >= rows * cols:
                break
            row = i // cols
            col = i % cols

            # Badge sits in the top-right of the card for visibility
            cx = grid_left + int(col * cell_w)
            cy = grid_top + int(row * cell_h)

            badge = self._badges[i]
            badge.set_data(
                score=card.get("composite_score"),
                tier=card.get("tier"),
                pair=card.get("best_pair"),
            )
            # Size the badge relative to cell size
            bw = max(48, int(cell_w * 0.30))
            bh = max(28, int(cell_h * 0.14))
            # Position: top-left corner of each card cell, inset a little
            badge_x = cx + int(cell_w * 0.05)
            badge_y = cy + int(cell_h * 0.04)
            badge.setGeometry(badge_x, badge_y, bw, bh)

            # Record this cell for calibration overlay
            calib.append(QRect(cx, cy, int(cell_w), int(cell_h)))

        self._calibration_rects = calib
        if self._calibration_mode:
            self.update()

    def _clear_badges(self) -> None:
        for b in self._badges:
            b.hide()
            b.deleteLater()
        self._badges = []
