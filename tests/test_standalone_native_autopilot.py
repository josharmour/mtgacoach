"""Wire native Mac autoplay through the real coach's startup and polling seams."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.autopilot_models import AutopilotState
from arenamcp.native_mac_autopilot import NativeMacAutopilot
from arenamcp.native_mac_input import DesktopUnavailable
from arenamcp.standalone import StandaloneCoach


def test_bug_report_attaches_exact_autoplay_image(monkeypatch, tmp_path):
    monkeypatch.setattr("arenamcp.standalone_diagnostics.LOG_DIR", tmp_path)
    monkeypatch.setattr("arenamcp.standalone_diagnostics.copy_to_clipboard", lambda value: False)
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._collect_debug_info = Mock(return_value={})
    coach._autopilot = SimpleNamespace(get_debug_screenshot=lambda: b"model-image")
    report_path = coach.save_bug_report(announce=False)
    report = json.loads(report_path.read_text())
    assert Path(report["screenshots"]["autoplay"]).read_bytes() == b"model-image"


def test_failed_autoplay_image_does_not_prevent_bug_report(monkeypatch, tmp_path):
    monkeypatch.setattr("arenamcp.standalone_diagnostics.LOG_DIR", tmp_path)
    monkeypatch.setattr("arenamcp.standalone_diagnostics.copy_to_clipboard", lambda value: False)
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._collect_debug_info = Mock(return_value={})
    coach._autopilot = SimpleNamespace(get_debug_screenshot=Mock(side_effect=RuntimeError("image failed")))
    report_path = coach.save_bug_report(announce=False)
    assert report_path.is_file()
    assert "screenshots" not in json.loads(report_path.read_text())


def make_coach(monkeypatch):
    controller = Mock()
    backend = Mock()
    backend_factory = Mock(return_value=backend)
    monkeypatch.setattr("arenamcp.coach.create_backend", backend_factory)
    monkeypatch.setattr("arenamcp.native_mac_autopilot.use_native_mac_autopilot", lambda: True)
    monkeypatch.setattr("arenamcp.native_mac_autopilot.NativeMacInput", lambda: controller)
    # Log-first: native autoplay decides with the text-model ActionPlanner and
    # only uses vision to operate the committed play.
    planner_cls = Mock(return_value=Mock(name="planner"))
    monkeypatch.setattr("arenamcp.action_planner.ActionPlanner", planner_cls)
    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._mcp = SimpleNamespace(get_game_state=lambda: {"match_id": "match"})
    coach._coach = object()
    coach.ui = Mock()
    coach.settings = {"autopilot_vision_model": "vision-model"}
    coach._backend_name = "online"
    coach._model_name = "text-model"
    coach._autopilot = None
    coach._autopilot_enabled = False
    coach._autopilot_dry_run = False
    coach._autopilot_afk = False
    coach._planner_cls = planner_cls
    return coach, controller, backend_factory


def test_native_initialization_uses_vision_model_and_checks_permissions(monkeypatch):
    coach, controller, backend_factory = make_coach(monkeypatch)
    coach._init_autopilot()
    assert isinstance(coach._autopilot, NativeMacAutopilot)
    backend_factory.assert_any_call("online", model="vision-model")
    # The planner decides with the text model, not the vision model.
    backend_factory.assert_any_call("online", model="text-model")
    assert coach._autopilot._log_planner is coach._planner_cls.return_value
    # Must not look like the bridge engine to the coaching loop.
    assert not hasattr(coach._autopilot, "_planner")
    controller.check_permissions.assert_called_once_with(request=True)


def test_native_autoplay_uses_direct_litellm_override(monkeypatch):
    coach, _, backend_factory = make_coach(monkeypatch)
    direct_factory = Mock(return_value=Mock())
    monkeypatch.setattr("arenamcp.backends.proxy.ProxyBackend", direct_factory)
    coach.settings.update(
        {
            "autopilot_vision_url": "http://10.0.0.10:8444/v1/",
            "autopilot_vision_model": "glm-5.3-flash",
            "autopilot_vision_api_key": "test-scoped-key",
        }
    )
    coach._init_autopilot()
    assert isinstance(coach._autopilot, NativeMacAutopilot)
    direct_factory.assert_called_once_with(
        model="glm-5.3-flash",
        base_url="http://10.0.0.10:8444/v1",
        api_key="test-scoped-key",
    )
    # Only the log planner's text backend comes from the gateway factory.
    backend_factory.assert_called_once_with("online", model="text-model")


def test_failed_permission_check_does_not_enable_autoplay(monkeypatch):
    coach, controller, _ = make_coach(monkeypatch)
    controller.check_permissions.side_effect = DesktopUnavailable("Accessibility is required")
    assert not coach.toggle_autopilot()
    assert coach._autopilot is None
    assert not coach._autopilot_enabled
    assert any("Accessibility" in call.args[0] for call in coach.ui.log.call_args_list)


def test_desktop_poll_does_not_need_a_game_trigger(monkeypatch):
    coach, _, _ = make_coach(monkeypatch)
    coach._init_autopilot()
    coach._autopilot_enabled = True
    coach._autopilot.process_trigger = Mock(return_value=False)
    assert coach._poll_desktop_autopilot()
    coach._autopilot.process_trigger.assert_called_once_with({"match_id": "match"}, "desktop_poll")


def test_native_pause_is_visible_in_control_status(monkeypatch):
    coach, _, _ = make_coach(monkeypatch)
    coach._init_autopilot()
    coach._autopilot_enabled = True
    coach._autopilot.process_trigger = Mock(return_value=False)
    coach._autopilot._state = AutopilotState.PAUSED
    assert coach._poll_desktop_autopilot()
    coach.ui.status.assert_called_with("AUTOPILOT", "AP:PAUSED")
    coach._autopilot._state = AutopilotState.IDLE
    coach._poll_desktop_autopilot()
    coach.ui.status.assert_called_with("AUTOPILOT", "AP:ON")
    coach._autopilot_enabled = False
    assert coach._autopilot_control_status() == "AP:OFF"


def test_disabled_native_autoplay_never_polls(monkeypatch):
    coach, _, _ = make_coach(monkeypatch)
    coach._init_autopilot()
    coach._autopilot.process_trigger = Mock()
    assert not coach._poll_desktop_autopilot()
    coach._autopilot.process_trigger.assert_not_called()


def test_bridge_autoplay_does_not_use_desktop_poll(monkeypatch):
    coach, _, _ = make_coach(monkeypatch)
    coach._autopilot_enabled = True
    coach._autopilot = SimpleNamespace(process_trigger=Mock())
    assert not coach._poll_desktop_autopilot()
    coach._autopilot.process_trigger.assert_not_called()
