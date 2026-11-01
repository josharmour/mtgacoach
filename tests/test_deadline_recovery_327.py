"""Deadline and missing-request regressions for #327 / #206."""

import time
from copy import deepcopy

import pytest
from tests.fakes import make_engine
from tests.test_decision_watchdog import SEARCH, Clock, stalled_search_coach
from tests.test_priority_after_cast_206 import _blind_bridge, _blind_state

from arenamcp.decision_watchdog import DecisionWatchdog, request_key


def test_first_observation_with_little_time_uses_safe_default():
    watchdog = DecisionWatchdog(monotonic=Clock())
    assert watchdog.may_act(SEARCH, clock_remaining_s=5)
    step = watchdog.step(SEARCH, clock_remaining_s=5)
    assert step.action == "safe_default"
    assert step.age_s == 0


def test_no_request_still_cannot_act_despite_expired_clock():
    watchdog = DecisionWatchdog(monotonic=Clock())
    assert not watchdog.may_act(None, clock_remaining_s=0)
    assert watchdog.step(None, clock_remaining_s=0) is None


def test_synthetic_priority_deadline_survives_unrelated_state_id_updates():
    engine = make_engine(bridge=_blind_bridge())
    clock = Clock()
    watchdog = DecisionWatchdog(monotonic=clock)
    state = _blind_state(match_id="match-a", game_number=1)
    actions = []
    for age in range(31):
        clock.now = 1000 + age
        state["_bridge_game_state_id"] = 400 + age
        result = watchdog.step(engine.log_priority_request(state))
        if result:
            actions.append((age, result.action))
    assert actions == [(12, "replan"), (30, "safe_default")]


@pytest.mark.parametrize("field,value", [("match_id", "match-b"), ("game_number", 2)])
def test_synthetic_priority_identity_does_not_cross_games(field, value):
    engine = make_engine(bridge=_blind_bridge())
    state = _blind_state(match_id="match-a", game_number=1)
    first = request_key(engine.log_priority_request(state))
    state[field] = value
    assert request_key(engine.log_priority_request(state)) != first


def test_synthetic_priority_identity_changes_with_phase():
    engine = make_engine(bridge=_blind_bridge())
    state = _blind_state(match_id="match-a", game_number=1)
    first = request_key(engine.log_priority_request(state))
    state = deepcopy(state)
    state["turn"]["phase"] = "Phase_Combat"
    assert request_key(engine.log_priority_request(state)) != first


def test_watchdog_uses_fresh_urgent_clock_on_first_loop_tick():
    coach = stalled_search_coach(Clock(), 30)
    state = {
        "local_seat_id": 1,
        "timer_state": {
            1: {
                "is_ticking": True,
                "time_remaining_ms": 5000,
                "received_at": time.time(),
            }
        },
    }
    assert coach._tick_decision_watchdog(state) == "submitted"
    assert len(coach._autopilot.safe_defaults) == 1


def test_new_request_does_not_inherit_old_requests_stale_urgent_clock():
    coach = stalled_search_coach(Clock(), 30)
    coach._decision_watchdog.step(SEARCH, wall_now=time.time() - 200)
    coach._bridge_poller.resp = {**SEARCH, "msg_id": 999}
    state = {
        "local_seat_id": 1,
        "timer_state": {
            1: {
                "is_ticking": True,
                "time_remaining_ms": 5000,
                "received_at": time.time() - 100,
            }
        },
    }
    assert coach._tick_decision_watchdog(state) is None
    assert not coach._autopilot.safe_defaults


def test_ended_match_cannot_synthesize_a_request_from_leftover_stack():
    engine = make_engine(bridge=_blind_bridge())
    assert engine.log_priority_request(_blind_state(match_ended=True)) is None
