"""Tests for the X-cost chooser bridge fix (issue #390).

Tests the Python-side of the submit_x command and numeric-input field
surfacing, using mocked pipe responses. No MTGA or Windows required.

The C# side (Plugin.cs) was already patched in the parent commit to:
- Surface numeric_min/max/step/disallowed/suggested fields
- Add the submit_x pipe command that calls SubmitX on the child

These tests verify the Python bridge can consume those fields correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.gre_bridge import GREBridge, BridgeDecisionPoller, GREBridgeError


# ---------------------------------------------------------------------------
# GREBridge.submit_x — pipe-level command encoding and error handling
# ---------------------------------------------------------------------------


def test_submit_x_sends_correct_command():
    """submit_x encodes action='submit_x' with the given value."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    with patch.object(bridge, "_send_safe", return_value={"ok": True, "submitted_type": "CastingTimeOption_X"}) as mock_send:
        result = bridge.submit_x(5)
        assert result is True
        mock_send.assert_called_once_with({"action": "submit_x", "value": 5})


def test_submit_x_returns_true_on_ok():
    """submit_x returns True when the plugin responds with ok=true."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    with patch.object(bridge, "_send_safe", return_value={"ok": True, "submitted_type": "CastingTimeOption_X", "value": 3}):
        assert bridge.submit_x(3) is True


def test_submit_x_returns_false_on_error():
    """submit_x returns False when the plugin responds with ok=false."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    with patch.object(bridge, "_send_safe", return_value={"ok": False, "error": "No numeric child"}):
        assert bridge.submit_x(0) is False


def test_submit_x_returns_false_on_comm_error():
    """submit_x returns False on GREBridgeError (disconnected, timeout)."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    with patch.object(bridge, "_send_safe", side_effect=GREBridgeError("Not connected")):
        assert bridge.submit_x(7) is False


def test_submit_x_zero_is_valid():
    """X=0 is a valid submission (e.g. Silkguard with X=0)."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    with patch.object(bridge, "_send_safe", return_value={"ok": True, "submitted_type": "CastingTimeOption_X", "value": 0}):
        assert bridge.submit_x(0) is True


# ---------------------------------------------------------------------------
# _BridgeSubmitMixin._safe_default_numeric — safe value selection
# ---------------------------------------------------------------------------

def _default_numeric(pending: dict) -> int:
    """Call _safe_default_numeric via the mixin."""
    mixin = _BridgeSubmitMixin.__new__(_BridgeSubmitMixin)
    return mixin._safe_default_numeric(pending)


def test_default_numeric_empty_pending():
    """Empty or missing fields return 0."""
    assert _default_numeric({}) == 0


def test_default_numeric_uses_suggested():
    """Suggested values are preferred over min."""
    pending = {
        "numeric_min": 0,
        "numeric_max": 10,
        "numeric_suggested": [5],
    }
    assert _default_numeric(pending) == 5


def test_default_numeric_skips_disallowed_suggested():
    """Disallowed suggested values are skipped; falls to min."""
    pending = {
        "numeric_min": 0,
        "numeric_max": 10,
        "numeric_disallowed": [0, 5],
        "numeric_suggested": [5, 0],
    }
    # 5 and 0 are disallowed; min=0 is also disallowed, so we'd try 1
    assert _default_numeric(pending) == 1


def test_default_numeric_falls_back_to_min():
    """No suggested values: use numeric_min (skipping disallowed)."""
    pending = {
        "numeric_min": 3,
        "numeric_max": 10,
    }
    assert _default_numeric(pending) == 3


def test_default_numeric_skips_disallowed_min():
    """Min is disallowed: walk up until finding a legal value."""
    pending = {
        "numeric_min": 2,
        "numeric_max": 10,
        "numeric_disallowed": [2, 3, 4],
    }
    assert _default_numeric(pending) == 5


def test_default_numeric_non_int_fields():
    """Non-integer values in fields don't crash."""
    pending = {
        "numeric_min": "abc",
        "numeric_max": "xyz",
        "numeric_disallowed": ["foo"],
        "numeric_suggested": ["bar"],
    }
    assert _default_numeric(pending) == 0


# ---------------------------------------------------------------------------
# GameAction.numeric_value — the numeric value is storable on actions
# ---------------------------------------------------------------------------

def test_action_carries_numeric_value():
    """GameAction instances carry a numeric_value field for X spells."""
    from arenamcp.action_planner import GameAction

    action = GameAction(action_type="CAST_SPELL", card_name="Lightning Bolt", numeric_value=3)
    assert action.numeric_value == 3


# ---------------------------------------------------------------------------
# get_pending_actions — pipe response shape for casting_time_options
# ---------------------------------------------------------------------------

def test_get_pending_actions_passthrough_fields():
    """The numeric_* fields are forwarded from plugin to caller."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    plugin_response = {
        "ok": True,
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "can_pass": True,
        "actions": [
            {"actionType": "CastingTimeOption", "choiceKind": "done", "label": "Done"},
        ],
        "decision_context": {
            "type": "casting_time_options",
            "num_options": 1,
            "options": [
                {"actionType": "CastingTimeOption", "choiceKind": "done", "label": "Done"},
            ],
        },
        "numeric_min": 0,
        "numeric_max": 10,
        "numeric_step": 1,
        "numeric_input_type": "Integer",
    }

    with patch.object(bridge, "_send_safe", return_value=plugin_response):
        result = bridge.get_pending_actions()
        assert result is not None
        assert result["ok"] is True
        assert result["has_pending"] is True
        assert result["numeric_min"] == 0
        assert result["numeric_max"] == 10
        assert result["numeric_step"] == 1
        assert result["numeric_input_type"] == "Integer"
        assert result["decision_context"]["type"] == "casting_time_options"


def test_get_pending_actions_disallowed_field():
    """The numeric_disallowed array is forwarded properly."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    plugin_response = {
        "ok": True,
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "can_pass": True,
        "actions": [],
        "decision_context": {"type": "casting_time_options", "num_options": 0, "options": []},
        "numeric_min": 0,
        "numeric_max": 10,
        "numeric_disallowed": [0, 9, 10],
    }

    with patch.object(bridge, "_send_safe", return_value=plugin_response):
        result = bridge.get_pending_actions()
        assert result is not None
        assert result["numeric_disallowed"] == [0, 9, 10]
        assert _default_numeric(result) == 1  # 0 is disallowed, try 1


def test_get_pending_actions_disallow_even():
    """DisallowEven flag is forwarded (numeric_disallow_even: true)."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    plugin_response = {
        "ok": True,
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "can_pass": True,
        "actions": [],
        "decision_context": {"type": "casting_time_options", "num_options": 0, "options": []},
        "numeric_min": 1,
        "numeric_max": 10,
        "numeric_disallow_even": True,
    }

    with patch.object(bridge, "_send_safe", return_value=plugin_response):
        result = bridge.get_pending_actions()
        assert result is not None
        assert result.get("numeric_disallow_even") is True


def test_get_pending_actions_grp_id():
    """GrpId from the numeric child surfaces through get_pending_actions."""
    bridge = GREBridge()
    bridge._connected = True
    bridge._pipe_file = MagicMock()

    plugin_response = {
        "ok": True,
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "can_pass": True,
        "actions": [],
        "decision_context": {"type": "casting_time_options", "num_options": 0, "options": []},
        "numeric_min": 0,
        "numeric_max": 10,
        "grp_id": 76543,
    }

    with patch.object(bridge, "_send_safe", return_value=plugin_response):
        result = bridge.get_pending_actions()
        assert result is not None
        assert result["grp_id"] == 76543


# ---------------------------------------------------------------------------
# BridgeDecisionPoller — enrichment shapes for casting_time_options
# ---------------------------------------------------------------------------

class _DummyBridge:
    connected = True

    def connect(self) -> bool:
        return True


def test_bridge_enrich_snapshot_casting_options():
    """enrich_snapshot populates _bridge_* fields for casting_time_options."""
    poller = BridgeDecisionPoller(_DummyBridge())
    poller._last_poll_result = {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionRequest",
        "can_cancel": True,
        "actions": [
            {"actionType": "CastingTimeOption", "choiceKind": "done", "label": "Done"},
        ],
        "can_pass": True,
        "decision_context": {
            "type": "casting_time_options",
            "num_options": 1,
            "options": [
                {"actionType": "CastingTimeOption", "choiceKind": "done", "label": "Done"},
            ],
        },
    }

    snapshot: dict = {}
    poller.enrich_snapshot(snapshot)

    assert snapshot["_bridge_request_type"] == "CastingTimeOptions"
    assert snapshot["_bridge_request_class"] == "CastingTimeOptionRequest"
    assert snapshot["_bridge_can_cancel"] is True
    assert snapshot["decision_context"]["type"] == "casting_time_options"
    assert snapshot["pending_decision"] == "Casting Option"
