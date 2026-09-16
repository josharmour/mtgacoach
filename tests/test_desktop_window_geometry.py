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
    window._session.shutdown()
    window.close()
