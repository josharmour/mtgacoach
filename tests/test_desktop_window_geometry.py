from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QMainWindow

from arenamcp.desktop.main_window import MainWindow


@pytest.mark.parametrize("available", [QRect(0, 0, 1280, 720), QRect(-1280, -720, 1280, 720)])
def test_window_frame_fits_available_screen(qapp, monkeypatch, available):
    window = QMainWindow()
    window.resize(1500, 1000)
    window.move(available.left(), available.top() - 100)
    window.show()
    qapp.processEvents()
    screen = Mock()
    screen.availableGeometry.return_value = available
    monkeypatch.setattr("arenamcp.desktop.main_window.QGuiApplication.screenAt", lambda point: screen)

    MainWindow._keep_window_on_screen(window)

    assert available.contains(window.frameGeometry())
    window.close()


def test_saved_position_restores_frame_origin(qapp):
    window = QMainWindow()
    window._WINDOW_GEOMETRY_KEY = MainWindow._WINDOW_GEOMETRY_KEY
    window._settings = Mock()
    available = window.screen().availableGeometry()
    window._settings.get.return_value = {
        "x": available.left() + 20,
        "y": available.top() + 20,
        "width": 300,
        "height": 400,
    }

    MainWindow._apply_window_geometry(window)
    window.show()
    qapp.processEvents()

    assert window.frameGeometry().x() == available.left() + 20
    assert window.frameGeometry().y() == available.top() + 20
    assert window.size().width() == 300
    assert window.size().height() == 400
    assert window.minimumWidth() == 240
    window.close()


def test_coach_stack_can_resize_to_sidebar_width(qapp):
    from arenamcp.desktop.coach_session import CoachSession
    from arenamcp.desktop.repair_tab import RepairTab

    window = QMainWindow()
    window._session = CoachSession(window)
    window.repair_tab = RepairTab()
    window._show_coach_view = Mock()
    window._show_repair_view = Mock()
    window._show_performance_view = Mock()
    window._restart_coach = Mock()
    MainWindow._build_central_widget(window)
    window.setMinimumWidth(240)
    window.show()
    qapp.processEvents()
    window.resize(240, 900)
    qapp.processEvents()

    assert window.width() == 240
    assert window.coach_panel.width() <= 240
    window.coach_panel.restart_btn.click()
    window._restart_coach.assert_called_once()
    window._session.shutdown()
    window.close()


def test_restart_stops_coach_once_and_blocks_delayed_start(qapp, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QPushButton

    from arenamcp.desktop import main_window
    from arenamcp.desktop.app import RESTART_EXIT_CODE

    window = QMainWindow()
    window._closed = False
    window._settings = Mock()
    window._WINDOW_GEOMETRY_KEY = MainWindow._WINDOW_GEOMETRY_KEY
    window._session = Mock()
    window._session.shutdown.side_effect = lambda: MainWindow._start_session(window)
    window._hotkeys = Mock()
    window._ui_watchdog = Mock()
    window.coach_panel = SimpleNamespace(restart_btn=QPushButton("Restart Coach", window))
    app = Mock()
    monkeypatch.setattr(main_window, "QApplication", SimpleNamespace(instance=lambda: app))

    MainWindow._restart_coach(window)
    MainWindow._restart_coach(window)

    assert window._closed
    assert not window.coach_panel.restart_btn.isEnabled()
    assert window.coach_panel.restart_btn.text() == "Restarting…"
    window._session.shutdown.assert_called_once()
    window._session.start.assert_not_called()
    window._hotkeys.unregister_all.assert_called_once()
    window._ui_watchdog.stop.assert_called_once()
    window._settings.set.assert_called_once()
    app.exit.assert_called_once_with(RESTART_EXIT_CODE)
    window.close()
