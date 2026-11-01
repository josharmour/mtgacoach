"""Arena's rope bounds decision work, including formatting, hedges and repairs."""

import concurrent.futures
import time
from unittest.mock import Mock

import pytest
from tests.test_targeting_fallback_decline import _decision, _state

from arenamcp.action_planner import ActionPlan, ActionPlanner
from arenamcp.decision_clock import decision_seconds_remaining, seat_entry


def state_with_clock(remaining=9, *, active=1, age=0, turn=4, timer_turn=4, running=True):
    now = time.time()
    kind = "TimerType_ActivePlayer" if active == 1 else "TimerType_NonActivePlayer"
    entry = seat_entry(
        [
            (
                {
                    "type": kind,
                    "behavior": "TimerBehavior_TakeControl",
                    "durationSec": 60,
                    "elapsedSec": 60 - remaining,
                    "running": running,
                },
                now - age,
                timer_turn,
            )
        ],
        now - age,
    )
    state = _state(own_ids=(607,), their_ids=(812,))
    state.update(
        local_seat_id=1,
        match_id="m1",
        turn={"turn_number": turn, "active_player": active},
        timer_state={1: entry},
    )
    return state


@pytest.mark.parametrize("active", [1, 2])
def test_running_rope_survives_new_request_despite_old_reading(active):
    state = state_with_clock(25, active=active, age=10)
    assert decision_seconds_remaining(state, not_before=time.time() - 3) == pytest.approx(15, abs=0.1)


@pytest.mark.parametrize("change", ["previous_turn", "stopped", "ended", "opponents_clock"])
def test_clock_does_not_cross_turn_seat_or_match_end(change):
    state = state_with_clock(
        25, age=10, timer_turn=3 if change == "previous_turn" else 4, running=change != "stopped"
    )
    if change == "ended":
        state["match_ended"] = True
    if change == "opponents_clock":
        state["timer_state"] = {2: state["timer_state"][1]}
    assert decision_seconds_remaining(state, not_before=time.time() - 3) is None


def test_recent_report_still_cannot_reuse_known_previous_turn_rope():
    assert decision_seconds_remaining(state_with_clock(timer_turn=3), not_before=time.time() - 3) is None


@pytest.mark.parametrize("critical", [False, True])
def test_typed_model_calls_and_second_look_share_arena_budget(monkeypatch, critical):
    calls = []

    class Backend:
        def complete(self, *a, **kw):
            calls.append(kw["request_timeout_s"])
            return '{"option_ids": ["tgt:812"]}'

    planner = ActionPlanner(Backend(), timeout=15)
    monkeypatch.setattr(
        "arenamcp.action_planner.critical_decision", lambda *a, **k: "removal" if critical else ""
    )
    monkeypatch.setattr(planner, "_yield_background", lambda: True)

    def second(decision, state, data, chosen, concerns, ask, parse, message, answered_high):
        return parse(ask(message, effort="high", budget=15))

    monkeypatch.setattr(planner, "_second_look", second)
    assert planner._llm_decision_options(_decision([607, 812]), state_with_clock()) == ["tgt:812"]
    assert len(calls) >= 2
    assert all(0 < budget <= 4 for budget in calls)


def test_expired_clock_does_not_build_prompt_or_send_model(monkeypatch):
    backend = Mock()
    planner = ActionPlanner(backend)
    build = Mock(side_effect=AssertionError("must reserve time for fallback"))
    monkeypatch.setattr(planner, "_line_prompt_context", build)
    with pytest.raises(concurrent.futures.TimeoutError, match="Arena clock"):
        planner._llm_decision_options(_decision([607, 812]), state_with_clock(4))
    build.assert_not_called()
    backend.complete.assert_not_called()


def test_prompt_work_counts_against_timestamp_free_bridge_clock(monkeypatch):
    backend = Mock()
    planner = ActionPlanner(backend)
    state = state_with_clock(9)
    state["timer_state"][1].pop("received_at")
    mono = [100.0]
    monkeypatch.setattr("arenamcp.decision_clock.time.monotonic", lambda: mono[0])
    original = planner._line_prompt_context

    def build(*args):
        mono[0] += 6
        return original(*args)

    monkeypatch.setattr(planner, "_line_prompt_context", build)
    with pytest.raises(concurrent.futures.TimeoutError, match="Arena clock"):
        planner._llm_decision_options(_decision([607, 812]), state)
    backend.complete.assert_not_called()


@pytest.mark.parametrize("high_effort", [False, True])
def test_declaration_and_repair_receive_only_arena_budget(monkeypatch, high_effort):
    calls = []

    class Backend:
        def complete(self, *a, **kw):
            calls.append(kw["request_timeout_s"])
            return "invalid json"

    planner = ActionPlanner(Backend(), timeout=15, land_drop_first=False)
    monkeypatch.setattr(
        "arenamcp.action_planner.declaration_effort", lambda *a: ("high" if high_effort else None, "test")
    )
    monkeypatch.setattr(planner, "_parse_response", lambda *a, **k: ActionPlan())
    planner.plan_actions(
        state_with_clock(), "decision_required", ["Block with: Spider", "Done"], {"type": "declare_blockers"}
    )
    assert len(calls) >= 2
    assert all(0 < budget <= 4 for budget in calls)
