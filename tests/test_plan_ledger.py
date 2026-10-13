"""The match ledger: per-turn plan step, decisions and board swing, with deterministic lessons."""

from __future__ import annotations

from arenamcp.game_plan import GamePlan, GamePlanManager
from arenamcp.plan_ledger import PlanLedger


def snap(our_life=20, opp_life=20, ours=(0, 0), theirs=(0, 0), hand=7):
    return {
        "our_life": our_life,
        "opp_life": opp_life,
        "our_creatures": ours[0],
        "our_power": ours[1],
        "their_creatures": theirs[0],
        "their_power": theirs[1],
        "our_hand": hand,
    }


def plan_with(turn, casts, attack="Void Extrapolator"):
    return GamePlan(
        win_conditions=["Chip damage"],
        turn_formed=turn - 2,
        turn_plan=[{"turn": turn, "land": "Swamp", "cast": casts, "attack": attack, "hold": ""}],
    )


def test_ledger_records_plan_step_decisions_and_swing():
    ledger = PlanLedger()
    decisions = {
        5: ["T5 ActionsAvailable: Play Land: Swamp", "T5 ActionsAvailable: Casting Void Extrapolator"]
    }
    ledger.turn_started(
        5, ours=True, snapshot=snap(), role="aggressor", plan=plan_with(5, ["Void Extrapolator"])
    )
    ledger.turn_started(5, ours=True, snapshot=snap())  # repeated observe: no new entry
    ledger.turn_started(
        6,
        ours=False,
        snapshot=snap(opp_life=18, ours=(1, 2)),
        decisions_for_turn=lambda t: decisions.get(t, []),
    )
    block = ledger.prompt_block()
    assert block.startswith("MATCH LEDGER")
    assert (
        "T5 (our turn, aggressor): plan said: play Swamp; cast Void Extrapolator; attack: Void Extrapolator; executed"
        in block
    )
    assert (
        "did: T5 ActionsAvailable: Play Land: Swamp | T5 ActionsAvailable: Casting Void Extrapolator" in block
    )
    assert "life us 20->20, them 20->18; creatures/power ours 0/0->1/2, theirs 0/0->0/0" in block
    assert "T6 (their turn)" in block and ledger.lessons() == []


def test_ledger_lessons_name_stalled_pressure_missed_casts_and_life_loss():
    ledger = PlanLedger()
    nothing = lambda t: ["T%d ActionsAvailable: Pass — holding" % t]  # noqa: E731
    ledger.turn_started(
        5,
        ours=True,
        snapshot=snap(opp_life=18),
        role="aggressor",
        plan=plan_with(5, ["Tinybones, Pocket Nuisance"]),
    )
    ledger.turn_started(6, ours=False, snapshot=snap(opp_life=18), decisions_for_turn=nothing)
    ledger.turn_started(
        7,
        ours=True,
        snapshot=snap(our_life=14, opp_life=18),
        role="aggressor",
        plan=plan_with(7, ["Tinybones, Pocket Nuisance"]),
        decisions_for_turn=nothing,
    )
    ledger.turn_started(8, ours=False, snapshot=snap(our_life=14, opp_life=18), decisions_for_turn=nothing)
    ledger.turn_started(9, ours=True, snapshot=snap(our_life=14, opp_life=18), decisions_for_turn=nothing)
    lessons = ledger.lessons()
    assert any(l.startswith("No damage dealt on our turns T5, T7") for l in lessons)
    assert any("Planned casts not made on 2 of our turns (Tinybones, Pocket Nuisance)" in l for l in lessons)
    assert any(l.startswith("We lost 6 life across T6") for l in lessons)
    assert "NOT executed (missing Tinybones, Pocket Nuisance)" in ledger.prompt_block()
    ledger.turn_started(1, ours=True, snapshot=snap())  # a new game: the ledger restarts
    assert len(ledger.entries) == 1


def test_manager_feeds_the_ledger_on_turn_changes_and_parses_lessons():
    mgr = GamePlanManager(backend=None)
    seen = {}
    mgr.set_turn_decisions_source(lambda turn: seen.get(turn, []))
    state = lambda turn: {  # noqa: E731
        "match_id": "m1",
        "turn": {"turn_number": turn, "active_player": 1},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life": 20, "is_local": True}, {"seat_id": 2, "life": 20}],
        "battlefield": [],
        "hand": [],
        "graveyard": [],
        "stack": [],
    }
    mgr.observe(state(3))
    seen[3] = ["T3 ActionsAvailable: Play Land: Island"]
    mgr.observe(state(4))
    block = mgr.ledger_block()
    assert "T3 (our turn" in block and "did: T3 ActionsAvailable: Play Land: Island" in block
    mgr.observe({**state(1), "match_id": "m2"})  # new match resets
    assert "T3" not in mgr.ledger_block()
    plan = GamePlanManager._parse(
        '{"win_conditions": ["x"], "path": "y", "lessons": ["Omit Variables got us to threshold", 2, "x"]}', 4
    )
    assert plan.lessons == ["Omit Variables got us to threshold", "x"]
    assert "  Lesson so far: Omit Variables got us to threshold" in plan.as_planner_block()
    assert plan.as_payload()["lessons"] == plan.lessons


def test_ledger_lessons_reach_the_tactical_strategy_block_and_the_bug_report_payload():
    mgr = GamePlanManager(backend=None)
    mgr._plan = GamePlan(
        win_conditions=["Chip damage"], path="attack", turn_formed=3, lessons=["model lesson"]
    )
    nothing = lambda t: ["T%d ActionsAvailable: Pass" % t]  # noqa: E731
    mgr._ledger.turn_started(5, ours=True, snapshot=snap(opp_life=18), role="aggressor")
    mgr._ledger.turn_started(6, ours=False, snapshot=snap(opp_life=18), decisions_for_turn=nothing)
    mgr._ledger.turn_started(
        7, ours=True, snapshot=snap(opp_life=18), role="aggressor", decisions_for_turn=nothing
    )
    mgr._ledger.turn_started(8, ours=False, snapshot=snap(opp_life=18), decisions_for_turn=nothing)
    block = mgr.strategy_block(None)
    assert "  Lesson so far: model lesson" in block
    assert "  Ledger this match: No damage dealt on our turns T5, T7" in block
    payload = mgr.ledger_payload()
    assert payload["turns"][0]["turn"] == 5 and payload["turns"][0]["did"] == ["T5 ActionsAvailable: Pass"]
    assert payload["turns"][2]["did"] == ["T7 ActionsAvailable: Pass"]
    assert payload["lessons"] and payload["block"].startswith("MATCH LEDGER")


def test_a_decision_call_makes_an_inflight_reform_yield_and_resume_later():
    import threading

    mgr = GamePlanManager(backend=None)
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=3)
    mgr._last_sig = (4,)
    mgr._last_reform_turn = 4
    mgr._seed = mgr._last_seed = None
    cancel = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel
    mgr._inflight_lane = None
    mgr.yield_for_decision()
    assert cancel.is_set() and mgr._inflight is False
    assert mgr._should_reform((5,), our_turn=False, key=mgr._last_key) == "resumed after a decision"
    mgr.yield_for_decision()  # nothing in flight: a no-op
    assert "resumed after a decision" in GamePlanManager._ROUTINE_REASONS


def test_a_slipping_lethal_turn_becomes_a_lesson():
    ledger = PlanLedger()
    nothing = lambda t: []  # noqa: E731
    for turn, lethal in ((31, 34), (33, 36), (35, 38)):
        ledger.turn_started(
            turn, ours=True, snapshot={**snap(opp_life=11), "lethal_turn": lethal}, role="aggressor"
        )
        ledger.turn_started(
            turn + 1, ours=False, snapshot=snap(opp_life=11 - (turn - 29)), decisions_for_turn=nothing
        )
    lessons = ledger.lessons()
    assert any(l.startswith("Projected lethal turn slipped T34 -> T36 -> T38") for l in lessons)
    steady = PlanLedger()
    for turn in (31, 33, 35):
        steady.turn_started(turn, ours=True, snapshot={**snap(), "lethal_turn": 36}, role="aggressor")
        steady.turn_started(turn + 1, ours=False, snapshot=snap(opp_life=10), decisions_for_turn=nothing)
    assert not any("slipped" in l for l in steady.lessons())


def test_a_reform_that_has_run_a_while_does_not_yield():
    import threading
    import time

    mgr = GamePlanManager(backend=None)
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=3)  # the first plan never yields
    cancel = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel
    mgr._inflight_lane = None
    mgr._inflight_started_at = time.monotonic() - 10.0
    mgr.yield_for_decision()
    assert not cancel.is_set() and mgr._inflight is True
    mgr._inflight_started_at = time.monotonic()
    mgr.yield_for_decision()
    assert cancel.is_set() and mgr._inflight is False


def test_plan_status_reports_none_forming_and_ready_and_the_first_plan_never_yields(caplog):
    import logging
    import threading
    import time

    mgr = GamePlanManager(backend=None)
    board = {"turn": {"turn_number": 5}}
    with caplog.at_level(logging.WARNING, logger="arenamcp.game_plan"):
        status = mgr.plan_status(board)
    assert status["state"] == "none" and status["attempts"] == 0
    assert any(m.startswith("No game plan on turn 5") for m in caplog.messages)
    # A reform in flight with no plan: "forming", and a decision call does not cancel it.
    cancel = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel
    mgr._inflight_lane = None
    mgr._inflight_plan_turn = 6
    mgr._inflight_started_at = time.monotonic() - 1.0
    mgr.yield_for_decision()
    assert not cancel.is_set()
    status = mgr.plan_status(board)
    assert status["state"] == "forming" and status["inflight_for_turn"] == 6
    # With a plan in hand, a fresh reform yields and the status says so.
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=3)
    mgr.yield_for_decision()
    assert cancel.is_set()
    status = mgr.plan_status({"turn": {"turn_number": 8}})
    assert status["state"] == "ready" and status["age_turns"] == 5 and status["cancels"] == 1
    assert status["last_outcome"] == "yielded to a decision"
    assert mgr.ui_payload({"turn": {"turn_number": 8}})["plan_status"]["state"] == "ready"


def test_the_first_plan_is_not_superseded_when_its_turn_passes():
    import threading

    mgr = GamePlanManager(backend=None)
    cancel = threading.Event()
    mgr._inflight = True
    mgr._inflight_cancel = cancel
    mgr._inflight_lane = None
    mgr._inflight_plan_turn = 20
    mgr._supersede_stale_inflight_locked({"turn": {"turn_number": 21}})
    assert not cancel.is_set() and mgr._inflight is True
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=18)
    mgr._supersede_stale_inflight_locked({"turn": {"turn_number": 21}})
    assert cancel.is_set() and mgr._plan_outcome == "superseded by a newer turn"


def test_the_first_plan_runs_at_low_effort_and_later_ones_at_high():
    calls = []

    class Backend:
        def complete(self, system, user, max_tokens, **kwargs):
            calls.append(kwargs.get("reasoning_effort"))
            return '{"win_conditions": ["x"], "path": "y", "role": "aggressor", "turns": []}'

    mgr = GamePlanManager(Backend())
    state = {
        "match_id": "m1",
        "turn": {"turn_number": 3, "active_player": 1},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "life_total": 20, "is_local": True}, {"seat_id": 2, "life_total": 20}],
        "battlefield": [],
        "hand": [],
        "graveyard": [],
        "stack": [],
    }
    assert mgr._reform(state, 3) is not None
    assert calls == ["low"]
    mgr._plan = GamePlan(win_conditions=["x"], path="y", turn_formed=3, effort="low")
    assert mgr.plan_status(state)["effort"] == "low"
    deeper = mgr._reform(state, 4)
    assert deeper is not None and deeper.effort == "high"
    assert calls == ["low", "high"]
