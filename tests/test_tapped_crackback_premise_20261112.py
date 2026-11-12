"""Regression: the tapped-enemy-crackback premise in Gar's attack reasoning.

2026-11-12, Gar vs Minsc turn 8 (recorded via the GRE bridge): the plan
attacked our hasty 4/4 Boo (instance 978) at their 3-loyalty Minsc & Boo,
Timeless Heroes saying "their big Boo is tapped so crackback is limited"
while their untapped Keen-Eyed Curator 3/3 and Delighted Halfling 2/3 could
double block (killing the 4/4 and leaving the walker standing even through
trample) and their tapped 7/7 Boo untaps normally before their next attack.

The correction must REPLACE an unsupported kill certainty with its attack
intent plus a solver-validated verdict (the reviewer's finding: "Kill their
Minsc" followed by "unharmed" is a contradiction, not a correction), state
current blockers and next-combat untap threats as independent facts, and
reconcile commitments the attack spends — as a justified revision, never as
an instruction after the choice, and never claiming that holding wins.

All combat arithmetic here delegates to the production solver
(:func:`arenamcp.combat_solver.optimal_blocks`); the module declines to
state anything definitive when a stat is missing.
"""

from types import SimpleNamespace

import pytest

from arenamcp import decision_memory
from arenamcp.attack_premises import (
    apply_tapped_crackback,
    best_block_clause,
    defender_life,
    held_blocker_reconciliation,
    tapped_crackback_correction,
)
from arenamcp.board_assessment import _body
from arenamcp.combat_solver import optimal_blocks

FIXTURE = "tests/fixtures/gar_attack_consistency_20261112_turn8_declareattackers.json"
TARGET = "Minsc & Boo, Timeless Heroes"
REASONING = "Kill their Minsc engine; their big Boo is tapped so crackback is limited."
STRATEGY = "Trade our hasty 4/4 Boo into their planeswalker while their 7/7 trampler is tapped."
CHOSEN = ["Boo [id:978] -> Minsc & Boo, Timeless Heroes [760]"]
UNTAP_PHRASE = "Tapped now is not tapped next combat"
BLOCK_PHRASE = "can still block this attack now"


@pytest.fixture()
def state():
    import json

    with open(FIXTURE) as handle:
        payload = json.load(handle)
    return payload["state"]


def _ours(state, name):
    return [
        card
        for card in state["battlefield"]
        if card.get("controller_seat_id") == 2 and card.get("name") == name
    ]


def _their_untapped(state):
    return [
        card
        for card in state["battlefield"]
        if card.get("controller_seat_id") == 1
        and card.get("is_tapped") is False
        and card.get("power") is not None
    ]


# --------------------------------------------------------------------------- #
# Premise detection on the recorded turn-8 reasoning
# --------------------------------------------------------------------------- #


def test_recorded_t8_premise_yields_replace_correction_with_both_independent_facts(state):
    boo = _ours(state, "Boo")[0]
    correction = tapped_crackback_correction(state, REASONING, attackers=[boo], target=TARGET)
    assert correction.mode == "replace"
    assert correction.sentence == REASONING
    # The kill certainty is gone; what remains is the attack intent plus the
    # solver-validated verdict (reviewer: replace, never append a contradiction).
    assert correction.text.startswith("Attack their Minsc engine;")
    assert "Kill their Minsc engine" not in correction.text
    assert f"{TARGET} takes no damage this combat" in correction.text
    assert BLOCK_PHRASE in correction.text
    assert UNTAP_PHRASE in correction.text


def test_apply_tapped_crackback_replaces_in_place_and_is_idempotent(state):
    boo = _ours(state, "Boo")[0]
    once, first = apply_tapped_crackback(state, REASONING, attackers=[boo], target=TARGET)
    assert first.mode == "replace"
    assert once.startswith("Attack their Minsc engine;")
    assert "Kill their Minsc engine" not in once
    twice, second = apply_tapped_crackback(state, once, attackers=[boo], target=TARGET)
    assert twice == once
    assert second.text == ""


def test_premise_without_a_consequence_is_not_flagged(state):
    reasoning = "Their big Boo is tapped, so we have a window this turn."
    assert tapped_crackback_correction(state, reasoning).text == ""


def test_our_own_tapped_creature_is_not_the_premise(state):
    reasoning = "Our Boo is tapped so we cannot also block with it; crackback is scary."
    assert tapped_crackback_correction(state, reasoning).text == ""


def test_their_tapped_creature_without_a_blocking_conclusion_is_not_flagged(state):
    reasoning = "Their big Boo is tapped from blocking earlier."
    assert tapped_crackback_correction(state, reasoning).text == ""


# --------------------------------------------------------------------------- #
# Current blockers vs next combat are independent facts
# --------------------------------------------------------------------------- #


def test_all_enemy_creatures_tapped_still_flags_next_combat_threat(state):
    for card in state["battlefield"]:
        if card.get("controller_seat_id") == 1:
            card["is_tapped"] = True
    boo = _ours(state, "Boo")[0]
    correction = tapped_crackback_correction(state, REASONING, attackers=[boo], target=TARGET)
    assert correction.text
    assert UNTAP_PHRASE in correction.text
    assert BLOCK_PHRASE not in correction.text


def test_no_enemy_creature_tapped_still_flags_current_blockers(state):
    for card in state["battlefield"]:
        if card.get("controller_seat_id") == 1:
            card["is_tapped"] = False
    boo = _ours(state, "Boo")[0]
    correction = tapped_crackback_correction(state, REASONING, attackers=[boo], target=TARGET)
    assert correction.text
    assert BLOCK_PHRASE in correction.text
    assert UNTAP_PHRASE not in correction.text


def test_multi_attacker_declarations_get_the_qualitative_clause_only(state):
    ours_boos = _ours(state, "Boo")
    correction = tapped_crackback_correction(state, REASONING, attackers=ours_boos, target=TARGET)
    # Two attackers: no single-attacker arithmetic — the qualitative facts only.
    assert BLOCK_PHRASE in correction.text
    assert UNTAP_PHRASE in correction.text
    assert "would kill the" not in correction.text


# --------------------------------------------------------------------------- #
# Solver-backed best-block verdicts (mechanic regressions)
# --------------------------------------------------------------------------- #


def test_best_block_verdict_on_the_recorded_board(state):
    boo = _ours(state, "Boo")[0]
    clause = best_block_clause(boo, _their_untapped(state), TARGET)
    # Double block kills the 4/4; trample leaves nothing through (6 >= 4 lethal).
    assert "their best block (Keen-Eyed Curator, Delighted Halfling) kills the 4/4 attacker" in clause
    assert f"{TARGET} takes no damage this combat" in clause


def test_best_block_verdict_agrees_with_the_production_solver(state):
    boo = _ours(state, "Boo")[0]
    blockers = _their_untapped(state)
    clause = best_block_clause(boo, blockers, TARGET)
    attacker_body = _body(boo, 8, {})
    blocker_bodies = [_body(card, 8, {}) for card in blockers]
    plan = optimal_blocks([attacker_body], blocker_bodies, defender_life(state), caller="test")
    through = int(plan.damage_through or 0)
    dead = attacker_body["instance_id"] in (plan.attackers_killed_ids or frozenset())
    if dead:
        assert "kills the 4/4 attacker" in clause
        if through == 0:
            assert f"{TARGET} takes no damage this combat" in clause
        else:
            assert f"{through} damage gets through to {TARGET}" in clause
    else:
        assert "cannot kill the 4/4 attacker" in clause


def test_lone_small_blocker_follows_the_defenders_best_plan():
    attacker = {
        "instance_id": 1,
        "name": "Trampling Bear",
        "power": 4,
        "toughness": 4,
        "type_line": "Creature — Bear",
        "keywords": ["trample"],
    }
    blocker = {
        "instance_id": 2,
        "name": "Delighted Halfling",
        "power": 2,
        "toughness": 3,
        "type_line": "Creature — Halfling Citizen",
    }
    clause = best_block_clause(attacker, [blocker], TARGET)
    # The production solver decides: at full life it keeps a small blocker
    # back rather than chump it into a trampling 4/4. Whatever it chooses,
    # the clause states that plan and never invents a kill.
    assert clause == (
        f"their best block leaves the attack unblocked; all 4 damage goes to {TARGET} this combat."
    )


def test_deathtouch_blocker_kills_above_its_power():
    attacker = {
        "instance_id": 1,
        "name": "Big Attacker",
        "power": 4,
        "toughness": 4,
        "type_line": "Creature — Beast",
    }
    blocker = {
        "instance_id": 2,
        "name": "Deathtouch Bear",
        "power": 1,
        "toughness": 1,
        "type_line": "Creature — Bear",
        "keywords": ["deathtouch"],
    }
    clause = best_block_clause(attacker, [blocker], TARGET)
    # A 1/1 deathtouch blocker DOES kill the 4/4 (reviewer's false-certainty example).
    assert "their best block (Deathtouch Bear) kills the 4/4 attacker" in clause


def test_indestructible_attacker_survives_the_block():
    attacker = {
        "instance_id": 1,
        "name": "Indomitable",
        "power": 5,
        "toughness": 5,
        "type_line": "Creature — Rhino",
        "keywords": ["indestructible"],
    }
    blocker = {
        "instance_id": 2,
        "name": "Grizzly Bears",
        "power": 2,
        "toughness": 2,
        "type_line": "Creature — Bear",
    }
    clause = best_block_clause(attacker, [blocker], TARGET)
    # An indestructible attacker does not die to ordinary combat damage.
    assert "cannot kill the 5/5 attacker" in clause


def test_flying_attacker_needs_flying_or_reach_to_be_blocked():
    attacker = {
        "instance_id": 1,
        "name": "Slickshot Show-Off",
        "power": 2,
        "toughness": 2,
        "type_line": "Creature — Wizard",
        "keywords": ["flying"],
    }
    blocker = {
        "instance_id": 2,
        "name": "Ground Grizzly",
        "power": 3,
        "toughness": 3,
        "type_line": "Creature — Bear",
    }
    clause = best_block_clause(attacker, [blocker], TARGET)
    # Ground blockers do not kill a flyer (reviewer's false-certainty example).
    assert (
        f"none of their untapped creatures can legally block the 2/2 attacker "
        f"(only a creature with flying or reach can block a flyer); "
        f"all 2 damage goes to {TARGET} this combat."
    ) in clause


def test_blocked_non_trample_attack_deals_no_damage():
    attacker = {
        "instance_id": 1,
        "name": "Grizzly Bears",
        "power": 2,
        "toughness": 2,
        "type_line": "Creature — Bear",
    }
    blocker = {
        "instance_id": 2,
        "name": "Wall of Tarpit",
        "power": 1,
        "toughness": 4,
        "type_line": "Creature — Wall",
    }
    clause = best_block_clause(attacker, [blocker], TARGET)
    # Walls can block; without trample a blocked attack deals no damage.
    assert (
        f"their best block (Wall of Tarpit) cannot kill the 2/2 attacker and {TARGET} takes no damage this combat."
        in clause
    )


def test_missing_attacker_stats_yield_no_math():
    unknown = {
        "instance_id": 1,
        "name": "? Mystery ?",
        "power": None,
        "toughness": None,
        "type_line": "Creature",
    }
    bear = {"instance_id": 2, "name": "Bear", "power": 1, "toughness": 1, "type_line": "Creature"}
    # Unknown stats: decline definitive arithmetic instead of assuming zeros.
    assert best_block_clause(unknown, [bear], TARGET) == ""
    assert best_block_clause(bear, [], TARGET) == ""


# --------------------------------------------------------------------------- #
# Held-blocker reconciliation: justified revision, never instruction-after-choice
# --------------------------------------------------------------------------- #

GUARD_HOLD_LABELS = ["attack held by guard: Boo not attacking"]
COMMITTED_REASONING = "+1 pumps my upkeep Boo to 4/4, so blocking their 7/7 trampler absorbs 4 damage."


def _record(memory_obj, state_, labels, reasoning="", model_unavailable=False):
    memory_obj.record(
        state_,
        SimpleNamespace(request_type="ChooseAbility", source_label="model", options=[]),
        labels,
        reasoning=reasoning,
        model_unavailable=model_unavailable,
    )


def test_guard_hold_back_note_states_the_reversal(state):
    memory_obj = decision_memory.DecisionMemory()
    memory_obj.record(
        state,
        SimpleNamespace(request_type="DeclareAttackers", source_label="guard", options=[]),
        GUARD_HOLD_LABELS,
        reasoning="",
    )
    note = held_blocker_reconciliation(memory_obj.this_turn(state), ["Boo"])
    assert note == "Attacking with Boo reverses this turn's hold-back."


def test_committed_blocker_reasoning_states_the_reversal_with_the_snippet(state):
    memory_obj = decision_memory.DecisionMemory()
    memory_obj.record(
        state,
        SimpleNamespace(request_type="ChooseAbility", source_label="model", options=[]),
        ["Boo"],
        reasoning=COMMITTED_REASONING,
    )
    note = held_blocker_reconciliation(memory_obj.this_turn(state), ["Boo"])
    assert note.startswith('Earlier this turn you committed Boo as a blocker ("+1 pumps')
    assert COMMITTED_REASONING in note
    assert "attacking with it reverses that commitment" in note


def test_outage_fallback_records_are_history_not_commitments(state):
    memory_obj = decision_memory.DecisionMemory()
    memory_obj.record(
        state,
        SimpleNamespace(request_type="ChooseAbility", source_label="fallback", options=[]),
        ["Boo"],
        reasoning=COMMITTED_REASONING,
        model_unavailable=True,
    )
    assert held_blocker_reconciliation(memory_obj.this_turn(state), ["Boo"]) == ""


def test_mention_without_blocking_intent_is_not_a_commitment(state):
    memory_obj = decision_memory.DecisionMemory()
    memory_obj.record(
        state,
        SimpleNamespace(request_type="ChooseAbility", source_label="model", options=[]),
        ["Boo"],
        reasoning="+1 puts a counter on boo; it grows.",
    )
    assert held_blocker_reconciliation(memory_obj.this_turn(state), ["Boo"]) == ""


def test_other_holds_do_not_leak_into_this_attack(state):
    memory_obj = decision_memory.DecisionMemory()
    memory_obj.record(
        state,
        SimpleNamespace(request_type="DeclareAttackers", source_label="guard", options=[]),
        ["attack held by guard: Keen-Eyed Curator not attacking"],
        reasoning="",
    )
    assert held_blocker_reconciliation(memory_obj.this_turn(state), ["Boo"]) == ""
