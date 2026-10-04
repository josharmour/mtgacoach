from __future__ import annotations

import json
from pathlib import Path

import pytest

from arenamcp.autopilot_bug_capture import AutopilotBugCapture, capture_mtga_screenshot


def read_events(path: Path) -> list[dict]:
    report = json.loads(path.read_text())
    return [
        json.loads(line) for line in Path(report["manual_recovery"]["events_path"]).read_text().splitlines()
    ]


def test_preserves_pre_pause_intent_and_changed_recovery_states(tmp_path):
    recorder = AutopilotBugCapture(tmp_path)
    initial = {
        "game_state": {"match_id": "match-1", "pending_decision": "Choose attackers", "deck_cards": [1, 2]},
        "autopilot": {"plan": {"action": "attack", "card": "Emrakul"}},
    }
    path = recorder.begin("incident-1", initial)
    initial["autopilot"]["plan"].clear()  # Abort can clear the live plan.
    assert not recorder.observe(initial["game_state"])
    manual = {
        "match_id": "match-1",
        "pending_decision": None,
        "action_history": [{"action": "declare_attackers", "instance_ids": [42]}],
        "recent_events": [{"type": "attacked", "instance_id": 42}],
    }
    recorder.observe(manual)
    recorder.observe(manual)
    assert recorder.observe(manual, autopilot_active=True)
    assert not recorder.active
    report = json.loads(path.read_text())
    assert report["autopilot"]["plan"] == {"action": "attack", "card": "Emrakul"}
    assert report["manual_recovery"]["finish_reason"] == "autopilot_resumed"
    events = read_events(path)
    assert len(events) == 2
    assert "deck_cards" not in events[0]["game_state"]
    assert events[1]["game_state"]["action_history"] == manual["action_history"]
    assert events[1]["game_state"]["recent_events"] == manual["recent_events"]


def test_dedupes_capture_ids_and_attaches_late_screenshot_error(tmp_path):
    recorder = AutopilotBugCapture(tmp_path)
    path = recorder.begin("unique", {"game_state": {"match_id": "first"}})
    assert recorder.begin("unique", {"game_state": {"match_id": "wrong"}}) == path
    recorder.finish("user_finished")
    assert recorder.attach_screenshots("unique", {}, error="Screen recording permission denied")
    assert not recorder.attach_screenshots("other-id", {"mtga": "wrong.png"})
    report = json.loads(path.read_text())
    assert report["game_state"]["match_id"] == "first"
    assert report["screenshot_error"] == "Screen recording permission denied"
    assert report["screenshots"] == {}


def test_timeout_finalizes_without_any_new_state(tmp_path):
    clock = [10.0]
    recorder = AutopilotBugCapture(tmp_path, clock=lambda: clock[0], max_duration_s=2)
    path = recorder.begin("timeout", {"game_state": {"match_id": "m"}})
    clock[0] = 12.0
    assert recorder.observe({"match_id": "m"})
    assert json.loads(path.read_text())["manual_recovery"]["finish_reason"] == "time_limit"


@pytest.mark.parametrize(
    "following,reason",
    [
        ({"match_id": "different", "secret": "next match"}, "match_changed"),
        ({"match_id": None}, "match_ended"),
        ({"match_id": "first", "last_game_result": {"winner": 1}}, "match_ended"),
    ],
)
def test_stops_at_match_boundaries(tmp_path, following, reason):
    recorder = AutopilotBugCapture(tmp_path)
    path = recorder.begin("boundary", {"game_state": {"match_id": "first"}})
    assert recorder.observe(following)
    assert recorder.status["finish_reason"] == reason
    if reason == "match_changed":
        assert (
            "next match"
            not in Path(json.loads(path.read_text())["manual_recovery"]["events_path"]).read_text()
        )


@pytest.mark.parametrize(
    "limits,reason", [({"max_events": 1}, "event_limit"), ({"max_bytes": 400}, "size_limit")]
)
def test_recovery_files_are_bounded(tmp_path, limits, reason):
    recorder = AutopilotBugCapture(tmp_path, **limits)
    path = recorder.begin("bounded", {"game_state": {"match_id": "m"}})
    recorder.observe({"match_id": "m", "payload": "x" * 1000})
    assert not recorder.active
    report = json.loads(path.read_text())
    assert report["manual_recovery"]["finish_reason"] == reason
    assert report["manual_recovery"]["event_count"] <= recorder.max_events
    assert Path(report["manual_recovery"]["events_path"]).stat().st_size <= recorder.max_bytes


def test_capture_id_cannot_escape_report_directory(tmp_path):
    recorder = AutopilotBugCapture(tmp_path)
    with pytest.raises(ValueError):
        recorder.begin("../../outside", {})
    assert list(tmp_path.iterdir()) == []


def test_arena_screenshot_uses_only_existing_window_rectangle(tmp_path, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr("arenamcp.desktop.window_tracking.get_mtga_window_rect", lambda: (10, 20, 200, 100))
    picture = Mock()
    grab = Mock(return_value=picture)
    monkeypatch.setattr("PIL.ImageGrab.grab", grab)
    shots = capture_mtga_screenshot(tmp_path, "incident")
    assert grab.call_args.kwargs["bbox"] == (10, 20, 210, 120)
    assert shots == {"mtga": str(tmp_path / "incident_mtga.png")}
    picture.save.assert_called_once_with(shots["mtga"], "PNG")
