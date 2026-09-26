"""Native autoplay must execute without a bridge and reject obsolete input."""

import ctypes
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from arenamcp.autopilot_models import AutopilotConfig, AutopilotState
from arenamcp.native_mac_autopilot import NativeMacAutopilot, ground_desktop_action, use_native_mac_autopilot
from arenamcp.native_mac_input import (
    DesktopAction,
    DesktopFrame,
    DesktopUnavailable,
    GameWindow,
    NativeMacInput,
    _ForegroundProcess,
    frame_changed,
)


def make_frame(*, window=None, color="green", size=(1440, 900)):
    return DesktopFrame(
        window or GameWindow(42, 123, (100, 50, 720, 450)),
        Image.new("RGB", size, color),
        time.monotonic(),
    )


def command(kind="click", **kwargs):
    return {
        "kind": kind,
        "point": [0.5, 0.6],
        "reason": "Select the visible card",
        "confidence": 0.95,
        **kwargs,
    }


def make_engine(monkeypatch, *, response=None, state=None, dry_run=False):
    monkeypatch.setattr(
        "arenamcp.gre_bridge.get_bridge", Mock(side_effect=AssertionError("No bridge allowed"))
    )
    state = state if state is not None else {"match_id": "match", "pending_decision": "Select Cards"}
    controller = Mock()
    controller.capture.side_effect = make_frame
    controller.execute.return_value = True
    backend = Mock()
    backend.complete_with_image.return_value = json.dumps(response or command())
    notices = Mock()
    engine = NativeMacAutopilot(
        backend=backend,
        controller=controller,
        get_game_state=lambda: state,
        config=AutopilotConfig(dry_run=dry_run),
        ui_advice_fn=notices,
    )
    engine._ground_action = Mock(side_effect=lambda frame, action: action)
    return engine, controller, backend, state, notices


def test_localized_pixels_replace_planner_coordinates_before_input(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    del engine._ground_action
    backend.complete_with_image.side_effect = [
        json.dumps(command("double_click", point=[0.66, 0.8])),
        json.dumps({"point": [360, 720], "confidence": 0.9}),
    ]
    assert engine.process_trigger(state, "desktop_poll")
    action = controller.execute.call_args.args[1]
    assert action.kind == "double_click"
    assert action.point == (0.25, 0.8)
    assert action.confidence == 0.9


def test_uncertain_localization_never_clicks(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    del engine._ground_action
    backend.complete_with_image.side_effect = [
        json.dumps(command()),
        json.dumps({"point": None, "confidence": 0.4}),
    ]
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert engine.state == AutopilotState.IDLE
    backend.complete_with_image.side_effect = [
        json.dumps(command()),
        json.dumps({"point": [720, 540], "confidence": 0.95}),
    ]
    engine._next_poll = 0
    assert engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_count == 1


@pytest.mark.parametrize("kind", ["click", "double_click", "drag"])
def test_uncertain_grounding_preserves_validated_coordinates(kind):
    action = DesktopAction.from_dict(command(kind, end=[0.5, 0.3]))
    grounded = ground_desktop_action(json.dumps({"point": None, "confidence": 0.4}), action, (1600, 935))
    assert grounded.confidence == 0.4
    assert grounded.point == action.point
    assert grounded.end == action.end


def test_repeated_uncertain_localization_is_bounded(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    del engine._ground_action
    backend.complete_with_image.side_effect = [
        json.dumps(command()),
        json.dumps({"point": None, "confidence": 0.4}),
    ] * 3
    for _ in range(4):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    assert engine.state == AutopilotState.PAUSED
    assert backend.complete_with_image.call_count == 6
    controller.execute.assert_not_called()


@pytest.mark.parametrize("point", [[0.4, 0.8], [1600, 700], [-1, 20], [True, 700], None])
def test_grounding_rejects_ambiguous_or_invalid_pixel_coordinates(point):
    with pytest.raises(ValueError):
        ground_desktop_action(
            json.dumps({"point": point, "confidence": 0.9}), DesktopAction.from_dict(command()), (1600, 935)
        )


def test_new_match_clears_previous_match_pause_but_not_abort(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch, response=command(confidence=0.3))
    for _ in range(3):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    assert engine.state == AutopilotState.PAUSED
    state["match_id"] = "new-match"
    state["pending_decision"] = "Mulligan"
    backend.complete_with_image.return_value = json.dumps(command())
    assert engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_count == 1
    engine.on_abort()
    state["match_id"] = "third-match"
    assert not engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_count == 1


def test_native_click_without_bridge_or_log_trigger(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    assert engine.process_trigger(state, "desktop_poll")
    frame, action, _ = controller.execute.call_args.args
    assert action.kind == "click"
    assert frame.window.screen_point(action.point) == (460, 320)
    assert engine.get_debug_info()["inputs_sent"] == 1
    assert backend.complete_with_image.call_args.args[2].startswith(b"\x89PNG")
    assert backend.complete_with_image.call_args.kwargs["json_mode"] is True


def test_dialog_can_continue_without_a_log_change(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    backend.complete_with_image.side_effect = [json.dumps(command()), json.dumps(command(point=[0.8, 0.8]))]
    assert engine.process_trigger(state, "desktop_poll")
    engine._next_poll = 0
    assert engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_count == 2
    second_prompt = json.loads(backend.complete_with_image.call_args.args[1])
    assert second_prompt["recent_inputs"][0]["kind"] == "click"


def test_card_play_prefers_double_click_then_observes_targeting(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch, response=command("double_click"))
    assert engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_args.args[1].kind == "double_click"
    assert "Use double_click on the visible card in hand" in backend.complete_with_image.call_args.args[0]
    backend.complete_with_image.return_value = json.dumps(command(point=[0.5, 0.4]))
    engine._next_poll = 0
    assert engine.process_trigger(state, "desktop_poll")
    prompt = json.loads(backend.complete_with_image.call_args.args[1])
    assert prompt["recent_inputs"][0]["kind"] == "double_click"
    assert controller.execute.call_args.args[1].point == (0.5, 0.4)


def test_stop_during_model_request_never_clicks(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)

    def respond(*args, **kwargs):
        engine.on_abort()
        return json.dumps(command())

    backend.complete_with_image.side_effect = respond
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert not engine._lock.locked()


@pytest.mark.parametrize("payload", [command(), command(confidence=0.3), command("stop")])
def test_new_log_state_during_model_request_discards_action(monkeypatch, payload):
    engine, controller, backend, state, _ = make_engine(monkeypatch)

    def respond(*args, **kwargs):
        state["pending_decision"] = "Priority (Pass Only)"
        return json.dumps(payload)

    backend.complete_with_image.side_effect = respond
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert engine.state == AutopilotState.IDLE


@pytest.mark.parametrize(
    "fresh",
    [
        make_frame(color="red"),
        make_frame(window=GameWindow(42, 123, (200, 50, 720, 450))),
    ],
)
def test_ui_changes_during_model_request_discard_action(monkeypatch, fresh):
    engine, controller, _, state, _ = make_engine(monkeypatch)
    controller.capture.side_effect = [make_frame(), fresh]
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()


def test_dry_run_never_sends_input(monkeypatch):
    engine, controller, _, state, _ = make_engine(monkeypatch, dry_run=True)
    assert engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert engine.get_debug_info()["inputs_sent"] == 0


def test_debug_report_retains_exact_model_image_and_proposal(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    assert engine.get_debug_screenshot() is None
    assert engine.process_trigger(state, "desktop_poll")
    assert engine.get_debug_screenshot() == backend.complete_with_image.call_args.args[2]
    info = engine.get_debug_info()
    assert info["last_proposal"]["kind"] == "click"
    assert info["window"]["pid"] == make_frame().window.pid
    assert info["image_size"] == list(make_frame().image.size)


@pytest.mark.parametrize("payload", [command(confidence=0.3), command("stop")])
def test_uncertain_action_retries_then_pauses(monkeypatch, payload):
    engine, controller, _, state, _ = make_engine(monkeypatch, response=payload)
    for _ in range(3):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert engine.state == AutopilotState.PAUSED


def test_uncertain_decision_pause_recovers_when_turn_advances(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch, response=command(confidence=0.3))
    for _ in range(3):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    assert engine.state == AutopilotState.PAUSED
    assert not engine.process_trigger(state, "desktop_poll")
    assert backend.complete_with_image.call_count == 3
    state["pending_decision"] = "Priority"
    state["turn"] = {"turn_number": 3}
    backend.complete_with_image.return_value = json.dumps(command("double_click"))
    assert engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_called_once()
    assert engine.state == AutopilotState.IDLE


def test_uncertain_decision_pause_never_overrides_user_abort(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch, response=command(confidence=0.3))
    for _ in range(3):
        engine._next_poll = 0
        engine.process_trigger(state, "desktop_poll")
    engine.on_abort()
    state["pending_decision"] = "Priority"
    backend.complete_with_image.return_value = json.dumps(command())
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()


def test_stale_uncertain_grounding_does_not_pause_new_decision(monkeypatch):
    engine, controller, _, state, _ = make_engine(monkeypatch)
    engine._uncertain_frames = 2

    def stale_grounding(frame, action):
        state["pending_decision"] = "Priority"
        return DesktopAction.from_dict(command(confidence=0.3))

    engine._ground_action.side_effect = stale_grounding
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert engine.state == AutopilotState.IDLE


def test_repeated_no_progress_inputs_are_bounded(monkeypatch):
    engine, controller, _, state, _ = make_engine(monkeypatch)
    for _ in range(4):
        engine._next_poll = 0
        engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_count == 3
    assert engine.state == AutopilotState.PAUSED


def test_bad_model_response_stops_instead_of_clicking(monkeypatch):
    engine, controller, backend, state, notices = make_engine(monkeypatch)
    backend.complete_with_image.return_value = "This model does not accept images"
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()
    assert engine.state == AutopilotState.PAUSED
    assert notices.called


def test_transient_vision_failure_retries_a_fresh_image_before_input(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    backend.complete_with_image.side_effect = [
        "[BACKEND ERROR] vision analysis failed: Request timed out.",
        json.dumps(command()),
    ]
    assert not engine.process_trigger(state, "desktop_poll")
    assert engine.state == AutopilotState.IDLE
    controller.execute.assert_not_called()
    assert not engine.process_trigger(state, "desktop_poll")
    assert backend.complete_with_image.call_count == 1
    engine._next_poll = 0
    assert engine.process_trigger(state, "desktop_poll")
    assert controller.capture.call_count == 3
    assert controller.execute.call_count == 1
    assert engine._vision_failures == 0


def test_persistent_vision_failure_pauses_without_input(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    backend.complete_with_image.return_value = "[BACKEND ERROR] vision endpoint unavailable"
    for _ in range(4):
        engine._next_poll = 0
        assert not engine.process_trigger(state, "desktop_poll")
    assert backend.complete_with_image.call_count == 3
    assert engine.state == AutopilotState.PAUSED
    controller.execute.assert_not_called()
    engine._clear_events()
    backend.reset_vision_failures.assert_called_once_with()
    assert engine._vision_failures == 0


@pytest.mark.parametrize("prose", ["The card is in the lower row.", "Cast the card for {W} using {1}{W}."])
def test_glm_prose_before_json_is_not_treated_as_an_action(monkeypatch, prose):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    backend.complete_with_image.return_value = prose + "\n```json\n" + json.dumps(command()) + "\n```"
    assert engine.process_trigger(state, "desktop_poll")
    assert controller.execute.call_args.args[1].kind == "click"


def test_multiple_json_commands_are_rejected(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    backend.complete_with_image.return_value = json.dumps(command()) + json.dumps(command())
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()


@pytest.mark.parametrize("prefix", ["", "Use these actions: "])
def test_action_array_is_rejected(monkeypatch, prefix):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    backend.complete_with_image.return_value = prefix + json.dumps([command()])
    assert not engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()


def test_focus_loss_waits_without_requesting_model(monkeypatch):
    engine, controller, backend, state, _ = make_engine(monkeypatch)
    controller.capture.side_effect = DesktopUnavailable("Bring Arena to the foreground")
    assert not engine.process_trigger(state, "desktop_poll")
    backend.complete_with_image.assert_not_called()
    controller.execute.assert_not_called()
    assert engine.state == AutopilotState.IDLE


def test_wait_does_not_send_input(monkeypatch):
    engine, controller, _, state, _ = make_engine(monkeypatch, response=command("wait"))
    assert engine.process_trigger(state, "desktop_poll")
    controller.execute.assert_not_called()


def test_resume_checks_permissions_and_clears_pause(monkeypatch):
    engine, controller, _, _, _ = make_engine(monkeypatch)
    engine._pause("Stopped")
    engine.on_abort()
    engine._clear_events()
    controller.check_permissions.assert_called_once_with(request=True)
    assert not engine._abort_event.is_set()
    assert engine.state == AutopilotState.IDLE


def test_resume_cannot_reenable_inflight_request(monkeypatch):
    engine, _, _, _, _ = make_engine(monkeypatch)
    engine._lock.acquire()
    try:
        engine.on_abort()
        with pytest.raises(RuntimeError, match="still stopping"):
            engine._clear_events()
        assert engine._abort_event.is_set()
    finally:
        engine._lock.release()


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


def fake_native_controller():
    controller = NativeMacInput.__new__(NativeMacInput)
    controller._quartz = SimpleNamespace(
        kCGEventMouseMoved=5,
        kCGEventLeftMouseDown=1,
        kCGEventLeftMouseUp=2,
        kCGEventLeftMouseDragged=6,
    )
    controller.check_permissions = Mock()
    controller._check_focus = Mock()
    controller._check_point = Mock()
    controller._mouse = Mock()
    controller.capture = Mock(side_effect=make_frame)
    return controller


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


@pytest.mark.parametrize(
    ("platform", "bridge_capable", "mac_bridge", "expected"),
    [
        ("darwin", False, False, True),
        ("darwin", False, True, False),  # native-Mac IL2CPP bridge connected: GRE engine
        ("darwin", True, False, False),
        ("linux", True, False, False),
        ("win32", True, False, False),
    ],
)
def test_select_native_engine_only_for_native_mac(monkeypatch, platform, bridge_capable, mac_bridge, expected):
    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr("arenamcp.platform_integration.bridge_capable", lambda: bridge_capable)
    monkeypatch.setattr("arenamcp.platform_integration.mac_bridge_installed", lambda: False)
    monkeypatch.setattr("arenamcp.native_mac_autopilot.mac_gre_bridge_connected", lambda: mac_bridge)
    assert use_native_mac_autopilot() is expected
