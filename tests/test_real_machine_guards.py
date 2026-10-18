"""The suite's guards against acting on the real machine (tests/conftest.py)."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture
def guards():
    conftest = sys.modules["conftest"]
    yield conftest
    # The refusals below are deliberate: keep them from failing this test.
    conftest._REAL_MACHINE_VIOLATIONS.clear()


def test_a_real_repair_run_is_refused(guards):
    from arenamcp.repair_engine import RepairEngine

    with pytest.raises(guards.RealMachineAccess, match="repair pipeline"):
        RepairEngine().run()
    assert guards._REAL_MACHINE_VIOLATIONS


def test_a_fully_stubbed_repair_run_is_allowed(guards):
    from arenamcp.repair_engine import RepairEngine

    engine = RepairEngine()
    for name in guards._REPAIR_CHECKS:
        setattr(engine, name, lambda: None)
    assert engine.run().results == []
    assert guards._REAL_MACHINE_VIOLATIONS == []


def test_a_real_screen_grab_is_refused(guards, monkeypatch, tmp_path):
    from PIL import ImageGrab

    from arenamcp.autopilot_bug_capture import capture_mtga_screenshot
    from arenamcp.desktop import window_tracking

    with pytest.raises(guards.RealMachineAccess, match="real screen"):
        ImageGrab.grab()
    # The bug-report capture with Arena's window found reaches the guard too.
    monkeypatch.setattr(window_tracking, "get_mtga_window_rect", lambda *a, **k: (0, 0, 100, 100))
    with pytest.raises(guards.RealMachineAccess):
        capture_mtga_screenshot(tmp_path, "x")
    assert not list(tmp_path.glob("*.png"))
