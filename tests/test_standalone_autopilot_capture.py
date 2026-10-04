import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_autopilot_capture import _AutopilotCaptureMixin


class Runtime(_AutopilotCaptureMixin):
    def __init__(self):
        self.ui = Mock()
        self.settings = Mock()
        self._autopilot_enabled = True
        self.plan = {"action": "select_x", "x": 5, "failures": 3}
        self.state = {"match_id": "m1", "pending_decision": "Select X", "action_history": []}
        self._autopilot = SimpleNamespace(on_abort=self.plan.clear)
        self._start_recovery_observer = Mock()

    def _capture_observed_state(self, state=None):
        return copy.deepcopy(self.state if state is None else state)

    def _collect_autopilot_info(self):
        return {"enabled": self._autopilot_enabled, "pending_plan": self.plan}

    def _collect_bridge_state(self):
        return {"pending": "Select X"}


def test_capture_preserves_stuck_action_before_abort_and_records_manual_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr("arenamcp.standalone_autopilot_capture.LOG_DIR", tmp_path)
    runtime = Runtime()
    runtime.capture_autopilot_bug("test-incident", "clicked")
    assert not runtime._autopilot_enabled
    assert not runtime.plan
    recorder = runtime._autopilot_bug_capture
    initial = json.loads(recorder.report_path.read_text())
    assert initial["autopilot"]["pending_plan"]["x"] == 5
    assert initial["autopilot"]["enabled"]
    runtime.state.update(pending_decision="Select Targets", action_history=[{"action": "X=2"}])
    recorder.observe(runtime.state)
    runtime.attach_autopilot_bug_screenshots("test-incident", {}, "Arena screenshot unavailable")
    runtime._finish_autopilot_recovery("autopilot_resumed")
    report = json.loads(recorder.report_path.read_text())
    assert report["manual_recovery"]["finish_reason"] == "autopilot_resumed"
    assert report["screenshot_error"] == "Arena screenshot unavailable"
    events_path = report["manual_recovery"]["events_path"]
    from pathlib import Path

    events = [json.loads(line) for line in Path(events_path).read_text().splitlines()]
    assert events[-1]["game_state"]["action_history"] == [{"action": "X=2"}]
    assert runtime.ui._emit.call_args.args[0]["phase"] == "completed"


def test_failed_capture_still_pauses_and_releases_ui(tmp_path, monkeypatch):
    monkeypatch.setattr("arenamcp.standalone_autopilot_capture.LOG_DIR", tmp_path)
    runtime = Runtime()
    runtime.capture_autopilot_bug("../invalid")
    assert not runtime._autopilot_enabled
    assert not runtime.plan
    assert runtime.ui._emit.call_args.args[0]["phase"] == "error"


def test_capture_pipe_protocol():
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = Mock()
    adapter._dispatch({"cmd": "autopilot_bug", "capture_id": "one", "clicked_at": "now"})
    adapter._coach.capture_autopilot_bug.assert_called_once_with("one", "now")
    adapter._dispatch(
        {
            "cmd": "autopilot_bug_screenshots",
            "capture_id": "one",
            "screenshots": {},
            "screenshot_error": "no Arena window",
        }
    )
    adapter._coach.attach_autopilot_bug_screenshots.assert_called_once_with("one", {}, "no Arena window")


def test_recovery_observer_finishes_without_the_coaching_loop(tmp_path):
    import threading

    from arenamcp.autopilot_bug_capture import AutopilotBugCapture

    runtime = Runtime()
    runtime._autopilot_enabled = False
    recorder = runtime._autopilot_bug_capture = AutopilotBugCapture(tmp_path, max_duration_s=0)
    recorder.begin("time-limited", {"game_state": runtime.state})
    finished = threading.Event()
    runtime._emit_autopilot_capture_status = finished.set
    _AutopilotCaptureMixin._start_recovery_observer(runtime, recorder, "time-limited")
    assert finished.wait(2)
    assert recorder.status["finish_reason"] == "time_limit"


def test_stuck_guard_callback_saves_reason_and_arena_image_locally(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr("arenamcp.standalone_autopilot_capture.LOG_DIR", tmp_path)
    done = threading.Event()
    runtime = Runtime()
    original = runtime.attach_autopilot_bug_screenshots

    def attach(*args):
        original(*args)
        done.set()

    runtime.attach_autopilot_bug_screenshots = attach
    monkeypatch.setattr(
        "arenamcp.autopilot_bug_capture.capture_mtga_screenshot",
        lambda folder, stem: {"mtga": str(folder / f"{stem}_mtga.png")},
    )
    runtime._auto_capture_autopilot_stuck("No target selection progress", {"attempts": 3, "elapsed_s": 9})
    assert done.wait(2)
    recorder = runtime._autopilot_bug_capture
    report = json.loads(recorder.report_path.read_text())
    assert report["automatic"] is True
    assert report["stuck_detection"]["attempts"] == 3
    assert report["autopilot"]["pending_plan"]["x"] == 5
    assert report["screenshots"]["mtga"].startswith(str(tmp_path))
    assert not runtime._autopilot_enabled
    runtime._finish_autopilot_recovery("test_complete")
