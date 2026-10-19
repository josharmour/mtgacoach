"""Auto-queue waits out a model outage and requeues after a coach restart (#32, restart gap).

2026-10-19 (standalone.log): at 09:34:55-09:35:03 the model server was down
(vLLM hung, circuit open) and three skipped navigation calls counted as
navigation failures: "Could not verify the next Arena navigation step", a
pause that lasted until the coach was restarted at 09:53:29. After the
restarts at 09:53:35 and 10:06:23 Arena sat on Home after a match, auto-queue
said "Waiting for this match to finish", and a human clicked Play both times.
"""

from __future__ import annotations

import json
import time
from unittest.mock import Mock

import pytest
from tests.mac_fakes import make_frame
from tests.test_auto_queue import arm, proposal, step
from tests.test_standalone_auto_queue import Runtime

from arenamcp import auto_queue, server
from arenamcp.auto_queue import AutoQueueNavigator

CIRCUIT_OPEN = "[BACKEND ERROR] model server unavailable (circuit open; retry in 4s)"


class _Backend:
    """A screenshot model behind the shared breaker: ``up`` is what available() reports."""

    def __init__(self, response=None, *, up=True, probe=(True, "ok")):
        self.up = up
        self.probe = probe
        self.probes = 0
        self.complete_with_image = Mock(return_value=response or proposal("open_play", "home"))

    def available(self):
        return self.up

    def probe_inference(self, timeout=8.0):
        self.probes += 1
        return self.probe


def _navigator(backend):
    controller = Mock()
    controller.capture.side_effect = make_frame
    controller.execute.return_value = True
    statuses: list[str] = []
    nav = AutoQueueNavigator(
        backend=backend,
        controller=controller,
        get_game_state=lambda: {"match_id": "ended"},
        status_fn=statuses.append,
        refine_target=lambda frame, data, action: action,
    )
    return nav, controller, statuses


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("arenamcp.auto_queue.time.monotonic", lambda: now[0])
    return now


def test_a_skipped_call_while_the_circuit_is_open_waits_instead_of_failing(clock):
    """09:34:55 "Auto-queue step failed: Navigation model request failed" x3 -> pause."""
    backend = _Backend(CIRCUIT_OPEN)
    nav, controller, statuses = _navigator(backend)
    arm(nav)
    for _ in range(5):
        step(nav)
        clock[0] += auto_queue._MODEL_POLL_S
    assert not nav.paused_reason
    assert nav.get_debug_info()["failures"] == 0
    assert statuses[-1].startswith("Waiting for the model server to come back")
    controller.execute.assert_not_called()


def test_no_screenshot_while_the_breaker_reports_the_server_down_and_resume_when_back(clock):
    backend = _Backend(proposal("open_play", "home", label="Play"), up=False)
    nav, controller, statuses = _navigator(backend)
    arm(nav)
    for _ in range(36):  # three minutes: a vLLM restart takes ~2m45s
        step(nav)
        clock[0] += auto_queue._MODEL_POLL_S
    controller.capture.assert_not_called()
    backend.complete_with_image.assert_not_called()
    assert not nav.paused_reason and nav.active

    backend.up = True  # the circuit closed (its own probe succeeded)
    step(nav)
    controller.execute.assert_called_once()  # navigation continued by itself
    assert nav.get_debug_info()["last_progress"].startswith("clicked open_play")


def test_a_failed_call_the_breaker_does_not_know_about_is_checked_with_a_probe(clock):
    """Image calls never count toward the breaker; one text probe tells an outage from a bad call."""
    backend = _Backend("[BACKEND ERROR] vision analysis failed: Request timed out.", probe=(False, "down"))
    nav, controller, statuses = _navigator(backend)
    arm(nav)
    for _ in range(4):
        step(nav)
        clock[0] += auto_queue._MODEL_POLL_S
    assert not nav.paused_reason and nav.get_debug_info()["failures"] == 0
    assert backend.complete_with_image.call_count == 1, "while waiting only the probe asks the server"
    assert backend.probes == 4

    backend.probe = (True, "ok")
    backend.complete_with_image.return_value = proposal("open_play", "home")
    step(nav)
    controller.execute.assert_called_once()


def test_a_failed_call_from_a_server_that_answers_is_still_a_navigation_failure(clock):
    backend = _Backend("[BACKEND ERROR] vision analysis failed: bad image", probe=(True, "ok"))
    nav, _, _ = _navigator(backend)
    arm(nav)
    for _ in range(3):
        step(nav)
    assert nav.paused_reason == "Could not verify the next Arena navigation step"


def test_the_model_wait_is_bounded(clock):
    backend = _Backend(CIRCUIT_OPEN, up=False)
    nav, _, statuses = _navigator(backend)
    arm(nav)
    step(nav)
    clock[0] += auto_queue._MODEL_WAIT_LIMIT_S
    step(nav)
    assert nav.paused_reason.startswith("The model server stayed unavailable for 15 min")
    assert statuses[-1].endswith("Toggle auto queue off/on to retry this navigation.")


def test_a_model_wait_is_not_a_navigation_stall_for_the_watchdog(clock, caplog):
    backend = _Backend(CIRCUIT_OPEN, up=False)
    nav, _, _ = _navigator(backend)
    arm(nav)
    step(nav)
    clock[0] += 120
    with caplog.at_level("WARNING", logger="arenamcp.auto_queue"):
        nav._watchdog(clock[0])
    assert "no navigation progress" not in caplog.text


# --- requeue after a coach restart


@pytest.fixture
def restart_navigator(monkeypatch):
    nav = Mock()
    nav.paused_reason = ""
    nav.process_tick.return_value = True
    nav.note_match_end.return_value = True
    monkeypatch.setattr("arenamcp.auto_queue.AutoQueueNavigator", Mock(return_value=nav))
    monkeypatch.setattr("arenamcp.idle_sleep.SleepInhibitor", Mock())
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    return nav


IDLE = {
    "match_id": "6175f002",
    "event_id": "Ladder",
    "deck_name": "Mono-Red Burn",
    "deck_id": "1543b787",
    "completed_at": time.time() - 60,
    "match_complete": True,
}


def _restarted(monkeypatch, completed):
    runtime = Runtime()
    runtime._auto_queue_restart_pending = True
    scans = []
    monkeypatch.setattr(server, "idle_completed_match_from_log", lambda: scans.append(1) or completed)
    return runtime, scans


@pytest.mark.parametrize(
    "state",
    [{"match_id": None}, {"match_id": "6175f002", "turn": {"turn_number": 0}}],
    ids=["no-match", "restored-finished-match"],
)
def test_a_restart_with_arena_idle_after_a_match_requeues_it(restart_navigator, monkeypatch, state):
    runtime, scans = _restarted(monkeypatch, dict(IDLE))
    assert runtime._poll_auto_queue(state)
    restart_navigator.note_match_end.assert_called_once_with(IDLE, confirmed_match_end=True)
    runtime._poll_auto_queue(state)
    assert scans == [1], "checked once per coach start"
    assert restart_navigator.note_match_end.call_count == 1


def test_a_restart_during_a_match_waits_for_its_end(restart_navigator, monkeypatch):
    runtime, scans = _restarted(monkeypatch, dict(IDLE))
    runtime._poll_auto_queue({"match_id": "live", "turn": {"turn_number": 4}})
    restart_navigator.note_match_end.assert_not_called()
    assert scans == []


@pytest.mark.parametrize("completed", [None, {**IDLE, "match_id": "older"}], ids=["not-idle", "other-match"])
def test_a_restart_without_an_idle_finished_match_does_not_queue(restart_navigator, monkeypatch, completed):
    runtime, _ = _restarted(monkeypatch, completed)
    runtime._poll_auto_queue({"match_id": "6175f002", "turn": {"turn_number": 0}})
    restart_navigator.note_match_end.assert_not_called()


def test_turning_auto_queue_on_never_starts_a_match(restart_navigator, monkeypatch):
    runtime, scans = _restarted(monkeypatch, dict(IDLE))
    runtime.set_auto_queue(True)
    runtime._poll_auto_queue({"match_id": None})
    restart_navigator.note_match_end.assert_not_called()
    assert scans == []


def test_no_restart_requeue_without_autoplay_driving(restart_navigator, monkeypatch):
    runtime, scans = _restarted(monkeypatch, dict(IDLE))
    runtime.ap_status = "AP:OFF"
    runtime._poll_auto_queue({"match_id": None})
    assert scans == []
    runtime.ap_status = "AP:ON"  # autoplay turned on later in the session
    runtime._poll_auto_queue({"match_id": None})
    restart_navigator.note_match_end.assert_called_once()


# --- Player.log: is Arena idle after the last match?


def _room(match_id: str, state: str, ts_ms: int, event_id: str = "Ladder") -> str:
    return json.dumps(
        {
            "transactionId": "t",
            "timestamp": str(ts_ms),
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {
                        "reservedPlayers": [{"userId": "me", "systemSeatId": 1, "eventId": event_id}],
                        "matchId": match_id,
                    },
                    "stateType": state,
                }
            },
        }
    )


def _set_deck(event: str, deck: str) -> str:
    request = json.dumps({"EventName": event, "Summary": {"DeckId": "1543b787", "Name": deck}})
    return "[UnityCrossThreadLogger]==> EventSetDeckV3 " + json.dumps({"id": "r", "request": request})


def _log(tmp_path, monkeypatch, *, completed_ago=60.0, event="Ladder", after=()):
    now_ms = int((time.time() - completed_ago) * 1000)
    lines = [
        _set_deck(event, "Mono-Red Burn"),
        "[UnityCrossThreadLogger]==> EventEnterPairing {}",
        "[UnityCrossThreadLogger]10/19/2026 10:00:00 AM: Match to me: MatchGameRoomStateChangedEvent",
        _room("6175f002", "MatchGameRoomStateType_Playing", now_ms - 300_000, event),
        '{ "greToClientEvent": { "greToClientMessages": [] } }',
        "[UnityCrossThreadLogger]10/19/2026 10:05:59 AM: Match to me: MatchGameRoomStateChangedEvent",
        _room("6175f002", "MatchGameRoomStateType_MatchCompleted", now_ms, event),
        "[UnityCrossThreadLogger]==> StartHook {}",
        '{"Courses":[{"InternalEventName":"QuickDraft_FDN"}]}',
        "[UnityCrossThreadLogger]==> EventGetActiveMatches {}",
        *after,
    ]
    path = tmp_path / "Player.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(server, "_log_path", lambda: path)


def test_the_log_shows_arena_idle_after_the_last_match(tmp_path, monkeypatch):
    _log(tmp_path, monkeypatch)
    found = server.idle_completed_match_from_log()
    assert found["match_id"] == "6175f002" and found["match_complete"] is True
    assert found["event_id"] == "Ladder" and found["deck_name"] == "Mono-Red Burn"
    assert found["deck_id"] == "1543b787"
    assert time.time() - found["completed_at"] == pytest.approx(60, abs=5)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"after": ['[UnityCrossThreadLogger]==> EventJoin {"id":"j"}']},  # already queued (10:06:51)
        {"after": [_set_deck("Ladder", "Mono-Red Burn")]},
        {"after": [_room("0f6e8634", "MatchGameRoomStateType_Playing", 0)]},  # the next match
        {"after": ['{ "greToClientEvent": {} }']},
        {"after": ['[UnityCrossThreadLogger]==> BotDraftDraftPick {"id":"d"}']},
        {"completed_ago": 3600.0},  # a session that ended an hour ago is not restarted
        {"event": "QuickDraft_FDN_20261001"},  # draft matches belong to the draft driver
    ],
    ids=["queued", "deck-set", "next-match", "gameplay", "draft-pick", "too-old", "draft-event"],
)
def test_anything_after_the_match_or_an_old_or_draft_match_is_not_idle(tmp_path, monkeypatch, kwargs):
    _log(tmp_path, monkeypatch, **kwargs)
    assert server.idle_completed_match_from_log() is None
