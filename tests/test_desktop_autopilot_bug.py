from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.coach_session import CoachSession


@pytest.fixture
def session(qapp, monkeypatch, tmp_path):
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    session = CoachSession()
    session._process = Mock(is_running=True)
    session._tts = Mock()
    yield session
    session.shutdown()


def test_pause_request_precedes_screenshot_and_clicks_are_deduplicated(session, monkeypatch):
    def screenshot(directory, stem):
        payload = session._process.send_payload.call_args.args[0]
        assert payload["cmd"] == "autopilot_bug"
        return {"mtga": str(directory / f"{stem}_mtga.png")}

    monkeypatch.setattr("arenamcp.autopilot_bug_capture.capture_mtga_screenshot", screenshot)
    session.trigger_autopilot_bug()
    session.trigger_autopilot_bug()
    payloads = [call.args[0] for call in session._process.send_payload.call_args_list]
    assert len(payloads) == 2
    assert payloads[0]["cmd"] == "autopilot_bug"
    assert payloads[1]["cmd"] == "autopilot_bug_screenshots"
    assert payloads[0]["capture_id"] == payloads[1]["capture_id"]
    assert session.last_autopilot_bug_status["phase"] == "capturing"
    session._tts.stop_speech.assert_called_once()
    session._handle_process_event(
        {"type": "autopilot_bug_status", "phase": "recording", "capture_id": payloads[0]["capture_id"]}
    )
    assert session.last_autopilot_bug_status["phase"] == "recording"


def test_screenshot_failure_does_not_cancel_state_capture(session, monkeypatch):
    monkeypatch.setattr(
        "arenamcp.autopilot_bug_capture.capture_mtga_screenshot", Mock(side_effect=RuntimeError("denied"))
    )
    session.trigger_autopilot_bug()
    payload = session._process.send_payload.call_args.args[0]
    assert payload["cmd"] == "autopilot_bug_screenshots"
    assert payload["screenshots"] == {}
    assert payload["screenshot_error"] == "denied"


def test_offline_capture_marks_last_known_state_and_no_recovery(session, monkeypatch, tmp_path):
    session._process.is_running = False
    session._last_game_state = {"match_id": "previous", "pending_decision": "Choose targets"}
    monkeypatch.setattr("arenamcp.autopilot_bug_capture.capture_mtga_screenshot", lambda *_: {})
    session.trigger_autopilot_bug()
    saved = list((tmp_path / "bug_reports").glob("autopilot_bug_*.json"))
    assert len(saved) == 1
    report = json.loads(saved[0].read_text())
    assert report["game_state_source"] == "desktop_last_known_state"
    assert report["game_state"]["pending_decision"] == "Choose targets"
    assert report["manual_recovery"]["finish_reason"] == "engine_unavailable"
    assert session.last_autopilot_bug_status["phase"] == "completed"
    assert Path(session.last_autopilot_bug_status["path"]) == saved[0]
    session._process.send_payload.assert_not_called()


def test_engine_exit_releases_pending_capture_button(session, monkeypatch):
    monkeypatch.setattr("arenamcp.autopilot_bug_capture.capture_mtga_screenshot", lambda *_: {})
    session.trigger_autopilot_bug()
    session._handle_exited(1)
    assert session.last_autopilot_bug_status["phase"] == "error"


def test_automatic_capture_can_follow_completed_manual_report(session):
    session._last_autopilot_bug_status = {"phase": "completed", "capture_id": "manual"}
    session._handle_process_event(
        {
            "type": "autopilot_bug_status",
            "phase": "recording",
            "capture_id": "automatic",
            "automatic": True,
        }
    )
    assert session.last_autopilot_bug_status["capture_id"] == "automatic"
    session._handle_process_event(
        {
            "type": "autopilot_bug_status",
            "phase": "completed",
            "capture_id": "manual",
        }
    )
    assert session.last_autopilot_bug_status["capture_id"] == "automatic"
    assert session.last_autopilot_bug_status["phase"] == "recording"
