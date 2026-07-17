"""Cross-platform MTGA window locator for the desktop overlays.

Single entry point: :func:`get_mtga_window_rect` returns the MTGA window's
``(x, y, width, height)`` in physical screen pixels, or ``None`` when MTGA
isn't running, is minimized, or the platform backend is unavailable.

Per-platform backends:

- **win32** — pygetwindow title match (the historical overlay approach,
  moved here verbatim: exact-title ``"MTGA"`` windows, minimized → None).
- **linux** — delegates to :func:`arenamcp.desktop.runtime.get_linux_window_geometry`
  (xwininfo under X11/XWayland; returns None on pure Wayland without it).
- **darwin** — Quartz ``CGWindowListCopyWindowInfo`` filtered by owner name
  containing ``"MTGA"``. Requires pyobjc's Quartz; missing pyobjc degrades
  to ``None`` rather than raising.

Results are cached briefly (overlays poll every ~250-1000 ms, several
windows poll concurrently) so a burst of calls costs one lookup.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Any, Optional

Rect = tuple[int, int, int, int]

# How long a located rect stays fresh. Overlays poll at 250-1000 ms; several
# overlay windows share this module, so a sub-poll TTL collapses their
# concurrent lookups into one real query without adding visible lag.
_CACHE_TTL_S = 0.25

# macOS: ignore tiny windows (menu-bar extras, tooltips) when scanning the
# Quartz window list for the game window.
_DARWIN_MIN_DIMENSION = 200

try:  # Windows backend (also importable elsewhere, harmless off-Windows)
    import pygetwindow as gw
except (ImportError, NotImplementedError):  # pragma: no cover - env specific
    gw = None  # type: ignore[assignment]


def _platform() -> str:
    """Indirection point so tests can force a platform."""
    return sys.platform


# -- win32 --------------------------------------------------------------------


def _win32_rect(title: str) -> Optional[Rect]:
    """Historical pygetwindow lookup, behavior-identical to the old overlays:
    exact-title match, minimized window → None."""
    if gw is None:
        return None
    try:
        windows = [w for w in gw.getWindowsWithTitle(title) if w.title == title]
        if not windows:
            return None
        mtga = windows[0]
        if mtga.isMinimized:
            return None
        return (int(mtga.left), int(mtga.top), int(mtga.width), int(mtga.height))
    except Exception:
        return None


# -- linux --------------------------------------------------------------------


def _linux_rect(title: str) -> Optional[Rect]:
    try:
        from arenamcp.desktop.runtime import get_linux_window_geometry
    except Exception:  # pragma: no cover - runtime always present in-app
        return None
    try:
        geom = get_linux_window_geometry(title)
    except Exception:
        return None
    if not geom or geom.get("is_minimized"):
        return None
    try:
        return (
            int(geom["left"]),
            int(geom["top"]),
            int(geom["width"]),
            int(geom["height"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


# -- darwin -------------------------------------------------------------------

_quartz_module: Any = None
_quartz_checked = False


def _load_quartz() -> Any:
    """Import pyobjc's Quartz once; missing pyobjc degrades to None."""
    global _quartz_module, _quartz_checked
    if not _quartz_checked:
        _quartz_checked = True
        try:
            import Quartz  # type: ignore[import-not-found]

            _quartz_module = Quartz
        except Exception:
            _quartz_module = None
    return _quartz_module


def _darwin_rect(title: str) -> Optional[Rect]:
    quartz = _load_quartz()
    if quartz is None:
        return None
    try:
        window_list = quartz.CGWindowListCopyWindowInfo(
            quartz.kCGWindowListOptionOnScreenOnly,
            quartz.kCGNullWindowID,
        )
    except Exception:
        return None

    # Exact owner-name match beats a substring match: third-party companion
    # apps like "MTGA_Draft_Tool" also contain "MTGA" in their owner name and
    # can sit in front of the game in the (front-to-back) window list.
    own_pid = os.getpid()
    substring_match: Optional[Rect] = None
    for info in window_list or []:
        try:
            # Never match our own windows: this process is "MTGA Coach", so a
            # substring scan for "MTGA" would lock the overlay onto the app's
            # own main window when the game isn't running — a feedback loop
            # observed live on the first Mac run (overlay parked at the main
            # window's rect, stealing clicks).
            if int(info.get("kCGWindowOwnerPID") or 0) == own_pid:
                continue
            owner = str(info.get("kCGWindowOwnerName") or "")
            if title not in owner:
                continue
            # Layer 0 is the normal window layer; skip status-bar items,
            # floating panels, and other non-document layers.
            if int(info.get("kCGWindowLayer") or 0) != 0:
                continue
            bounds = info.get("kCGWindowBounds") or {}
            x = int(bounds.get("X") or 0)
            y = int(bounds.get("Y") or 0)
            w = int(bounds.get("Width") or 0)
            h = int(bounds.get("Height") or 0)
            # Skip tiny helper windows (tooltips, menu extras).
            if w < _DARWIN_MIN_DIMENSION or h < _DARWIN_MIN_DIMENSION:
                continue
            if owner == title:
                return (x, y, w, h)
            if substring_match is None:
                substring_match = (x, y, w, h)
        except Exception:
            continue
    return substring_match


# -- dispatch + cache -----------------------------------------------------------


def _locate_uncached(title: str) -> Optional[Rect]:
    plat = _platform()
    if plat == "win32":
        return _win32_rect(title)
    if plat == "darwin":
        return _darwin_rect(title)
    return _linux_rect(title)


_cache_lock = threading.Lock()
_cache_value: dict[str, tuple[float, Optional[Rect]]] = {}


def get_mtga_window_rect(title: str = "MTGA") -> Optional[Rect]:
    """Return MTGA's window rect ``(x, y, width, height)`` or ``None``.

    ``None`` means "no usable window right now": MTGA not running,
    minimized, or the platform locator (pygetwindow / xwininfo / Quartz)
    is unavailable. Callers should hide their overlay in that case.
    """
    now = time.monotonic()
    with _cache_lock:
        cached = _cache_value.get(title)
        if cached is not None and now - cached[0] < _CACHE_TTL_S:
            return cached[1]

    rect = _locate_uncached(title)

    with _cache_lock:
        _cache_value[title] = (time.monotonic(), rect)
    return rect


def clear_cache() -> None:
    """Drop cached lookups (used by tests and after display changes)."""
    with _cache_lock:
        _cache_value.clear()


def apply_system_click_through(widget: Any, enabled: bool = True) -> bool:
    """OS-level click-through for a top-level Qt window.

    Qt's ``WA_TransparentForMouseEvents`` only makes *Qt* ignore mouse
    events — the native window still receives clicks from the OS and
    swallows anything that should reach the app underneath. Windows
    overlays solve this with ``WS_EX_TRANSPARENT``; the macOS analogue is
    ``NSWindow.ignoresMouseEvents``, which Qt never sets. Without it an
    invisible overlay steals every click over the game (observed live
    2026-07-16 on the first Mac run).

    Returns True when a system-level setting was applied. X11/Wayland have
    no portable equivalent here; callers keep the Qt attribute regardless.
    """
    if sys.platform != "darwin":
        return False
    try:
        import ctypes

        import objc  # pyobjc — same dependency as the Quartz locator
        from PySide6.QtGui import QGuiApplication

        # winId() is only an NSView* under the real cocoa QPA — under
        # "offscreen" (tests) it's a fake handle and dereferencing it via
        # objc would crash the interpreter.
        if QGuiApplication.platformName() != "cocoa":
            return False

        wid = int(widget.winId())  # NSView* on macOS
        if not wid:
            return False
        view = objc.objc_object(c_void_p=ctypes.c_void_p(wid))
        window = view.window()
        if window is None:
            return False
        window.setIgnoresMouseEvents_(bool(enabled))
        return True
    except Exception:
        return False
