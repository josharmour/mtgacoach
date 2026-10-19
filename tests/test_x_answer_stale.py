"""A stale X answer is skipped, not escalated.

2026-09-24 (real match, Green Sun's Zenith), turn 3: a second trigger
re-planned X from stale log state after Arena had moved to the library
search; the failed submit_x went MANUAL REQUIRED and marked the search window
given up, so the autopilot never searched. (X=0 on turn 1 is covered by
test_x_chooser_plumbing.)
"""

from unittest.mock import MagicMock

from tests.fakes import make_engine

from arenamcp.action_planner import ActionType, GameAction


def test_stale_x_answer_is_skipped_not_escalated():
    pending = {"has_pending": True, "request_class": "SearchRequest", "request_type": "Search"}
    bridge = MagicMock()
    bridge.connected = True
    bridge.connect.return_value = True
    bridge.get_pending_actions.return_value = pending
    bridge.submit_x.return_value = False
    bridge.submit_numeric.return_value = False
    engine = make_engine(bridge, MagicMock())
    state = {
        "pending_decision": "Search Library",
        "_bridge_request_class": "SearchRequest",
        "_bridge_request_type": "Search",
        "_bridge_connected": True,
    }
    result = engine._execute_action(GameAction(action_type=ActionType.NUMERIC_INPUT, numeric_value=1), state)
    assert result.success
    assert "stale-skip" in result.error
    # The search that replaced the X prompt is still the autopilot's to answer.
    assert not engine.is_window_given_up(state)
