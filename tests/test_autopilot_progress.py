"""Semantic loop protection measures submissions rather than log traffic."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_models import AutopilotState, ClickResult
from arenamcp.autopilot_progress import DecisionProgressGuard
from test_autopilot_bridge_lock import _DummyBridge, _make_engine
from test_typed_decision_path import _engine, _planner_with, _TypedBridge


def _poll(kind="SelectN", **updates):
    return {
        "has_pending": True,
        "request_type": kind,
        "game_state_id": 1,
        "msg_id": 10,
        "source_id": 9,
        "select_n_ids": [101, 102],
        "select_n_min": 1,
        "select_n_max": 1,
        **updates,
    }


def _clock_guard():
    clock = [0.0]
    return clock, DecisionProgressGuard(clock=lambda: clock[0])


def _three_attempts(guard, clock, poll, state=None):
    for i, now in enumerate((0.0, 3.0, 6.0)):
        clock[0] = now
        assert guard.observe({**poll, "game_state_id": i, "msg_id": i}, state or {}) is None
        guard.note_attempt("submit_selection", [101])


@pytest.mark.parametrize(
    "kind", ["SelectN", "SelectTargets", "NumericInput", "Order", "DeclareAttackers", "DeclareBlockers"]
)
def test_changing_transport_ids_does_not_hide_stuck_choices(kind):
    clock, guard = _clock_guard()
    _three_attempts(guard, clock, _poll(kind))
    clock[0] = 9
    proof = guard.observe(_poll(kind, game_state_id=999, msg_id=999), {})
    assert proof["attempts"] == 3 and proof["elapsed_s"] == 9
    assert guard.observe(_poll(kind), {}) is None  # report once
    assert guard.blocked


@pytest.mark.parametrize(
    "progress",
    [
        {"selected_targets": [101]},
        {"remaining": 0},
        {"select_n_min": 0},
        {"source_id": 10},
        {"select_n_ids": [101, 103]},
        {"numeric_value": 2},
        {"ordered_ids": [102, 101]},
        {"has_pending": False},
    ],
)
def test_selection_count_numeric_order_source_or_cancel_progress_resets(progress):
    clock, guard = _clock_guard()
    _three_attempts(guard, clock, _poll())
    clock[0] = 10
    assert guard.observe(_poll(**progress), {}) is None
    assert guard.snapshot()["attempts"] == 0


def test_new_match_resets_and_observation_alone_never_counts():
    clock, guard = _clock_guard()
    _three_attempts(guard, clock, _poll(), {"match_id": "one"})
    clock[0] = 10
    for i in range(50):
        assert guard.observe(_poll(msg_id=i), {"match_id": "two"}) is None
    assert guard.snapshot()["attempts"] == 0


def test_fast_submissions_wait_for_time_gate():
    clock, guard = _clock_guard()
    _three_attempts(guard, clock, _poll())
    clock[0] = 7.9
    assert guard.observe(_poll(), {}) is None
    clock[0] = 8.0
    assert guard.observe(_poll(), {})["attempts"] == 3


@pytest.mark.parametrize(
    "request_class", ["DeclareAttackerRequest", "CastingTimeOptionRequest", "OrderBlockersReq"]
)
def test_native_request_class_alias_uses_semantic_guard(request_class):
    clock, guard = _clock_guard()
    poll = {"has_pending": True, "request_class": request_class, "options": []}
    _three_attempts(guard, clock, poll)
    clock[0] = 9
    assert guard.observe(poll, {})["attempts"] == 3


def test_typed_path_pauses_and_captures_before_a_fourth_model_call(monkeypatch):
    bridge = _TypedBridge(_poll())
    planner = _planner_with('{"option_ids":["sel:101"]}')
    engine = _engine(monkeypatch, bridge, planner)
    clock = [0.0]
    monkeypatch.setattr("arenamcp.autopilot.time.monotonic", lambda: clock[0])
    engine._current_plan = ActionPlan(actions=[GameAction(action_type=ActionType.SELECT_N)])
    evidence = []

    def capture(reason, context):
        evidence.append((context, engine._current_plan))
        engine.force_stop()

    engine._stuck_report_fn = capture
    for i, now in enumerate((0.0, 3.0, 6.0, 9.0)):
        clock[0] = now
        bridge.poll_resp = _poll(game_state_id=i + 1, msg_id=i + 10)
        assert engine._try_typed_decision_path({}, "decision_required") is True
    assert bridge.submitted == [("selection", [101])] * 3
    assert planner._backend.calls == 3
    assert len(evidence) == 1 and evidence[0][1] is not None
    assert evidence[0][0]["semantic_progress"]["attempts"] == 3
    assert engine._abort_event.is_set() and engine.state == AutopilotState.PAUSED
    assert engine._gre_bridge is bridge


def test_legacy_selection_hook_blocks_further_submits_and_cancel_noop_does_not_reset(monkeypatch):
    bridge = _TypedBridge(_poll())
    bridge.cancel_action = Mock(return_value=True)
    engine = _engine(monkeypatch, bridge, _planner_with("{}"))
    clock = [0.0]
    monkeypatch.setattr("arenamcp.autopilot.time.monotonic", lambda: clock[0])
    engine._stuck_report_fn = Mock()
    action = GameAction(action_type=ActionType.SELECT_N)
    for i, now in enumerate((0.0, 3.0, 6.0)):
        clock[0] = now
        bridge.poll_resp = _poll(game_state_id=i, msg_id=i)
        assert engine._try_gre_bridge_select_n(action, {}).success
    clock[0] = 9
    engine._try_gre_bridge_select_n(action, {})
    assert len(bridge.submitted) == 3
    engine._stuck_report_fn.assert_called_once()
    assert engine.get_debug_info()["semantic_progress"]["blocked"]
    assert not engine._progress_bridge(poll=bridge.poll_resp).cancel_action()
    bridge.cancel_action.assert_not_called()


def test_failed_plan_never_announces_completion_or_executes_followup_done(monkeypatch):
    state = {
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_game_state_id": 10,
        "pending_decision": "Action Required",
        "turn": {"turn_number": 3, "active_player": 1},
        "players": [{"seat_id": 1, "is_local": True}],
        "legal_actions": ["Cast Original [OK]"],
        "hand": [{"name": "Original"}],
    }
    notes = []
    engine, planner = _make_engine(
        monkeypatch,
        lambda: deepcopy(state),
        _DummyBridge({"has_pending": True, "request_type": "ActionsAvailable", "game_state_id": 10}),
        notes,
    )
    engine._config.dry_run = False
    engine._config.verify_after_action = False
    planner.plan_actions = Mock(
        return_value=ActionPlan(
            actions=[
                GameAction(action_type=ActionType.CAST_SPELL, card_name="Original"),
                GameAction(action_type=ActionType.CLICK_BUTTON, card_name="Done"),
            ]
        )
    )
    monkeypatch.setattr(engine, "_try_typed_decision_path", lambda *args: None)
    monkeypatch.setattr(engine, "_get_game_state", lambda: deepcopy(state))
    monkeypatch.setattr(engine, "_get_legal_actions", lambda game: game["legal_actions"])

    def execute(*args):
        engine._state = AutopilotState.PAUSED
        return ClickResult(False, error="manual required")

    execution = Mock(side_effect=execute)
    monkeypatch.setattr(engine, "_execute_action", execution)
    assert engine.process_trigger(deepcopy(state), "decision_required") is False
    assert execution.call_count == planner.plan_actions.call_count == 1
    assert engine._plans_completed == 0 and engine.state == AutopilotState.PAUSED
    assert not any("Plan complete" in note for note in notes)


def test_manual_combat_failure_stays_paused_on_external_tick_with_new_transport_ids(monkeypatch):
    state = {
        "match_id": "one",
        "turn": {"turn_number": 3},
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "DeclareAttackers",
        "_bridge_game_state_id": 10,
        "pending_decision": "Declare Attackers",
        "decision_context": {"type": "declare_attackers", "legal_attacker_ids": [101, 102]},
    }
    bridge = _DummyBridge({"has_pending": True, "request_type": "DeclareAttackers"})
    engine, planner = _make_engine(monkeypatch, lambda: state, bridge)
    planner.plan_actions = Mock()
    engine._config.dry_run = False
    engine._pause_for_manual("Attack declaration could not be submitted", state)
    advanced_id = {**state, "_bridge_game_state_id": 999}
    assert engine.process_trigger(advanced_id, "decision_required") is False
    assert engine.state == AutopilotState.PAUSED
    planner.plan_actions.assert_not_called()
    # A manual selection is observable progress and releases the hold.
    progressed = deepcopy(advanced_id)
    progressed["decision_context"]["selected_attacker_ids"] = [101]
    assert not engine.is_window_given_up(progressed)


def test_acknowledged_cancel_without_a_changed_choice_still_counts_attempts(monkeypatch):
    bridge = _TypedBridge(_poll())
    bridge.cancel_action = Mock(return_value=True)
    engine = _engine(monkeypatch, bridge, _planner_with("{}"))
    engine._stuck_report_fn = Mock()
    clock = [0.0]
    monkeypatch.setattr("arenamcp.autopilot.time.monotonic", lambda: clock[0])
    for now in (0, 3, 6):
        clock[0] = now
        assert engine._progress_bridge(poll=bridge.poll_resp).cancel_action()
    clock[0] = 9
    assert not engine._progress_bridge(poll=bridge.poll_resp).cancel_action()
    assert bridge.cancel_action.call_count == 3
    engine._stuck_report_fn.assert_called_once()


def test_single_target_shortcut_is_guarded_without_model_calls(monkeypatch):
    poll = _poll("SelectTargets", target_candidates=[{"targetInstanceId": 101}])
    bridge = _TypedBridge(poll)
    engine = _engine(monkeypatch, bridge, _planner_with("{}"))
    engine._stuck_report_fn = Mock()
    monkeypatch.setattr(engine, "_pick_single_target_candidate", lambda state: 101)
    monkeypatch.setattr(engine, "_try_typed_decision_path", lambda *args: None)
    clock = [0.0]
    monkeypatch.setattr("arenamcp.autopilot.time.monotonic", lambda: clock[0])
    for index, now in enumerate((0, 3, 6, 9)):
        clock[0] = now
        state = {
            "turn": {"turn_number": 3},
            "_bridge_connected": True,
            "_bridge_has_pending": True,
            "_bridge_request_type": "SelectTargets",
            "pending_decision": "Select Targets",
            "_bridge_game_state_id": index,
            "_bridge_last_poll": {**poll, "game_state_id": index, "msg_id": index},
        }
        assert engine.process_trigger(state, "decision_required") is True
    assert len(bridge.submitted) == 3
    assert engine._planner._backend.calls == 0
    engine._stuck_report_fn.assert_called_once()
