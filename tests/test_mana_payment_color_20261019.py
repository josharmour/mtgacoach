"""Manual mana payment on the Mac bridge, from match 115944ce (2026-10-19, ledger #63).

Player.log 11:17:34, PayCostsReq msgId 24: Slickshot Show-Off's plot ({1}{R})
with one untapped Mountain (#283) and no Auto Pay. The autopilot paid one
source at a time (mana_payment.choose_payment_source) and asked the bridge for
"ManaColor_Red" — the protobuf name Player.log prints. The IL2CPP enum (like
the CLR one the BepInEx plugin parses) is ``ManaColor.Red``: the coach log's
own "manaCost": [{"color": '[ "Red" ]'}] shows it. The adapter answered
"Instance 283 cannot make ManaColor_Red" for a Mountain, so every manual
payment failed. The cost was also more than the one Mountain could pay: the
cast should be cancelled before tapping anything.
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

import pytest

from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_pay_costs
from arenamcp.mana_payment import payment_shortfall

LOCAL = 1

PAY_COSTS_24 = json.loads(
    """{"type": "GREMessageType_PayCostsReq", "systemSeatIds": [1], "msgId": 24, "gameStateId": 8,
 "prompt": {"promptId": 11, "parameters": [{"parameterName": "Cost", "type": "ParameterType_NonLocalizedString",
  "stringValue": "o1oR"}]},
 "payCostsReq": {"manaCost": [{"color": ["ManaColor_Generic"], "count": 1, "objectId": 284, "abilityGrpId": 328},
   {"color": ["ManaColor_Red"], "count": 1, "objectId": 284, "abilityGrpId": 328}],
  "paymentActions": {"actions": [
   {"actionType": "ActionType_Activate_Mana", "grpId": 65385, "instanceId": 283, "facetId": 283, "abilityGrpId": 1004,
    "manaPaymentOptions": [{"mana": [{"manaId": 2, "color": "ManaColor_Red", "srcInstanceId": 283,
     "specs": [{"type": "ManaSpecType_Predictive"}], "abilityGrpId": 1004, "count": 1}]}], "isBatchable": true,
    "uniqueAbilityId": 209}]},
  "paymentSelection": {"context": "SelectionContext_ManaPool", "optionContext": "OptionContext_Payment",
   "listType": "SelectionListType_Dynamic", "idType": "IdType_ManaId",
   "validationType": "SelectionValidationType_NonRepeatable", "minWeight": -2147483648, "maxWeight": 2147483647}},
 "allowCancel": "AllowCancel_Abort", "allowUndo": true}"""
)


def test_bridge_asks_for_the_clr_color_name():
    from arenamcp.gre_bridge import GREBridge

    bridge = GREBridge.__new__(GREBridge)
    sent = []
    bridge._send_safe = lambda command, timeout=None: sent.append(command) or {"ok": True}
    assert bridge.submit_payment_action(283, 1004, "R") is True
    assert sent == [
        {"action": "submit_payment_action", "instance_id": 283, "ability_grp_id": 1004, "color": "Red"}
    ]


@pytest.mark.parametrize("color", ["Red", "ManaColor_Red"])
def test_mac_adapter_matches_il2cpp_enum_names(color):
    from arenamcp.mac_bridge_adapter import MacBridgeAdapter

    red = {"$c": "ManaPaymentOption", "$h": 31, "mana_": {"$items": [{"color_": {"e": "Red", "v": 4}}]}}
    action = {
        "$c": "Wotc.Mtgo.Gre.External.Messaging.Action",
        "$h": 20,
        "instanceId_": 283,
        "abilityGrpId_": 1004,
        "manaPaymentOptions_": {"$items": [red]},
    }
    request = {
        "$c": "GreClient.Rules.PayCostsRequest",
        "$h": 7,
        "PaymentActions": {"$c": "ActionsAvailableRequest", "$h": 10, "Actions": {"$items": [action]}},
    }
    sent = []

    def send(command, timeout):
        sent.append(command)
        return {"ok": True, "results": [request, None]} if len(sent) == 1 else {"ok": True, "results": []}

    adapter = MacBridgeAdapter(send, "il2cpp-macos")
    reply = adapter.handle(
        {"action": "submit_payment_action", "instance_id": 283, "ability_grp_id": 1004, "color": color}
    )
    assert reply == {"ok": True, "submitted_type": "PaymentAction", "instance_id": 283}
    assert sent[-1]["ops"][5]["args"] == [{"h": 31}]  # the red option, kept alone on the copy


class _PayCostsBridge:
    """The Mac bridge while the plot's PayCostsRequest (no AutoTapActions child) is pending."""

    connected = True

    def __init__(self):
        self.payments = []
        self.cancel_calls = 0

    def connect(self):
        return True

    def get_pending_actions(self):
        return {
            "ok": True,
            "has_pending": True,
            "request_type": "PayCostsReq",
            "request_class": "PayCostsRequest",
            "can_cancel": True,
            "game_state_id": 8,
            "msg_id": 24,
        }

    def submit_auto_tap(self, solution_index=0):
        return False  # "Pending is PayCostsRequest, no AutoTapActionsRequest available"

    def submit_payment_action(self, instance_id, ability_grp_id, color):
        self.payments.append((instance_id, color))
        return True

    def cancel_action(self):
        self.cancel_calls += 1
        return True


def test_plot_cost_one_mountain_cannot_cover_is_cancelled_before_tapping(monkeypatch):
    import arenamcp.autopilot as autopilot_module
    from arenamcp.autopilot import AutopilotConfig, AutopilotEngine

    log = GameState()
    _handle_pay_costs(log, PAY_COSTS_24)
    context = log.decision_context
    assert context["mana_requirements"] == {"generic": 1, "R": 1}
    assert payment_shortfall(context) == "1 mana offered for a cost of 2"

    bridge = _PayCostsBridge()
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    monkeypatch.setattr(autopilot_module.time, "sleep", lambda *_: None)
    engine = AutopilotEngine(planner=Mock(), config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = bridge
    engine._pause_for_manual = Mock()
    engine._try_typed_decision_path = lambda game_state, trigger: None  # PayCosts is not a typed family
    engine._recent_bot_submissions.append((time.monotonic(), "slickshot show-off"))
    snapshot = {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 1, "active_player": LOCAL},
        "pending_decision": "Pay Costs",
        "decision_context": dict(context),
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "PayCostsReq",
        "_bridge_request_class": "PayCostsRequest",
    }
    assert engine.process_trigger(snapshot, "decision_required") is True
    assert bridge.payments == [] and bridge.cancel_calls == 1
    engine._pause_for_manual.assert_not_called()


def test_payment_shortfall_counts_multi_mana_sources_and_colors():
    def context(needs, sources, pool=0):
        return {
            "requirements_modeled": True,
            "mana_requirements": needs,
            "payment_sources": sources,
            "pool_mana_count": pool,
        }

    sol_ring = {"instance_id": 7, "colors": ["C"], "amount": 2}
    mountain = {"instance_id": 8, "colors": ["R"]}
    assert payment_shortfall(context({"generic": 2, "R": 1}, [sol_ring, mountain])) == ""
    assert payment_shortfall(context({"generic": 3, "R": 1}, [sol_ring, mountain])) == (
        "3 mana offered for a cost of 4"
    )
    assert (
        payment_shortfall(context({"generic": 1, "G": 1}, [sol_ring, mountain])) == "nothing offered makes G"
    )
    assert payment_shortfall(context({"generic": 1, "G": 1}, [sol_ring, mountain], pool=1)) == ""
    # A source whose mana is unknown leaves the plan to the payment steps.
    assert payment_shortfall(context({"generic": 3}, [{"instance_id": 9, "colors": []}])) == ""


def test_log_capture_counts_a_source_making_two_mana():
    message = json.loads(json.dumps(PAY_COSTS_24))
    message["payCostsReq"]["paymentActions"]["actions"].append(
        {
            "actionType": "ActionType_Activate_Mana",
            "instanceId": 290,
            "abilityGrpId": 1152,
            "manaPaymentOptions": [{"mana": [{"color": "ManaColor_Colorless", "count": 2}]}],
        }
    )
    log = GameState()
    _handle_pay_costs(log, message)
    sources = log.decision_context["payment_sources"]
    assert [source.get("amount") for source in sources] == [None, 2]
    assert payment_shortfall(log.decision_context) == ""
