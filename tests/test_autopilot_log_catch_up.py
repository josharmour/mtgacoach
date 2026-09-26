"""The typed-decision path must plan on a board at least as new as the bridge
request it answers (2026-09-24, Android: a Pass planned on the opponent's
turn-12 board was submitted to the new turn-13 Main 1 request, twice, and the
whole turn was skipped)."""

from unittest.mock import MagicMock

from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


def _poll(state_id: int) -> dict:
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": state_id,
        "can_pass": True,
        "can_cancel": True,
        "actions": [{"actionType": "Cast", "instanceId": 10, "hasAutoTap": True}],
    }


def _engine(boards: list[dict]):
    planner = MagicMock()
    planner.plan_decision_options.return_value = ["pass"]
    feed = iter(boards)
    engine = AutopilotEngine(
        planner=planner,
        config=AutopilotConfig(dry_run=False),
        get_game_state=lambda: next(feed, boards[-1]),
    )
    engine._gre_bridge = MagicMock()
    engine._gre_bridge.connected = True
    engine._LOG_CATCH_UP_TIMEOUT_S = 0.5
    return engine


def test_plans_on_the_caught_up_board_not_the_stale_one():
    stale = {"_log_game_state_id": 165, "turn": {"turn_number": 12}}
    fresh = {"_log_game_state_id": 166, "turn": {"turn_number": 13}}
    engine = _engine([stale, fresh])
    engine._gre_bridge.get_pending_actions.return_value = _poll(166)

    engine._try_typed_decision_path(stale, "decision_required")

    planned_on = engine._planner.plan_decision_options.call_args[0][1]
    assert planned_on["_log_game_state_id"] == 166


def test_log_that_never_catches_up_is_not_answered():
    stale = {"_log_game_state_id": 165}
    engine = _engine([stale])
    engine._gre_bridge.get_pending_actions.return_value = _poll(166)

    assert engine._try_typed_decision_path(stale, "decision_required") is True

    engine._planner.plan_decision_options.assert_not_called()
    engine._gre_bridge.submit_pass.assert_not_called()
    engine._gre_bridge.submit_action_by_index.assert_not_called()


def test_unknown_ids_keep_the_old_behaviour():
    engine = _engine([{}])
    assert engine._await_log_catch_up(_poll(0), {"x": 1}) == {"x": 1}
    assert engine._await_log_catch_up(_poll(7), {"x": 1}) == {"x": 1}  # log id unknown
    board = {"_log_game_state_id": 9}
    assert engine._await_log_catch_up(_poll(7), board) is board  # log already ahead
