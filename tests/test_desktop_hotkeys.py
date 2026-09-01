"""Tests for the cross-platform HotkeyManager (arenamcp.desktop.hotkeys).

The real ``keyboard`` package is never imported: it hard-aborts the process
on macOS and requires root on Linux, so every test injects a stub into
``sys.modules`` before any code path can reach ``import keyboard``.
"""

import sys
from unittest.mock import MagicMock

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
def manager(parent_widget):
    mgr = HotkeyManager(parent=parent_widget)
    mgr._darwin_listener = None
    yield mgr
    mgr.unregister_all()


def test_register_creates_qshortcut(manager, parent_widget):
    callback = MagicMock()
    manager.register("F4", callback)
    assert "F4" in manager._shortcuts
    keys = [s.key().toString() for s in parent_widget.findChildren(QShortcut)]
    assert "F4" in keys


def test_register_darwin_delegates_to_listener(parent_widget, monkeypatch):
    monkeypatch.setattr("arenamcp.desktop.hotkeys.sys.platform", "darwin")
    mgr = HotkeyManager(parent=parent_widget)
    fake_listener = MagicMock()
    fake_listener.register.return_value = True
    mgr._darwin_listener = fake_listener

    callback = MagicMock()
    mgr.register("F3", callback)
    fake_listener.register.assert_called_once_with("F3", callback)
    assert "F3" not in mgr._shortcuts


def test_unregister_all_clears_shortcuts(manager, parent_widget):
    manager.register("F5", MagicMock())
    assert manager._shortcuts
    manager.unregister_all()
    assert not manager._shortcuts
    for shortcut in parent_widget.findChildren(QShortcut):
        assert not shortcut.isEnabled()
