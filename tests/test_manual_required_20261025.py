"""Finale stale payment snapshot and rejected Charm AutoResp, bug_20261025_132718."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from tests.test_llm_down_fallback import _Bridge, _engine

import arenamcp.autopilot as ap
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_pay_costs


def payment_state():
    req = json.loads((Path(__file__).parent / "fixtures/finale_unpayable_20261025.json").read_text())
    state = GameState()
    state.set_local_seat_id(1, source=2)
    _handle_pay_costs(state, {"payCostsReq": req})
    return {
        "match_id": "finale-match",
        "local_seat_id": 1,
        "turn": {"turn_number": 8, "active_player": 1, "phase": "Phase_Main1"},
        "pending_decision": "Pay Costs",
        "decision_context": state.decision_context,
        "_bridge_request_type": "PayCostsReq",
        "_bridge_request_class": "PayCostsRequest",
        "_bridge_has_pending": True,
        "_bridge_connected": True,
    }


class PaymentBridge(_Bridge):
    def __init__(self):
        super().__init__(
            {
                "has_pending": True,
                "request_type": "PayCostsReq",
                "request_class": "PayCostsRequest",
                "can_cancel": True,
            }
        )
        self.submit_auto_tap = Mock(return_value=False)
        self.cancel_action = Mock(return_value=True)
        self.submit_payment_action = Mock(return_value=True)


def test_refresh_payment_details_before_manual_handoff(monkeypatch):
    fresh = payment_state()
    bridge = PaymentBridge()
    engine = _engine(monkeypatch, Mock(), bridge, fresh)
    monkeypatch.setattr(ap.time, "sleep", lambda *_: None)
    engine._try_typed_decision_path = lambda *_: None
    engine._pause_for_manual = Mock()
    stale = copy.deepcopy(fresh)
    stale["decision_context"] = {"type": "pay_costs"}
    assert engine.process_trigger(stale, "decision_required")
    bridge.cancel_action.assert_called_once()
    bridge.submit_payment_action.assert_not_called()
    engine._pause_for_manual.assert_not_called()


def test_watchdog_cancels_impossible_finale_instead_of_autorespond(monkeypatch):
    state = payment_state()
    bridge = PaymentBridge()
    engine = _engine(monkeypatch, Mock(), bridge, state)
    assert engine._try_interactive_safe_default(state, "watchdog")
    bridge.cancel_action.assert_called_once()
    bridge.submit_payment_action.assert_not_called()


@pytest.mark.parametrize("accepted", [True, False])
def test_watchdog_submits_charm_mode_without_autorespond(monkeypatch, accepted):
    poll = {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "request_class": "CastingTimeOptionsRequest",
        "can_cancel": True,
        "game_state_id": 197,
        "msg_id": 270,
        "actions": [
            {
                "actionType": "CastingTimeOption",
                "choiceKind": "modal",
                "childIndex": 0,
                "optionIndex": i,
                "grpId": gid,
                "sourceId": 776,
                "minSel": 1,
                "maxSel": 1,
                "label": label,
            }
            for i, gid, label in [
                (0, 170534, "Search your library for a creature or land card"),
                (1, 2571, "Exile target artifact or enchantment"),
            ]
        ],
    }
    bridge = _Bridge(poll)
    bridge.submit_action_by_index = Mock(return_value=accepted)
    state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 8},
        "_bridge_request_type": "CastingTimeOptions",
        "decision_context": {"type": "casting_time_options"},
        "battlefield": [],
        "stack": [],
        "hand": [],
    }
    engine = _engine(monkeypatch, Mock(), bridge, state)
    assert engine._try_interactive_safe_default(state, "watchdog") is accepted
    bridge.submit_action_by_index.assert_called_once()
    assert bridge.submit_action_by_index.call_args.args[0] in [0, 1]
