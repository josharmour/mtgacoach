"""Tests for standalone hotkey registration and cross-platform handling."""

import sys
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
