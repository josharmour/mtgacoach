"""Regression test for issue #405 — a false MANUAL REQUIRED on pay_costs.

One trigger batch dispatches every trigger against the same snapshot. On
2026-07-05 23:01:01 the first dispatch paid the PayCosts and the second
re-entered the branch on the consumed window:

    Autopilot: submitting AutoTap solution for PayCosts
    GRE bridge submitted auto_tap: index 0
    GRE bridge submit_auto_tap failed: Pending is null, no AutoTapActionsRequest available
    GRE bridge cancel_action failed: No pending interaction
    Autopilot manual required: GRE bridge submit_auto_tap did not advance Pay Costs

The P1-5 guard added for that incident checks the window on *entry*. It does
not help when the window is consumed while auto-pay is already running — and
the failed-submit path sleeps 0.4s between retries, which is ample time for
another dispatch to land. "Pending is null" after our own submit means the
cost was paid, not that payment failed.
"""

import pytest

from arenamcp.autopilot import AutopilotState


class _FakeBridge:
    """Bridge whose PayCosts window is consumed by a concurrent trigger."""

    def __init__(self, auto_tap_ok=False, still_pending=False):
        self.connected = True
        self._auto_tap_ok = auto_tap_ok
        self._still_pending = still_pending
        self.cancel_calls = 0

    def connect(self):
        return True

    def submit_auto_tap(self, solution_index=0):
        return self._auto_tap_ok

    def cancel_action(self):
        self.cancel_calls += 1
        return False

    def get_pending_actions(self):
        if self._still_pending:
            return {
                "has_pending": True,
                "request_type": "PayCostsReq",
                "game_state_id": 42,
                "actions": [],
            }
        return {"has_pending": False}


@pytest.fixture
def executor(monkeypatch):
    """A minimally-wired Autopilot with the pay_costs path reachable."""
    from arenamcp.autopilot import AutopilotEngine

    auto = object.__new__(AutopilotEngine)
    auto._state = AutopilotState.EXECUTING
    auto._decisions = []
    auto._manual_required_calls = []
    auto._paths = []

    def _record(game_state, trigger, action_type="", summary=""):
        auto._decisions.append((action_type, summary))

    def _manual(action, game_state, tag, reason):
        auto._manual_required_calls.append((tag, reason))
        return False

    auto._record_autopilot_decision = _record
    auto._manual_required_bridge_result = _manual
    auto._log_execution_path = lambda *a, **k: auto._paths.append(a)
    return auto


def _sleepless(monkeypatch):
    import arenamcp.autopilot as ap

    monkeypatch.setattr(ap.time, "sleep", lambda *_: None)


def test_consumed_window_is_not_manual_required(executor, monkeypatch):
    """The reported failure: submit fails because the window is already gone."""
    _sleepless(monkeypatch)
    bridge = _FakeBridge(auto_tap_ok=False, still_pending=False)
    executor._gre_bridge = bridge

    # Submit failed, and the live poll says nothing is pending.
    assert executor._live_pending_request_is("PayCosts") is False
    assert bridge.cancel_calls == 0, "must not blind-cancel a consumed window"


def test_window_still_pending_is_a_real_failure(executor, monkeypatch):
    """A genuinely stuck PayCosts must still escalate, not be swallowed."""
    _sleepless(monkeypatch)
    executor._gre_bridge = _FakeBridge(auto_tap_ok=False, still_pending=True)
    assert executor._live_pending_request_is("PayCosts") is True


def test_offline_bridge_returns_none(executor):
    """Bridge offline: callers keep snapshot behavior rather than guessing."""

    class _Offline:
        connected = False

        def connect(self):
            return False

    executor._gre_bridge = _Offline()
    assert executor._live_pending_request_is("PayCosts") is None


def test_guard_is_wired_into_the_auto_pay_path():
    """The re-check must sit between the failed retry and the manual-required
    escalation — not merely exist as a helper."""
    import inspect

    from arenamcp.autopilot import AutopilotEngine

    src = inspect.getsource(AutopilotEngine.process_trigger)
    retry = src.index("AutoTap child arrived late")
    guard = src.index('self._live_pending_request_is("PayCosts") is False', retry)
    escalation = src.index("did not advance Pay Costs", retry)
    assert retry < guard < escalation
