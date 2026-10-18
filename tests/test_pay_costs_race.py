"""Regression test for issue #405 — a false MANUAL REQUIRED on pay_costs.

One trigger batch dispatches every trigger against the same snapshot. On
2026-07-05 23:01:01 the first dispatch paid the PayCosts and the second
re-entered the branch on the consumed window:

    Autopilot: submitting AutoTap solution for PayCosts
    GRE bridge submitted auto_tap: index 0
    GRE bridge submit_auto_tap failed: Pending is null, no AutoTapActionsRequest available
    GRE bridge cancel_action failed: No pending interaction
    Autopilot manual required: GRE bridge submit_auto_tap did not advance Pay Costs

The P1-5 guard checks the window on *entry*. It does not help when the window
is consumed while auto-pay is already running — the failed submit retries
after 0.4 s, ample time for another dispatch to land. "Pending is null" after
our own submit means the cost was paid, not that payment failed.
"""

from unittest.mock import Mock

import arenamcp.autopilot as autopilot_module
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine, AutopilotState


class _ConsumedWindowBridge:
    """PayCosts is pending on entry; once we try to auto-pay, another dispatch has paid it."""

    connected = True

    def __init__(self) -> None:
        self.auto_tap_calls = 0
        self.cancel_calls = 0

    def connect(self) -> bool:
        return True

    def get_pending_actions(self) -> dict:
        if self.auto_tap_calls:
            return {"ok": True, "has_pending": False}
        return {
            "ok": True,
            "has_pending": True,
            "request_type": "PayCostsReq",
            "request_class": "PayCostsRequest",
            "game_state_id": 42,
            "msg_id": 7,
        }

    def submit_auto_tap(self, solution_index: int = 0) -> bool:
        self.auto_tap_calls += 1
        return False  # "Pending is null, no AutoTapActionsRequest available"

    def cancel_action(self) -> bool:
        self.cancel_calls += 1
        return False  # "No pending interaction"


def test_a_window_paid_by_a_concurrent_trigger_is_done_not_manual_required(monkeypatch):
    bridge = _ConsumedWindowBridge()
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: bridge)
    monkeypatch.setattr(autopilot_module.time, "sleep", lambda *_: None)  # the 0.4 s late-child retry
    engine = AutopilotEngine(planner=Mock(), config=AutopilotConfig(dry_run=False))
    engine._gre_bridge = bridge
    engine._try_typed_decision_path = lambda game_state, trigger: None  # PayCosts is not a typed family
    engine._pause_for_manual = Mock()
    engine._manual_required_bridge_result = Mock()
    engine._record_autopilot_decision = Mock()
    snapshot = {
        "local_seat_id": 1,
        "turn": {"turn_number": 6, "active_player": 1, "phase": "Phase_Main1"},
        "pending_decision": "Pay Costs",
        "decision_context": {"type": "pay_costs", "source_card": "Hei Bai, Forest Guardian", "mana_cost": "{3}{G}"},
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "PayCostsReq",
        "_bridge_request_class": "PayCostsRequest",
    }

    assert engine.process_trigger(snapshot, "decision_required") is True

    assert bridge.auto_tap_calls == 2  # the submit and its late-child retry both failed
    assert bridge.cancel_calls == 0, "must not blind-cancel a consumed window"
    engine._pause_for_manual.assert_not_called()
    engine._manual_required_bridge_result.assert_not_called()
    assert engine._record_autopilot_decision.call_args.kwargs["summary"] == (
        "PayCosts already paid by a concurrent trigger"
    )
    assert engine._state == AutopilotState.IDLE
