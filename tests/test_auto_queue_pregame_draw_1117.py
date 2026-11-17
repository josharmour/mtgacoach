"""Regression tests for pre-game Turn-0 Draw stalls under Auto-queue."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp import server
from arenamcp.auto_queue import AutoQueueNavigator
from arenamcp.standalone_auto_queue import _AutoQueueMixin


def _make_runtime(monkeypatch, tmp_path, completed_ref):
    monkeypatch.setattr(server, "_log_path", lambda: tmp_path / "Player.log")
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: dict(completed_ref))
    monkeypatch.setattr(
        "arenamcp.auto_queue.AutoQueueNavigator.process_tick",
        lambda self, st: not self._new_match(st) and self.active,
    )

    class Runtime(_AutoQueueMixin):
        def __init__(self):
            self.settings = Mock()
            self.settings.get.return_value = None
            self.ui = Mock()
            self._coach = SimpleNamespace(_backend=Mock())
            self._mcp = Mock()
            self._auto_queue_enabled = True
            self._auto_queue_since = time.time() - 5.0
            self._autopilot = SimpleNamespace(_gre_bridge=SimpleNamespace(connected=True))

        def _autopilot_control_status(self):
            return "AP:ON"

    return Runtime()


def test_queued_pregame_draw_arms_navigation_instead_of_handoff(monkeypatch, tmp_path):
    """Exact 2026-11-17 07:16 incident: Match A ended, queued for Match B, Match B aborted at Turn 0 Draw."""
    now = time.time()
    completed = {
        "match_id": "8c19489a-old",
        "completed_at": now - 40.0,
        "arena_completed_at": now - 40.0,
        "match_complete": True,
        "outcome": "win",
    }
    runtime = _make_runtime(monkeypatch, tmp_path, completed)
    nav = AutoQueueNavigator(
        backend=Mock(),
        get_game_state=lambda: {
            "match_id": "54e8b7b1-draw",
            "turn": {"turn_number": 0},
            "last_game_result": "draw",
        },
        bridge=SimpleNamespace(connected=True, client_runtime="il2cpp-macos"),
    )
    nav.set_enabled(True)
    assert nav.note_match_end(
        {"match_id": "8c19489a-old", "completed_at": now - 40.0}, confirmed_match_end=True
    )
    nav._queue_started = True
    nav._stage = "queue"
    runtime._auto_queue_navigator = nav
    runtime._auto_queue_observed_match = "8c19489a-old"
    runtime._auto_queue_last_noted = "8c19489a-old"

    completed.update(
        {
            "match_id": "54e8b7b1-draw",
            "completed_at": now,
            "arena_completed_at": now,
            "match_complete": True,
            "outcome": "draw",
        }
    )
    state = {"match_id": "54e8b7b1-draw", "turn": {"turn_number": 0}, "last_game_result": "draw"}
    owns = runtime._poll_auto_queue(state)
    assert owns is True
    assert nav.active is True
    assert nav._ended_match_id == "54e8b7b1-draw"
    assert nav._stage == "results"
    assert not nav._new_match(state)


def test_turn0_playing_then_delayed_turn0_draw_rearms_auto_queue(monkeypatch, tmp_path):
    """Turn-0 room enters Playing -> hands off -> 60s later aborts Turn 0 Draw -> re-arms navigator."""
    now = time.time()
    completed = {
        "match_id": "8c19489a-old",
        "completed_at": now - 100.0,
        "arena_completed_at": now - 100.0,
        "match_complete": True,
        "outcome": "win",
    }
    state = {"match_id": "54e8b7b1-room", "turn": {"turn_number": 0}, "last_game_result": None}
    runtime = _make_runtime(monkeypatch, tmp_path, completed)
    nav = AutoQueueNavigator(
        backend=Mock(),
        get_game_state=lambda: state,
        bridge=SimpleNamespace(connected=True, client_runtime="il2cpp-macos"),
    )
    nav.set_enabled(True)
    nav.note_match_end({"match_id": "8c19489a-old", "completed_at": now - 100.0}, confirmed_match_end=True)
    nav._queue_started = True
    nav._stage = "queue"
    runtime._auto_queue_navigator = nav
    runtime._auto_queue_observed_match = "8c19489a-old"
    runtime._auto_queue_last_noted = "8c19489a-old"

    # First poll: live Turn-0 room (Playing) hands off to autoplay after recording observed_match.
    assert runtime._poll_auto_queue(state) is False
    nav._handoff()
    assert not nav.active
    assert runtime._auto_queue_observed_match == "54e8b7b1-room"

    # Second poll 60s later: MatchCompleted(Draw) at Turn 0.
    state.update({"last_game_result": "draw"})
    completed.update(
        {
            "match_id": "54e8b7b1-room",
            "completed_at": time.time(),
            "arena_completed_at": time.time(),
            "match_complete": True,
            "outcome": "draw",
        }
    )
    owns = runtime._poll_auto_queue(state)
    assert owns is True
    assert nav.active is True
    assert nav._ended_match_id == "54e8b7b1-room"
    assert nav._stage == "results"


def test_contrary_startup_replayed_draw_does_not_arm_auto_queue(monkeypatch, tmp_path):
    """Startup log replay of an already-completed Turn-0 draw must NOT arm navigation."""
    now = time.time()
    completed = {
        "match_id": "stale-replayed",
        "completed_at": now,
        "arena_completed_at": now,
        "match_complete": True,
        "outcome": "draw",
    }
    runtime = _make_runtime(monkeypatch, tmp_path, completed)
    nav = AutoQueueNavigator(
        backend=Mock(),
        get_game_state=lambda: {
            "match_id": "stale-replayed",
            "turn": {"turn_number": 0},
            "last_game_result": "draw",
        },
        bridge=SimpleNamespace(connected=True, client_runtime="il2cpp-macos"),
    )
    nav.set_enabled(True)
    runtime._auto_queue_navigator = nav

    owns = runtime._poll_auto_queue(
        {"match_id": "stale-replayed", "turn": {"turn_number": 0}, "last_game_result": "draw"}
    )
    assert owns is False
    assert not nav.active


def test_contrary_live_turn0_room_still_hands_off_to_autoplay(monkeypatch):
    """#347 live Turn-0 room (last_game_result=None, not completed) still returns _new_match=True."""
    monkeypatch.setattr(server, "get_completed_match_for_navigation", lambda: {})
    state = {"match_id": "live-m2", "turn": {"turn_number": 0}, "last_game_result": None}
    nav = AutoQueueNavigator(
        backend=Mock(),
        get_game_state=lambda: state,
        bridge=SimpleNamespace(connected=True, client_runtime="il2cpp-macos"),
    )
    nav.set_enabled(True)
    nav.note_match_end({"match_id": "ended-m1", "completed_at": time.time() - 10}, confirmed_match_end=True)
    assert nav._new_match(state) is True
    assert nav.process_tick(state) is False
    assert nav.active is False
