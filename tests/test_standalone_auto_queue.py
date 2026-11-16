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
        self._autopilot = SimpleNamespace(_gre_bridge=SimpleNamespace(connected=True))

    def _autopilot_control_status(self):
        return self.ap_status


@pytest.fixture
def navigator(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_log_path", lambda: tmp_path / "Player.log")
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
    event.update(match_id="new", completed_at=time.time(), outcome="win")
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
    event = {"match_id": "one", "completed_at": time.time() - 0.05, "match_complete": True, "outcome": "win"}
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


@pytest.mark.parametrize("retry_scheduled", [True, False])
def test_mac_stays_awake_through_a_pause_that_retries_by_itself(navigator, retry_scheduled):
    # An unattended Mac that idle-sleeps during the 30 s cooldown never retries.
    nav, _ = navigator
    nav.paused_reason = "Could not verify the next Arena navigation step"
    nav.retry_scheduled = retry_scheduled
    runtime = Runtime()
    runtime._poll_auto_queue({"match_id": None})
    inhibitor = runtime._auto_queue_sleep_inhibitor
    assert inhibitor.start.called is retry_scheduled
    assert inhibitor.stop.called is not retry_scheduled


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


@pytest.mark.parametrize("breaker", ["win", "draw"])
def test_loss_stop_before_fourth_entry_and_fresh_explicit_run(navigator, monkeypatch, breaker):
    nav, _ = navigator
    run = Runtime()
    persisted = {}
    run.settings.get.side_effect = persisted.get
    run.settings.set.side_effect = lambda key, value, **kw: persisted.update({key: value})
    event = {}
    monkeypatch.setattr(server, "_completed_match_for_navigation", event)
    for n, outcome in enumerate(["loss", "loss", breaker, "loss", "loss", "loss"]):
        mid = f"match-{n}"
        run._poll_auto_queue({"match_id": mid, "turn": {"turn_number": 1}})
        event.update(match_id=mid, completed_at=time.time(), match_complete=True, outcome=outcome)
        if n == 5:
            fresh = Runtime()
            fresh.settings = run.settings
            fresh._auto_queue_restart_pending = True
            fresh._auto_queue_resume = {"ended_match_id": mid}
            run = fresh
            nav.reset_mock()
        for _ in range(2):
            run._poll_auto_queue({})
        assert run._auto_queue_enabled is (n < 5)
    nav.process_tick.assert_not_called()
    nav.note_match_end.assert_not_called()
    nav.restore_after_reload.assert_not_called()
    assert persisted["auto_queue_enabled"] is False
    run.ui.status.assert_any_call("AUTO_QUEUE", "OFF")
    assert "3 consecutive match losses" in run.ui.status.call_args.args[1]
    run.set_auto_queue(True)
    assert persisted["auto_queue_enabled"] is True
    assert persisted["auto_queue_streak"].get("losses", 0) == 0


def test_quick_loss_and_deferred_final_outcome(navigator, monkeypatch):
    nav, _ = navigator
    run = Runtime()
    event = {"match_id": "fast", "arena_completed_at": time.time(), "match_complete": True}
    monkeypatch.setattr(server, "_completed_match_for_navigation", event)
    run._poll_auto_queue({})
    nav.process_tick.assert_not_called()
    event["outcome"] = "loss"
    run._poll_auto_queue({})
    assert run._queue_streak()["losses"] == 1
    event.update(match_id="old", arena_completed_at=run._auto_queue_since - 30)
    run._poll_auto_queue({})
    assert run._queue_streak()["losses"] == 1


@pytest.mark.parametrize("from_checkpoint", [True, False])
def test_restart_recovers_unobserved_third_loss_before_any_navigation(
    navigator, monkeypatch, from_checkpoint, tmp_path
):
    import json

    nav, _ = navigator
    runtime = Runtime()
    since = time.time() - 30
    runtime._auto_queue_restart_pending = True
    runtime.settings.get.return_value = {
        "since": since,
        "losses": 2,
        "last": "previous",
        "through": since + 2,
    }
    if from_checkpoint:
        runtime._auto_queue_resume = {"ended_match_id": "previous", "saved_at": since + 3}
    room = {
        "stateType": "MatchGameRoomStateType_MatchCompleted",
        "gameRoomConfig": {
            "matchId": "third",
            "reservedPlayers": [{"userId": "local", "systemSeatId": 2, "teamId": 2}],
        },
        "finalMatchResult": {
            "resultList": [
                {"scope": "MatchScope_Game", "winningTeamId": 2},
                {"scope": "MatchScope_Match", "winningTeamId": 1},
            ]
        },
    }
    line = json.dumps(
        {"timestamp": int((since + 10) * 1000), "matchGameRoomStateChangedEvent": {"gameRoomInfo": room}}
    )
    path = tmp_path / "Player.log"
    # Even after long menu/game traffic or an Arena restart, the loss still counts.
    source = path.with_name("Player-prev.log") if from_checkpoint else path
    source.write_bytes(line.encode() + b"\n" + b"menu log\n" * (server._RESTART_SCAN_BYTES // 9 + 1))
    monkeypatch.setattr(server, "_log_path", lambda: str(path) if from_checkpoint else path)
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: "local")
    runtime._poll_auto_queue({})
    assert not runtime._auto_queue_enabled
    nav.restore_after_reload.assert_not_called()
    nav.note_match_end.assert_not_called()
    nav.process_tick.assert_not_called()


def test_pending_missing_result_retries_without_losing_identity(navigator, monkeypatch):
    nav, _ = navigator
    runtime = Runtime()
    runtime._auto_queue_streak = {"since": time.time() - 300, "losses": 2, "pending": "third"}
    lookup = Mock(side_effect=[[], [{"outcome": "loss", "arena_completed_at": time.time()}]])
    monkeypatch.setattr(server, "logged_queue_results", lookup)
    for _ in range(2):
        runtime._poll_auto_queue({"match_id": "new", "turn": {"turn_number": 1}})
        assert runtime._auto_queue_streak["pending"] == "third"
    assert lookup.call_count == 1
    runtime._queue_retry_at = 0
    runtime._poll_auto_queue({})
    assert not runtime._auto_queue_enabled
    nav.process_tick.assert_not_called()


def test_retrying_queue_recovers_losses_including_games_played_while_off(navigator, monkeypatch):
    nav, _ = navigator
    run = Runtime()
    store = {"auto_queue_streak": {"since": 50, "last": "one", "through": 60, "losses": 1}}
    run.settings.get.side_effect = store.get
    run.settings.set.side_effect = lambda k, v, **kw: store.update({k: v})
    run.set_auto_queue(False)
    recorded = [
        {"match_id": name, "arena_completed_at": stamp, "outcome": "loss"}
        for name, stamp in (("one", 60), ("two", 70), ("three", 80))
    ]
    monkeypatch.setattr(
        server,
        "logged_queue_results",
        lambda since, **kw: [r for r in recorded if r["arena_completed_at"] > since],
    )
    run.set_auto_queue(True)
    run._poll_auto_queue({})
    assert not run._auto_queue_enabled
    assert store["auto_queue_streak"]["losses"] == 3
    nav.process_tick.assert_not_called()
    run.set_auto_queue(True)
    assert run._queue_loss_guard({}, {}) and run._queue_streak().get("losses", 0) == 0


def test_prior_build_undercount_reconciles_against_native_results(navigator, monkeypatch):
    nav, _ = navigator
    run = Runtime()
    run.settings.get.side_effect = {
        "auto_queue_streak": {"since": 75, "last": "four", "through": 80, "losses": 1}
    }.get
    rows = [
        {"match_id": n, "arena_completed_at": t, "outcome": "loss"}
        for n, t in (("two", 65), ("three", 70), ("four", 80))
    ]
    monkeypatch.setattr(server, "logged_queue_results", lambda since, **kw: rows)
    run._poll_auto_queue({})
    assert run._queue_streak()["losses"] == 3
    assert not run._auto_queue_enabled
    nav.process_tick.assert_not_called()


def test_later_confirmed_win_clears_losses_before_manual_resume(navigator, monkeypatch):
    run = Runtime()
    run.settings.get.side_effect = {
        "auto_queue_streak": {"since": 20, "last": "two", "through": 30, "losses": 2}
    }.get
    rows = [
        {"match_id": n, "arena_completed_at": t, "outcome": o}
        for n, t, o in (("three", 40, "loss"), ("won", 50, "win"), ("next", 60, "loss"))
    ]
    monkeypatch.setattr(server, "logged_queue_results", lambda since, **kw: rows)
    assert run._queue_loss_guard({}, {})
    assert run._queue_streak()["losses"] == 1


def test_delayed_old_pending_win_does_not_erase_newer_losses(navigator, monkeypatch):
    run = Runtime()
    run.settings.get.side_effect = {
        "auto_queue_streak": {"since": 5, "last": "before", "through": 10, "pending": "old"}
    }.get
    rows = [
        {"match_id": n, "arena_completed_at": t, "outcome": o}
        for n, t, o in (("won", 30, "win"), ("loss1", 40, "loss"), ("loss2", 50, "loss"))
    ]
    old = {"match_id": "old", "arena_completed_at": 20, "outcome": "win"}
    monkeypatch.setattr(
        server, "logged_queue_results", lambda since, match_id=None, **kw: [old] if match_id else rows
    )
    assert run._queue_loss_guard({}, {})
    assert run._queue_streak()["losses"] == 2 and "pending" not in run._queue_streak()
    assert not run._queue_loss_guard({}, {"match_id": "third", "arena_completed_at": 60, "outcome": "loss"})


def test_unresolved_loss_before_old_retry_checkpoint_holds_navigation(navigator, monkeypatch):
    run = Runtime()
    run.settings.get.side_effect = {
        "auto_queue_streak": {"since": 75, "last": "C", "through": 80, "losses": 1}
    }.get
    rows = [
        {"match_id": n, "arena_completed_at": t, "outcome": o}
        for n, t, o in (("A", 60, "loss"), ("B", 70, None), ("C", 80, "loss"))
    ]
    resolved = []

    def read(since, match_id=None, **kw):
        assert since == 0
        return resolved if match_id else list(rows)

    monkeypatch.setattr(server, "logged_queue_results", read)
    assert not run._queue_loss_guard({}, {})
    assert run._auto_queue_enabled
    run._queue_history_retry_at = 0
    resolved.append(dict(rows[1], outcome="loss"))
    assert not run._queue_loss_guard({}, {})
    assert not run._auto_queue_enabled
