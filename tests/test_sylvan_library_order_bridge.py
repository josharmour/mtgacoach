"""bug_20261108_191826: Sylvan Library OrderReq 263/190 must not pass.

After selecting two cards, Arena offered ids [602, 601]. The planner's order
pick had no mapping; Done sent submit_pass, then manual-required and Oops.
"""

from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin


def engine(request="OrderRequest", success=True):
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.connect.return_value = True
    pilot._gre_bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": request,
        "request_type": "Order",
        "ids": [602, 601],
        "game_state_id": 190,
        "msg_id": 263,
    }
    pilot._gre_bridge.submit_order.return_value = success
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    return pilot


def test_order_menu_pick_is_executable():
    planner = ActionPlanner(Mock())
    menu = ["Order triggered abilities", "Done"]
    planner._last_menu = menu
    plan = planner._parse_response('{"actions":[{"pick":1}]}', menu)
    assert len(plan.actions) == 1
    assert plan.actions[0].action_type == ActionType.ORDER_TRIGGERS
    pilot = engine()
    result = pilot._try_gre_bridge(plan.actions[0], {})
    assert result.success
    pilot._gre_bridge.submit_order.assert_called_once_with()
    pilot._gre_bridge.submit_pass.assert_not_called()


@pytest.mark.parametrize("request_type", ["Order", "OrderReq", "OrderRequest", "OrderTriggers"])
@pytest.mark.parametrize("key", ["_bridge_request_type", "_bridge_request_class"])
def test_done_confirms_order_instead_of_passing(request_type, key):
    pilot = engine()
    result = pilot._try_gre_bridge(GameAction(ActionType.CLICK_BUTTON, card_name="done"), {key: request_type})
    assert result.success
    pilot._gre_bridge.submit_order.assert_called_once_with()
    pilot._gre_bridge.submit_pass.assert_not_called()


def test_failed_order_never_falls_back_to_pass():
    pilot = engine(success=False)
    result = pilot._try_gre_bridge(
        GameAction(ActionType.CLICK_BUTTON, card_name="done"),
        {"_bridge_request_type": "Order"},
    )
    assert result is None
    pilot._gre_bridge.submit_order.assert_called_once_with()
    pilot._gre_bridge.submit_pass.assert_not_called()


def test_changed_live_request_does_not_submit_old_order_or_pass():
    pilot = engine(request="ActionsAvailableRequest")
    result = pilot._try_gre_bridge(
        GameAction(ActionType.CLICK_BUTTON, card_name="done"),
        {"_bridge_request_type": "Order"},
    )
    assert result is None
    pilot._gre_bridge.submit_order.assert_not_called()
    pilot._gre_bridge.submit_pass.assert_not_called()
