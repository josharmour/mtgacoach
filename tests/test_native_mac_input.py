"""Native macOS screen input (auto-queue's clicks) refuses stale, covered or unverifiable input."""

import ctypes
import threading
from unittest.mock import Mock

import pytest
from tests.mac_fakes import command, fake_native_controller, make_frame

from arenamcp.native_mac_input import (
    DesktopAction,
    DesktopUnavailable,
    GameWindow,
    NativeMacInput,
    _ForegroundProcess,
    frame_changed,
)


@pytest.mark.parametrize(
    "payload",
    [
        command(point=[-0.1, 0.5]),
        command(point=[1.1, 0.5]),
        command(point=[float("nan"), 0.5]),
        command(point=[True, 0.5]),
        command(confidence=float("inf")),
        command(confidence=True),
        command("key", key="cmd+q"),
        command("text", text="hello"),
        command("text", text="1000"),
        command("scroll", amount=100),
        command("drag", end=[0, 1]),
        command("shell"),
    ],
)
def test_reject_invalid_input(payload):
    with pytest.raises(ValueError):
        DesktopAction.from_dict(payload)


def test_retina_scaling_and_negative_monitor_origin():
    window = GameWindow(42, 123, (-1440, -100, 1440, 900))
    frame = make_frame(window=window, size=(2880, 1800))
    assert frame.window.screen_point((0.25, 0.5)) == (-1080, 350)
    action = DesktopAction.from_dict(command())
    assert not frame_changed(frame, make_frame(window=window, size=(1440, 900)), action)


def test_foreground_changes_are_observed_without_appkit_run_loop():
    controller = NativeMacInput.__new__(NativeMacInput)
    window = make_frame().window
    controller._game_window = Mock(return_value=window)
    controller._foreground = _ForegroundProcess.__new__(_ForegroundProcess)
    controller._foreground._services = Mock()
    controller._foreground._services.GetFrontProcess.return_value = 0
    active_pid = 999

    def get_pid(serial, output):
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int))[0] = active_pid
        return 0

    controller._foreground._services.GetProcessPID.side_effect = get_pid
    with pytest.raises(DesktopUnavailable, match="foreground"):
        controller._check_focus(window)
    active_pid = window.pid
    controller._check_focus(window)
    active_pid = 999
    with pytest.raises(DesktopUnavailable, match="foreground"):
        controller._check_focus(window)


@pytest.mark.parametrize("failed_call", ["GetFrontProcess", "GetProcessPID"])
def test_failed_foreground_query_refuses_input(failed_call):
    foreground = _ForegroundProcess.__new__(_ForegroundProcess)
    foreground._services = Mock()
    foreground._services.GetFrontProcess.return_value = 0
    foreground._services.GetProcessPID.return_value = 0
    getattr(foreground._services, failed_call).return_value = -1
    with pytest.raises(DesktopUnavailable, match="Cannot verify"):
        foreground.pid()


def test_cancelled_drag_always_releases_mouse():
    controller = fake_native_controller()
    aborted = threading.Event()

    def mouse(event_type, *args):
        if event_type == 1:
            aborted.set()

    controller._mouse.side_effect = mouse
    action = DesktopAction.from_dict(command("drag", end=[0.5, 0.2]))
    assert not controller.execute(make_frame(), action, aborted)
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5, 1, 2]


def test_double_click_posts_two_complete_clicks_with_click_counts():
    controller = fake_native_controller()
    action = DesktopAction.from_dict(command("double_click"))
    assert controller.execute(make_frame(), action, threading.Event())
    events = [call.args for call in controller._mouse.call_args_list]
    assert [event[0] for event in events] == [5, 1, 2, 1, 2]
    assert [event[2] for event in events[1:]] == [1, 1, 2, 2]
    assert all(event[1] == (460, 320) for event in events)


@pytest.mark.parametrize("kind", ["click", "double_click", "drag"])
def test_hover_reflow_is_observed_before_pressing_mouse(kind):
    controller = fake_native_controller()
    controller.capture.side_effect = lambda: make_frame(color="red")
    action = DesktopAction.from_dict(command(kind, **({"end": [0.5, 0.2]} if kind == "drag" else {})))
    with pytest.raises(DesktopUnavailable, match="changed on hover"):
        controller.execute(make_frame(), action, threading.Event())
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5]


def test_stop_during_hover_capture_never_presses_mouse():
    controller = fake_native_controller()
    aborted = threading.Event()

    def capture():
        aborted.set()
        return make_frame()

    controller.capture.side_effect = capture
    assert not controller.execute(make_frame(), DesktopAction.from_dict(command("double_click")), aborted)
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5]


def test_stop_after_first_click_prevents_second_click():
    controller = fake_native_controller()
    aborted = threading.Event()

    def mouse(event_type, *args):
        if event_type == 2:
            aborted.set()

    controller._mouse.side_effect = mouse
    action = DesktopAction.from_dict(command("double_click"))
    assert not controller.execute(make_frame(), action, aborted)
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5, 1, 2]


def test_stale_capture_is_not_clicked():
    controller = fake_native_controller()
    frame = make_frame()
    frame.captured_at -= 5
    with pytest.raises(DesktopUnavailable, match="expired"):
        controller.execute(frame, DesktopAction.from_dict(command()), threading.Event())
    controller._mouse.assert_not_called()


def test_covering_window_blocks_click():
    controller = fake_native_controller()
    controller._system_element = object()
    controller._accessibility = Mock()
    controller._accessibility.AXUIElementCopyElementAtPosition.return_value = (0, object())
    controller._accessibility.AXUIElementGetPid.return_value = (0, 999)
    with pytest.raises(DesktopUnavailable, match="covers"):
        NativeMacInput._check_point(controller, make_frame().window, (0.5, 0.5))


def test_click_through_overlay_does_not_block_arena_input():
    controller = fake_native_controller()
    controller._windows = Mock(side_effect=AssertionError("Window rectangles are not input hit tests"))
    controller._system_element = object()
    controller._accessibility = Mock()
    controller._accessibility.AXUIElementCopyElementAtPosition.return_value = (0, object())
    controller._accessibility.AXUIElementGetPid.return_value = (0, make_frame().window.pid)
    NativeMacInput._check_point(controller, make_frame().window, (0.5, 0.5))
    controller._accessibility.AXUIElementCopyElementAtPosition.assert_called_once_with(
        controller._system_element, 460, 275, None
    )


@pytest.mark.parametrize(
    "hit_result,pid_result", [((-1, None), (0, 123)), ((0, None), (0, 123)), ((0, object()), (-1, 123))]
)
def test_unverifiable_input_target_blocks_click(hit_result, pid_result):
    controller = fake_native_controller()
    controller._system_element = object()
    controller._accessibility = Mock()
    controller._accessibility.AXUIElementCopyElementAtPosition.return_value = hit_result
    controller._accessibility.AXUIElementGetPid.return_value = pid_result
    with pytest.raises(DesktopUnavailable, match="Cannot verify"):
        NativeMacInput._check_point(controller, make_frame().window, (0.5, 0.5))
