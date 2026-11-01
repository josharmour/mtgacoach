"""Offline regressions for ledger #325/#326/#327; never connect to Arena."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_decision_watchdog import Clock, stalled_search_coach
from tests.test_decision_watchdog import server_game_state as offline_server_game_state  # noqa: F401
from tests.test_standalone_coaching_loop import make_loop_coach, make_state, run_loop

from arenamcp.autopilot_models import AutopilotState
from arenamcp.gre_bridge import BridgeDecisionPoller, GREBridge
from arenamcp.mac_bridge_adapter import AdapterError, MacBridgeAdapter

pytestmark = pytest.mark.usefixtures("offline_server_game_state")


@pytest.mark.parametrize("frequency", ["start_of_turn", "smart", "every_priority"])
def test_block_trigger_survives_stale_payment_label(monkeypatch, frequency):
    state = make_state(pending="Pay Costs", decision_type="pay_costs")
    state["local_seat_id"] = 1
    state["turn"].update(active_player=2, priority_player=2, phase="Phase_Combat", step="DeclareBlock")
    coach = make_loop_coach(state, ["combat_blockers"])
    coach.advice_frequency = frequency
    coach._bridge_poller.connected = True
    coach._bridge_poller.latest_pending = lambda: None
    coach._autopilot_enabled = True
    engine = SimpleNamespace(
        requires_desktop_poll=False,
        is_window_given_up=lambda s: False,
        process_trigger=Mock(return_value=True),
    )
    coach._autopilot = engine
    run_loop(monkeypatch, coach, 1)
    engine.process_trigger.assert_called_once()
    assert engine.process_trigger.call_args.args[1] == "combat_blockers"


def test_block_trigger_does_not_cross_turn_ownership(monkeypatch):
    state = make_state()
    state["local_seat_id"] = 1
    coach = make_loop_coach(state, ["combat_blockers"])
    coach._autopilot_enabled = True
    coach._autopilot = SimpleNamespace(
        requires_desktop_poll=False,
        is_window_given_up=lambda s: False,
        process_trigger=Mock(return_value=True),
    )
    run_loop(monkeypatch, coach, 1)
    coach._autopilot.process_trigger.assert_not_called()


@pytest.mark.parametrize("trigger", ["stack_spell_yours", "stack_spell_opponent"])
@pytest.mark.parametrize("autoplay", [False, True])
def test_quiet_stack_priority_reaches_autopilot_only(monkeypatch, trigger, autoplay):
    state = make_state()
    state["stack"] = [{"name": "Trigger", "controller_seat_id": 2}]
    coach = make_loop_coach(state, [trigger])
    coach._autopilot_enabled = autoplay
    coach._autopilot = SimpleNamespace(
        requires_desktop_poll=False,
        is_window_given_up=lambda s: False,
        process_trigger=Mock(return_value=True),
    )
    run_loop(monkeypatch, coach, 1)
    assert coach._autopilot.process_trigger.call_count == int(autoplay)
    assert not coach._coach.calls


def frozen_adapter():
    send = Mock(
        return_value={
            "ok": False,
            "error": "main thread did not answer in time (ticks=36202); outcome unknown",
        }
    )
    return MacBridgeAdapter(send), send


def test_identical_failed_frame_polls_latch_and_bound_all_reflection():
    adapter, send = frozen_adapter()
    for _ in range(3):
        assert not adapter.handle({"action": "get_pending_actions"})["ok"]
    assert "36202" in adapter.client_frozen_reason
    calls = send.call_count
    for action in ["get_pending_actions", "get_game_state", "submit_pass", "queue_match"] * 3:
        assert adapter.handle({"action": action})["client_frozen"]
    assert send.call_count == calls == 3
    # Socket pings are not proof that Unity has recovered.
    adapter._cmd_ping = lambda cmd, timeout: {"ok": True}
    assert adapter.handle({"action": "ping"})["ok"]
    assert adapter.client_frozen_reason
    # New handshake constructs a new adapter; no stale latch crosses clients.
    assert MacBridgeAdapter(send).client_frozen_reason is None


@pytest.mark.parametrize("middle", ["progress", "success", "unrelated"])
def test_three_consecutive_identical_ticks_required(middle):
    adapter, _ = frozen_adapter()
    adapter.handle({"action": "get_pending_actions"})
    old = adapter._cmd_get_pending_actions
    if middle == "success":
        adapter._cmd_get_pending_actions = lambda c, t: {"ok": True, "has_pending": False}
    else:

        def fail(c, t):
            raise AdapterError(
                "main thread did not answer in time (ticks=36203)"
                if middle == "progress"
                else "missing request"
            )

        adapter._cmd_get_pending_actions = fail
    adapter.handle({"action": "get_pending_actions"})
    adapter._cmd_get_pending_actions = old
    adapter.handle({"action": "get_pending_actions"})
    adapter.handle({"action": "get_pending_actions"})
    assert adapter.client_frozen_reason is None


def test_frozen_client_blocks_existing_automation_readiness_and_alerts_once():
    adapter, _ = frozen_adapter()
    for _ in range(3):
        adapter.handle({"action": "get_pending_actions"})
    bridge = GREBridge()
    bridge._connected = bridge._handshake_complete = True
    bridge._mac_adapter = adapter
    bridge.client_runtime = "il2cpp-macos"
    assert bridge.automation_blocker() == adapter.client_frozen_reason
    coach = stalled_search_coach(Clock(), 30)
    coach._bridge_poller = BridgeDecisionPoller(bridge)
    assert not coach._bridge_poller.connected
    coach._bridge_poller.latest_pending = Mock(side_effect=AssertionError("must not poll frozen Arena"))
    for _ in range(3):
        assert coach._tick_decision_watchdog(make_state()) == "paused"
    assert coach._autopilot._state == AutopilotState.PAUSED
    assert len(coach.speak_advice_calls) == 1
    assert coach.speech_categories == ["essential"]
    assert not coach._autopilot.safe_defaults
