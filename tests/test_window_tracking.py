"""Tests for arenamcp.desktop.window_tracking — the per-platform MTGA
window locator used by the desktop overlays.

Pure-python tests (dispatch, darwin parsing, caching) run headless on any
platform; the Qt overlay smoke tests are skipped when PySide6 is missing.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any, Optional

import pytest

from arenamcp.desktop import window_tracking as wt


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts with an empty locate cache and a clean Quartz probe."""
    wt.clear_cache()
    wt._quartz_module = None
    wt._quartz_checked = False
    yield
    wt.clear_cache()
    wt._quartz_module = None
    wt._quartz_checked = False


# -- Platform dispatch ---------------------------------------------------------


class _Recorder:
    def __init__(self, result: Optional[tuple] = None):
        self.calls: list[str] = []
        self.result = result

    def __call__(self, title: str):
        self.calls.append(title)
        return self.result


@pytest.mark.parametrize(
    "platform,backend",
    [
        ("win32", "_win32_rect"),
        ("darwin", "_darwin_rect"),
        ("linux", "_linux_rect"),
        # anything not win32/darwin dispatches to the linux/X11 backend
        ("freebsd14", "_linux_rect"),
    ],
)
def test_dispatch_routes_to_platform_backend(monkeypatch, platform, backend):
    monkeypatch.setattr(wt, "_platform", lambda: platform)
    recorders = {
        name: _Recorder(result=(1, 2, 300, 400))
        for name in ("_win32_rect", "_darwin_rect", "_linux_rect")
    }
    for name, rec in recorders.items():
        monkeypatch.setattr(wt, name, rec)

    assert wt.get_mtga_window_rect() == (1, 2, 300, 400)
    assert recorders[backend].calls == ["MTGA"]
    for name, rec in recorders.items():
        if name != backend:
            assert rec.calls == []


def test_dispatch_passes_custom_title(monkeypatch):
    monkeypatch.setattr(wt, "_platform", lambda: "linux")
    rec = _Recorder(result=None)
    monkeypatch.setattr(wt, "_linux_rect", rec)
    assert wt.get_mtga_window_rect("SomeGame") is None
    assert rec.calls == ["SomeGame"]


# -- Caching -------------------------------------------------------------------


def test_result_is_cached_briefly(monkeypatch):
    monkeypatch.setattr(wt, "_platform", lambda: "linux")
    rec = _Recorder(result=(10, 20, 800, 600))
    monkeypatch.setattr(wt, "_linux_rect", rec)

    assert wt.get_mtga_window_rect() == (10, 20, 800, 600)
    assert wt.get_mtga_window_rect() == (10, 20, 800, 600)
    assert wt.get_mtga_window_rect() == (10, 20, 800, 600)
    # Three overlay polls inside the TTL → one real lookup
    assert len(rec.calls) == 1


def test_none_results_are_cached_too(monkeypatch):
    monkeypatch.setattr(wt, "_platform", lambda: "linux")
    rec = _Recorder(result=None)
    monkeypatch.setattr(wt, "_linux_rect", rec)
    assert wt.get_mtga_window_rect() is None
    assert wt.get_mtga_window_rect() is None
    assert len(rec.calls) == 1


def test_clear_cache_forces_relookup(monkeypatch):
    monkeypatch.setattr(wt, "_platform", lambda: "linux")
    rec = _Recorder(result=(0, 0, 100, 100))
    monkeypatch.setattr(wt, "_linux_rect", rec)
    wt.get_mtga_window_rect()
    wt.clear_cache()
    wt.get_mtga_window_rect()
    assert len(rec.calls) == 2


def test_cache_expires_after_ttl(monkeypatch):
    monkeypatch.setattr(wt, "_platform", lambda: "linux")
    rec = _Recorder(result=(0, 0, 100, 100))
    monkeypatch.setattr(wt, "_linux_rect", rec)

    fake_now = [1000.0]
    monkeypatch.setattr(wt.time, "monotonic", lambda: fake_now[0])
    wt.get_mtga_window_rect()
    fake_now[0] += wt._CACHE_TTL_S + 0.01
    wt.get_mtga_window_rect()
    assert len(rec.calls) == 2


# -- darwin (Quartz) parsing ----------------------------------------------------


def _fake_quartz(window_list: Any) -> types.SimpleNamespace:
    """Build a stand-in for pyobjc's Quartz module."""

    def copy_window_info(options, relative_to):
        # The backend must ask for on-screen windows only.
        assert options == "OPT_ONSCREEN"
        assert relative_to == "NULL_WINDOW"
        return window_list

    return types.SimpleNamespace(
        kCGWindowListOptionOnScreenOnly="OPT_ONSCREEN",
        kCGNullWindowID="NULL_WINDOW",
        CGWindowListCopyWindowInfo=copy_window_info,
    )


def _install_quartz(monkeypatch, window_list: Any) -> None:
    quartz = _fake_quartz(window_list)
    monkeypatch.setattr(wt, "_load_quartz", lambda: quartz)


def test_darwin_finds_mtga_window(monkeypatch):
    _install_quartz(monkeypatch, [
        {
            "kCGWindowOwnerName": "Dock",
            "kCGWindowLayer": 20,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 3456, "Height": 80},
        },
        {
            "kCGWindowOwnerName": "MTGA",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 100.0, "Y": 50.0, "Width": 1920.0, "Height": 1080.0},
        },
    ])
    assert wt._darwin_rect("MTGA") == (100, 50, 1920, 1080)


def test_darwin_matches_owner_name_containing_title(monkeypatch):
    # Steam's process name may be e.g. "MTGA.app" / "MTGA Helper" — the
    # filter is "owner name CONTAINS the title".
    _install_quartz(monkeypatch, [
        {
            "kCGWindowOwnerName": "com.wizards.MTGA Helper",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 5, "Y": 25, "Width": 1280, "Height": 720},
        },
    ])
    assert wt._darwin_rect("MTGA") == (5, 25, 1280, 720)


def test_darwin_skips_tiny_and_nonzero_layer_windows(monkeypatch):
    _install_quartz(monkeypatch, [
        # Menu-bar extra owned by MTGA: too small.
        {
            "kCGWindowOwnerName": "MTGA",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 3000, "Y": 0, "Width": 30, "Height": 24},
        },
        # Floating panel: non-zero layer.
        {
            "kCGWindowOwnerName": "MTGA",
            "kCGWindowLayer": 3,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1920, "Height": 1080},
        },
        # The actual game window.
        {
            "kCGWindowOwnerName": "MTGA",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 10, "Y": 40, "Width": 1600, "Height": 900},
        },
    ])
    assert wt._darwin_rect("MTGA") == (10, 40, 1600, 900)


def test_darwin_exact_owner_beats_substring_match(monkeypatch):
    # Observed live on the dev Mac: "MTGA_Draft_Tool" (a third-party
    # companion app) sits in front of the real "MTGA" window in the
    # front-to-back Quartz list. The exact owner must win.
    _install_quartz(monkeypatch, [
        {
            "kCGWindowOwnerName": "MTGA_Draft_Tool",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 48, "Y": 33, "Width": 600, "Height": 994},
        },
        {
            "kCGWindowOwnerName": "MTGA",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 273, "Y": 33, "Width": 1280, "Height": 748},
        },
    ])
    assert wt._darwin_rect("MTGA") == (273, 33, 1280, 748)


def test_darwin_no_match_returns_none(monkeypatch):
    _install_quartz(monkeypatch, [
        {
            "kCGWindowOwnerName": "Finder",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 1200, "Height": 800},
        },
    ])
    assert wt._darwin_rect("MTGA") is None


def test_darwin_empty_or_none_window_list(monkeypatch):
    _install_quartz(monkeypatch, [])
    assert wt._darwin_rect("MTGA") is None
    _install_quartz(monkeypatch, None)
    assert wt._darwin_rect("MTGA") is None


def test_darwin_malformed_entries_are_skipped(monkeypatch):
    _install_quartz(monkeypatch, [
        {"kCGWindowOwnerName": "MTGA"},  # no bounds at all → w/h 0 → skipped
        {"kCGWindowOwnerName": "MTGA", "kCGWindowBounds": "garbage"},
        {
            "kCGWindowOwnerName": "MTGA",
            "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": 1, "Y": 2, "Width": 640, "Height": 480},
        },
    ])
    assert wt._darwin_rect("MTGA") == (1, 2, 640, 480)


def test_darwin_missing_pyobjc_degrades_to_none(monkeypatch):
    monkeypatch.setattr(wt, "_load_quartz", lambda: None)
    assert wt._darwin_rect("MTGA") is None


def test_load_quartz_handles_import_error(monkeypatch):
    # Force the real loader down the ImportError path.
    monkeypatch.setitem(sys.modules, "Quartz", None)  # import → ImportError
    wt._quartz_checked = False
    wt._quartz_module = None
    assert wt._load_quartz() is None
    # Result is memoized.
    assert wt._quartz_checked is True


# -- linux delegation ------------------------------------------------------------


def test_linux_rect_delegates_to_runtime(monkeypatch):
    from arenamcp.desktop import runtime

    monkeypatch.setattr(
        runtime,
        "get_linux_window_geometry",
        lambda title="MTGA": {
            "left": 7, "top": 8, "width": 1024, "height": 768,
            "is_minimized": False,
        },
    )
    assert wt._linux_rect("MTGA") == (7, 8, 1024, 768)


def test_linux_rect_minimized_or_missing_is_none(monkeypatch):
    from arenamcp.desktop import runtime

    monkeypatch.setattr(
        runtime,
        "get_linux_window_geometry",
        lambda title="MTGA": {
            "left": 7, "top": 8, "width": 1024, "height": 768,
            "is_minimized": True,
        },
    )
    assert wt._linux_rect("MTGA") is None

    monkeypatch.setattr(runtime, "get_linux_window_geometry", lambda title="MTGA": None)
    assert wt._linux_rect("MTGA") is None


# -- win32 backend (pygetwindow shim) --------------------------------------------


class _FakeWin:
    def __init__(self, title, left=1, top=2, width=800, height=600, minimized=False):
        self.title = title
        self.left = left
        self.top = top
        self.width = width
        self.height = height
        self.isMinimized = minimized


def test_win32_rect_exact_title_match(monkeypatch):
    fake_gw = types.SimpleNamespace(
        getWindowsWithTitle=lambda title: [
            _FakeWin("MTGA launcher helper"),  # substring hit, wrong title
            _FakeWin("MTGA", left=11, top=22, width=1920, height=1080),
        ]
    )
    monkeypatch.setattr(wt, "gw", fake_gw)
    assert wt._win32_rect("MTGA") == (11, 22, 1920, 1080)


def test_win32_rect_minimized_returns_none(monkeypatch):
    fake_gw = types.SimpleNamespace(
        getWindowsWithTitle=lambda title: [_FakeWin("MTGA", minimized=True)]
    )
    monkeypatch.setattr(wt, "gw", fake_gw)
    assert wt._win32_rect("MTGA") is None


def test_win32_rect_no_pygetwindow_returns_none(monkeypatch):
    monkeypatch.setattr(wt, "gw", None)
    assert wt._win32_rect("MTGA") is None


def test_win32_rect_swallows_backend_errors(monkeypatch):
    def boom(title):
        raise RuntimeError("EnumWindows failed")

    monkeypatch.setattr(wt, "gw", types.SimpleNamespace(getWindowsWithTitle=boom))
    assert wt._win32_rect("MTGA") is None


# -- Qt overlay smoke tests (skipped when PySide6 unavailable) --------------------

try:
    import PySide6  # noqa: F401

    _HAVE_PYSIDE6 = True
except Exception:  # pragma: no cover - env specific
    _HAVE_PYSIDE6 = False

needs_pyside6 = pytest.mark.skipif(
    not _HAVE_PYSIDE6, reason="PySide6 not installed in this environment"
)


@pytest.fixture()
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@needs_pyside6
def test_hud_window_shows_on_any_platform(qt_app):
    """A4 regression: setVisible(True) must not early-return off-Windows."""
    from arenamcp.desktop.hud import HudWindow

    hud = HudWindow()
    try:
        hud.setVisible(True)
        assert hud.isVisible()
    finally:
        hud.close()


@needs_pyside6
def test_overlays_show_on_any_platform(qt_app):
    from PySide6.QtCore import Qt
    from arenamcp.desktop.advice_panel import AdvicePanelWindow
    from arenamcp.desktop.card_overlay import CardOverlayWindow
    from arenamcp.desktop.match_overlay import MatchOverlayWindow

    for cls in (CardOverlayWindow, MatchOverlayWindow, AdvicePanelWindow):
        w = cls()
        try:
            w.setVisible(True)
            assert w.isVisible(), f"{cls.__name__} refused to show"
            assert w.windowFlags() & Qt.WindowType.FramelessWindowHint
            assert w.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
            assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        finally:
            w.close()


@needs_pyside6
def test_hud_click_through_uses_qt_attribute(qt_app):
    from PySide6.QtCore import Qt
    from arenamcp.desktop.hud import HudWindow

    hud = HudWindow()
    try:
        hud.set_click_through(True)
        assert hud.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        hud.set_click_through(False)
        assert not hud.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    finally:
        hud.close()


@needs_pyside6
def test_hud_follows_located_window(qt_app, monkeypatch):
    """When the locator reports a rect, the HUD shows and moves next to it."""
    from arenamcp.desktop import hud as hud_mod
    from arenamcp.desktop.hud import HudWindow

    monkeypatch.setattr(hud_mod, "get_mtga_window_rect", lambda: (100, 200, 1280, 720))
    hud = HudWindow()
    try:
        hud._should_show = True
        hud._follow_mtga()
        assert hud.isVisible()
        assert (hud.x(), hud.y()) == (110, 210)  # rect origin + 10px margin

        # Locator loses the window → HUD hides.
        monkeypatch.setattr(hud_mod, "get_mtga_window_rect", lambda: None)
        hud._follow_mtga()
        assert not hud.isVisible()
    finally:
        hud.close()
