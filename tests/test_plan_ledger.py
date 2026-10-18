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
    block = mgr.ledger_payload()["block"]
    assert "T3 (our turn" in block and "did: T3 ActionsAvailable: Play Land: Island" in block
    mgr.observe({**state(1), "match_id": "m2"})  # new match resets
    assert "T3" not in mgr.ledger_payload()["block"]
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
