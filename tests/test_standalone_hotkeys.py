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
    # The real module would install global keyboard hooks on Windows/Linux.
    monkeypatch.setattr("arenamcp.standalone_hotkeys.keyboard", None)
    coach = DummyCoach()
    coach._register_hotkeys()
    coach.ui.log.assert_not_called()


def test_register_hotkeys_hooks_the_function_keys_and_ctrl_0(monkeypatch):
    mock_keyboard = MagicMock()
    monkeypatch.setattr("arenamcp.standalone_hotkeys.keyboard", mock_keyboard)
    coach = DummyCoach()
    handlers = {
        "f2": "_on_style_toggle_hotkey",
        "f3": "_on_frequency_toggle_hotkey",
        "f5": "_on_mute_hotkey",
        "f6": "_on_voice_cycle_hotkey",
        "f7": "_on_bug_report_hotkey",
        "f8": "_on_swap_seat_hotkey",
        "f10": "run_speed_test",
        "f11": "_on_provider_cycle_hotkey",
        "f12": "_on_model_cycle_hotkey",
        "ctrl+0": "_on_read_win_plan",
    }
    for name in handlers.values():
        setattr(coach, name, MagicMock(name=name))

    coach._register_hotkeys()

    keys = [c.args[0] for c in mock_keyboard.on_press_key.call_args_list]
    assert keys == ["f2", "f3", "f5", "f6", "f7", "f8", "f10", "f11", "f12"]
    mock_keyboard.add_hotkey.assert_called_once()
    assert mock_keyboard.add_hotkey.call_args.args[0] == "ctrl+0"
    calls = mock_keyboard.on_press_key.call_args_list + mock_keyboard.add_hotkey.call_args_list
    assert all(c.kwargs == {"suppress": False} for c in calls)
    # Each key reaches its own handler.
    for c in mock_keyboard.on_press_key.call_args_list:
        c.args[1](None)
    mock_keyboard.add_hotkey.call_args.args[1]()
    for name in handlers.values():
        getattr(coach, name).assert_called_once_with()


def test_register_hotkeys_skips_when_disabled(monkeypatch):
    """Verify _register_hotkeys does not hook keyboard when _register_keyboard is False."""
    mock_keyboard = MagicMock()
    monkeypatch.setattr("arenamcp.standalone_hotkeys.keyboard", mock_keyboard)
    coach = DummyCoach()
    coach._register_keyboard = False
    coach._register_hotkeys()
    mock_keyboard.on_press_key.assert_not_called()
    mock_keyboard.add_hotkey.assert_not_called()
