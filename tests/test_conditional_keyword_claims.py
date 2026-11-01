"""Conditional reach claim found while reproducing historical #138.

The original synthetic negative control produced 'Viashino Pyromancer has no
reach' for 'If Viashino Pyromancer gains reach, it is a blocker for Slickshot.'
Actual selections must survive this narration-only check unchanged.
"""

from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import _claim_corrections, corrected_decision_reasoning, decision_claim_notes

CARD = dict(
    instance_id=313,
    name="Viashino Pyromancer",
    controller_seat_id=1,
    owner_seat_id=1,
    type_line="Creature — Lizard Wizard",
    power=2,
    toughness=1,
    oracle_text="When this creature enters, it deals 2 damage to target player or planeswalker.",
)
CASE = "If Viashino Pyromancer gains reach, it is a blocker for Slickshot."


def state(reach=False):
    card = dict(CARD)
    if reach:
        card["keywords"] = ["reach"]
    return dict(
        local_seat_id=2, turn=dict(turn_number=7, active_player=2), battlefield=[card], hand=[], stack=[]
    )


@pytest.mark.parametrize(
    "text",
    [
        CASE,
        "If we give it an Aura, Viashino Pyromancer gains reach.",
        "Unless Viashino Pyromancer has reach, attack with the flier.",
        "When Viashino Pyromancer gains reach, hold back.",
        "Assuming Viashino Pyromancer has reach, hold back.",
        "Viashino Pyromancer gains reach if we enchant it.",
        "Viashino Pyromancer has reach only if the grant resolves.",
        "Viashino Pyromancer has reach, if the grant resolves.",
        "Viashino Pyromancer would gain reach if enchanted.",
        "If Viashino Pyromancer (reach, flying) gets the grant, hold back.",
        "Viashino Pyromancer (reach, flying) only if the grant resolves.",
    ],
)
def test_conditional_keyword_is_not_reported_as_a_current_fact(text):
    assert decision_claim_notes(text, state()) == []
    assert corrected_decision_reasoning(text, state()) == text


@pytest.mark.parametrize(
    "reach,text,flag",
    [
        (False, "Viashino Pyromancer has reach.", True),
        (False, "Viashino Pyromancer gained reach.", True),
        (False, "Viashino Pyromancer (reach) blocks the flier.", True),
        (False, "Viashino Pyromancer has no reach.", False),
        (True, "Viashino Pyromancer has reach.", False),
        (True, "Viashino Pyromancer has no reach.", True),
        (True, "If Viashino Pyromancer has no reach, attack.", False),
        (True, "Viashino Pyromancer has no reach unless its Aura resolves.", False),
        (False, "Viashino Pyromancer does not have reach.", False),
    ],
)
def test_asserted_and_negated_keyword_controls(reach, text, flag):
    assert bool(decision_claim_notes(text, state(reach))) is flag


@pytest.mark.parametrize(
    "text",
    [
        "If we draw burn, cast it. Viashino Pyromancer has reach.",
        "If we draw burn, cast it; Viashino Pyromancer has reach.",
        "If we draw burn, cast it | Viashino Pyromancer has reach.",
        "If we draw burn, cast it\nViashino Pyromancer has reach.",
        "If we draw burn, cast it, but Viashino Pyromancer has reach.",
        "Viashino Pyromancer has reach, so if we attack, it can block.",
        "Viashino Pyromancer has reach, but if we remove it, attack.",
        "Viashino Pyromancer has reach because its ability grants it.",
    ],
)
def test_unrelated_condition_does_not_hide_an_asserted_false_fact(text):
    assert decision_claim_notes(text, state())


def test_plan_text_preserves_hypothesis_but_corrects_separate_assertion():
    text = CASE + " Viashino Pyromancer has flying."
    snapshot = state()
    corrected, notes = _claim_corrections(
        text,
        ours=[],
        theirs=snapshot["battlefield"],
        hand=[],
        battlefield=snapshot["battlefield"],
        our_turn=True,
    )
    assert corrected.startswith(CASE)
    assert not any("reach" in note for note in notes)
    assert any("has no flying" in note for note in notes)


def test_conditional_note_keeps_selection_and_reasoning_unchanged():
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = CASE
    planner._last_decision_trace = {}
    decision = PendingDecision((1, 2), "SelectTargets", (DecisionOption("tgt:313", "Viashino Pyromancer"),))
    chosen = ["tgt:313"]
    planner._note_false_claims(state(), decision, chosen)
    assert chosen == ["tgt:313"]
    assert planner._last_decision_reasoning == CASE
    assert planner._last_decision_trace == {}


@pytest.mark.parametrize(
    "text,flag",
    [
        ("If they have no reach, attack.", False),
        ("They have no reach unless the Aura resolves.", False),
        ("There are no blockers if they tap out.", False),
        ("They have no reach.", True),
        ("If they tap out, attack. They have no reach.", True),
        ("There are no blockers, so if we attack, we win.", True),
    ],
)
def test_session_reach_checker_uses_same_condition_scope(text, flag):
    from arenamcp.session_facts import claim_notes

    assert bool(claim_notes(text, state(reach=True))) is flag
