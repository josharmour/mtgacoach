"""A "tapped enemy limits crackback" attack premise is corrected from the board (2026-11-12 T8).

Gar vs Minsc, turn 8: the plan attacked our hasty 4/4 Boo at their 3-loyalty
Minsc saying "Kill their Minsc engine; their big Boo is tapped so crackback is
limited." Two things are wrong with that premise, generically:

- their big Boo untaps normally before their next combat, so being tapped now
  says nothing about their NEXT attackers;
- their untapped Keen-Eyed Curator 3/3 and Delighted Halfling 2/3 can block
  THIS attack now — a double block kills the 4/4 and leaves the walker
  standing — so the planeswalker kill was never guaranteed.

The correction is narration-only: the pick stands, the reasoning gains the
board facts, and the trace records the premise. Legal tactical attacks are
never overridden.

Fixture: the VERBATIM recorded turn-8 DeclareAttackers state_snapshot — their
7/7 Boo still tapped, Curator and Halfling still untapped, exactly as recorded.
The correction must fire on that real state, not on a normalized copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from arenamcp.action_planner import ActionPlanner
from arenamcp.attack_premises import (
    held_blocker_reconciliation,
    tapped_crackback_correction,
)
from arenamcp.decision_memory import DecisionMemory

FIXTURE = Path(__file__).parent / "fixtures" / "gar_attack_consistency_20261112_turn8_declareattackers.json"

REASONING = "Kill their Minsc engine; their big Boo is tapped so crackback is limited."
CHOSEN = ["Boo [id:978] -> Minsc & Boo, Timeless Heroes [760]"]


def _fixture():
    return json.loads(FIXTURE.read_text())


def _fixture_state():
    return _fixture()["state"]


def _planner(reasoning: str = REASONING) -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = reasoning
    planner._last_decision_trace = {}
    return planner


def _declare_attackers_decision() -> SimpleNamespace:
    return SimpleNamespace(request_type="DeclareAttackers")


# --- premise detection -----------------------------------------------------------------------


def test_recorded_t8_premise_sentence_is_detected():
    state = _fixture_state()
    # The recorded snapshot really does carry both halves of the contradiction:
    # their 7/7 Boo tapped, and untapped creatures that can block right now.
    theirs = [
        card
        for card in state["battlefield"]
        if card.get("controller_seat_id") == 1 and "Creature" in str(card.get("type_line") or "")
    ]
    assert any(card.get("is_tapped") for card in theirs)
    untapped = [card for card in theirs if not card.get("is_tapped")]
    assert {card.get("name") for card in untapped} >= {"Keen-Eyed Curator", "Delighted Halfling"}

    correction = tapped_crackback_correction(state, REASONING)
    assert correction
    assert "Tapped now is not tapped next combat" in correction
    assert "untaps before their next attack" in correction
    assert "Keen-Eyed Curator 3/3" in correction
    assert "Delighted Halfling 2/3" in correction
    assert "double block" in correction


def test_premise_without_a_consequence_is_not_flagged():
    state = _fixture_state()
    # The recorded strategy line mentions the tapped trampler but draws no
    # crackback conclusion from it.
    assert not tapped_crackback_correction(state, _fixture()["strategy"])


def test_our_own_tapped_creature_is_not_the_premise():
    state = _fixture_state()
    assert not tapped_crackback_correction(state, "Our Boo is tapped so we can't block next turn.")


def test_their_tapped_creature_without_a_blocking_conclusion_is_not_flagged():
    state = _fixture_state()
    assert not tapped_crackback_correction(state, "Their big Boo is tapped; it attacked last turn.")


# --- board conditions -------------------------------------------------------------------------


def test_no_correction_when_their_whole_board_is_tapped():
    state = _fixture_state()
    for card in state["battlefield"]:
        if card.get("controller_seat_id") == 1 and "Creature" in str(card.get("type_line") or ""):
            card["is_tapped"] = True
    # With no untapped blocker the premise holds for this combat.
    assert not tapped_crackback_correction(state, REASONING)


def test_no_correction_when_no_enemy_creature_is_tapped():
    state = _fixture_state()
    for card in state["battlefield"]:
        card["is_tapped"] = False
    assert not tapped_crackback_correction(state, REASONING)


# --- planner hook (narration-only) ------------------------------------------------------------


def test_recorded_t8_declaration_gains_board_facts_and_keeps_intent():
    state = _fixture_state()
    planner = _planner()

    planner._correct_tapped_crackback_claim(state, _declare_attackers_decision(), CHOSEN)

    reasoning = planner._last_decision_reasoning
    # The model's intent survives (2026-10-19: stripping intent starved the target step).
    assert reasoning.startswith(REASONING)
    # The false premise gains both board facts.
    assert "Tapped now is not tapped next combat" in reasoning
    assert "Keen-Eyed Curator" in reasoning
    trace = planner.get_last_decision_trace()
    assert trace["tapped_crackback_claim"]["correction"]
    assert trace["tapped_crackback_claim"]["reconciliation"] == ""


def test_non_declare_attackers_requests_are_untouched():
    state = _fixture_state()
    planner = _planner()

    planner._correct_tapped_crackback_claim(state, SimpleNamespace(request_type="ActionsAvailable"), ["pass"])

    assert planner._last_decision_reasoning == REASONING
    assert "tapped_crackback_claim" not in planner.get_last_decision_trace()


def test_reasoning_without_the_premise_is_left_alone():
    state = _fixture_state()
    reasoning = "Attack with Boo; it trades into the curator profitably."
    planner = _planner(reasoning)

    planner._correct_tapped_crackback_claim(state, _declare_attackers_decision(), CHOSEN)

    assert planner._last_decision_reasoning == reasoning


# --- held-blocker reconciliation ---------------------------------------------------------------


def test_guard_hold_spent_by_the_attack_requires_a_rationale():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.note(state, "DeclareAttackers", "attack held by guard: Boo not attacking")

    note = held_blocker_reconciliation(memory.this_turn(state), ["Boo"])

    assert note == (
        "Attacking with Boo reverses this turn's hold-back; say why the revised line is worth it."
    )


def test_other_holds_do_not_fire_for_this_attacker():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.note(state, "DeclareAttackers", "attack held by guard: Slickshot Show-Off not attacking")

    assert not held_blocker_reconciliation(memory.this_turn(state), ["Boo"])
