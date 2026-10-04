import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from arenamcp import server
from arenamcp.gamestate import GameState
from arenamcp.pipe_adapter import PipeAdapter
from arenamcp.standalone_auto_queue import _AutoQueueMixin


class Runtime(_AutoQueueMixin):
    def __init__(self):
        self.settings = Mock()
        self.ui = Mock()
        self._coach = SimpleNamespace(_backend=Mock())
        self._mcp = Mock()
        self._auto_queue_enabled = True
        self._auto_queue_since = time.time() - 1
        self.ap_status = "AP:ON"

    def _autopilot_control_status(self):
        return self.ap_status


@pytest.fixture
def navigator(monkeypatch):
    nav = Mock()
    nav.paused_reason = ""
    nav.process_tick.return_value = False
    factory = Mock(return_value=nav)
    monkeypatch.setattr("arenamcp.auto_queue.AutoQueueNavigator", factory)
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    return nav, factory


def test_only_a_match_observed_active_can_arm_queue(navigator, monkeypatch):
    nav, _ = navigator
    runtime = Runtime()
    # A completed event replayed at startup must not start an initial match.
    event = {"match_id": "old", "completed_at": time.time(), "match_complete": True}
    monkeypatch.setattr(server, "_completed_match_for_navigation", event)
    runtime._poll_auto_queue({"match_id": "old", "turn": {"turn_number": 15}})
    nav.note_match_end.assert_not_called()
    # The next game is observed alive; its eventual completion can arm.
    runtime._poll_auto_queue({"match_id": "new", "turn": {"turn_number": 1}})
    event.update(match_id="new", completed_at=time.time())
    runtime._poll_auto_queue({"match_id": None})
    nav.note_match_end.assert_called_once_with(event, confirmed_match_end=True)
    runtime._poll_auto_queue({"match_id": None})
    assert nav.note_match_end.call_count == 1


@pytest.mark.parametrize("status", ["AP:OFF", "AP:PAUSED"])
def test_queue_never_navigates_when_autoplay_not_driving(navigator, status):
    nav, factory = navigator
    runtime = Runtime()
    runtime.ap_status = status
    assert not runtime._poll_auto_queue({})
    factory.assert_not_called()
    nav.process_tick.assert_not_called()


def test_disable_cancels_pending_navigation_and_protocol_uses_boolean(navigator):
    nav, _ = navigator
    runtime = Runtime()
    runtime._auto_queue_navigator = nav
    runtime.set_auto_queue(False)
    nav.set_enabled.assert_called_with(False)
    runtime.settings.set.assert_called_with("auto_queue_enabled", False)
    adapter = PipeAdapter.__new__(PipeAdapter)
    adapter._coach = Mock()
    adapter._dispatch({"cmd": "set_auto_queue", "enabled": True})
    adapter._coach.set_auto_queue.assert_called_with(True)
    adapter._dispatch({"cmd": "set_auto_queue", "enabled": False})
    adapter._coach.set_auto_queue.assert_called_with(False)


def test_explicit_toggle_preserves_trusted_completion_for_retry(monkeypatch):
    from arenamcp.auto_queue import AutoQueueNavigator

    runtime = Runtime()
    nav = AutoQueueNavigator(backend=Mock(), get_game_state=Mock())
    nav.process_tick = Mock(return_value=True)  # No background desktop worker.
    runtime._auto_queue_navigator = nav
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    runtime._poll_auto_queue({"match_id": "one", "turn": {"turn_number": 10}})
    event = {"match_id": "one", "completed_at": time.time(), "match_complete": True}
    monkeypatch.setattr(server, "_completed_match_for_navigation", event)
    runtime._poll_auto_queue({"match_id": "one"})
    assert nav.active
    nav._pause("Could not verify the next Arena navigation step")
    runtime.set_auto_queue(False)
    runtime.set_auto_queue(True)
    assert event["completed_at"] < runtime._auto_queue_since
    runtime._poll_auto_queue({"match_id": "one"})
    assert nav.active and not nav.paused_reason
    assert nav.get_debug_info()["stage"] == "resuming"


@pytest.mark.parametrize(
    "scope,result,completed",
    [
        ("MatchScope_Game", "ResultType_Win", False),
        ("MatchScope_Match", "ResultType_None", False),
        ("MatchScope_Match", "ResultType_Draw", True),
    ],
)
def test_match_scope_completion_excludes_sideboarding_and_unresolved_results(
    monkeypatch, scope, result, completed
):
    state = GameState()
    state.match_id = "one"
    state.last_game_result = "win"  # New completion must still be noticed after a game result.
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: None)
    server._handle_match_created(
        {
            "matchId": "one",
            "finalMatchResult": {
                "resultList": [
                    {"scope": scope, "result": result},
                ]
            },
        }
    )
    assert bool(server.get_completed_match_for_navigation()) is completed
