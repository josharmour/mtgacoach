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
from tests.fakes import VirtualClock

import arenamcp.native_mac_input as native_mac_input
from arenamcp.native_mac_input import DesktopUnavailable, GameWindow, NativeMacInput

ARENA = GameWindow(window_id=7, pid=52636, bounds=(0, 0, 1600, 928))
TERMINAL = 4242


class _Foreground:
    def __init__(self, pid: int):
        self.current = pid

    def pid(self) -> int:
        return self.current


ANY_INPUT, NULL_EVENT = 4294967295, 0
# Quartz's CGEventType values for keyboard, mouse and trackpad input.
USER_EVENTS = {
    "kCGEventKeyDown": 10,
    "kCGEventKeyUp": 11,
    "kCGEventFlagsChanged": 12,
    "kCGEventMouseMoved": 5,
    "kCGEventLeftMouseDown": 1,
    "kCGEventLeftMouseDragged": 6,
    "kCGEventRightMouseDown": 3,
    "kCGEventRightMouseDragged": 7,
    "kCGEventOtherMouseDown": 25,
    "kCGEventOtherMouseDragged": 27,
    "kCGEventScrollWheel": 22,
}


def _quartz(idle: float, tickle: float | None = None) -> SimpleNamespace:
    """HID idle times: ``idle`` since real input, ``tickle`` since an app's display-wake null event."""
    tickle = idle if tickle is None else min(idle, tickle)

    def seconds_since(state, kind):
        assert state == 1
        return tickle if kind in (ANY_INPUT, NULL_EVENT) else idle

    return SimpleNamespace(
        kCGEventSourceStateHIDSystemState=1,
        kCGAnyInputEventType=ANY_INPUT,
        kCGEventNull=NULL_EVENT,
        CGEventSourceSecondsSinceLastEventType=seconds_since,
        **USER_EVENTS,
    )


def _controller(idle: float, threshold: float | None = 20.0, tickle: float | None = None) -> NativeMacInput:
    controller = NativeMacInput.__new__(NativeMacInput)
    controller._raise_when_idle_s = threshold
    controller._foreground = _Foreground(TERMINAL)
    controller._game_window = Mock(return_value=ARENA)
    controller._quartz = _quartz(idle, tickle)
    controller.focus_wait = ""
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
    # The foreground wait polls every 0.1 s up to its deadline: run it on
    # virtual time (a no-op sleep alone spun the CPU for the full 0.8 s).
    monkeypatch.setattr(native_mac_input, "time", VirtualClock())
    return state


def test_an_active_user_keeps_the_wait(appkit):
    controller = _controller(idle=3.0)
    appkit.controller = controller
    with pytest.raises(DesktopUnavailable, match=r"leave the keyboard and mouse for 20s"):
        controller._check_focus(ARENA)
    assert appkit.calls == []
    # The reason is kept for auto-queue's still-waiting log line.
    assert "keyboard/mouse used 3s ago" in controller.focus_wait
    assert "after 20s idle" in controller.focus_wait


def test_an_idle_user_gets_arena_brought_forward(appkit, caplog):
    controller = _controller(idle=25.0)
    appkit.controller = controller
    controller.focus_wait = "stale"
    with caplog.at_level("INFO", logger="arenamcp.native_mac_input"):
        controller._check_focus(ARENA)  # no raise: Arena is in front now
    assert appkit.calls == [("activate", 2)]
    assert controller.focus_wait == ""
    assert "Brought Arena forward" in caplog.text


def test_display_wake_tickles_from_arena_do_not_count_as_the_user(appkit):
    """2026-10-18 19:02:12-19:16:09: the Victory screen sat 14 minutes behind another app.

    `pmset -g assertions` showed MTGA holding "...queue.tickle.nxevent
    service:IOHIDSystem pid:58967 process:MTGA"; HID idle for
    kCGAnyInputEventType (and kCGEventNull) reset every ~10 s while keyDown idle
    was 4353 s and mouseMoved 318 s. Counting the tickle, the user never looked
    idle for 20 s, so Arena was never brought forward (no /usr/bin/open ran).
    """
    controller = _controller(idle=840.0, tickle=3.0)
    appkit.controller = controller
    assert controller._idle_seconds() == 840.0
    controller._check_focus(ARENA)  # no raise: Arena is in front now
    assert appkit.calls == [("activate", 2)]


def test_idle_uses_any_input_when_quartz_lacks_the_event_types():
    controller = _controller(idle=7.0)
    controller._quartz = SimpleNamespace(
        kCGEventSourceStateHIDSystemState=1,
        kCGAnyInputEventType=ANY_INPUT,
        CGEventSourceSecondsSinceLastEventType=lambda state, kind: 7.0 if kind == ANY_INPUT else 0.0,
    )
    assert controller._idle_seconds() == 7.0


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
    with pytest.raises(DesktopUnavailable, match="foreground"):
        controller._check_focus(ARENA)
    assert "macOS refused to bring Arena forward" in controller.focus_wait


def test_without_the_option_nothing_is_activated(appkit):
    controller = _controller(idle=600.0, threshold=None)
    appkit.controller = controller
    with pytest.raises(DesktopUnavailable, match=r"bring Arena to the foreground\.$"):
        controller._check_focus(ARENA)
    assert appkit.calls == []


def test_auto_queue_opts_in(monkeypatch):
    """The navigator builds its screen input with the idle-raise option on."""
    import arenamcp.auto_queue as auto_queue

    built: list[dict] = []

    class _Input:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def capture(self):
            raise DesktopUnavailable("bring Arena to the foreground.")

    monkeypatch.setattr(auto_queue, "NativeMacInput", _Input)
    monkeypatch.setattr(auto_queue.sys, "platform", "darwin")
    nav = auto_queue.AutoQueueNavigator(
        backend=SimpleNamespace(complete_with_image=Mock()),
        get_game_state=lambda: {"match_id": "ended"},
        bridge=SimpleNamespace(connected=False),
    )
    nav.set_enabled(True)
    assert nav.note_match_end({"match_id": "ended", "event_id": "Ladder"}, confirmed_match_end=True)
    nav._step(nav._generation, nav._abort)
    assert built == [{"raise_when_idle_s": auto_queue.FOCUS_WHEN_IDLE_S}]
    assert auto_queue.FOCUS_WHEN_IDLE_S >= 10
