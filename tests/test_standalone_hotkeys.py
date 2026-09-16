"""Tests for standalone hotkey registration and cross-platform handling."""

from unittest.mock import MagicMock

from arenamcp.standalone_hotkeys import _StandaloneHotkeysMixin


class DummyCoach(_StandaloneHotkeysMixin):
    def __init__(self):
        self._register_keyboard = True
        self._hotkey_registration_in_progress = False
        self._voice_output = None
        self.ui = MagicMock()


def test_register_hotkeys_does_not_raise_nameerror(monkeypatch):
    """Verify _register_hotkeys handles missing keyboard module without NameError on any OS."""
    coach = DummyCoach()
    # Execute synchronous hotkey registration logic
    coach._register_hotkeys()
    assert True


def test_register_hotkeys_skips_when_disabled(monkeypatch):
    """Verify _register_hotkeys does not hook keyboard when _register_keyboard is False."""
    import sys
    mock_keyboard = MagicMock()
    monkeypatch.setattr("arenamcp.standalone_hotkeys.keyboard", mock_keyboard)
    coach = DummyCoach()
    coach._register_keyboard = False
    coach._register_hotkeys()
    mock_keyboard.on_press_key.assert_not_called()
    mock_keyboard.add_hotkey.assert_not_called()
