"""In-match overlay that highlights the suggested card/action directly on MTGA.

Uses ground-truth screen positions from the BepInEx plugin's `get_card_positions`
command (rather than heuristic layouts), so highlights stay aligned with cards
as they move around the battlefield.

A suggested-actions event carries an ordered list of actions. Each action is
drawn as a numbered pulsing ring at its target's screen rectangle, colored by
action type (cast=green, attack=red, block=blue, target=yellow, etc.). Auto-
clears after N seconds or when a new event arrives.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

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

logger = logging.getLogger(__name__)


# Action type → highlight color (RGB). These are GRE-flavored names, we map
# the common ones. Unknown types fall back to yellow.
ACTION_COLORS: dict[str, tuple[int, int, int]] = {
    "cast_spell":           (74, 222, 128),   # green
    "play_land":            (74, 222, 128),   # green
    "activate_ability":     (147, 197, 253),  # light blue
    "declare_attackers":    (248, 113, 113),  # red
    "declare_blockers":     (96, 165, 250),   # blue
    "select_target":        (250, 204, 21),   # yellow
    "select_n":             (250, 204, 21),   # yellow
    "search_library":       (216, 180, 254),  # purple
    "modal_choice":         (251, 191, 36),   # amber
    "pay_costs":            (251, 146, 60),   # orange
    "mulligan_keep":        (74, 222, 128),   # green
    "mulligan_mull":        (248, 113, 113),  # red
    "distribute":           (250, 204, 21),   # yellow
    "pass_priority":        (156, 163, 175),  # grey
}

DEFAULT_COLOR = (250, 204, 21)  # yellow


class MatchOverlayWindow(QWidget):
    """Transparent always-on-top window drawing numbered highlights over MTGA.

    - Tracks MTGA window bounds (matches CardOverlayWindow approach)
    - Click-through via WS_EX_TRANSPARENT
    - Polls the BepInEx bridge for card screen rects every `position_poll_ms`
    - Receives suggested_actions events from the coach process and caches them
    - Pulses a numbered ring at each suggested card; auto-clears after TTL
    """

    FOLLOW_INTERVAL_MS = 250      # overlay reposition cadence
    POSITION_POLL_MS = 300        # bridge.get_card_positions() cadence in match
    ACTION_TTL_SEC = 30.0         # clear highlights after this if no new event
    PULSE_MS = 1200               # pulse cycle length

    ADVICE_TTL_SEC = 25.0  # auto-fade advice after this many seconds

    def __init__(self, bridge_getter=None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # bridge_getter is a callable returning the GREBridge or None if
        # unavailable. We inject it so this class doesn't depend on the
        # coach process layout.
        self._bridge_getter = bridge_getter
        self._suggested_actions: list[dict[str, Any]] = []
        self._actions_expire_at: float = 0.0
        self._card_positions: dict[int, dict[str, Any]] = {}
        self._screen_w: int = 0
        self._screen_h: int = 0
        self._user_enabled = True
        self._match_active = False
        self._calibration_mode = False
        # Affine post-transform applied on top of the auto-derived window
        # rect. Lets the user manually nudge/scale the card overlay when
        # MTGA's render area doesn't perfectly match GetClientRect (e.g.
        # letterbox scaling at non-16:9 windows). Identity by default.
        self._calib_offset_x: float = 0.0
        self._calib_offset_y: float = 0.0
        self._calib_scale_x: float = 1.0
        self._calib_scale_y: float = 1.0
        # Active drag state for pan gesture
        self._calib_drag_origin: Optional[QPoint] = None
        self._calib_drag_ox0: float = 0.0
        self._calib_drag_oy0: float = 0.0
        self._calib_saved_at: float = 0.0
        self._load_calibration()
        # Latest coach advice (plain text). Displayed on the overlay so the
        # user can keep eyes on MTGA instead of switching windows.
        self._advice_text: str = ""
        self._advice_seat: str = ""
        self._advice_expire_at: float = 0.0
        # Position of the advice panel corner, expressed as a fraction of
        # the overlay window (0..1 in x/y). Default = top-right.
        # User can move it via settings or future drag gesture.
        self._advice_panel_anchor: str = "top-right"  # or "top-left", "bottom-right", "bottom-left"
        # Whether to show the advice panel at all (may want pill only).
        self._show_advice_panel: bool = True

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setStyleSheet("background: transparent;")

        self._follow_timer = QTimer(self)
        self._follow_timer.timeout.connect(self._tick)
        self._follow_timer.start(self.FOLLOW_INTERVAL_MS)

        self._position_timer = QTimer(self)
        self._position_timer.timeout.connect(self._refresh_card_positions)
        self._position_timer.start(self.POSITION_POLL_MS)

        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self.update)
        self._pulse_timer.start(50)  # 20 FPS pulse animation

    def closeEvent(self, event) -> None:
        for t in (self._follow_timer, self._position_timer, self._pulse_timer):
            if t.isActive():
                t.stop()
        super().closeEvent(event)

    # -- Public API ----------------------------------------------------------

    def set_bridge_getter(self, getter) -> None:
        """Inject or replace the bridge getter (called after a coach process start)."""
        self._bridge_getter = getter

    def set_enabled(self, enabled: bool) -> None:
        """Toggle the entire overlay on/off without destroying it."""
        self._user_enabled = enabled
        if not enabled and self.isVisible():
            self.hide()

    def set_advice_panel_visible(self, visible: bool) -> None:
        """Show or hide just the advice panel (keeps pill + highlights)."""
        self._show_advice_panel = bool(visible)
        self.update()

    def set_advice_anchor(self, anchor: str) -> None:
        """Move the advice panel. Valid anchors:
        'top-right', 'top-left', 'bottom-right', 'bottom-left'.
        """
        if anchor in ("top-right", "top-left", "bottom-right", "bottom-left"):
            self._advice_panel_anchor = anchor
            self.update()

    def cycle_advice_anchor(self) -> str:
        """Rotate through the four corners. Returns the new anchor."""
        order = ["top-right", "bottom-right", "bottom-left", "top-left"]
        try:
            idx = order.index(self._advice_panel_anchor)
        except ValueError:
            idx = -1
        self._advice_panel_anchor = order[(idx + 1) % len(order)]
        self.update()
        return self._advice_panel_anchor

    def on_match_active(self, active: bool) -> None:
        """Let the coach tell us whether a match is in progress. When inactive,
        we skip the BepInEx position polls to avoid spamming the bridge.
        """
        self._match_active = bool(active)
        if not active:
            self.clear_actions()

    def set_calibration(self, enabled: bool) -> None:
        """Toggle calibration mode — draws a thin outline around every card
        the plugin reports, labeled with its instance_id. Useful for verifying
        ground-truth positions line up with MTGA's rendered cards.

        In calibration mode we also force match_active=True and disable
        click-through, so the visualization is visible even outside a match.
        """
        self._calibration_mode = bool(enabled)
        if enabled:
            self._match_active = True  # force polling so we see positions
            # Disable click-through so Windows reliably composites the paint
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
            # Take keyboard focus so arrow keys / Ctrl+S work in calibration mode
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.activateWindow()
            self.setFocus(Qt.FocusReason.OtherFocusReason)
            # Immediate poll so we don't have to wait up to 300ms
            self._refresh_card_positions()
        else:
            self._calib_drag_origin = None
            self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._apply_click_through()
        self.update()

    def set_suggested_actions(self, actions: list[dict[str, Any]]) -> None:
        """Replace the current highlight sequence.

        Each action dict should contain at minimum:
          - `action_type`: e.g. "cast_spell", "declare_attackers"
          - `instance_id`: the primary target's GRE instance id
          - `card_name`: display label (optional)
          - `reason`: explanation to show under the highlight (optional)
          - `target_instance_ids`: list of secondary target instance ids
            (for attack/block/select_target)

        The order of `actions` determines the sequence number 1..N shown
        in each ring.
        """
        self._suggested_actions = list(actions or [])
        if self._suggested_actions:
            self._actions_expire_at = time.time() + self.ACTION_TTL_SEC
        else:
            self._actions_expire_at = 0.0
        self.update()

    def clear_actions(self) -> None:
        self._suggested_actions = []
        self._actions_expire_at = 0.0
        self.update()

    def set_advice(self, text: str, seat_info: str = "") -> None:
        """Set the latest advice text to display on the overlay.

        Coach passes this along with each advice event. The overlay keeps
        it visible for ADVICE_TTL_SEC seconds or until replaced by a new
        advice. Pass empty text to clear immediately.
        """
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

    # -- Calibration persistence --------------------------------------------

    def _calibration_path(self) -> Path:
        """Resolve where overlay_calibration.json lives.

        Prefers %LOCALAPPDATA%/mtgacoach (matches the rest of the app's
        runtime state). Falls back to ~/.mtgacoach for non-Windows dev.
        """
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

    def _load_calibration(self) -> None:
        try:
            p = self._calibration_path()
            if not p.exists():
                return
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            self._calib_offset_x = float(data.get("offset_x", 0.0) or 0.0)
            self._calib_offset_y = float(data.get("offset_y", 0.0) or 0.0)
            self._calib_scale_x = float(data.get("scale_x", 1.0) or 1.0)
            self._calib_scale_y = float(data.get("scale_y", 1.0) or 1.0)
            if self._calib_scale_x <= 0:
                self._calib_scale_x = 1.0
            if self._calib_scale_y <= 0:
                self._calib_scale_y = 1.0
        except Exception as e:
            logger.debug(f"load overlay_calibration failed: {e}")

    def _save_calibration(self) -> None:
        try:
            p = self._calibration_path()
            data = {
                "offset_x": round(self._calib_offset_x, 3),
                "offset_y": round(self._calib_offset_y, 3),
                "scale_x": round(self._calib_scale_x, 5),
                "scale_y": round(self._calib_scale_y, 5),
            }
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._calib_saved_at = time.time()
            logger.info(f"overlay calibration saved: {data}")
        except Exception as e:
            logger.error(f"save overlay_calibration failed: {e}")
        self.update()

    def _reset_calibration(self) -> None:
        """Revert in-memory calibration to identity. Does not delete the
        saved file — user must Ctrl+S to persist the reset.
        """
        self._calib_offset_x = 0.0
        self._calib_offset_y = 0.0
        self._calib_scale_x = 1.0
        self._calib_scale_y = 1.0
        self.update()

    # -- Click-through -------------------------------------------------------

    def _apply_click_through(self) -> None:
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

    # -- Interactive calibration input --------------------------------------

    _CALIB_SCALE_MIN = 0.2
    _CALIB_SCALE_MAX = 5.0
    _CALIB_WHEEL_FACTOR = 1.05  # 5% per notch

    def _clamp_scale(self, value: float) -> float:
        return max(self._CALIB_SCALE_MIN, min(self._CALIB_SCALE_MAX, value))

    def _zoom_axis_about(
        self,
        factor: float,
        anchor: float,
        offset_attr: str,
        scale_attr: str,
    ) -> None:
        """Scale one axis while keeping the given anchor point stationary."""
        old_scale = getattr(self, scale_attr)
        new_scale = self._clamp_scale(old_scale * factor)
        if old_scale <= 0 or new_scale == old_scale:
            return
        old_offset = getattr(self, offset_attr)
        new_offset = anchor - (anchor - old_offset) * (new_scale / old_scale)
        setattr(self, scale_attr, new_scale)
        setattr(self, offset_attr, new_offset)

    def wheelEvent(self, event) -> None:
        if not self._calibration_mode:
            super().wheelEvent(event)
            return
        try:
            pos = event.position()
            cursor_x = float(pos.x())
            cursor_y = float(pos.y())
        except Exception:
            cursor_x = self.width() / 2.0
            cursor_y = self.height() / 2.0
        steps = event.angleDelta().y() / 120.0
        if steps == 0:
            event.accept()
            return
        factor = self._CALIB_WHEEL_FACTOR ** steps
        mods = event.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if shift and not ctrl:
            self._zoom_axis_about(factor, cursor_x, "_calib_offset_x", "_calib_scale_x")
        elif ctrl and not shift:
            self._zoom_axis_about(factor, cursor_y, "_calib_offset_y", "_calib_scale_y")
        else:
            self._zoom_axis_about(factor, cursor_x, "_calib_offset_x", "_calib_scale_x")
            self._zoom_axis_about(factor, cursor_y, "_calib_offset_y", "_calib_scale_y")
        self.update()
        event.accept()

    def mousePressEvent(self, event) -> None:
        if not self._calibration_mode:
            super().mousePressEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._calib_drag_origin = event.position().toPoint()
            self._calib_drag_ox0 = self._calib_offset_x
            self._calib_drag_oy0 = self._calib_offset_y
            event.accept()
        elif event.button() == Qt.MouseButton.RightButton:
            self._reset_calibration()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not self._calibration_mode or self._calib_drag_origin is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position().toPoint() - self._calib_drag_origin
        self._calib_offset_x = self._calib_drag_ox0 + delta.x()
        self._calib_offset_y = self._calib_drag_oy0 + delta.y()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if not self._calibration_mode:
            super().mouseReleaseEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._calib_drag_origin = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:
        if not self._calibration_mode:
            super().keyPressEvent(event)
            return
        key = event.key()
        mods = event.modifiers()
        step = 10 if (mods & Qt.KeyboardModifier.ShiftModifier) else 1
        if key == Qt.Key.Key_S and (mods & Qt.KeyboardModifier.ControlModifier):
            self._save_calibration()
            event.accept()
        elif key == Qt.Key.Key_R:
            self._reset_calibration()
            event.accept()
        elif key == Qt.Key.Key_Left:
            self._calib_offset_x -= step
            self.update()
            event.accept()
        elif key == Qt.Key.Key_Right:
            self._calib_offset_x += step
            self.update()
            event.accept()
        elif key == Qt.Key.Key_Up:
            self._calib_offset_y -= step
            self.update()
            event.accept()
        elif key == Qt.Key.Key_Down:
            self._calib_offset_y += step
            self.update()
            event.accept()
        else:
            super().keyPressEvent(event)

    # -- MTGA bounds tracking ------------------------------------------------

    def _get_mtga_rect(self) -> Optional[QRect]:
        """Return MTGA's **client-area** rect in Qt logical pixels.

        The plugin reports normalized card coords against Unity's
        `Screen.width/height`, which is the render client area — not the
        full OS window. Using `pygetwindow`'s full window bounds (title
        bar, borders, DWM invisible frames) made the overlay larger than
        the game surface and shifted all highlights. Prefer
        `GetClientRect + ClientToScreen`.
        """
        if os.name != "nt":
            return None

        # Preferred: GetClientRect + ClientToScreen. Returns the render
        # client area in physical screen pixels, matching Unity.Screen.
        client_rect = None
        if find_mtga_hwnd is not None and get_client_rect is not None:
            try:
                hwnd = find_mtga_hwnd()
                if hwnd:
                    client_rect = get_client_rect(hwnd)
            except Exception:
                client_rect = None

        left_px = top_px = width_px = height_px = None
        if client_rect is not None:
            left_px, top_px, width_px, height_px = client_rect
            if width_px <= 0 or height_px <= 0:
                client_rect = None

        # Fallback: pygetwindow full-window rect (keeps us working on
        # environments without ctypes user32 for whatever reason).
        if client_rect is None:
            if gw is None:
                return None
            try:
                wins = [w for w in gw.getWindowsWithTitle("MTGA") if w.title == "MTGA"]
                if not wins:
                    return None
                m = wins[0]
                if m.isMinimized:
                    return None
                left_px, top_px, width_px, height_px = m.left, m.top, m.width, m.height
            except Exception:
                return None

        # Per-monitor DPI ratio so the overlay sits correctly on high-DPI displays.
        ratio = 1.0
        try:
            from PySide6.QtGui import QGuiApplication
            center_px = (left_px + width_px // 2, top_px + height_px // 2)
            screen = QGuiApplication.screenAt(
                self.mapToGlobal(self.rect().topLeft())
            ) or QGuiApplication.primaryScreen()
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
        rect = self._get_mtga_rect()
        # Show the overlay whenever MTGA is visible — the small "armed"
        # badge is the user's only signal that the pipeline is alive, so
        # don't gate it on the flaky match_active flag. Card-position
        # polling + highlight drawing still uses match_active / calib.
        should_show = self._user_enabled and rect is not None
        if not should_show:
            if self.isVisible():
                self.hide()
            return

        # Match MTGA window bounds so our local coords align with it
        if self.geometry() != rect:
            self.setGeometry(rect)

        # Expire stale highlights
        now = time.time()
        if self._suggested_actions and now > self._actions_expire_at:
            self.clear_actions()
        # Expire stale advice
        if self._advice_text and now > self._advice_expire_at:
            self._advice_text = ""
            self._advice_seat = ""
            self.update()

        if not self.isVisible():
            self.show()
            self._apply_click_through()

        # Force topmost on every tick. MTGA's borderless-fullscreen mode
        # often pushes non-foreground windows behind it even when they have
        # WindowStaysOnTopHint. SetWindowPos(HWND_TOPMOST, NOACTIVATE) forces
        # us back above without stealing focus.
        self._force_topmost()

    def _force_topmost(self) -> None:
        """Force the overlay to the top of the Windows z-order."""
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
            # Fallback: Qt-level raise
            try:
                self.raise_()
            except Exception:
                pass

    # -- Card position polling ----------------------------------------------

    def update_card_positions(self, payload: dict[str, Any]) -> None:
        """Receive card screen rects pushed from the coach process.

        The UI process does NOT own a GRE bridge instance (two servers
        would fight over the single-instance pipe). The coach polls the
        bridge and forwards the result via the `card_positions` pipe
        event, which calls this setter.
        """
        if not isinstance(payload, dict):
            return
        try:
            self._screen_w = int(payload.get("screen_w") or 0)
            self._screen_h = int(payload.get("screen_h") or 0)
            new_positions: dict[int, dict[str, Any]] = {}
            for card in payload.get("cards", []) or []:
                if not isinstance(card, dict):
                    continue
                iid = int(card.get("instance_id") or 0)
                if iid:
                    new_positions[iid] = card
            self._card_positions = new_positions
        except Exception as e:
            logger.debug(f"update_card_positions parse failed: {e}")

    def _refresh_card_positions(self) -> None:
        """Legacy path — kept as a no-op for callers. The coach process
        now pushes positions via `update_card_positions`.
        """
        if not self._user_enabled:
            return
        if self._get_mtga_rect() is None:
            return
        if self._bridge_getter is None:
            # Coach-pushed mode; nothing to do.
            return
        try:
            bridge = self._bridge_getter()
        except Exception:
            return
        if bridge is None:
            return

        try:
            resp = bridge.get_card_positions()
        except Exception as e:
            logger.debug(f"get_card_positions failed: {e}")
            return
        if not resp or not resp.get("ok"):
            return

        self._screen_w = int(resp.get("screen_w") or 0)
        self._screen_h = int(resp.get("screen_h") or 0)

        new_positions: dict[int, dict[str, Any]] = {}
        for card in resp.get("cards", []):
            iid = int(card.get("instance_id") or 0)
            if iid:
                new_positions[iid] = card
        self._card_positions = new_positions

    # -- Coord mapping ------------------------------------------------------

    def _plugin_to_local(self, card: dict[str, Any]) -> Optional[QRect]:
        """Map a card entry from the plugin (pixels in Unity Screen space, with
        the overlay having already flipped Y to top-left origin) to this
        widget's local coordinates.

        We prefer normalized coords so the mapping survives DPI scaling and
        MTGA client-area ≠ Unity-Screen size. If normalized are missing, fall
        back to raw pixels scaled by the ratio of window to Unity screen.

        Returns None for cards projected outside MTGA's visible area
        (common for spawn/despawn animations and hidden-zone objects) to
        avoid drawing rogue boxes on desktop space.
        """
        win_w = self.width()
        win_h = self.height()
        if win_w <= 0 or win_h <= 0:
            return None

        nx = card.get("nx")
        ny = card.get("ny")
        nw = card.get("nw")
        nh = card.get("nh")
        if nx is None or ny is None or nw is None or nh is None:
            return None
        try:
            fnx = float(nx)
            fny = float(ny)
            fnw = float(nw)
            fnh = float(nh)
        except (TypeError, ValueError):
            return None

        # Drop cards whose projection lies substantially outside the 0..1
        # normalized viewport. Small slop (5%) allowed for cards animating
        # just off-screen. Also drop anything with zero/near-zero size.
        OVERFLOW = 0.05
        if (
            fnx < -OVERFLOW
            or fny < -OVERFLOW
            or fnx + fnw > 1.0 + OVERFLOW
            or fny + fnh > 1.0 + OVERFLOW
            or fnw < 0.002
            or fnh < 0.002
        ):
            return None

        eff_w = win_w * self._calib_scale_x
        eff_h = win_h * self._calib_scale_y
        x = int(fnx * eff_w + self._calib_offset_x)
        y = int(fny * eff_h + self._calib_offset_y)
        w = int(fnw * eff_w)
        h = int(fnh * eff_h)
        return QRect(x, y, max(1, w), max(1, h))

    # -- Painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # Always-visible "armed" badge whenever the overlay is shown (i.e.
        # MTGA is detected). Calibration mode has its own visualization.
        if not self._calibration_mode:
            self._draw_armed_badge(painter)
            if self._advice_text and self._show_advice_panel:
                self._draw_advice_panel(painter)

        # Calibration mode: draw a thin outline around every detected card
        # so we can verify BepInEx is returning accurate positions.
        if self._calibration_mode:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 40))
            painter.setPen(QPen(QColor("cyan"), 2))
            painter.drawRect(self.rect().adjusted(1, 1, -1, -1))

            saved_hint = ""
            if self._calib_saved_at and time.time() - self._calib_saved_at < 2.5:
                saved_hint = "  ✓ SAVED"
            header_lines = [
                (
                    f"MATCH CALIBRATION — window {self.width()}x{self.height()}  "
                    f"unity {self._screen_w}x{self._screen_h}  "
                    f"cards {len(self._card_positions)}{saved_hint}"
                ),
                (
                    f"offset ({self._calib_offset_x:+.0f}, {self._calib_offset_y:+.0f})  "
                    f"scale ({self._calib_scale_x:.3f}, {self._calib_scale_y:.3f})"
                ),
                "drag = pan   wheel = scale (shift=X only, ctrl=Y only)   arrows = nudge (shift=×10)",
                "Ctrl+S = save   R = reset   right-click = reset",
            ]
            painter.setPen(QColor(20, 20, 25, 200))
            painter.setBrush(QColor(20, 20, 25, 200))
            block_h = 20 * len(header_lines) + 10
            painter.drawRoundedRect(6, 6, min(self.width() - 12, 720), block_h, 6, 6)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QColor("white"))
            font = QFont(); font.setBold(True); font.setPixelSize(13)
            painter.setFont(font)
            for i, line in enumerate(header_lines):
                painter.drawText(14, 24 + i * 18, line)

            # Zone colors for quick visual grouping
            zone_colors = {
                "Hand":        QColor(74, 222, 128),
                "Battlefield": QColor(251, 191, 36),
                "Graveyard":   QColor(156, 163, 175),
                "Stack":       QColor(248, 113, 113),
                "Exile":       QColor(216, 180, 254),
                "Library":     QColor(96, 165, 250),
            }
            small = QFont(); small.setPixelSize(10)
            painter.setFont(small)
            for iid, card in self._card_positions.items():
                rect = self._plugin_to_local(card)
                if rect is None:
                    continue
                zone = str(card.get("zone") or "")
                color = zone_colors.get(zone, QColor(200, 200, 200))
                painter.setPen(QPen(color, 2))
                painter.drawRect(rect)
                label = f"{iid} {zone[:3]}"
                painter.setPen(color)
                painter.drawText(rect.x() + 2, rect.y() + 12, label)

        if not self._suggested_actions:
            return

        # Pulse amplitude (0.0 - 1.0) via cosine wave
        t = (time.time() * 1000.0) % self.PULSE_MS
        pulse = 0.5 * (1 + math.cos(2 * math.pi * t / self.PULSE_MS))  # 0..1

        sequence_font = QFont()
        sequence_font.setBold(True)
        sequence_font.setPixelSize(20)

        for seq, action in enumerate(self._suggested_actions, start=1):
            atype = str(action.get("action_type", "")).lower()
            color_rgb = ACTION_COLORS.get(atype, DEFAULT_COLOR)
            base_color = QColor(*color_rgb)

            # Primary target highlight
            iid = int(action.get("instance_id") or 0)
            if iid:
                card = self._card_positions.get(iid)
                if card:
                    rect = self._plugin_to_local(card)
                    if rect is not None:
                        self._draw_ring(painter, rect, base_color, pulse, seq)
                        label = str(action.get("card_name") or "")
                        if label:
                            self._draw_label(painter, rect, label, base_color)

            # Secondary targets (e.g., attack into a creature, blocker → attacker)
            for tid in action.get("target_instance_ids") or []:
                try:
                    tid_int = int(tid)
                except (TypeError, ValueError):
                    continue
                if tid_int <= 0:
                    continue
                card = self._card_positions.get(tid_int)
                if not card:
                    continue
                rect = self._plugin_to_local(card)
                if rect is None:
                    continue
                secondary = QColor(base_color)
                secondary.setAlpha(180)
                self._draw_ring(painter, rect, secondary, pulse, seq, secondary=True)

    def _draw_ring(
        self,
        painter: QPainter,
        rect: QRect,
        color: QColor,
        pulse: float,
        sequence: int,
        secondary: bool = False,
    ) -> None:
        """Draw a rounded-rect outline around a card with a pulse effect."""
        # CRITICAL: drawRoundedRect fills with the current brush and strokes
        # with the current pen. `_draw_advice_panel` sets a solid green
        # accent brush and leaves it active, so without clearing we'd fill
        # the card area with opaque green. Always start with NoBrush for
        # outline-only rings.
        painter.setBrush(Qt.NoBrush)

        # Expand slightly outside the card for visibility
        expand = 4 + int(pulse * 4)
        outer = rect.adjusted(-expand, -expand, expand, expand)
        thickness = 4 if not secondary else 3

        glow = QColor(color)
        glow.setAlpha(int(100 + pulse * 155))
        painter.setPen(QPen(glow, thickness + 4))
        painter.drawRoundedRect(outer, 8, 8)

        hard = QColor(color)
        hard.setAlpha(230)
        painter.setPen(QPen(hard, thickness))
        painter.drawRoundedRect(outer, 8, 8)

        # Sequence number badge (top-left of the card)
        if not secondary:
            badge_size = 28
            bx = outer.x() - 6
            by = outer.y() - 6
            badge_rect = QRect(bx, by, badge_size, badge_size)
            painter.fillRect(badge_rect, QColor(20, 20, 25, 230))
            painter.setPen(QPen(hard, 2))
            painter.drawRect(badge_rect)
            painter.setPen(QColor("white"))
            font = QFont()
            font.setBold(True)
            font.setPixelSize(16)
            painter.setFont(font)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, str(sequence))

    def _draw_advice_panel(self, painter: QPainter) -> None:
        """Render the latest coach advice as a panel in the top-right corner.

        Word-wrapped; fades out after ADVICE_TTL_SEC seconds or when
        replaced. Styled to stand out against MTGA art but not block
        important game elements.
        """
        max_w = min(480, self.width() // 3)
        pad_x = 14
        pad_y = 10

        title_font = QFont(); title_font.setPixelSize(11); title_font.setBold(True)
        body_font = QFont(); body_font.setPixelSize(15)

        painter.setFont(body_font)
        # Crude word-wrap via QFontMetrics.boundingRect with text width
        metrics = painter.fontMetrics()
        wrap_rect = metrics.boundingRect(
            0, 0, max_w - pad_x * 2, 10000,
            Qt.TextFlag.TextWordWrap,
            self._advice_text,
        )
        body_w = wrap_rect.width()
        body_h = wrap_rect.height()

        painter.setFont(title_font)
        title_metrics = painter.fontMetrics()
        title = f"COACH{f'  ·  {self._advice_seat}' if self._advice_seat else ''}"
        title_h = title_metrics.height()

        panel_w = max(body_w, title_metrics.horizontalAdvance(title)) + pad_x * 2
        panel_h = title_h + 6 + body_h + pad_y * 2

        margin_x = 14
        # Drop the top margin below the usual MTGA title/player banner row
        # so the panel doesn't cover opponent name, mana icons, or the
        # close button.
        margin_top = 48
        margin_bottom = 40  # leaves room for the pill
        anchor = self._advice_panel_anchor
        if anchor == "top-right":
            panel_x = self.width() - panel_w - margin_x
            panel_y = margin_top
        elif anchor == "top-left":
            panel_x = margin_x
            panel_y = margin_top
        elif anchor == "bottom-left":
            panel_x = margin_x
            panel_y = self.height() - panel_h - margin_bottom
        else:  # bottom-right (default fallback)
            panel_x = self.width() - panel_w - margin_x
            panel_y = self.height() - panel_h - margin_bottom

        # Background + accent border
        bg = QColor(18, 22, 28, 225)
        accent = QColor(105, 212, 108)  # green (advice)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(panel_x, panel_y, panel_w, panel_h, 8, 8)
        painter.setPen(QPen(accent, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(panel_x, panel_y, panel_w, panel_h, 8, 8)
        # Accent bar on the left edge
        painter.setPen(Qt.NoPen)
        painter.setBrush(accent)
        painter.drawRect(panel_x, panel_y, 3, panel_h)

        # Title
        painter.setFont(title_font)
        painter.setPen(QColor(148, 163, 184))  # cool grey
        painter.drawText(
            panel_x + pad_x, panel_y + pad_y + title_h - 3, title
        )

        # Body
        painter.setFont(body_font)
        painter.setPen(QColor(240, 244, 248))
        body_rect = QRect(
            panel_x + pad_x, panel_y + pad_y + title_h + 6, body_w, body_h
        )
        painter.drawText(
            body_rect, int(Qt.TextFlag.TextWordWrap), self._advice_text
        )
        # Reset brush so later paint calls (ring draws, badge) don't
        # inherit the green accent fill.
        painter.setBrush(Qt.NoBrush)
        painter.setPen(Qt.NoPen)

    def _draw_armed_badge(self, painter: QPainter) -> None:
        """Small unobtrusive badge confirming the overlay is alive in a match.

        Lives in the lower-right corner so it doesn't cover MTGA's own HUD.
        Displays the number of detected cards from the BepInEx bridge so the
        user can tell at a glance whether ground-truth positions are flowing.
        """
        card_count = len(self._card_positions)
        dot_color = QColor(74, 222, 128) if card_count > 0 else QColor(251, 191, 36)
        label = f"● mtgacoach • {card_count} cards" if card_count else "● mtgacoach • waiting for bridge"

        font = QFont()
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        text_w = metrics.horizontalAdvance(label)
        text_h = metrics.height()

        pad_x = 8
        pad_y = 4
        badge_w = text_w + pad_x * 2
        badge_h = text_h + pad_y * 2
        badge_x = self.width() - badge_w - 12
        badge_y = self.height() - badge_h - 12

        # Background pill
        bg = QColor(20, 20, 25, 180)
        painter.setPen(Qt.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(badge_x, badge_y, badge_w, badge_h, 6, 6)

        # Text + colored bullet
        painter.setPen(dot_color)
        painter.drawText(badge_x + pad_x, badge_y + pad_y + text_h - 3, "●")
        painter.setPen(QColor("white"))
        painter.drawText(
            badge_x + pad_x + metrics.horizontalAdvance("● "),
            badge_y + pad_y + text_h - 3,
            label[2:] if label.startswith("● ") else label,
        )

    def _draw_label(self, painter: QPainter, rect: QRect, text: str, color: QColor) -> None:
        label_rect = QRect(rect.x(), rect.bottom() + 4, rect.width(), 20)
        painter.fillRect(label_rect, QColor(20, 20, 25, 200))
        painter.setPen(QColor("white"))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(12)
        painter.setFont(font)
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, text[:40])
