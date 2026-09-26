"""X answers: zero is a value, and a stale answer is skipped, not escalated.

2026-09-24 (real match, Green Sun's Zenith):
- turn 1, X=0: "Bridge couldn't handle numeric_input" three times — the
  handler read 0 as "no value".
- turn 3: a second trigger re-planned X from stale log state after Arena had
  moved to the library search; the failed submit_x went MANUAL REQUIRED and
  marked the search window given up, so the autopilot never searched.
"""

from unittest.mock import MagicMock

import arenamcp.autopilot as autopilot_module
from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot import AutopilotConfig, AutopilotEngine


class _DummyBridge:
    connected = False

    def connect(self):
        return False


def _engine(monkeypatch, pending: dict) -> tuple[AutopilotEngine, MagicMock]:
    monkeypatch.setattr(autopilot_module, "get_bridge", lambda: _DummyBridge())
    engine = AutopilotEngine(planner=MagicMock(), get_game_state=lambda: {}, config=AutopilotConfig(dry_run=False))
    bridge = MagicMock()
    bridge.connected = True
    bridge.connect.return_value = True
    bridge.get_pending_actions.return_value = pending
    asks_for_number = "CastingTimeOption" in str(pending.get("request_class"))
    bridge.submit_x.return_value = asks_for_number
    bridge.submit_numeric.return_value = False
    engine._gre_bridge = bridge
    return engine, bridge


def test_x_zero_is_submitted(monkeypatch):
    pending = {"has_pending": True, "request_class": "CastingTimeOptionRequest", "request_type": "CastingTimeOptions"}
    engine, bridge = _engine(monkeypatch, pending)
    state = {"_bridge_request_class": "CastingTimeOptionRequest", "_bridge_connected": True}
    result = engine._execute_action(GameAction(action_type=ActionType.NUMERIC_INPUT, numeric_value=0), state)
    assert result.success
    bridge.submit_x.assert_called_once_with(0)


def test_stale_x_answer_is_skipped_not_escalated(monkeypatch):
    pending = {"has_pending": True, "request_class": "SearchRequest", "request_type": "Search"}
    engine, _ = _engine(monkeypatch, pending)
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
