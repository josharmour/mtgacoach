"""Critical decisions hedge: the high-effort call and the normal low-effort one run at once.

2026-10-18 14:31-14:33 (standalone.log): under shared-GPU load 4 of 5
critical decision.typed calls (reasoning_effort=high, 15 s budget) ended in
"LLM streaming time budget exhausted" with no content, while low-effort
decisions in the same window finished in ~6 s. Board math then played the
turn's key spells and said "Model unavailable". A high-effort
background.game_plan shared the GPU meanwhile.
"""

import threading
import time

import pytest
from tests.jace_states import jace_engine, jace_state

from arenamcp import action_planner
from arenamcp.game_plan import GamePlanManager, critical_decision_call, critical_decision_call_inflight

HIGH = '{"option_ids": ["idx:2"], "reasoning": "high: Jace -3 draws."}'
LOW = '{"option_ids": ["idx:2"], "reasoning": "low: Jace -3."}'


class _Legs:
    """A fake backend: per effort, a delay and an answer (or an exception)."""

    def __init__(self, high=(0.05, HIGH), low=(0.01, LOW)):
        self.plan = {"high": high, None: low}
        self.calls: list = []
        self.cancels: dict = {}
        self.inflight_seen: list = []

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" not in user:
            return "{}"
        effort = kwargs.get("reasoning_effort")
        cancel = kwargs.get("cancel_event")
        self.calls.append(effort)
        self.cancels[effort] = cancel
        if effort == "high":
            self.inflight_seen.append(critical_decision_call_inflight())
        delay, answer = self.plan[effort]
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("request cancelled")
            time.sleep(0.005)
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def critical(monkeypatch):
    monkeypatch.setattr(action_planner, "critical_decision", lambda *a, **k: "removal")


def _run(backend):
    engine, bridge, _ = jace_engine(HIGH)
    engine._planner._backend = backend
    started = time.monotonic()
    assert engine._try_typed_decision_path(jace_state(), "decision_required") is True
    return engine, bridge, time.monotonic() - started


def test_high_in_time_is_used_and_low_is_cancelled(critical, caplog):
    backend = _Legs()
    engine, bridge, _ = _run(backend)
    assert sorted(backend.calls, key=str) == sorted(["high", None], key=str)
    assert engine._planner._last_decision_reasoning.startswith("high:")
    assert backend.cancels[None].is_set() and not backend.cancels["high"].is_set()
    bridge.submit_action_by_index.assert_called_once()
    assert "typed-decision hedge: high won in" in caplog.text


def test_high_timing_out_uses_the_ready_low_answer_at_once(critical, monkeypatch, caplog):
    monkeypatch.setattr(action_planner.ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 0.3)
    backend = _Legs(high=(0.3, TimeoutError("LLM streaming time budget exhausted")))
    engine, bridge, elapsed = _run(backend)
    assert engine._planner._last_decision_reasoning.startswith("low:")
    assert elapsed < 0.3 + 1.0  # not the high leg's budget plus the low leg's again
    bridge.submit_action_by_index.assert_called_once()
    assert "typed-decision hedge: low won in" in caplog.text


def test_an_unparseable_high_answer_also_falls_to_low(critical):
    backend = _Legs(high=(0.02, "no json here"))
    engine, _, _ = _run(backend)
    assert engine._planner._last_decision_reasoning.startswith("low:")


def test_both_failing_falls_back_and_says_too_slow(critical):
    slow = TimeoutError("LLM streaming time budget exhausted")
    backend = _Legs(high=(0.02, slow), low=(0.02, TimeoutError("LLM streaming time budget exhausted")))
    engine, _, _ = _run(backend)
    assert engine._planner.last_llm_failure == "unavailable"  # autopilot semantics unchanged
    assert engine._planner._last_decision_reasoning.startswith("Model too slow")


def test_a_routine_decision_makes_one_low_effort_call():
    backend = _Legs()
    engine, _, _ = _run(backend)
    assert backend.calls == [None]


def test_no_game_plan_reform_starts_while_a_critical_call_is_in_flight(critical):
    backend = _Legs()
    _run(backend)
    assert backend.inflight_seen == [True]
    assert not critical_decision_call_inflight()

    class _Backend:
        def complete(self, *a, **k):
            return "{}"

    mgr = GamePlanManager(_Backend())
    with critical_decision_call():
        assert not mgr.request_reform(jace_state())
    assert mgr._last_attempt_at is None  # nothing attempted: re-requested at the next window


def test_the_in_flight_mark_counts_overlapping_calls():
    inside, release = threading.Event(), threading.Event()

    def other():
        with critical_decision_call():
            inside.set()
            release.wait(1)

    worker = threading.Thread(target=other)
    with critical_decision_call():
        worker.start()
        assert inside.wait(1)
    assert critical_decision_call_inflight()  # the other call is still in flight
    release.set()
    worker.join()
    assert not critical_decision_call_inflight()


def test_the_request_changing_cancels_both_legs(critical):
    backend = _Legs(high=(0.5, HIGH), low=(0.5, LOW))
    engine, bridge, _ = jace_engine(HIGH)
    engine._planner._backend = backend
    outer = threading.Event()
    engine._planner.set_decision_cancel_event(outer)
    threading.Timer(0.05, outer.set).start()
    from tests.jace_states import POLL

    from arenamcp.decisions import build_pending_decision

    decision = build_pending_decision(POLL, resolve_instance=lambda _iid: "")
    started = time.monotonic()
    engine._planner.plan_decision_options(decision, jace_state())
    assert time.monotonic() - started < 0.4
    assert backend.cancels["high"].is_set() and backend.cancels[None].is_set()


# 2026-10-19 Mono-Red Burn (standalone.log 07:15-09:08): with a valid low answer
# in, the hedge waited a mean 3.1 s more for the high one; at 08:25:31 the low
# answer was ready at 4.9 s and the action left at 15.0 s, when the high leg ran
# out of budget with nothing.
OTHER = '{"option_ids": ["idx:1"], "reasoning": "high: something else."}'


def _hedge(high, low, low_agrees=None):
    """Run ``_hedged_decision`` alone: per effort, (delay, answer or exception)."""
    import json

    planner = action_planner.ActionPlanner(backend=None)
    planner._last_decision_trace = {}
    cancels: dict = {}

    def ask(message, *, effort, budget, cancel_event=None):
        cancels[effort] = cancel_event
        delay, answer = high if effort == "high" else low
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("request cancelled")
            time.sleep(0.005)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def parse(text):
        data = json.loads(text)
        return data, [str(i) for i in data["option_ids"]]

    started = time.monotonic()
    data, chosen = planner._hedged_decision(ask, parse, "PENDING DECISION", 2.0, low_agrees)
    return data, chosen, time.monotonic() - started, cancels, planner._last_decision_trace


def test_a_valid_low_answer_waits_only_a_short_grace_for_high(monkeypatch, caplog):
    monkeypatch.setattr(action_planner.ActionPlanner, "_HIGH_AFTER_LOW_S", 0.1)
    data, chosen, elapsed, cancels, trace = _hedge(high=(2.0, OTHER), low=(0.01, LOW))
    assert data["reasoning"].startswith("low:") and chosen == ["idx:2"]
    assert elapsed < 0.6  # not the high leg's 2 s
    assert cancels["high"].is_set()
    assert "typed-decision hedge: low won in" in caplog.text and "not waited for" in caplog.text
    assert trace["hedge"]["winner"] == "low"


def test_a_high_answer_inside_the_grace_still_wins_and_the_log_compares_them(caplog):
    data, chosen, _, cancels, trace = _hedge(high=(0.1, OTHER), low=(0.01, LOW))
    assert data["reasoning"].startswith("high:") and chosen == ["idx:1"]
    assert cancels[None].is_set() and not cancels["high"].is_set()
    assert "typed-decision hedge: high won in" in caplog.text
    assert "(low: ['idx:2'] at" in caplog.text
    assert trace["hedge"]["winner"] == "high"
    _hedge(high=(0.05, HIGH), low=(0.01, LOW))
    assert "(low: the same at" in caplog.text


def test_a_low_answer_that_is_the_board_math_play_does_not_wait_for_high(caplog):
    # The hedge hands the check the low answer's ids and its parsed JSON (its stated target, #56).
    def agrees(ids, data):
        return "the board-math line casts Jace" if ids == ["idx:2"] else ""

    data, _, elapsed, cancels, _ = _hedge(high=(2.0, OTHER), low=(0.01, LOW), low_agrees=agrees)
    assert data["reasoning"].startswith("low:")
    assert elapsed < 0.5
    assert cancels["high"].is_set()
    assert "high not awaited: the low answer is the board-math play" in caplog.text
    # A low answer the board math does not share waits for the high one as before.
    data, _, _, _, _ = _hedge(high=(0.1, OTHER), low=(0.01, LOW), low_agrees=lambda ids, data: "")
    assert data["reasoning"].startswith("high:")


def test_a_failed_low_leg_still_waits_for_high():
    slow = TimeoutError("LLM streaming time budget exhausted")
    data, _, _, _, _ = _hedge(high=(0.2, HIGH), low=(0.01, slow))
    assert data["reasoning"].startswith("high:")


def test_board_math_agreement_is_only_the_fallbacks_own_play_in_our_main_phase():
    from arenamcp.decisions import DecisionOption, PendingDecision

    mountain = {
        "instance_id": 166,
        "grp_id": 65385,
        "name": "Mountain",
        "card_types": ["Land"],
        "type_line": "Basic Land — Mountain",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    board = {
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "active_player": 1, "priority_player": 1},
        "players": [{"seat_id": 1, "life_total": 20, "is_local": True}, {"seat_id": 2, "life_total": 9}],
        "local_seat_id": 1,
        "hand": [mountain],
        "battlefield": [],
        "stack": [],
    }
    decision = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:6", "Play Land: Mountain", meta={"actionType": "ActionType_Play", "instanceId": 166}
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )
    assert action_planner.board_math_agreement(decision, board, ["idx:6"])
    assert action_planner.board_math_agreement(decision, board, ["pass"]) == ""
    combat = dict(board, turn=dict(board["turn"], phase="Phase_Combat", step="Step_DeclareAttack"))
    assert action_planner.board_math_agreement(decision, combat, ["idx:6"]) == ""


def test_carrying_out_a_passes_stated_plan_is_not_asked_again(caplog):
    """2026-10-19 08:04:23: the re-ask cost another 12.7 s ("Coaching loop ... resumed after ~23s")."""
    from tests.fakes import planner_with

    from arenamcp.decisions import DecisionOption, PendingDecision

    shock = {
        "instance_id": 201,
        "name": "Shock",
        "card_types": ["Instant"],
        "oracle_text": "Shock deals 2 damage to any target.",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    bill = {
        "instance_id": 301,
        "name": "Bristly Bill",
        "card_types": ["Creature"],
        "power": 4,
        "toughness": 4,
        "owner_seat_id": 2,
        "controller_seat_id": 2,
    }
    window = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0", "Cast Shock", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 201}
            ),
            DecisionOption("pass", "Pass"),
        ),
        can_pass=True,
    )
    answer = '{"option_ids": ["idx:0"], "reasoning": "Blocks are in: Shock face now."}'

    def board(step):
        return {
            "match_id": "m1",
            "turn": {"turn_number": 8, "phase": "Phase_Combat", "step": step, "active_player": 1},
            "local_seat_id": 1,
            "players": [{"seat_id": 1, "life": 16, "is_local": True}, {"seat_id": 2, "life": 7}],
            "hand": [shock],
            "battlefield": [bill],
            "graveyard": [],
            "exile": [],
            "stack": [],
        }

    def asked(earlier_step, now_step, reasoning):
        planner = planner_with(answer)
        planner._memory().record(board(earlier_step), window, ["pass"], reasoning)
        assert planner._llm_decision_options(window, board(now_step)) == ["idx:0"]
        return planner._backend.calls

    kept = "Holding Shock as post-combat reach closes out lethal this turn"
    assert asked("Step_DeclareAttack", "Step_DeclareBlock", kept) == 1
    assert "carries out the plan an earlier pass stated" in caplog.text
    # Still asked again: the same step, or a pass that rejected the card.
    assert asked("Step_DeclareBlock", "Step_DeclareBlock", kept) == 2
    assert asked("Step_DeclareAttack", "Step_DeclareBlock", "Shock on Bill is a bad trade") == 2
