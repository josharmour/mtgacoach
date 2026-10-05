"""Order Replacement must resolve through the replacement request, not a pass.

2026-10-04 22:50:48: the planner picked "Select replacement effect order",
which mapped to no action; the fallback chose "Done", the bridge sent
submit_pass and the Mac bridge refused it (pending SelectReplacementRequest),
so the autopilot asked for a manual click.
"""

from __future__ import annotations

from unittest.mock import Mock

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin


def engine():
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.connect.return_value = True
    pilot._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "SelectReplacementRequest",
        "request_type": "SelectReplacement",
    }
    pilot._gre_bridge.submit_select_replacement.return_value = True
    pilot._gre_bridge.submit_pass.return_value = False
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    return pilot


STATE = {
    "_bridge_connected": True,
    "_bridge_request_type": "SelectReplacement",
    "_bridge_request_class": "SelectReplacementRequest",
}


def test_replacement_menu_entry_maps_to_the_replacement_request():
    action = ActionPlanner.__new__(ActionPlanner)._legal_action_to_action("Select replacement effect order")
    assert (action.action_type, action.modal_index) == (ActionType.SELECT_REPLACEMENT, 0)


def test_done_on_order_replacement_keeps_the_default_replacement():
    pilot = engine()
    result = pilot._try_gre_bridge(GameAction(ActionType.CLICK_BUTTON, card_name="done"), dict(STATE))
    assert result is not None and result.success
    pilot._gre_bridge.submit_select_replacement.assert_called_once_with(index=0)
    pilot._gre_bridge.submit_pass.assert_not_called()
