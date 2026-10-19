"""Turn-level clock: wrap up the turn when Arena's rope runs low or a timeout was charged (#66).

Evidence (2026-10-19, match 115944ce, our seat 2, turn 8): a cast/cancel loop
of Iroh's Demonstration ran from 11:12:55 to 11:14:17. Arena sent no
TimerStateMessage between 11:12:52 (ActivePlayer 75 s, 7 s elapsed) and its
TimeoutMessage at 11:14:00 (a banked timeout: 75 -> 105 s); at 11:14:30 its AI
took our seat (ControllerType_AI_Goldfish), cancelled the pending cast and
passed Main 1. The coach only logged the timeout. Every GameStateMessage of the
loop carried our ActivePlayer timer (timerId 9), running: 55.6 s elapsed at
11:13:40.689, 73.2 s at 11:13:58.309. Payloads below are trimmed from
Player.log (lines 168756-170638) to their timer, turn and player fields.
"""

from __future__ import annotations

import time

import pytest
from tests.test_self_cancel_memory_20261019 import actions_poll, board_state, make_iroh_engine

from arenamcp.decision_clock import seat_entry, seconds_remaining, wrap_up_reason
from arenamcp.gamestate import GameState, create_game_state_handler

ACTIVE = "TimerType_ActivePlayer"


@pytest.fixture
def iroh_engine(monkeypatch):
    return make_iroh_engine(monkeypatch)


def event(timestamp_ms, *messages):
    return {"timestamp": str(timestamp_ms), "greToClientEvent": {"greToClientMessages": list(messages)}}


def gsm(game_state_id, timers, turn_info=None, players=None):
    body = {"timers": timers}
    if turn_info:
        body["turnInfo"] = turn_info
    if players:
        body["players"] = players
    return {"type": "GREMessageType_GameStateMessage", "gameStateId": game_state_id, "gameStateMessage": body}


def rope(timer_id, duration, elapsed_ms=None, running=False, kind=ACTIVE):
    timer = {
        "timerId": timer_id,
        "type": kind,
        "durationSec": duration,
        "behavior": "TimerBehavior_TakeControl",
    }
    if elapsed_ms is not None:
        timer["elapsedSec"], timer["elapsedMs"] = elapsed_ms // 1000, elapsed_ms
    if running:
        timer["running"] = True
    return timer


INACTIVITY = {
    "timerId": 12,
    "type": "TimerType_Inactivity",
    "durationSec": 150,
    "running": True,
    "behavior": "TimerBehavior_Timeout",
    "elapsedMs": 1,
}
TURN_8 = {
    "phase": "Phase_Main1",
    "turnNumber": 8,
    "activePlayer": 2,
    "priorityPlayer": 2,
    "decisionPlayer": 2,
}
PLAYERS = [
    {"systemSeatNumber": 1, "timerIds": [1, 2, 3, 4, 5, 6]},
    {"systemSeatNumber": 2, "timerIds": [7, 8, 9, 10, 11, 12]},
]

# 11:12:36.961 turn 8 begins: Arena resets seat 1's rope; ours is reset later.
GSM_182 = event(
    1792433556961, gsm(182, [rope(3, 61), rope(10, 45, kind="TimerType_NonActivePlayer")], TURN_8, PLAYERS)
)
# 11:12:52.046 the last TimerStateMessage before the timeout.
TSM_190 = event(
    1792433572046,
    {
        "type": "GREMessageType_TimerStateMessage",
        "gameStateId": 190,
        "timerStateMessage": {
            "seatId": 2,
            "timers": [
                {
                    "timerId": 7,
                    "type": "TimerType_Prologue",
                    "durationSec": 120,
                    "behavior": "TimerBehavior_TakeControl",
                },
                {
                    "timerId": 8,
                    "type": "TimerType_Epilogue",
                    "durationSec": 145,
                    "behavior": "TimerBehavior_TakeControl",
                },
                rope(9, 75, 7017, running=True),
                rope(10, 45, kind="TimerType_NonActivePlayer"),
                {
                    "timerId": 11,
                    "type": "TimerType_Delay",
                    "durationSec": 2,
                    "behavior": "TimerBehavior_StartDelayedTimer",
                },
                {**INACTIVITY, "elapsedMs": 0},
            ],
        },
    },
)
# 11:13:04.567 the cancel's GameStateMessage: only seat 1's timers (ours runs on).
GSM_193 = event(1792433584567, gsm(193, [rope(3, 61), rope(4, 45, 2000, kind="TimerType_NonActivePlayer")]))
GSM_206 = event(1792433620689, gsm(206, [rope(9, 75, 55614, running=True), INACTIVITY]))
TIMEOUT_215 = event(
    1792433640099,
    {
        "type": "GREMessageType_TimeoutMessage",
        "gameStateId": 215,
        "timeoutMessage": {"seatId": 2, "timer": rope(9, 105, 75000, running=True)},
    },
)
GSM_216 = event(1792433640683, gsm(216, [rope(9, 105, 75581, running=True), INACTIVITY]))


def parsed(*events) -> GameState:
    state = GameState()
    handler = create_game_state_handler(state)
    for payload in events:
        handler(payload)
    return state


def at(timestamp_ms, plus=0.0):
    return timestamp_ms / 1000 + plus


def test_game_state_messages_keep_the_rope_current():
    state = parsed(GSM_182, TSM_190, GSM_206)
    entry = state.timer_state[2]
    assert entry["rope_s"][ACTIVE] == pytest.approx(75 - 55.614)
    assert entry["rope_running"][ACTIVE] is True
    assert entry["rope_turn"][ACTIVE] == 8
    # The watchdog sees it for a request that appeared after the last TimerStateMessage.
    assert seconds_remaining(
        state.timer_state, 2, now=at(1792433620689, 0.5), not_before=at(1792433618000)
    ) == pytest.approx(75 - 55.614 - 0.5)


def test_a_message_with_only_the_opponents_timers_still_refreshes_ours():
    state = parsed(GSM_182, TSM_190, GSM_193)
    entry = state.timer_state[2]
    assert entry["received_at"] == pytest.approx(at(1792433584567))
    # 68 s left at 11:12:52.046, running since: 55.5 s at 11:13:04.567.
    assert entry["rope_s"][ACTIVE] == pytest.approx(75 - 7.017 - 12.521)


def test_the_loop_wraps_up_twenty_seconds_before_the_rope_ends():
    state = parsed(GSM_182, TSM_190)
    assert wrap_up_reason(state.timer_state, 2, 8, 2, now=at(1792433572046, 30)) == ""  # 38 s left
    state = parsed(GSM_182, TSM_190, GSM_206)
    assert (
        wrap_up_reason(state.timer_state, 2, 8, 2, now=at(1792433620689, 0.5)) == "19s left on our turn clock"
    )
    assert wrap_up_reason(state.timer_state, 2, 8, 1, now=at(1792433620689, 0.5)) == ""  # their turn


def test_last_turns_rope_does_not_wrap_up_this_turn():
    # Turn 6 ended with 2 s on our rope; turn 8's first request comes before
    # Arena resets it (11:12:36.961 -> 11:12:38.961).
    turn_6 = event(1792433450000, gsm(120, [rope(9, 104, 102000)], {**TURN_8, "turnNumber": 6}, PLAYERS))
    state = parsed(turn_6, GSM_182)
    assert wrap_up_reason(state.timer_state, 2, 8, 2, now=at(1792433556961, 0.1)) == ""


def test_an_arena_timeout_wraps_up_the_rest_of_the_turn():
    state = parsed(GSM_182, TSM_190, GSM_206, TIMEOUT_215, GSM_216)
    entry = state.timer_state[2]
    assert entry["timed_out_turn"] == 8
    assert entry["rope_s"][ACTIVE] == pytest.approx(105 - 75.581)  # the banked 30 s
    reason = wrap_up_reason(state.timer_state, 2, 8, 2, now=at(1792433640683, 1))
    assert reason == "Arena's turn timer already ran out on us this turn"
    assert wrap_up_reason(state.timer_state, 2, 9, 1, now=at(1792433640683, 40)) == ""


# --- the autopilot: a priority window in wrap-up is passed, no model call -----------


def clock(remaining, *, running=True, turn=8, timed_out=False):
    now = time.time()
    entry = seat_entry([(rope(9, 75, int((75 - remaining) * 1000), running=running), now, turn)], now)
    if timed_out:
        entry["timed_out_turn"] = turn
    return {2: entry}


def test_the_autopilot_passes_instead_of_recasting_when_the_rope_is_low(iroh_engine):
    engine, bridge, planner = iroh_engine
    state = board_state()
    state["timer_state"] = clock(17)
    bridge.get_pending_actions.return_value = actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    planner.plan_decision_options.assert_not_called()
    bridge.submit_pass.assert_called_once()
    bridge.submit_action_by_index.assert_not_called()


def test_after_arenas_timeout_the_autopilot_passes_main_two_too(iroh_engine):
    # 11:15:02: after the restart the coach recast Iroh's in Main 2 of the
    # turn Arena had already charged a timeout for.
    engine, bridge, planner = iroh_engine
    state = board_state()
    state["turn"]["phase"] = "Phase_Main2"
    state["timer_state"] = clock(49, running=False, timed_out=True)
    bridge.get_pending_actions.return_value = actions_poll(239)
    assert engine._try_typed_decision_path(state, "decision_required")
    planner.plan_decision_options.assert_not_called()
    bridge.submit_pass.assert_called_once()


def test_plenty_of_rope_plans_as_usual(iroh_engine):
    engine, bridge, planner = iroh_engine
    state = board_state()
    state["timer_state"] = clock(40)
    bridge.get_pending_actions.return_value = actions_poll()
    assert engine._try_typed_decision_path(state, "decision_required")
    planner.plan_decision_options.assert_called_once()
    bridge.submit_action_by_index.assert_called_once()  # Cast Iroh's Demonstration
