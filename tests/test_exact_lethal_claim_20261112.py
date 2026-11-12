"""An adjective-form "exact lethal" claim the board math contradicts is corrected (2026-11-11 T9).

19:08:36: "Lightning Strike to the face plus pumped Slickshot (3 flying) and
Cindermaw's trample gives 10 damage — exact lethal on their 7 life even if
Dazzling Angel chumps Slickshot." Opponent sat at 7 with three untapped
blockers; the best line was lethal on T11, not this turn. The noun-adjective
form ("exact lethal") slipped the exactly/precisely patterns, so no correction
fired and the attack plan inherited the false claim. The target step was then
cancelled mid-planning and the deterministic fallback picked a creature while
nothing recorded that divergence.

Fixture: recorded turn-9 Strike-decision state (Hired Claw resolved onto the
battlefield before the Strike decision).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from arenamcp import board_assessment
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot

CLAIM = (
    "Lightning Strike to the face plus pumped Slickshot (3 flying) and Cindermaw's trample "
    "gives 10 damage — exact lethal on their 7 life even if Dazzling Angel chumps Slickshot."
)

FIXTURE = Path(__file__).parent / "fixtures" / "plan_consistency_20261112_turn9_strike.json"


def _fixture_state():
    return json.loads(FIXTURE.read_text())["state"]


# --- claim detection ------------------------------------------------------------------------


def test_adjective_exact_lethal_claim_is_detected():
    planner = ActionPlanner.__new__(ActionPlanner)
    assert planner._lethal_claims(CLAIM) == [CLAIM]
    # The pre-existing adverb forms still match.
    assert planner._lethal_claims("this is exactly lethal this turn")
    assert planner._lethal_claims("this is precisely lethal this turn")


def test_negated_or_later_turn_wording_is_not_flagged():
    planner = ActionPlanner.__new__(ActionPlanner)
    assert not planner._lethal_claims("this is not exact lethal yet")
    assert not planner._lethal_claims("holding Strike for exact lethal next turn")


# --- recorded-board correction --------------------------------------------------------------


def test_recorded_turn9_exact_lethal_claim_is_corrected_from_real_board():
    state = _fixture_state()
    facts = board_assessment.assess(state)
    assert facts.lethal_now is False  # the recorded claim was false on this board

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(state, None)

    reasoning = planner._last_decision_reasoning
    assert "exact lethal" not in reasoning
    assert reasoning.startswith("Board math: not lethal this turn (they are at 7;")
    assert "T11" in reasoning  # the real best line, from the recorded state
    trace = planner.get_last_decision_trace()
    assert trace["false_lethal_claim"] == [CLAIM]
    # Decision memory keeps the model's intent plus the correction (2026-10-19:
    # stripping the intent made the target step aim at a 5/5 instead of "the face").
    kept = planner._false_lethal_memory[1]
    assert kept.startswith("Lightning Strike to the face")
    assert "Board math:" in kept


def test_true_exact_lethal_claim_survives(monkeypatch):
    line = SimpleNamespace(win_at=2, win_turn=9)
    facts = SimpleNamespace(lethal_now=True, posture="attack", lines=[line], opp_life=7)
    monkeypatch.setattr(board_assessment, "assess", lambda state: facts)

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLAIM
    planner._last_decision_trace = {}

    planner._correct_false_lethal_claim(_fixture_state(), None)

    assert planner._last_decision_reasoning == CLAIM
    assert "false_lethal_claim" not in planner.get_last_decision_trace()


# --- target-fallback provenance -------------------------------------------------------------


class _CancelledBackend:
    """Model server that reports the request changed during planning."""

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def complete(self, system, user, *args, **kwargs):
        self.calls += 1
        raise ValueError("request changed during planning")


LOCAL, OPP = 1, 2


def _creature(iid, name, power, toughness, seat=OPP):
    return {
        "instance_id": iid,
        "grp_id": 90000 + iid,
        "name": name,
        "type_line": "Creature — Angel",
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
        "turn_entered_battlefield": 5,
    }


STRIKE_ORACLE = "Lightning Strike deals 3 damage to any target."


def _spell(iid, name, type_line, cost, oracle, seat=LOCAL):
    return {
        "instance_id": iid,
        "grp_id": 90000 + iid,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": oracle,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def _target_state():
    return {
        "match_id": "fe9d1f07",
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 9, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": OPP, "life_total": 7, "is_local": False},
            {"seat_id": LOCAL, "life_total": 20, "is_local": True},
        ],
        "battlefield": [_creature(334, "Dazzling Angel", 2, 3)],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [_spell(900001, "Lightning Strike", "Instant", "{1}{R}", STRIKE_ORACLE)],
        "_bridge_request_payload": {"sourceId": 900001},
        "decision_context": {"type": "target_selection", "source_id": 900001},
    }


def _target_decision():
    return PendingDecision(
        request_id=(276, 376),
        request_type="SelectTargets",
        options=(
            DecisionOption("tgt:327", "Sun-Blessed Healer", meta={"instanceId": 327}),
            DecisionOption("tgt:334", "Dazzling Angel", meta={"instanceId": 334}),
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Lightning Strike",
        slots=(TargetSlot(1, 1, 1, 0, (327, 334)),),
    )


def test_selecttargets_fallback_after_cancellation_records_provenance():
    backend = _CancelledBackend()
    planner = ActionPlanner(backend, timeout=5, land_drop_first=False)
    state = _target_state()

    picked = planner.plan_decision_options(_target_decision(), state)

    trace = planner.get_last_decision_trace()
    assert trace["llm_failure"] == "bad_answer"
    # The deterministic fallback chose the target and the trace says so — a
    # cancelled target step leaves no completed decision record otherwise.
    assert trace["targeting_fallback"] == picked
