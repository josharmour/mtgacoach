"""Privacy, opt-out, bounds and recovery metadata for deferred #214/#245 capture."""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from tests.test_parser_backlog_214_245 import BLOCK_PREFIX, DAMAGE_PREFIX

from arenamcp import action_diagnostics as diag
from arenamcp.action_planner import ActionPlanner, ActionType

SECRET = "arbitrary-private-canary-47"
RAW = (
    '{"password":"'
    + SECRET
    + '","token":"Bearer sk-supersecret12345","playerName":"Private Person","path":"/Users/private/home","email":"private@example.com","bad":"broken "quote"'
)


@pytest.fixture
def capture(tmp_path, monkeypatch, caplog):
    destination = tmp_path / "diagnostics"
    monkeypatch.setattr(diag, "MALFORMED_ACTION_DIR", destination)
    monkeypatch.setenv("ARENAMCP_ACTION_JSON_DIAGNOSTICS", "syntax")
    caplog.set_level(logging.DEBUG, logger="arenamcp.action_planner")
    caplog.set_level(logging.DEBUG, logger="arenamcp.action_diagnostics")
    return destination


def parse(raw=RAW):
    return ActionPlanner(Mock())._parse_response(
        raw,
        [SECRET],
        {"secret": SECRET},
        "DeclareBlockers",
        {
            "match_id": SECRET,
            "players": [{"name": SECRET}],
            "_bridge_request_payload": {
                "gameStateId": 17,
                "msgId": 23,
                "token": SECRET,
                "nested": {"raw": SECRET},
            },
        },
    )


def files(destination):
    return sorted(destination.glob("action_json_diagnostic_*.json"))


def saved(destination):
    return json.loads(files(destination)[0].read_text())


@pytest.mark.parametrize("mode", [None, "", "off", "0", "false", "raw", "full", "invalid"])
def test_capture_is_explicit_opt_in_and_never_creates_or_rotates_on_optout(
    capture, monkeypatch, caplog, mode
):
    if mode is None:
        monkeypatch.delenv("ARENAMCP_ACTION_JSON_DIAGNOSTICS")
    else:
        monkeypatch.setenv("ARENAMCP_ACTION_JSON_DIAGNOSTICS", mode)
    monkeypatch.setattr(diag, "_syntax_window", lambda *args: pytest.fail("optout read body"))
    parse()
    assert not capture.exists()
    assert SECRET not in caplog.text and "supersecret" not in caplog.text


@pytest.mark.parametrize(
    "disabled", ["planner_level", "capture_level", "planner_disabled", "capture_disabled", "global"]
)
def test_existing_logging_disable_wins_over_capture_optin(capture, monkeypatch, disabled):
    planner_log = logging.getLogger("arenamcp.action_planner")
    if disabled == "planner_level":
        monkeypatch.setattr(planner_log, "level", logging.WARNING)
        logging.Logger.manager._clear_cache()
    elif disabled == "capture_level":
        monkeypatch.setattr(diag.logger, "level", logging.WARNING)
        logging.Logger.manager._clear_cache()
    elif disabled == "planner_disabled":
        monkeypatch.setattr(planner_log, "disabled", True)
    elif disabled == "capture_disabled":
        monkeypatch.setattr(diag.logger, "disabled", True)
    old_disable = logging.Logger.manager.disable
    try:
        if disabled == "global":
            logging.disable(logging.CRITICAL)
        parse()
        assert not capture.exists()
    finally:
        logging.disable(old_disable)


def test_syntax_capture_contains_no_content_or_request_context(capture, caplog):
    parse()
    data = saved(capture)
    encoded = files(capture)[0].read_text()
    for private in [
        SECRET,
        "supersecret12345",
        "Private Person",
        "/Users/private",
        "private@example.com",
        "password",
        "token",
        "playerName",
    ]:
        assert private not in encoded and private not in caplog.text
    assert data["request_id"] == [17, 23]
    assert data["request_type"] == "DeclareBlockers"
    assert data["legal_action_count"] == 1
    assert data["recovery"] == "none"
    assert data["error"]["position"] > 0
    assert data["response_chars"] == len(RAW)
    assert set(data["response_prefix"]["shape"]) <= set('{}[]:,"\\ \t\r\nx')
    assert all(
        key not in data
        for key in [
            "response",
            "parse_input",
            "decision_context",
            "request_payload",
            "legal_actions",
            "match_id",
        ]
    )
    if os.name == "posix":
        assert files(capture)[0].stat().st_mode & 0o077 == 0


def test_metadata_mode_has_no_body_windows(capture, monkeypatch):
    monkeypatch.setenv("ARENAMCP_ACTION_JSON_DIAGNOSTICS", "metadata")
    monkeypatch.setattr(diag, "_syntax_window", lambda *args: pytest.fail("metadata read body"))
    parse()
    assert "response_prefix" not in saved(capture)
    assert "parse_error_window" not in saved(capture)


@pytest.mark.parametrize("raw", [DAMAGE_PREFIX, BLOCK_PREFIX])
def test_recovered_payload_retains_safe_metadata_and_still_requires_legality(capture, raw):
    ActionPlanner(Mock())._parse_response(raw, [])
    data = saved(capture)
    assert data["recovery"] == "action_payload"
    assert data["legality_still_required"] is True
    assert data["error"]["line"] == 1


def test_pick_salvage_reports_its_distinct_outcome(capture):
    planner = ActionPlanner(Mock())
    planner._last_menu = ["Pass"]
    plan = planner._parse_response('{"pick":1, "broken": garbage}', ["Pass"])
    assert plan.actions[0].action_type == ActionType.PASS_PRIORITY
    assert saved(capture)["recovery"] == "pick"


def test_fence_normalization_and_error_window_have_original_offsets(capture):
    parse("```json\n" + BLOCK_PREFIX + "\n```")
    data = saved(capture)
    assert data["normalization_changed"]
    assert data["response_chars"] > data["parse_input_chars"]
    expected = BLOCK_PREFIX[data["parse_error_window"]["start"] : data["parse_error_window"]["end"]]
    assert data["parse_error_window"]["shape"] == diag._syntax_window(expected, 0)["shape"]


def test_large_unicode_failure_and_sensitive_error_message_are_bounded(capture):
    raw = ("密" + SECRET + '\\"') * 100000
    error = json.JSONDecodeError(SECRET, raw, len(raw) - 12)
    path = diag.record_parse_failure(
        raw,
        parse_input=raw,
        error=error,
        recovery="none",
        legal_action_count=2,
        request_type=SECRET,
        request_id=(SECRET, 23),
    )
    assert path.stat().st_size <= diag.MALFORMED_ACTION_MAX_BYTES
    data = saved(capture)
    assert data["error"]["kind"] == "invalid_json"
    assert data["request_type"] == "other" and data["request_id"] == []
    assert len(data["parse_error_window"]["shape"]) <= 1024
    assert data["parse_error_window"]["start"] <= error.pos < data["parse_error_window"]["end"]
    assert SECRET not in path.read_text() and "密" not in path.read_text()


def test_hard_byte_cap_drops_windows_then_refuses_oversized_metadata(capture, monkeypatch):
    monkeypatch.setattr(diag, "MALFORMED_ACTION_MAX_BYTES", 600)
    parse('"' + "a" * 8000)
    data = saved(capture)
    assert data["syntax_omitted"] == "byte_cap"
    assert files(capture)[0].stat().st_size <= 600
    monkeypatch.setattr(diag, "MALFORMED_ACTION_MAX_BYTES", 1)
    parse()
    assert len(files(capture)) == 1


def test_retention_is_bounded_concurrently_and_only_touches_own_family(capture, monkeypatch):
    capture.mkdir()
    unrelated = capture / "game_plan_unusable_evidence.txt"
    unrelated.write_text("preserve")
    monkeypatch.setattr(diag, "MALFORMED_ACTION_KEEP", 3)
    monkeypatch.setattr(diag.time, "time_ns", lambda: 1000)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: parse(), range(8)))
    assert len(files(capture)) == 3
    assert sum(p.stat().st_size for p in files(capture)) <= 3 * diag.MALFORMED_ACTION_MAX_BYTES
    assert unrelated.read_text() == "preserve"


def test_write_failure_does_not_break_recovery_or_log_sensitive_path(capture, monkeypatch, caplog):
    obstacle = capture.parent / SECRET
    obstacle.write_text("file")
    monkeypatch.setattr(diag, "MALFORMED_ACTION_DIR", obstacle)
    plan = ActionPlanner(Mock())._parse_response(DAMAGE_PREFIX, [])
    assert plan.actions[0].distribution == {"Sneaking Guide": 1, "Skirk Prospector": 1}
    assert SECRET not in caplog.text


def test_valid_json_does_not_capture(capture):
    ActionPlanner(Mock())._parse_response('{"actions":[]}', [])
    assert not capture.exists()


def test_optout_preserves_existing_diagnostics_without_rotation(capture, monkeypatch):
    capture.mkdir()
    existing = capture / "action_json_diagnostic_prior.json"
    existing.write_text("existing evidence")
    monkeypatch.setenv("ARENAMCP_ACTION_JSON_DIAGNOSTICS", "off")
    parse()
    assert files(capture) == [existing]
    assert existing.read_text() == "existing evidence"
