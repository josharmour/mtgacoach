from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.main_window import MainWindow


def test_main_window_init(qapp):
    win = MainWindow()
    win.show()
    assert win.coach_panel is not None
    assert win.repair_tab is not None
    assert win._stack.count() == 3

    # Test navigation
    win._show_repair_view()
    assert win._stack.currentIndex() == 1
    win._show_performance_view()
    assert win._stack.currentIndex() == 2
    win._show_coach_view()
    assert win._stack.currentIndex() == 0

    win.close()


def test_restart_exit_code_is_defined():
    from arenamcp.desktop.app import RESTART_EXIT_CODE

    assert RESTART_EXIT_CODE == 42


def test_watchdog_ping_bridge_delivers_pong(qapp):
    from arenamcp.desktop.ui_watchdog import WatchdogPingBridge

    bridge = WatchdogPingBridge()
    pongs = []

    def on_pong():
        pongs.append(True)

    bridge.ping_requested.emit(on_pong)
    qapp.processEvents()

    assert pongs == [True]


def test_reload_engine_keeps_window_open(qapp, monkeypatch):
    from unittest.mock import Mock

    win = MainWindow()
    reload_engine = Mock()
    monkeypatch.setattr(win._session, "restart", reload_engine)
    try:
        win.coach_panel.reload_engine_requested.emit()
        reload_engine.assert_called_once_with()
        assert not win._closed
        assert win._session is win.coach_panel._session
    finally:
        win.close()


def test_autopilot_bug_button_routes_to_capture(qapp, monkeypatch):
    from unittest.mock import Mock

    capture = Mock()
    monkeypatch.setattr("arenamcp.desktop.coach_session.CoachSession.trigger_autopilot_bug", capture)
    win = MainWindow()
    try:
        win.coach_panel.autopilot_bug_requested.emit()
        capture.assert_called_once_with()
    finally:
        win.close()
