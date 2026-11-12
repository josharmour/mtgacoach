"""A "tapped enemy limits crackback" attack premise is corrected from the board (2026-11-12 T8).

Gar vs Minsc, turn 8: the plan attacked our hasty 4/4 Boo at their 3-loyalty
Minsc saying "Kill their Minsc engine; their big Boo is tapped so crackback is
limited." Three things are wrong with that premise, generically:

- their untapped Keen-Eyed Curator 3/3 and Delighted Halfling 2/3 can block
  THIS attack now — a double block kills the 4/4 and, even through trample,
  leaves the walker standing — so the planeswalker kill was never guaranteed;
- their big Boo untaps normally before their next combat, so being tapped now
  says nothing about their NEXT attackers — a threat that stands even when
  their whole board is tapped today;
- our own earlier decisions this turn committed the pumped Boo as a blocker,
  so spending it on attack reverses that commitment and the reasoning must
  say what changed or why spending it beats keeping it home.

The correction is narration-only: the pick stands, the reasoning gains the
board facts. Legal tactical attacks are never overridden and holding is never
claimed to win.

REAL PATH (review finding on bb0673c): the recorded turn-8 request arrived via
the GRE bridge with EMPTY typed options — ``build_pending_decision`` returns
None for every attack alias on that shape — so the typed-decision hook
(``_correct_tapped_crackback_claim``) never runs on it. The regressions below
drive the plan-guard chain entry (``_check_attack_premises``) with the
recorded declaration shape and assert the corrected reasoning lands on the
DECLARE_ATTACKERS action — the field ``_record_combat_decision`` records
first.

Fixture: the VERBATIM recorded turn-8 DeclareAttackers state_snapshot — their
7/7 Boo still tapped, Curator and Halfling still untapped, exactly as recorded.
The correction must fire on that real state, not on a normalized copy.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.attack_premises import (
    best_block_clause,
    held_blocker_reconciliation,
    tapped_crackback_correction,
)
from arenamcp.decision_memory import DecisionMemory

FIXTURE = Path(__file__).parent / "fixtures" / "gar_attack_consistency_20261112_turn8_declareattackers.json"

REASONING = "Kill their Minsc engine; their big Boo is tapped so crackback is limited."
STRATEGY = "Trade our hasty 4/4 Boo into their planeswalker while their 7/7 trampler is tapped."
CHOSEN = ["Boo [id:978] -> Minsc & Boo, Timeless Heroes [760]"]
TARGET = "Minsc & Boo, Timeless Heroes"


def _fixture():
    return json.loads(FIXTURE.read_text())


def _fixture_state():
    return _fixture()["state"]


def _our_boos(state):
    return [
        card
        for card in state["battlefield"]
        if card.get("controller_seat_id") == 2 and card.get("name") == "Boo"
    ]


def _their_untapped(state):
    return [
        card
        for card in state["battlefield"]
        if card.get("controller_seat_id") == 1
        and "Creature" in str(card.get("type_line") or "")
        and not card.get("is_tapped")
    ]


def _planner(memory: DecisionMemory | None = None) -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = REASONING
    planner._last_decision_trace = {}
    planner._decision_memory = memory if memory is not None else DecisionMemory()
    return planner


def _declare_attackers_decision() -> SimpleNamespace:
    return SimpleNamespace(request_type="DeclareAttackers")


def _recorded_plan() -> tuple[ActionPlan, GameAction]:
    """The recorded turn-8 plan shape: one DECLARE_ATTACKERS action via the GRE bridge."""
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Boo"],
        attacker_instance_ids=[978],
        attacker_targets={"Boo": TARGET},
        target_names=[TARGET],
        reasoning=REASONING,
        model_choice=True,
    )
    return ActionPlan(actions=[action], overall_strategy=STRATEGY), action


# --- premise detection -----------------------------------------------------------------------


def test_recorded_t8_premise_yields_both_independent_facts():
    state = _fixture_state()
    # The recorded snapshot really does carry both halves of the contradiction:
    # their 7/7 Boo tapped, and untapped creatures that can block right now.
    theirs = [
        card
        for card in state["battlefield"]
        if card.get("controller_seat_id") == 1 and "Creature" in str(card.get("type_line") or "")
    ]
    assert any(card.get("is_tapped") for card in theirs)
    assert {card.get("name") for card in _their_untapped(state)} >= {
        "Keen-Eyed Curator",
        "Delighted Halfling",
    }

    correction = tapped_crackback_correction(state, REASONING)
    assert correction
    # Current blockers: who can block this attack NOW.
    assert "can still block this attack now" in correction
    assert "Keen-Eyed Curator 3/3" in correction
    assert "Delighted Halfling 2/3" in correction
    # Next combat: tapped is not tapped then.
    assert "Tapped now is not tapped next combat" in correction
    assert "untap before their next attack" in correction


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


def test_all_enemy_creatures_tapped_still_flags_next_combat_threat():
    state = _fixture_state()
    for card in state["battlefield"]:
        if card.get("controller_seat_id") == 1 and "Creature" in str(card.get("type_line") or ""):
            card["is_tapped"] = True
    # Even with their whole board tapped today, the untap step still brings
    # their attackers back before their next combat (review finding on bb0673c).
    correction = tapped_crackback_correction(state, REASONING)
    assert correction
    assert "Tapped now is not tapped next combat" in correction
    assert "untap before their next attack" in correction
    assert "can still block this attack now" not in correction


def test_no_enemy_creature_tapped_still_flags_current_blockers():
    state = _fixture_state()
    for card in state["battlefield"]:
        card["is_tapped"] = False
    # The premise cites a tapped creature that is not there; the block threat
    # it dismissed stands regardless.
    correction = tapped_crackback_correction(state, REASONING)
    assert correction
    assert "can still block this attack now" in correction
    assert "Tapped now is not tapped next combat" not in correction


# --- best-block arithmetic ---------------------------------------------------------------------


def test_best_block_math_on_the_recorded_board():
    state = _fixture_state()
    boo = _our_boos(state)[0]
    clause = best_block_clause(boo, _their_untapped(state), TARGET)
    # Their pooled blockers (3/3 + 2/3) kill the 4/4 ...
    assert "together their 5 power would kill the 4/4 attacker" in clause
    # ... and even trample leaves the walker standing (4 power minus 6 lethal).
    assert f"even trample leaves {TARGET} unharmed this combat" in clause
    assert "(4 power minus 6 lethal blocker damage)" in clause


def test_weak_block_lets_trample_through():
    state = _fixture_state()
    boo = _our_boos(state)[0]
    halfling = next(c for c in state["battlefield"] if c.get("name") == "Delighted Halfling")
    clause = best_block_clause(boo, [halfling], TARGET)
    assert "their best block cannot kill the 4/4 attacker" in clause
    # The Halfling is 2/3: assigning its 3 lethal leaves 1 of Boo's 4 power.
    assert f"only 1 damage tramples through to {TARGET}" in clause


def test_blocked_non_trample_attack_deals_no_damage():
    attacker = {"name": "Grizzly Bears", "power": 2, "toughness": 2}
    blocker = {"name": "Wall", "power": 1, "toughness": 4}
    clause = best_block_clause(attacker, [blocker], "Their Planeswalker")
    assert "their best block cannot kill the 2/2 attacker" in clause
    assert "a blocked attack deals no damage to Their Planeswalker this combat" in clause


def test_missing_attacker_stats_yield_no_math():
    assert (
        best_block_clause({"name": "?", "power": None, "toughness": None}, [{"power": 1, "toughness": 1}])
        == ""
    )
    assert best_block_clause({"name": "?", "power": 2, "toughness": 2}, []) == ""


def test_multi_attacker_declarations_get_the_qualitative_clause_only():
    state = _fixture_state()
    boo = _our_boos(state)[0]
    pair = [boo, {"name": "Slickshot Show-Off", "power": 2, "toughness": 2}]
    correction = tapped_crackback_correction(state, REASONING, attackers=pair)
    assert correction
    # Combat arithmetic is bounded to single attackers; multi-attacker plans
    # get the who-can-block facts without a possibly-wrong pooled verdict.
    assert "can still block this attack now" in correction
    assert "would kill the" not in correction


# --- planner hook (narration-only) --------------------------------------------------------------


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
    planner = _planner()
    planner._last_decision_reasoning = reasoning

    planner._correct_tapped_crackback_claim(state, _declare_attackers_decision(), CHOSEN)

    assert planner._last_decision_reasoning == reasoning


# --- REAL PATH: the plan-guard chain entry ------------------------------------------------------


def test_recorded_gre_bridge_declaration_gains_board_facts_via_plan_guard():
    """Regression for the recorded path: empty typed options, GRE bridge, plan guard.

    The recorded turn-8 request reached the planner as a plan whose single
    DECLARE_ATTACKERS action carries the model's reasoning — never as a typed
    decision (``build_pending_decision`` returns None for every attack alias
    on that shape). The guard must correct THAT action's reasoning, which is
    what ``_record_combat_decision`` records first.
    """
    state = _fixture_state()
    plan, action = _recorded_plan()
    context = {"type": "declare_attackers", "legal_attacker_ids": [978]}
    planner = _planner()

    planner._check_attack_premises(plan, state, context)

    # Narration-only: one action, same pick, same target binding.
    assert len(plan.actions) == 1
    assert action.attacker_names == ["Boo"]
    assert action.attacker_instance_ids == [978]
    assert action.attacker_targets == {"Boo": TARGET}
    # The model's intent survives; the false premise gains all three facts.
    assert action.reasoning.startswith(REASONING)
    assert (
        "Their untapped Keen-Eyed Curator 3/3, Delighted Halfling 2/3 can still block this attack now"
        in action.reasoning
    )
    assert f"even trample leaves {TARGET} unharmed this combat" in action.reasoning
    assert "Tapped now is not tapped next combat" in action.reasoning


def test_plan_guard_resolves_attackers_from_legal_ids_when_instance_ids_missing():
    """The bridge binds instance ids late; resolution must not depend on them."""
    state = _fixture_state()
    plan, action = _recorded_plan()
    action.attacker_instance_ids = []
    context = {"type": "declare_attackers", "legal_attacker_ids": [978]}
    planner = _planner()

    planner._check_attack_premises(plan, state, context)

    # Without a resolved attacker card there is no pooled-power verdict, but
    # the who-can-block and untap facts still land.
    assert (
        "Their untapped Keen-Eyed Curator 3/3, Delighted Halfling 2/3 can still block this attack now"
        in action.reasoning
    )
    assert "Tapped now is not tapped next combat" in action.reasoning


def test_plan_guard_leaves_plans_without_the_premise_alone():
    state = _fixture_state()
    plan, action = _recorded_plan()
    action.reasoning = "Attack with Boo; it trades into the curator profitably."
    context = {"type": "declare_attackers", "legal_attacker_ids": [978]}
    planner = _planner()

    planner._check_attack_premises(plan, state, context)

    assert action.reasoning == "Attack with Boo; it trades into the curator profitably."


def test_plan_guard_ignores_non_declare_attackers_contexts_and_empty_declarations():
    state = _fixture_state()
    plan, action = _recorded_plan()
    planner = _planner()

    planner._check_attack_premises(plan, state, {"type": "blockers"})
    assert action.reasoning == REASONING

    empty = GameAction(action_type=ActionType.DECLARE_ATTACKERS)
    empty_plan = ActionPlan(actions=[empty], overall_strategy=STRATEGY)
    planner._check_attack_premises(empty_plan, state, {"type": "declare_attackers"})
    assert empty.reasoning == ""


def test_plan_guard_reversal_of_this_turns_blocker_commitment_is_named():
    """The model's own earlier decision reserved pumped Boo as a blocker."""
    state = _fixture_state()
    memory = DecisionMemory()
    memory.record(
        state,
        SimpleNamespace(request_type="ChooseAbility", source_label="model", options=[]),
        ["Boo"],
        "+1 pumps my upkeep Boo to 4/4, so blocking their 7/7 trampler absorbs 4 damage.",
    )
    plan, action = _recorded_plan()
    context = {"type": "declare_attackers", "legal_attacker_ids": [978]}
    planner = _planner(memory)

    planner._check_attack_premises(plan, state, context)

    assert "Earlier this turn you committed Boo as a blocker" in action.reasoning
    assert "blocking their 7/7 trampler absorbs 4 damage" in action.reasoning


# --- held-blocker reconciliation ---------------------------------------------------------------


def test_guard_hold_spent_by_the_attack_requires_a_rationale():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.note(state, "DeclareAttackers", "attack held by guard: Boo not attacking")

    note = held_blocker_reconciliation(memory.this_turn(state), ["Boo"])

    assert note == (
        "Attacking with Boo reverses this turn's hold-back; "
        "the reasoning must say what changed or why spending it now beats keeping it home."
    )


def test_model_commitment_from_an_earlier_decision_fires_with_the_snippet():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.record(
        state,
        SimpleNamespace(request_type="ChooseAbility", source_label="model", options=[]),
        ["Boo"],
        "+1 pumps my upkeep Boo to 4/4, so blocking their 7/7 trampler absorbs 4 damage.",
    )

    note = held_blocker_reconciliation(memory.this_turn(state), ["Boo"])

    assert note.startswith("Earlier this turn you committed Boo as a blocker")
    assert "blocking their 7/7 trampler absorbs 4 damage" in note


def test_outage_fallback_records_are_history_not_commitments():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.record(
        state,
        SimpleNamespace(request_type="DeclareAttackers", source_label="", options=[]),
        CHOSEN,
        "",
        model_unavailable=True,
    )

    assert not held_blocker_reconciliation(memory.this_turn(state), ["Boo"])


def test_mention_without_block_intent_is_not_a_commitment():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.record(
        state,
        SimpleNamespace(request_type="ChooseAbility", source_label="model", options=[]),
        ["Boo"],
        "+1 puts a counter on Boo; it grows to 4/4.",
    )

    assert not held_blocker_reconciliation(memory.this_turn(state), ["Boo"])


def test_other_holds_do_not_fire_for_this_attacker():
    state = _fixture_state()
    memory = DecisionMemory()
    memory.note(state, "DeclareAttackers", "attack held by guard: Slickshot Show-Off not attacking")

    assert not held_blocker_reconciliation(memory.this_turn(state), ["Boo"])
