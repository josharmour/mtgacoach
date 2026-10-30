from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from arenamcp.desktop.coach_session import CoachSession


def test_coach_session_bug_report_saved_event(qapp):
    session = CoachSession()
    received = []
    session.bugReportSaved.connect(lambda path, err: received.append((path, err)))

    event = {
        "type": "bug_report_saved",
        "path": "/path/to/bug_test.json",
        "screenshots": {},
    }
    session._handle_process_event(event)

    assert len(received) == 1
    assert received[0] == ("/path/to/bug_test.json", "")
    session.shutdown()


def test_backend_health_event_reaches_the_sidebar(qapp):
    """#215: the engine's backend_health snapshots must reach the desktop."""
    session = CoachSession()
    seen: list[dict] = []
    session.backendHealthChanged.connect(seen.append)

    session._handle_process_event(
        {
            "type": "backend_health",
            "state": "degraded",
            "detail": "stream stalled: no data for 4s",
            "timestamp": 1.0,
            "data": {"state": "degraded", "detail": "stream stalled: no data for 4s", "total_failures": 2},
        }
    )

    assert seen and seen[-1]["state"] == "degraded"
    assert seen[-1]["total_failures"] == 2
    assert session.last_backend_health["detail"] == "stream stalled: no data for 4s"

    # Flat payloads without a nested snapshot still carry state and detail.
    session._handle_process_event({"type": "backend_health", "state": "ok", "detail": "reachable"})
    assert seen[-1]["state"] == "ok"
    assert session.last_backend_health["state"] == "ok"
    session.shutdown()


def test_coach_session_offline_debug_report(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    session = CoachSession()
    # The real capture photographs the coach and the user's live Arena window.
    screenshots = {"coach": str(tmp_path / "coach.png"), "mtga": str(tmp_path / "mtga.png")}
    monkeypatch.setattr(session, "capture_screenshots", lambda: screenshots)
    received = []
    session.bugReportSaved.connect(lambda path, err: received.append((path, err)))

    # Subprocess is not running -> triggers offline fallback
    session.trigger_debug_report()

    assert len(received) == 1
    path, err = received[0]
    assert err == ""
    assert Path(path).exists()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["reason"] == "Desktop Bug Report (offline)"
    assert data["screenshots"] == screenshots
    session.shutdown()


def test_speak_request_reaches_the_desktop_voice(qapp, monkeypatch):
    session = CoachSession()
    captured: dict = {}
    spoken: list[str] = []
    monkeypatch.setattr(session._tts, "request_speech", lambda **kw: captured.update(kw))
    session.spokenLine.connect(spoken.append)

    session._handle_process_event(
        {
            "type": "speak_request",
            "text": "Play a land.",
            "voice_id": "am_adam",
            "voice_name": "Adam",
            "speed": 1.2,
        }
    )

    assert {key: captured[key] for key in ("text", "voice_id", "voice_name", "speed")} == {
        "text": "Play a land.",
        "voice_id": "am_adam",
        "voice_name": "Adam",
        "speed": 1.5,
    }
    assert captured["category"] == "workflow" and captured["provider"] is None
    assert captured["speech_id"].startswith("desktop:") and captured["valid"]()
    session._tts.speechStatus.emit(captured["speech_id"], "started")
    session._tts.speechStatus.emit(captured["speech_id"], "finished")
    assert not session._speech.voice_names
    assert spoken == ["Play a land."]
    session.shutdown()


def test_engine_speak_stop_halts_desktop_speech(qapp, monkeypatch):
    session = CoachSession()
    stops: list[bool] = []
    monkeypatch.setattr(session._tts, "stop_speech", lambda: stops.append(True))

    session._handle_process_event({"type": "speak_stop"})
    session._handle_process_event({"type": "speak_stop"})  # idempotent

    assert stops == [True, True]
    session.shutdown()
