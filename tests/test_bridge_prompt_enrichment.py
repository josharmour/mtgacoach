from arenamcp.coach import _build_bridge_context_lines
from arenamcp.gre_bridge import BridgeDecisionPoller, enrich_snapshot_from_pending_response


class _DummyBridge:
    connected = True

    def connect(self) -> bool:
        return True


def test_bridge_enrich_snapshot_merges_request_payload_and_decision_context():
    poller = BridgeDecisionPoller(_DummyBridge())
    poller._last_poll_result = {
        "has_pending": True,
        "request_type": "Search",
        "request_class": "SearchRequest",
        "actions": [],
        "can_pass": False,
        "can_cancel": True,
        "allow_undo": True,
        "request_payload": {
            "requestType": "Search",
            "zoneIds": [41],
            "options": [{"grpId": 1001}, {"grpId": 1002}],
        },
        "decision_context": {
            "zoneId": 41,
            "options": [{"grpId": 1001}, {"grpId": 1002}],
            "max": 1,
        },
    }

    snapshot = {"decision_context": {"type": "unknown_req", "existing": "keep-me"}}
    poller.enrich_snapshot(snapshot)

    assert snapshot["_bridge_request_type"] == "Search"
    assert snapshot["_bridge_request_class"] == "SearchRequest"
    assert snapshot["_bridge_can_cancel"] is True
    assert snapshot["_bridge_allow_undo"] is True
    assert snapshot["_bridge_request_payload"]["zoneIds"] == [41]
    assert snapshot["pending_decision"] == "Search Library"
    assert snapshot["decision_context"]["type"] == "search"
    assert snapshot["decision_context"]["existing"] == "keep-me"
    assert snapshot["decision_context"]["zoneId"] == 41
    assert snapshot["decision_context"]["_bridge_source"] is True


def test_build_bridge_context_lines_includes_payload_and_recent_gre():
    lines = _build_bridge_context_lines(
        {
            "_bridge_request_type": "Search",
            "_bridge_request_class": "SearchRequest",
            "_bridge_request_payload": {
                "zoneIds": [41],
                "options": [{"grpId": 1001, "label": "Forest"}],
            },
            "raw_gre_events": [
                {
                    "seq": 8,
                    "type": "GREMessageType_SearchReq",
                    "turn": 5,
                    "phase": "Phase_Main1",
                    "payload": {
                        "searchReq": {
                            "zoneIds": [41],
                            "options": [{"grpId": 1001, "label": "Forest"}],
                        }
                    },
                }
            ],
        },
        [],
    )

    assert any(line == "GRE_Request: Search" for line in lines)
    assert any(line == "GRE_RequestClass: SearchRequest" for line in lines)
    assert any(line.startswith("GRE_RequestPayload: ") and "zoneIds" in line for line in lines)
    assert any(line.startswith("GRE_Recent: ") and "GREMessageType_SearchReq" in line for line in lines)


def test_bridge_overrides_stale_actions_available_type_when_request_shifts():
    """Regression: previous snapshot type "actions_available" must not mask
    a bridge-authoritative Search/SelectTargets/PayCosts request. Otherwise
    rules_engine.get_legal_actions() falls through to stale cast actions.

    See github.com/josharmour/mtgacoach/issues/65.
    """
    poller = BridgeDecisionPoller(_DummyBridge())
    poller._last_poll_result = {
        "has_pending": True,
        "request_type": "Search",
        "request_class": "SearchRequest",
        "actions": [],
        "can_pass": False,
        "can_cancel": True,
        "allow_undo": False,
        "request_payload": {"requestType": "Search", "zoneIds": [41]},
        # Plugin did NOT provide a decision_context this poll — common for
        # requests whose specific shape the plugin hasn't serialized yet.
    }

    snapshot = {
        "decision_context": {"type": "actions_available"},
        "legal_actions": ["Cast Summon: Fenrir [OK]", "Pass"],
    }
    poller.enrich_snapshot(snapshot)

    assert snapshot["decision_context"]["type"] == "search"
    assert snapshot["decision_context"]["_bridge_source"] is True


def test_bridge_preserves_plugin_supplied_type_over_bridge_mapping():
    """If the plugin stamps a specific type (e.g. a casting-time subtype),
    the generic bridge-request mapping must not downgrade it.
    """
    poller = BridgeDecisionPoller(_DummyBridge())
    poller._last_poll_result = {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "actions": [],
        "can_pass": False,
        "can_cancel": False,
        "allow_undo": False,
        "decision_context": {"type": "casting_time_choose_or_cost"},
    }

    snapshot = {"decision_context": {"type": "actions_available"}}
    poller.enrich_snapshot(snapshot)

    assert snapshot["decision_context"]["type"] == "casting_time_choose_or_cost"


def test_enrich_snapshot_from_pending_response_clears_stale_state_when_bridge_idle():
    snapshot = {
        "pending_decision": "Select Targets",
        "decision_context": {"type": "target_selection"},
        "legal_actions": ["Select target: Alpha Myr"],
        "legal_actions_raw": [{"actionType": "ActionType_SelectTarget"}],
    }

    enrich_snapshot_from_pending_response(
        snapshot,
        {"has_pending": False},
        bridge_connected=True,
    )

    assert snapshot["_bridge_connected"] is True
    assert snapshot["pending_decision"] is None
    assert snapshot["decision_context"] is None
    assert snapshot["legal_actions"] == []
    assert snapshot["legal_actions_raw"] == []


def test_bridge_enrich_snapshot_infers_scry_from_generic_selection_payload():
    poller = BridgeDecisionPoller(_DummyBridge())
    poller._last_poll_result = {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "actions": [],
        "can_pass": False,
        "can_cancel": True,
        "allow_undo": False,
        "request_payload": {
            "requestType": "SelectN",
            "promptText": "Scry 1",
            "count": 1,
        },
        "decision_context": {
            "type": "selection_generic",
            "promptText": "Scry 1",
            "count": 1,
        },
    }

    snapshot: dict[str, object] = {}
    poller.enrich_snapshot(snapshot)

    assert snapshot["pending_decision"] == "Scry"
    assert snapshot["decision_context"]["type"] == "scry"
    assert snapshot["decision_context"]["_bridge_source"] is True
