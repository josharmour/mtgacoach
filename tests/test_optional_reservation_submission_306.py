"""#306 submission leg: a reserved optional payment is cancelled, never auto-tapped.

The producer/planner half is pinned by ``test_optional_reservation_306`` (the
decline reason) and ``test_post_payment_commitment_306`` (the refreshed hold
after confirmed spending). What was still unpinned is the last hop: the PayCosts
safe-default handler must submit MTGA's cancel instead of an AutoTap solution
when the reservation holds, and must still pay when the mana is spare or the
payment is unrelated to the reserve.

Historical raw packet is not bundled here; these are equivalent board/memory
regressions based on the ledger, not a claim of complete replay reconstruction.
"""

from __future__ import annotations

from tests.fakes import make_engine
from tests.test_optional_reservation_306 import case

from arenamcp.decisions import PendingDecision

PAY_COSTS_PENDING = {
    "ok": True,
    "has_pending": True,
    "request_type": "PayCosts",
    "request_class": "PayCostsRequest",
    "can_cancel": True,
}


class RecordingBridge:
    """A connected bridge showing one PayCosts poll and recording submissions."""

    def __init__(self, pending):
        self.connected = True
        self.pending = pending
        self.cancelled = 0
        self.auto_taps = 0

    def connect(self):
        return True

    def get_pending_actions(self):
        return self.pending

    def cancel_action(self):
        self.cancelled += 1
        return True

    def submit_auto_tap(self):
        self.auto_taps += 1
        return True


def pay_costs_state():
    state, planner, _ = case()
    state["_bridge_request_type"] = "PayCosts"
    state["_bridge_request_class"] = "PayCostsRequest"
    return state, planner


def submit(state, planner, pending=None):
    bridge = RecordingBridge(pending or PAY_COSTS_PENDING)
    engine = make_engine(bridge=bridge, planner=planner)
    handled = engine._try_interactive_safe_default(state, "decision_required")
    return handled, bridge


def test_reserved_payment_is_cancelled_at_submission_not_autotapped():
    state, planner = pay_costs_state()
    handled, bridge = submit(state, planner)
    assert handled is True
    assert bridge.cancelled == 1
    assert bridge.auto_taps == 0
    # The reservation guard decides before any pay/decline model call.
    assert not planner.plan_pay_or_decline.called


def test_spare_mana_still_pays_at_submission():
    state, planner = pay_costs_state()
    state["battlefield"].append({**state["battlefield"][0], "instance_id": 307})
    handled, bridge = submit(state, planner)
    assert handled is True
    assert bridge.auto_taps == 1
    assert bridge.cancelled == 0


def test_unrelated_payment_still_pays_at_submission():
    state, planner = pay_costs_state()
    planner._memory().record(
        state,
        PendingDecision((1, 2), "ActionsAvailable", (), can_pass=True),
        ["pass"],
        "Pass; nothing worth holding mana for.",
    )
    handled, bridge = submit(state, planner)
    assert handled is True
    assert bridge.auto_taps == 1
    assert bridge.cancelled == 0


def test_decline_without_cancel_permission_never_pays():
    state, planner = pay_costs_state()
    pending = {**PAY_COSTS_PENDING, "can_cancel": False}
    handled, bridge = submit(state, planner, pending)
    assert handled is False
    assert bridge.auto_taps == 0
    assert bridge.cancelled == 0


def dispatch_state(state):
    state.update(
        {
            "_bridge_request_type": "PayCosts",
            "_bridge_request_class": "PayCostsRequest",
            "_bridge_connected": True,
            "_bridge_has_pending": True,
            "_bridge_game_state_id": 1,
            "_bridge_last_poll": {**PAY_COSTS_PENDING, "game_state_id": 1, "msg_id": 10, "source_id": 500},
        }
    )
    return state


def test_main_dispatch_cancels_reserved_payment():
    """The production dispatch path shares the same guard as the safe-default net."""
    state, planner = pay_costs_state()
    state = dispatch_state(state)
    bridge = RecordingBridge({**PAY_COSTS_PENDING, "game_state_id": 1, "msg_id": 10, "source_id": 500})
    engine = make_engine(bridge=bridge, planner=planner)
    assert engine.process_trigger(state, "decision_required") is True
    assert bridge.cancelled == 1
    assert bridge.auto_taps == 0


def test_main_dispatch_pays_when_the_mana_is_spare():
    state, planner = pay_costs_state()
    state["battlefield"].append({**state["battlefield"][0], "instance_id": 307})
    state = dispatch_state(state)
    bridge = RecordingBridge({**PAY_COSTS_PENDING, "game_state_id": 1, "msg_id": 10, "source_id": 500})
    engine = make_engine(bridge=bridge, planner=planner)
    assert engine.process_trigger(state, "decision_required") is True
    assert bridge.auto_taps == 1
    assert bridge.cancelled == 0
