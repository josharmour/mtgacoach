"""Speech follows accepted actions rather than provisional plans."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_models import ClickResult
from test_autopilot_bridge_lock import _DummyBridge, _make_engine
from test_typed_decision_path import _engine, _planner_with, _TypedBridge


@pytest.mark.parametrize("outcome", ["success", "replaced", "failed", "window_closed", "stale"])
def test_plan_speech_waits_for_submission_and_uses_its_actual_action(monkeypatch, outcome):
    state = {
        "_bridge_connected": True,
        "_bridge_has_pending": True,
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_game_state_id": 10,
        "pending_decision": "Action Required",
        "turn": {"turn_number": 3, "active_player": 1},
        "players": [{"seat_id": 1, "is_local": True}],
        "legal_actions": ["Cast Original [OK]", "Cast Replacement [OK]"],
        "hand": [{"name": "Original"}, {"name": "Replacement"}],
    }
    bridge = _DummyBridge({"has_pending": True, "request_type": "ActionsAvailable"})
    engine, planner = _make_engine(monkeypatch, lambda: deepcopy(state), bridge)
    engine._config.dry_run = False
    engine._config.verify_after_action = False
    engine._MAX_CONTINUATION_DEPTH = 0
    planner.plan_actions = Mock(
        return_value=ActionPlan(
            actions=[
                GameAction(action_type=ActionType.CAST_SPELL, card_name="Original"),
            ]
        )
    )
    planner.note_executed = Mock()
    spoken = []
    engine._speak_fn = lambda text, blocking: spoken.append(text)
    monkeypatch.setattr(engine, "_try_typed_decision_path", lambda *args: None)
    monkeypatch.setattr(engine, "_get_game_state", lambda: deepcopy(state))
    monkeypatch.setattr(engine, "_get_legal_actions", lambda game: game["legal_actions"])

    def execute(action, game):
        assert spoken == []
        state["_bridge_game_state_id"] = 11
        if outcome == "replaced":
            return ClickResult(
                True, submitted_action=GameAction(action_type=ActionType.CAST_SPELL, card_name="Replacement")
            )
        if outcome == "failed":
            return ClickResult(False, error="not submitted")
        if outcome == "window_closed":
            return ClickResult(True, error="GRE bridge (no-op, window closed)")
        if outcome == "stale":
            return ClickResult(True, error="GRE bridge (stale-skip)")
        return ClickResult(True)

    monkeypatch.setattr(engine, "_execute_action", execute)
    engine.process_trigger(deepcopy(state), "decision_required")
    expected = {"success": "Original", "replaced": "Replacement"}.get(outcome)
    assert spoken == ([f"Casting {expected}."] if expected else [])
    if expected:
        assert planner.note_executed.call_args.args[0].card_name == expected


def test_background_strategy_does_not_announce_a_competing_action(monkeypatch):
    engine, _ = _make_engine(monkeypatch, lambda: {}, _DummyBridge())
    engine._game_plan_mgr = Mock()
    engine._game_plan_mgr.current.as_payload.return_value = {"path": "Attack with everyone"}
    engine._game_plan_mgr.coach_intro.return_value = "Attack with everyone"
    engine._ui_game_plan_fn = Mock()
    engine._speak_fn = Mock()
    engine._announce_game_plan()
    engine._ui_game_plan_fn.assert_called_once()
    engine._speak_fn.assert_not_called()


def test_stale_casting_options_do_not_answer_the_next_search(monkeypatch):
    bridge = _DummyBridge({"has_pending": True, "request_type": "Search", "request_class": "SearchRequest"})
    engine, _ = _make_engine(monkeypatch, lambda: {}, bridge)
    engine._config.dry_run = False
    monkeypatch.setattr(engine, "_try_gre_bridge", lambda *args: None)
    engine._pause_for_manual = Mock()
    action = GameAction(action_type=ActionType.CASTING_OPTIONS)
    for request in ("Search", "ActionsAvailable"):
        assert engine._is_planner_action_stale_vs_bridge(action, {"_bridge_request_type": request})
    assert not engine._is_planner_action_stale_vs_bridge(
        action, {"_bridge_request_class": "CastingTimeOptionsRequest"}
    )
    result = engine._execute_action(action, {"_bridge_request_type": "CastingTimeOptions"})
    assert result.success and "stale-skip" in result.error
    engine._pause_for_manual.assert_not_called()


def test_typed_cast_speaks_card_and_reason_without_protocol_label(monkeypatch):
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [{"actionType": "Cast", "instanceId": 7, "grpId": 77, "hasAutoTap": True}],
    }
    bridge = _TypedBridge(poll)
    bridge.submit_action_by_index = Mock(return_value=True)
    planner = _planner_with('{"option_ids":["idx:0"],"reasoning":"Add mana for next turn."}')
    engine = _engine(monkeypatch, bridge, planner)
    engine._speak_fn = Mock()
    state = {
        "turn": {"turn_number": 3},
        "players": [{"seat_id": 1, "is_local": True}],
        "_bridge_connected": True,
        "_bridge_request_type": "ActionsAvailable",
        "hand": [{"instance_id": 7, "grp_id": 77, "name": "Birds of Paradise"}],
    }
    monkeypatch.setattr("arenamcp.server.get_card_info", lambda grp: {"name": "Birds of Paradise"})
    assert engine._try_typed_decision_path(state, "decision_required") is True
    bridge.submit_action_by_index.assert_called_once()
    engine._speak_fn.assert_called_once_with(
        "Casting Birds of Paradise. Plan: adding mana for next turn.", False
    )
