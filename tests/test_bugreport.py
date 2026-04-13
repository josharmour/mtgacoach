from __future__ import annotations

from pathlib import Path

from arenamcp.bugreport import build_issue_payload, build_issue_url


def _sample_report() -> dict:
    return {
        "timestamp": "2026-04-12T12:00:00",
        "version": "2.0.0",
        "config": {
            "backend": "online",
            "model": "gpt-test",
            "advice_style": "concise",
            "auto_speak": True,
        },
        "voice": {
            "tts_voice": ["af_sarah", "Sarah"],
        },
        "game_state": {
            "pending_decision": "Choose attackers",
            "turn": {"turn_number": 4, "phase": "Combat"},
        },
        "match_context": {"match_id": "abc123"},
        "bridge_state": {"connected": True},
        "autopilot": {"enabled": False},
        "replay": {"available": True},
        "errors": [{"timestamp": "now", "context": "coach", "error": "boom"}],
        "recent_logs": ["line 1\n", "line 2\n"],
    }


def test_build_issue_payload_includes_core_context(tmp_path: Path) -> None:
    report_path = tmp_path / "bug.json"
    title, body = build_issue_payload(_sample_report(), report_path, "voice stalled after mulligan")

    assert "voice stalled after mulligan" in title
    assert "Version: `2.0.0`" in body
    assert "Pending decision: `Choose attackers`" in body
    assert str(report_path) in body
    assert "Debug Excerpt" in body


def test_build_issue_url_truncates_long_body() -> None:
    url = build_issue_url("Title", "x" * 20000, max_body_chars=500)

    assert len(url) < 4000
    assert "browser+draft+truncated" in url


def test_build_issue_payload_includes_post_match_feedback(tmp_path: Path) -> None:
    report_path = tmp_path / "bug.json"
    report = _sample_report()
    report["post_match_feedback"] = {
        "source": "post_match_analysis",
        "match_result": "win",
        "analysis": "The coach overcommitted to the aura line and missed a safer attack.",
        "user_feedback": "It should have mentioned the crack-back risk before recommending all-in.",
    }

    _title, body = build_issue_payload(report, report_path, "")

    assert "## Coaching Feedback" in body
    assert "Feedback source: `post_match_analysis`" in body
    assert "Match result: `win`" in body
    assert "crack-back risk" in body
    assert "Post-match analysis attached" in body
