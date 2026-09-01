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
