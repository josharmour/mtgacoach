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
    url = build_issue_url("Title", "x" * 20000, max_url_chars=2000)

    assert len(url) <= 2000
    assert "browser+draft+truncated" in url


def test_build_issue_url_limits_encoded_length_of_json_heavy_body() -> None:
    # JSON-heavy bodies inflate ~3x under URL-encoding; the cap must hold
    # on the final URL (GitHub bounces ~8K+ request lines).
    body = ('{"key": "value", "nested": {"a": [1, 2, 3]}}\n' * 500)
    url = build_issue_url("Desktop bug report: something", body)
    assert len(url) <= 7600
    assert "browser+draft+truncated" in url


def test_build_issue_url_short_body_untouched() -> None:
    url = build_issue_url("T", "short body")
    assert "truncated" not in url


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


def test_build_issue_payload_includes_bridge_miss_and_replay(tmp_path: Path) -> None:
    report_path = tmp_path / "bug.json"
    report = _sample_report()
    report["auto_fallback_bug"] = {
        "reason_tag": "bridge_submit_failed",
        "action_type": "cast_spell",
        "card_name": "Llanowar Elves",
        "target_names": [],
        "select_card_names": [],
        "bridge_request_type": "ActionsAvailableReq",
        "bridge_request_class": "ActionsAvailableRequest",
        "bridge": {"connected": True, "failed_methods": ["cast_spell"]},
    }
    report["replay"] = {
        "available": True,
        "latest_replay_path": "/tmp/replays/match_abc123.gretrace",
    }

    _title, body = build_issue_payload(report, report_path, "")

    assert "## Bridge Miss" in body
    assert "Reason tag: `bridge_submit_failed`" in body
    assert "Action type: `cast_spell`" in body
    assert "Card: `Llanowar Elves`" in body
    assert "Bridge request: `ActionsAvailableReq` / `ActionsAvailableRequest`" in body
    assert "Bridge connected: `True`" in body
    assert "Bridge failed methods: `['cast_spell']`" in body
    assert "Latest replay: `/tmp/replays/match_abc123.gretrace`" in body
