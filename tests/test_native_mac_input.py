"""Native macOS screen input (auto-queue's clicks) refuses stale, covered or unverifiable input."""

import ctypes
import time
from unittest.mock import Mock

import pytest
from PIL import Image, ImageDraw
from tests.mac_fakes import InstantEvent, command, fake_native_controller, make_frame

from arenamcp.native_mac_input import (
    DesktopAction,
    DesktopFrame,
    DesktopUnavailable,
    GameWindow,
    NativeMacInput,
    _ForegroundProcess,
    click_changed_screen,
    frame_changed,
)


@pytest.mark.parametrize(
    "payload",
    [
        command(point=[-0.1, 0.5]),
        command(point=[1.1, 0.5]),
        command(point=[float("nan"), 0.5]),
        command(point=[True, 0.5]),
        command(point=None),
        command(confidence=float("inf")),
        command(confidence=True),
        command(reason=" "),
        # Only auto-queue's click is left (bridge-only autoplay, 2026-10-18).
        command("double_click"),
        command("key", key="space"),
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


def test_stop_during_the_press_still_releases_mouse():
    controller = fake_native_controller()
    aborted = InstantEvent()

    def mouse(event_type, *args):
        if event_type == 1:
            aborted.set()

    controller._mouse.side_effect = mouse
    assert not controller.execute(make_frame(), DesktopAction.from_dict(command()), aborted)
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5, 1, 2]


def test_click_hovers_then_posts_one_complete_click_at_the_window_point():
    controller = fake_native_controller()
    assert controller.execute(make_frame(), DesktopAction.from_dict(command()), InstantEvent())
    events = [call.args for call in controller._mouse.call_args_list]
    assert [event[0] for event in events] == [5, 1, 2]
    assert all(event[1] == (460, 320) for event in events)


def test_hover_reflow_is_observed_before_pressing_mouse():
    controller = fake_native_controller()
    controller.capture.side_effect = lambda: make_frame(color="red")
    with pytest.raises(DesktopUnavailable, match="changed on hover"):
        controller.execute(make_frame(), DesktopAction.from_dict(command()), InstantEvent())
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5]


def test_stop_during_hover_capture_never_presses_mouse():
    controller = fake_native_controller()
    aborted = InstantEvent()

    def capture():
        aborted.set()
        return make_frame()

    controller.capture.side_effect = capture
    assert not controller.execute(make_frame(), DesktopAction.from_dict(command()), aborted)
    assert [call.args[0] for call in controller._mouse.call_args_list] == [5]


def test_stale_capture_is_not_clicked():
    controller = fake_native_controller()
    frame = make_frame()
    frame.captured_at -= 5
    with pytest.raises(DesktopUnavailable, match="expired"):
        controller.execute(frame, DesktopAction.from_dict(command()), InstantEvent())
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


# ---------------------------------------------------------------------------
# Whether a click did anything (auto-queue's "had no visible effect")
# ---------------------------------------------------------------------------

# Dark 1600x935 Arena screens like the 2026-10-19 captures: Claim at the bottom
# right, the reward card beside it, "[Click to Continue]" at the bottom centre.
CARD = (830, 250, 1050, 570)
CLAIM_BUTTON = (1335, 850, 1555, 900)
PROMPT = (715, 895, 885, 915)
CLAIM_CLICK = DesktopAction.from_dict(command(point=[0.901, 0.934]))
PROMPT_CLICK = DesktopAction.from_dict(command(point=[0.5, 0.97]))


def arena_screen(*boxes) -> DesktopFrame:
    image = Image.new("RGB", (1600, 935), (22, 20, 30))
    draw = ImageDraw.Draw(image)
    for box, color in boxes:
        draw.rectangle(box, fill=color)
    return DesktopFrame(GameWindow(42, 123, (100, 50, 800, 467)), image, time.monotonic())


def test_reward_card_turning_over_beside_claim_is_an_effect():
    # 07:46:51 Claim, 07:46:53 "previous claim click on 'Claim' had no visible
    # effect", yet the card turns over (captures: whole-window mean 3.9, click point 8.8).
    face_down = arena_screen((CARD, (110, 80, 50)), (CLAIM_BUTTON, (220, 120, 40)))
    face_up = arena_screen((CARD, (215, 210, 195)), (CLAIM_BUTTON, (220, 120, 40)))
    assert not frame_changed(face_down, face_up, CLAIM_CLICK)  # the stale-layout guard tolerates it
    assert click_changed_screen(face_down, face_up, CLAIM_CLICK)


def test_new_dark_screen_keeping_the_prompt_under_the_pointer_is_an_effect():
    # 08:05:18 Continue on the rank screen; at 08:05:26 the Reward screen, with
    # its own "[Click to Continue]" in the same place, counted as no effect.
    rank = arena_screen(((720, 410, 880, 580), (200, 130, 60)), (PROMPT, (230, 230, 230)))
    reward = arena_screen(
        ((140, 150, 1000, 660), (60, 55, 75)),
        ((1040, 150, 1470, 660), (55, 62, 72)),
        (PROMPT, (230, 230, 230)),
    )
    assert not frame_changed(rank, reward, PROMPT_CLICK)
    assert click_changed_screen(rank, reward, PROMPT_CLICK)


def test_hover_glow_and_sparkles_are_not_an_effect():
    # A missed click must still read as one: the pointer's own hover highlight
    # and small ambient particles do not count.
    before = arena_screen((CARD, (110, 80, 50)), (CLAIM_BUTTON, (220, 120, 40)))
    after = arena_screen(
        (CARD, (110, 80, 50)),
        (CLAIM_BUTTON, (250, 150, 70)),
        ((600, 330, 624, 354), (255, 240, 150)),
        ((560, 420, 580, 440), (255, 240, 150)),
    )
    assert not click_changed_screen(before, after, CLAIM_CLICK)
    assert not click_changed_screen(before, before, CLAIM_CLICK)
    moved = DesktopFrame(GameWindow(42, 123, (0, 0, 800, 467)), before.image, time.monotonic())
    assert click_changed_screen(before, moved, CLAIM_CLICK)
