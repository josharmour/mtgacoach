"""Tests for the cross-platform HotkeyManager (arenamcp.desktop.hotkeys).

The real ``keyboard`` package is never imported: it hard-aborts the process
on macOS and requires root on Linux, so every test injects a stub into
``sys.modules`` before any code path can reach ``import keyboard``.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

try:
    from PySide6.QtGui import QShortcut
    from PySide6.QtWidgets import QWidget
except ImportError:
    pytest.skip("PySide6 C-extensions not available", allow_module_level=True)

from arenamcp.desktop.hotkeys import HotkeyManager


@pytest.fixture
def keyboard_stub(monkeypatch):
    stub = MagicMock()
    monkeypatch.setitem(sys.modules, "keyboard", stub)
    return stub


@pytest.fixture
def parent_widget(qapp):
    widget = QWidget()
    yield widget
    widget.deleteLater()


@pytest.fixture
def manager(parent_widget, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    mgr = HotkeyManager(parent=parent_widget)
    mgr._darwin_listener = None
    yield mgr
    mgr.unregister_all()


def test_register_windows_uses_keyboard(manager, keyboard_stub):
    callback = MagicMock()
    with patch("os.name", "nt"):
        manager.register("F3", callback)
    assert keyboard_stub.add_hotkey.call_count == 1
    assert keyboard_stub.add_hotkey.call_args[0][0] == "F3"


def test_register_posix_creates_qshortcut(manager, parent_widget):
    manager.register("F4", MagicMock())
    assert "F4" in manager._shortcuts
    keys = [s.key().toString() for s in parent_widget.findChildren(QShortcut)]
    assert "F4" in keys


def test_unregister_all_windows_unhooks_keyboard(manager, keyboard_stub):
    with patch("os.name", "nt"):
        manager.unregister_all()
    keyboard_stub.unhook_all.assert_called_once()


def test_unregister_all_posix_clears_shortcuts(manager, parent_widget):
    manager.register("F5", MagicMock())
    assert manager._shortcuts
    manager.unregister_all()
    assert not manager._shortcuts
    for shortcut in parent_widget.findChildren(QShortcut):
        assert not shortcut.isEnabled()
