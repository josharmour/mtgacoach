"""Offline relaunch regressions: no running Arena/Coach or real profile access."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotEngine
from arenamcp.autopilot_models import AutopilotConfig, AutopilotState
from arenamcp.bridge_readiness import automation_blocker, incomplete_hand_blocker, native_arena_identity
from arenamcp.desktop.plan_text import plan_step_text, plan_steps_text
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.gre_bridge import BridgeDecisionPoller, GREBridge, GREBridgeError


def native_bridge(monkeypatch):
    bridge = GREBridge()
    bridge._connected = bridge._handshake_complete = True
    bridge.client_runtime = "il2cpp-macos"
    bridge._native_pid = 123
    bridge._native_identity = "Arena instance A"
    monkeypatch.setattr("arenamcp.bridge_readiness.native_arena_identity", lambda pid: "Arena instance A")
    return bridge


def test_pid_identity_checks_executable_and_birth_time(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout="123 Sat Oct 31 09:00:00 2026 /Games/MTGA.app/Contents/MacOS/MTGA\n"
        )
    )
    monkeypatch.setattr("arenamcp.bridge_readiness.subprocess.run", run)
    assert "09:00:00" in native_arena_identity(123)
    assert run.call_args.args[0] == ["/bin/ps", "-axo", "pid=,lstart=,comm="]
    run.return_value.stdout += "456 Sat Oct 31 09:00:01 2026 /Games/MTGA.app/Contents/MacOS/MTGA\n"
    assert native_arena_identity(123) is None  # two real Arena processes: do not guess
    run.return_value.stdout = "123 Sat Oct 31 09:00:00 2026 /bin/sh"
    assert native_arena_identity(123) is None
    assert native_arena_identity(True) is None


def test_replaced_process_invalidates_socket_adapter_and_cached_board(monkeypatch):
    bridge = native_bridge(monkeypatch)
    bridge._cached_game_state = {"turn": {"turn_number": 29}}
    bridge._mac_adapter = Mock()
    assert automation_blocker(bridge) is None
    monkeypatch.setattr("arenamcp.bridge_readiness.native_arena_identity", lambda pid: "Arena instance B")
    assert "cannot be verified" in automation_blocker(bridge)
    assert not bridge.connected
    assert bridge._cached_game_state is None
    assert bridge._mac_adapter is None
    assert not bridge._handshake_complete
    assert bridge.connection_generation == 1


def test_legacy_native_bridge_does_not_claim_automation_ready(monkeypatch):
    bridge = native_bridge(monkeypatch)
    bridge._native_pid = None
    assert "process-aware update" in automation_blocker(bridge)
    with pytest.raises(GREBridgeError, match="process-aware update"):
        bridge._send_command({"action": "submit_pass"})


def test_half_finished_handshake_is_not_connected():
    bridge = GREBridge()
    bridge._connected = True
    bridge._handshake_pending = True
    assert not bridge.connected
    assert not bridge.connect()
    assert automation_blocker(bridge)


def test_pending_and_prefetched_requests_do_not_cross_reconnection():
    bridge = SimpleNamespace(connected=True, connection_generation=1)
    poller = BridgeDecisionPoller(bridge)
    pending = {"has_pending": True, "request_type": "ActionsAvailable", "game_state_id": 29}
    poller._last_poll_result = pending
    poller._last_poll_at = time.monotonic()
    poller._last_poll_generation = 1
    poller._prefetched_generation = 1
    poller._prefetched = (time.monotonic(), pending)
    assert poller.latest_pending() == pending
    assert poller._fresh_prefetched() == pending
    bridge.connection_generation = 2
    assert poller.latest_pending() is None
    assert poller._fresh_prefetched() is None


def test_disconnect_cannot_return_a_cached_board(monkeypatch):
    bridge = native_bridge(monkeypatch)
    bridge._cached_game_state = {"ok": True, "old": True}
    bridge._cached_game_state_time = time.monotonic()
    bridge.disconnect()
    bridge._send_safe = Mock(side_effect=GREBridgeError("offline"))
    assert bridge.get_game_state() is None


@pytest.mark.parametrize("action", ["submit_pass", "submit_action", "queue_match", "cancel_action"])
def test_uncertain_submission_is_never_resent_after_disconnect(action):
    bridge = GREBridge()
    bridge._connected = True
    bridge._send_command = Mock(side_effect=GREBridgeError("response lost after write"))
    bridge.connect = Mock(return_value=True)
    with pytest.raises(GREBridgeError, match="response lost"):
        bridge._send_safe({"action": action})
    assert bridge._send_command.call_count == 1
    bridge.connect.assert_not_called()


def test_read_only_query_can_still_reconnect():
    bridge = GREBridge()
    bridge._connected = True
    bridge._send_command = Mock(side_effect=[GREBridgeError("offline"), {"ok": True}])
    bridge.connect = Mock(return_value=True)
    assert bridge._send_safe({"action": "get_pending_actions"}) == {"ok": True}
    bridge.connect.assert_called_once()


@pytest.mark.parametrize(
    "request_kind", ["ChooseStartingPlayer", "ChooseStartingPlayerReq", "ChooseStartingPlayerRequest"]
)
def test_play_draw_does_not_wait_for_the_hand_that_is_dealt_after_it(request_kind):
    assert incomplete_hand_blocker({"hand_complete": False, "_bridge_request_type": request_kind}) is None


@pytest.mark.parametrize("request_kind", ["MulliganReq", "ActionsAvailable", "DeclareAttackers"])
def test_hand_dependent_decisions_still_wait_for_complete_hand(request_kind):
    assert incomplete_hand_blocker({"hand_complete": False, "_bridge_request_type": request_kind})


def test_same_request_on_new_connection_wakes_listeners_again():
    request = {"ok": True, "has_pending": True, "request_type": "ActionsAvailable"}
    bridge = SimpleNamespace(connected=True, connection_generation=1, get_pending_actions=lambda: request)
    poller = BridgeDecisionPoller(bridge)
    heard = []
    poller.watch_requests(heard.append)
    assert poller._prefetch_once()
    assert not poller._prefetch_once()
    bridge.connection_generation = 2
    assert poller._prefetch_once()
    assert heard == [request, request]


def engine_with_bridge(bridge):
    engine = AutopilotEngine.__new__(AutopilotEngine)
    engine._config = AutopilotConfig()
    engine._abort_event = threading.Event()
    engine._gre_bridge = bridge
    engine._notify = Mock()
    engine._acquire_lock = Mock(return_value=True)
    engine._release_lock = Mock()
    engine._planner = Mock()
    return engine


@pytest.mark.parametrize(
    "state,connected", [({"_bridge_connected": True}, False), ({"hand_complete": False}, True)]
)
def test_planning_stops_before_model_for_offline_or_incomplete_state(state, connected):
    engine = engine_with_bridge(SimpleNamespace(connected=connected))
    assert engine.process_trigger(state, "new_turn")
    assert engine._state == AutopilotState.PAUSED
    engine._planner.plan_actions.assert_not_called()
    engine._notify.assert_called_once()


def test_execution_rechecks_process_after_plan(monkeypatch):
    bridge = native_bridge(monkeypatch)
    engine = engine_with_bridge(bridge)
    engine._try_gre_bridge = Mock()
    assert automation_blocker(bridge) is None  # planning was allowed
    monkeypatch.setattr("arenamcp.bridge_readiness.native_arena_identity", lambda pid: None)
    result = engine._execute_action(GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest"), {})
    assert not result.success
    engine._try_gre_bridge.assert_not_called()


def test_readiness_pause_recovers_after_refresh_but_never_overrides_stop():
    bridge = SimpleNamespace(connected=False)
    engine = engine_with_bridge(bridge)
    assert engine.process_trigger({}, "new_turn")
    assert not engine.refresh_automation_readiness({})
    bridge.connected = True
    assert not engine.refresh_automation_readiness({"match_id": "active", "hand_complete": False})
    engine._abort_event.set()
    assert not engine.refresh_automation_readiness({"hand_complete": True})
    engine._abort_event.clear()
    assert engine.refresh_automation_readiness({"match_id": "active", "hand_complete": True})
    assert engine.state == AutopilotState.IDLE
    engine._state = AutopilotState.PAUSED  # ordinary manual pause, no readiness reason
    assert not engine.refresh_automation_readiness({})


def test_valid_new_connection_still_cannot_execute_old_plan(monkeypatch):
    bridge = native_bridge(monkeypatch)
    engine = engine_with_bridge(bridge)
    engine._planning_bridge_generation = 1
    bridge.connection_generation = 2
    engine._try_gre_bridge = Mock()
    result = engine._execute_action(GameAction(action_type=ActionType.PLAY_LAND, card_name="Forest"), {})
    assert not result.success
    assert "old plan" in result.error
    engine._try_gre_bridge.assert_not_called()


def test_offline_queue_does_not_prepare_or_navigate(monkeypatch):
    from arenamcp.standalone_auto_queue import _AutoQueueMixin

    runtime = _AutoQueueMixin()
    runtime._auto_queue_enabled = True
    runtime._check_grind_availability = Mock(return_value=True)
    runtime._autopilot_control_status = Mock(return_value="AP:ON")
    runtime._suspend_auto_queue = Mock()
    runtime._prepare_auto_quest = Mock()
    runtime.ui = Mock()
    monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: SimpleNamespace(connected=False))
    assert not runtime._poll_auto_queue({"match_id": "completed"})
    runtime._prepare_auto_quest.assert_not_called()
    runtime.ui.status.assert_called_once()
    assert "disconnected" in runtime.ui.status.call_args.args[1]


def hand_message(ids, objects):
    return {
        "zones": [{"zoneId": 10, "type": "ZoneType_Hand", "ownerSeatId": 1, "objectInstanceIds": ids}],
        "gameObjects": [
            {"instanceId": i, "grpId": 1000 + i, "zoneId": 10, "ownerSeatId": 1} for i in objects
        ],
    }


def test_hand_is_unknown_until_membership_and_all_identities_arrive():
    state = GameState()
    state.set_local_seat_id(1)
    assert not state._hand_coverage_locked()["complete"]
    state.update_from_message(hand_message(list(range(1, 9)), [1]))
    assert state._hand_coverage_locked() == {"complete": False, "known_count": 1, "total_count": 8}
    diff = hand_message([], list(range(2, 9)))
    diff.pop("zones")
    state.update_from_message(diff)
    assert state._hand_coverage_locked() == {"complete": True, "known_count": 8, "total_count": 8}


def test_summarized_event_invalidates_membership_until_explicit_refresh():
    state = GameState()
    state.set_local_seat_id(1)
    state.update_from_message(hand_message([1], [1]))
    handler = create_game_state_handler(state)
    handler({"greToClientMessages": [{"type": "GREMessageType_GameStateMessage", "_summarized": True}]})
    assert not state._hand_coverage_locked()["complete"]
    state.update_from_message(
        {"gameObjects": [{"instanceId": 2, "grpId": 1002, "zoneId": 10, "ownerSeatId": 1}]}
    )
    assert not state._hand_coverage_locked()["complete"]
    state.update_from_message(hand_message([1, 2], []))
    assert state._hand_coverage_locked()["complete"]
    state.update_from_message(hand_message([], []))
    assert state._hand_coverage_locked() == {"complete": True, "known_count": 0, "total_count": 0}


def test_plan_is_readable_without_raw_dicts_or_unconfirmed_rationale():
    steps = [
        {"action_type": "play_land", "card_name": "Mountain", "rationale": "will win"},
        {"action_type": "cast_spell", "card_name": "Bolt", "target_names": ["Enemy"]},
        {"action_type": "pass_priority"},
    ]
    assert plan_steps_text(steps) == "Play Mountain → Cast Bolt → Enemy → Pass priority"
    assert plan_step_text(None) == ""
    assert plan_steps_text(["Play Forest", {}, 42]) == "Play Forest"


def test_ui_renders_structured_plan_and_incomplete_hand(qapp):
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    panel = CompactCoachPanel()
    try:
        panel._on_turn_plan_changed({"steps": [{"action_type": "play_land", "card_name": "Mountain"}]})
        text = panel.turn_plan_label.text()
        assert "Play Mountain" in text and "action_type" not in text
        html = panel._format_game_state_html({"hand_complete": False, "hand": [{"name": "Forest"}]})
        assert "1 known card" in html and "hand incomplete" in html
        assert "1 card in hand" not in html
        panel._on_turn_plan_changed({"steps": [{"action_type": "cast", "card_name": "<unknown>"}]})
        assert "&lt;unknown&gt;" in panel.turn_plan_label.text()
    finally:
        panel.close()


def test_navigation_disconnect_during_model_call_prevents_input():
    from tests.test_auto_queue import arm, navigator, proposal, step

    nav, controller, backend, _, _ = navigator()
    arm(nav)

    def response(*args, **kwargs):
        nav._bridge.connected = False
        return proposal()

    backend.complete_with_image.side_effect = response
    step(nav)
    controller.execute.assert_not_called()


def test_bridge_overlay_requires_complete_materialized_hand(monkeypatch):
    from arenamcp.server import _get_bridge_overlay

    def overlay(count, cards):
        bridge = SimpleNamespace(
            connected=True,
            get_game_state=lambda: {
                "ok": True,
                "zones": {"local_hand": {"total_count": count, "cards": cards}},
            },
        )
        monkeypatch.setattr("arenamcp.gre_bridge.get_bridge", lambda: bridge)
        return _get_bridge_overlay({}, [], {}, 1, 2)

    assert overlay(8, [])["hand_complete"] is False
    assert overlay(None, [])["hand_complete"] is False
    assert overlay(False, [])["hand_complete"] is False
    assert overlay(0, [])["hand_complete"] is True
