import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.parser import LogParser
from arenamcp.recovery_trace import RecoveryTrace, install_recovery_trace
from arenamcp.standalone_autopilot_capture import _AutopilotCaptureMixin


def request(msg=146, state=106):
    return {
        "greToClientEvent": {
            "greToClientMessages": [
                {
                    "type": "GREMessageType_SelectNReq",
                    "msgId": msg,
                    "gameStateId": state,
                    "prompt": {
                        "promptId": 5245,
                        "parameters": [{"parameterName": "CardId", "numberValue": 226}],
                    },
                    "selectNReq": {
                        "minSel": 1,
                        "maxSel": 1,
                        "sourceId": 9005,
                        "listType": "SelectionListType_Static",
                        "staticList": "StaticList_CardNames",
                        "unfilteredIds": [160, 163, 165, 166, 211, 223],
                    },
                }
            ]
        }
    }


def response(value=77529, msg=146, state=106):
    return {
        "requestId": 99,
        "transactionId": "private-envelope",
        "token": "private-token",
        "clientToMatchServiceMessageType": "ClientToMatchServiceMessageType_ClientToGREMessage",
        "payload": {
            "type": "ClientMessageType_SelectNResp",
            "gameStateId": state,
            "respId": msg,
            "selectNResp": {"ids": [value]},
        },
    }


def rejection(value=77529, msg=146, state=106):
    return {
        "greToClientEvent": {
            "greToClientMessages": [
                {
                    "type": "GREMessageType_IllegalRequest",
                    "gameStateId": state,
                    "illegalRequestMessage": {
                        "reason": "FailureReason_InvalidCardName",
                        "invalidMessage": response(value, msg, state)["payload"],
                    },
                }
            ]
        }
    }


def incident(trace):
    trace.observe_gre(request(), {"match_id": "match-one", "game_number": 1})
    trace.observe_bridge({"action": "submit_selection", "ids": [77529]}, {"ok": True})
    trace.observe_client(response())
    trace.observe_gre(rejection())
    trace.observe_gre(request(150, 107))
    trace.observe_client(response(219, 150, 107))
    trace.observe_gre(rejection(219, 150, 107))
    trace.observe_gre(request(153, 108))
    trace.observe_client(response(21415, 153, 108))


def test_real_name_selection_failure_and_manual_recovery_survive_clear_and_delayed_report(monkeypatch):
    monkeypatch.setattr(
        "arenamcp.card_name_choice.card_title_name",
        lambda value: "Channel" if value == 21415 else "",
    )
    clock = [0.0]
    trace = RecoveryTrace(clock=lambda: clock[0], max_events=10)
    incident(trace)
    # Live pending_decision clears, and ordinary traffic evicts the old tail.
    for _ in range(40):
        trace.observe_bridge({"action": "submit_action", "action_index": 0}, {"ok": True})
    clock[0] = 301
    result = trace.snapshot()
    assert result["events"] == []
    assert len(result["incidents"]) == 2
    first = result["incidents"][0]
    assert first["request"]["request_id"] == 146
    assert first["rejection"]["failure_reason"] == "FailureReason_InvalidCardName"
    assert first["rejection"]["details"]["ids"] == [77529]
    assert first["nearby_bridge_attempts"][0]["arguments"]["ids"] == [77529]
    recovered = first["follow_up_responses"][-1]
    assert recovered["response_to"] == 153 and recovered["request_match"] == "exact"
    assert recovered["selections"] == [{"id": 21415, "expected_id_domain": "TitleId", "card_name": "Channel"}]
    assert recovered["origin"] == "unknown_client_input"
    assert "success" not in recovered  # submission alone does not prove acceptance
    clock[0] = 1801
    assert trace.snapshot()["incidents"] == []


def test_real_multiline_parser_envelope_reaches_installed_capture(monkeypatch):
    monkeypatch.setattr("arenamcp.card_name_choice.card_title_name", lambda _: "")
    trace = RecoveryTrace()
    parser = LogParser()
    install_recovery_trace(parser, lambda: {"match_id": "match-one", "game_number": 1}, trace=trace)
    incoming = (
        "[UnityCrossThreadLogger]11:11:41 PM: GreToClientEvent\n" + json.dumps(request(153, 108)) + "\n"
    )
    outgoing = (
        "[UnityCrossThreadLogger]10/25/2026 11:11:46 PM: account-redacted to Match: ClientToGremessage\n"
        + json.dumps(response(21415, 153, 108), indent=2)
        + "\n"
    )
    # The watcher can split in the middle of a JSON payload.
    data = incoming + outgoing
    parser.process_chunk(data[:213])
    parser.process_chunk(data[213:])
    final = trace.snapshot()["events"][-1]
    assert final["kind"] == "client_response"
    assert final["request_match"] == "exact"
    assert final["selections"][0] == {"id": 21415, "expected_id_domain": "TitleId"}
    assert "private-token" not in json.dumps(trace.snapshot())


@pytest.mark.parametrize(
    "msg,state,expected",
    [(999, 106, "missing_request"), (146, 107, "state_mismatch"), (146, None, "missing_state_id")],
)
def test_never_matches_a_different_or_incomplete_request(msg, state, expected):
    trace = RecoveryTrace()
    trace.observe_gre(request())
    trace.observe_client(response(21415, msg, state))
    row = trace.snapshot()["events"][-1]
    assert row["request"] is None
    assert row["request_match"] == expected
    assert row["selections"][0]["expected_id_domain"] == "unknown"


def test_repeated_legitimate_requests_are_not_called_failures(monkeypatch):
    monkeypatch.setattr("arenamcp.card_name_choice.card_title_name", lambda _: "")
    trace = RecoveryTrace()
    trace.observe_gre(request())
    trace.observe_client(response())
    trace.observe_gre(request(150, 107))
    trace.observe_gre(request(150, 107))
    assert trace.snapshot()["incidents"] == []
    assert len(trace.snapshot()["events"]) == 3


def test_later_same_card_prompt_does_not_replace_the_recovery_response(monkeypatch):
    monkeypatch.setattr("arenamcp.card_name_choice.card_title_name", lambda _: "")
    trace = RecoveryTrace()
    incident(trace)
    trace.observe_gre(request(200, 150))
    trace.observe_client(response(99999, 200, 150))
    assert trace.snapshot()["incidents"][0]["follow_up_responses"][-1]["details"]["ids"] == [21415]


def test_incidents_keep_original_game_when_new_match_or_bo3_game_starts(monkeypatch):
    monkeypatch.setattr("arenamcp.card_name_choice.card_title_name", lambda _: "")
    trace = RecoveryTrace()
    incident(trace)
    trace.observe_gre(request(), {"match_id": "match-one", "game_number": 2})
    trace.observe_client(response())
    assert trace.snapshot()["incidents"][0]["game_number"] == 1
    assert len(trace.snapshot()["incidents"][0]["follow_up_responses"]) == 2
    trace.observe_gre(request(), {"match_id": "match-two", "game_number": 1})
    result = trace.snapshot()
    assert len(result["events"]) == 1
    assert result["incidents"][0]["match_id"] == "match-one"


def test_capture_is_bounded_and_drops_arbitrary_credentials_and_strings():
    trace = RecoveryTrace(max_events=5, max_bytes=6000)
    for _ in range(30):
        trace.observe_bridge(
            {
                "action": "submit_selection",
                "ids": list(range(10000)),
                "token": "credential",
                "password": "credential",
                "selection_id_type": "card_name",
            },
            {"ok": {"secret": "credential"}, "error": "Bearer credential"},
            "error contained credential",
        )
    trace.observe_bridge({"action": "submit_selection", "ids": ["credential"]}, {"ok": "credential"})
    result = trace.snapshot()
    encoded = json.dumps(result)
    assert "credential" not in encoded
    assert len(encoded) <= 6000
    assert len(result["events"]) <= 5 and len(result["incidents"]) <= 6
    assert result["events"][-1]["ok"] is None
    assert result["events"][-1]["arguments"]["ids"] == [None]


def test_autopilot_bug_clicked_after_recovery_includes_pre_click_incident(tmp_path, monkeypatch):
    monkeypatch.setattr("arenamcp.card_name_choice.card_title_name", lambda _: "")
    trace = RecoveryTrace()
    incident(trace)
    monkeypatch.setattr("arenamcp.recovery_trace.recovery_trace", trace)
    monkeypatch.setattr("arenamcp.standalone_autopilot_capture.LOG_DIR", tmp_path)

    class Runtime(_AutopilotCaptureMixin):
        def _capture_observed_state(self, state=None):
            return {"match_id": "match-one", "pending_decision": None}

        def _collect_autopilot_info(self):
            return {"pending_plan": None}

        def _collect_bridge_state(self):
            return {"has_pending": False}

    runtime = Runtime()
    runtime.ui, runtime.settings = Mock(), Mock()
    runtime._autopilot = SimpleNamespace(on_abort=Mock())
    runtime._start_recovery_observer = Mock()
    runtime.capture_autopilot_bug("after-manual-recovery")
    report = json.loads(runtime._autopilot_bug_capture.report_path.read_text())
    assert report["game_state"]["pending_decision"] is None
    assert report["recovery_trace"]["incidents"][0]["request"]["request_id"] == 146
    assert report["recovery_trace"]["incidents"][0]["follow_up_responses"][-1]["details"]["ids"] == [21415]
