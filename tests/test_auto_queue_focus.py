"""Auto-queue brings Arena forward once the user has left keyboard and mouse alone.

2026-10-13 00:09-00:14 (Auto-Queue on, the coach's terminal in front): the
Victory "[Click to Continue]" and Reward "Claim" screens sat with "Autoplay is
waiting: bring Arena to the foreground." until Arena was clicked by hand.
Auto-queue's clicks are real HID events that move the pointer, so it still
waits while someone is using another app.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import arenamcp.native_mac_input as native_mac_input
from arenamcp.native_mac_input import DesktopUnavailable, GameWindow, NativeMacInput

ARENA = GameWindow(window_id=7, pid=52636, bounds=(0, 0, 1600, 928))
TERMINAL = 4242


class _Foreground:
    def __init__(self, pid: int):
        self.current = pid

    def pid(self) -> int:
        return self.current


def _controller(idle: float, threshold: float | None = 20.0) -> NativeMacInput:
    controller = NativeMacInput.__new__(NativeMacInput)
    controller._raise_when_idle_s = threshold
    controller._foreground = _Foreground(TERMINAL)
    controller._game_window = Mock(return_value=ARENA)
    controller._quartz = SimpleNamespace(
        kCGEventSourceStateHIDSystemState=1,
        kCGAnyInputEventType=4294967295,
        CGEventSourceSecondsSinceLastEventType=lambda state, kind: idle,
    )
    return controller


@pytest.fixture
def appkit(monkeypatch):
    """A fake AppKit whose activation succeeds unless told otherwise."""
    state = SimpleNamespace(activates=True, calls=[], controller=None)

    class _App:
        def activateWithOptions_(self, options):
            state.calls.append(("activate", options))
            if state.activates:
                state.controller._foreground.current = ARENA.pid
            return state.activates

        def bundleURL(self):
            return SimpleNamespace(path=lambda: "/Applications/MTGA.app")

    fake = SimpleNamespace(
        NSApplicationActivateIgnoringOtherApps=2,
        NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=lambda pid: _App()),
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake)
    monkeypatch.setattr(native_mac_input.time, "sleep", lambda seconds: None)
    return state


def test_an_active_user_keeps_the_wait(appkit):
    controller = _controller(idle=3.0)
    appkit.controller = controller
    with pytest.raises(DesktopUnavailable, match=r"leave the keyboard and mouse for 20s"):
        controller._check_focus(ARENA)
    assert appkit.calls == []


def test_an_idle_user_gets_arena_brought_forward(appkit):
    controller = _controller(idle=25.0)
    appkit.controller = controller
    controller._check_focus(ARENA)  # no raise: Arena is in front now
    assert appkit.calls == [("activate", 2)]


def test_refused_activation_falls_back_to_launch_services(appkit, monkeypatch):
    controller = _controller(idle=25.0)
    appkit.controller = controller
    appkit.activates = False
    opened = []

    def fake_run(args, **kwargs):
        opened.append(args)
        controller._foreground.current = ARENA.pid
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(native_mac_input.subprocess, "run", fake_run)
    controller._check_focus(ARENA)
    assert opened == [["/usr/bin/open", "/Applications/MTGA.app"]]


def test_arena_that_never_comes_forward_still_waits(appkit, monkeypatch):
    controller = _controller(idle=25.0)
    appkit.controller = controller
    appkit.activates = False
    monkeypatch.setattr(
        native_mac_input.subprocess, "run", lambda args, **kwargs: SimpleNamespace(returncode=1)
    )
    clock = iter(range(0, 1000))
    monkeypatch.setattr(native_mac_input.time, "monotonic", lambda: next(clock))
    with pytest.raises(DesktopUnavailable, match="foreground"):
        controller._check_focus(ARENA)


def test_without_the_option_nothing_is_activated(appkit):
    controller = _controller(idle=600.0, threshold=None)
    appkit.controller = controller
    with pytest.raises(DesktopUnavailable, match=r"bring Arena to the foreground\.$"):
        controller._check_focus(ARENA)
    assert appkit.calls == []


def test_auto_queue_opts_in():
    import arenamcp.auto_queue as auto_queue

    source = open(auto_queue.__file__).read()
    assert "NativeMacInput(raise_when_idle_s=FOCUS_WHEN_IDLE_S)" in source
    assert auto_queue.FOCUS_WHEN_IDLE_S >= 10
