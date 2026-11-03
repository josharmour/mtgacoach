from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.main_window import MainWindow


def test_main_window_init(qapp, monkeypatch):
    from unittest.mock import Mock

    from arenamcp.desktop.repair_tab import RepairTab

    # Showing Setup & Repair auto-runs the real RepairEngine on a thread,
    # which repairs the real install (BepInEx into the MTGA folder).
    run_checks = Mock()
    monkeypatch.setattr(RepairTab, "run_checks", run_checks)
    win = MainWindow()
    win.show()
    assert win.coach_panel is not None
    assert win.repair_tab is not None
    assert win._stack.count() == 4  # coach, Setup & Repair, Match History, Decks

    # Test navigation
    win._show_repair_view()
    assert win._stack.currentIndex() == 1
    run_checks.assert_called_once()  # the tab's auto-run, into the stub
    win._show_performance_view()
    assert win._stack.currentIndex() == 2
    win._show_coach_view()
    assert win._stack.currentIndex() == 0

    win.close()


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


def test_update_button_tracks_download_and_requests_approval(qapp, monkeypatch, tmp_path):
    import json
    from unittest.mock import Mock

    from PySide6.QtWidgets import QMessageBox

    marker = tmp_path / "pending.json"
    monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", str(marker))
    win = MainWindow()
    try:
        button = win.coach_panel.update_btn
        assert not button.isHidden() and not button.isEnabled()
        from arenamcp.desktop.release_updates import pending_update

        marker.write_text(json.dumps({"version": "1.0.14", "downloading": True, "automatic": None}))
        win._show_pending_release()
        assert "Downloading 1.0.14" in button.text() and not button.isEnabled()
        assert pending_update() is None
        marker.write_text(json.dumps({"version": "1.0.14", "automatic": False}))
        win._show_pending_release()
        assert button.isEnabled() and button.text() == "Update to 1.0.14"
        apply = Mock()
        monkeypatch.setattr(win, "_apply_release", apply)
        for answer in (QMessageBox.No, QMessageBox.Yes):
            monkeypatch.setattr(QMessageBox, "question", lambda *args, result=answer: result)
            button.click()
            if answer == QMessageBox.No:
                apply.assert_not_called()
        apply.assert_called_once_with(approved=True)
        marker.unlink()
        win._show_pending_release()
        assert not button.isEnabled()
    finally:
        win.close()
