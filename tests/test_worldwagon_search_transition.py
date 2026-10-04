"""Worldwagon's accepted ETB must advance to Search without replaying Accept."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotState
from test_typed_decision_path import _engine, _planner_with, _TypedBridge

OPTIONAL = {
    "ok": True,
    "has_pending": True,
    "request_type": "OptionalAction",
    "request_class": "OptionalActionMessageRequest",
    "game_state_id": 73,
    "msg_id": 100,
    "source_instance_id": 464,
}
SEARCH = {
    "ok": True,
    "has_pending": True,
    "request_type": "Search",
    "request_class": "SearchRequest",
    "game_state_id": 74,
    "msg_id": 102,
    "select_n_min": 0,
    "select_n_max": 1,
    "search_candidates": [{"instanceId": 361, "name": "Forest", "type_line": "Basic Land — Forest"}],
    "decision_context": {"type": "search", "min": 0, "max": 1, "options": [361]},
}


class WorldwagonBridge(_TypedBridge):
    def submit_optional(self, accept, **kwargs):
        self.submitted.append(("optional", accept))
        return True


def optional_state():
    return {
        "match_id": "worldwagon-transition",
        "local_seat_id": 2,
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1}],
        "turn": {"turn_number": 4, "active_player": 2, "priority_player": 2, "phase": "Main1"},
        "pending_decision": "Optional Action",
        "decision_context": {"type": "optional_action"},
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "OptionalAction",
        "_bridge_request_class": "OptionalActionMessageRequest",
        "_log_game_state_id": 73,
        "legal_actions": ["Accept (yes)", "Decline (no)"],
        "hand": [],
        "stack": [],
        "battlefield": [],
    }


def test_accepted_worldwagon_etb_then_idle_tick_then_search_never_replans_accept(monkeypatch):
    planner = _planner_with('{"option_ids":["optional:accept"]}')
    planner.plan_actions = Mock(side_effect=AssertionError("Never re-plan the stale Accept menu"))
    planner.plan_decision_options = Mock(wraps=planner.plan_decision_options)
    bridge = WorldwagonBridge(deepcopy(OPTIONAL))
    engine = _engine(monkeypatch, bridge, planner)
    engine._game_plan_mgr = None
    engine._pause_for_manual = Mock()
    stale = optional_state()
    before = deepcopy(stale)
    assert engine.process_trigger(stale, "decision_required")
    assert bridge.submitted == [("optional", True)]
    bridge.poll_resp = {"ok": True, "has_pending": False}
    assert engine.process_trigger(stale, "stack_spell_yours")
    assert planner._backend.calls == 1
    assert engine._state is AutopilotState.IDLE
    bridge.poll_resp = deepcopy(SEARCH)
    planner._backend.response = '{"option_ids":["sel:361"]}'
    assert engine.process_trigger(stale, "decision_required")
    assert bridge.submitted == [("optional", True), ("selection", [361])]
    assert planner._backend.calls == 2
    planned_state = planner.plan_decision_options.call_args.args[1]
    assert planned_state["decision_context"]["type"] == "search"
    assert planned_state["pending_decision"] == "Search Library"
    assert stale == before
    assert engine._given_up_semantics is None
    engine._pause_for_manual.assert_not_called()
    planner.plan_actions.assert_not_called()


def test_vorinclex_search_uses_fresh_two_forest_options_after_cast_snapshot(monkeypatch):
    search = deepcopy(SEARCH)
    search.update(
        select_n_max=2,
        search_candidates=[
            {"instanceId": iid, "name": "Forest", "type_line": "Basic Land — Forest"} for iid in (480, 475)
        ],
        decision_context={"type": "search", "min": 0, "max": 2, "options": [480, 475]},
    )
    planner = _planner_with('{"option_ids":["sel:480","sel:475"]}')
    planner.plan_decision_options = Mock(wraps=planner.plan_decision_options)
    planner.plan_actions = Mock(side_effect=AssertionError("Search owns the new decision"))
    bridge = WorldwagonBridge(search)
    engine = _engine(monkeypatch, bridge, planner)
    engine._game_plan_mgr = None
    stale = optional_state()
    stale.update(
        pending_decision="Action Required",
        decision_context={"type": "actions_available"},
        _bridge_request_type="ActionsAvailable",
        _bridge_request_class="ActionsAvailableRequest",
    )
    assert engine.process_trigger(stale, "stack_spell_yours")
    assert bridge.submitted == [("selection", [480, 475])]
    decision, fresh = planner.plan_decision_options.call_args.args
    assert decision.max_select == 2
    assert fresh["decision_context"]["type"] == "search"
    assert fresh["decision_context"]["max"] == 2
    assert planner._backend.calls == 1
    planner.plan_actions.assert_not_called()


@pytest.mark.parametrize("poll", [{}, {"ok": False, "error": "transition"}, {"has_pending": False}])
def test_unavailable_connected_bridge_never_plans_an_old_optional_snapshot(monkeypatch, poll):
    planner = _planner_with("must not ask model")
    planner.plan_actions = Mock(side_effect=AssertionError("No legacy planning on unavailable live state"))
    engine = _engine(monkeypatch, WorldwagonBridge(poll), planner)
    assert engine.process_trigger(optional_state(), "stack_spell_yours")
    assert planner._backend.calls == 0
    planner.plan_actions.assert_not_called()


def test_connected_idle_arbiter_owns_stale_trigger_without_coaching_fallback(monkeypatch):
    monkeypatch.setattr("arenamcp.autopilot.time.sleep", lambda _: None)
    planner = _planner_with("must not ask model")
    planner.plan_actions = Mock(side_effect=AssertionError("Idle bridge must suppress stale coaching"))
    engine = _engine(monkeypatch, WorldwagonBridge({"has_pending": False}), planner)
    stale = optional_state()
    stale.update(
        _bridge_has_pending=False,
        _bridge_request_type=None,
        _bridge_request_class=None,
        pending_decision="Search Library",
        decision_context={"type": "search"},
    )
    assert engine.process_trigger(stale, "stack_spell_opponent")
    assert engine._state is AutopilotState.IDLE
    assert planner._backend.calls == 0
    planner.plan_actions.assert_not_called()


@pytest.mark.parametrize("button", ["accept", "yes", "decline", "no"])
def test_legacy_optional_button_displaced_by_search_silently_yields_without_submission(monkeypatch, button):
    bridge = WorldwagonBridge(deepcopy(SEARCH))
    engine = _engine(monkeypatch, bridge, _planner_with("must not ask model"))
    engine._pause_for_manual = Mock()
    action = GameAction(ActionType.CLICK_BUTTON, card_name=button)
    result = engine._execute_action(action, optional_state())
    assert result.success and "stale-skip" in result.error
    assert bridge.submitted == []
    assert engine._given_up_semantics is None
    engine._pause_for_manual.assert_not_called()


def test_still_pending_optional_accept_keeps_request_bound_submission(monkeypatch):
    bridge = WorldwagonBridge(deepcopy(OPTIONAL))
    bridge.submit_optional = Mock(wraps=bridge.submit_optional)
    engine = _engine(monkeypatch, bridge, _planner_with("must not ask model"))
    result = engine._execute_action(GameAction(ActionType.CLICK_BUTTON, card_name="accept"), optional_state())
    assert result.success
    bridge.submit_optional.assert_called_once_with(True, expected_request_id=(73, 100))
