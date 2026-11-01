"""Retained Finale cast rationale, recovered public board, supplied printed card facts."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.tutor_claims import correct_cast_tutor_blockers

FIXTURE = json.loads((Path(__file__).parent / "fixtures/gameplay_300_cast_tutor_blockers.json").read_text())
REASON = FIXTURE["reasoning"]


def context():
    raw = deepcopy(FIXTURE["pending_decision"])
    raw["request_id"] = tuple(raw["request_id"])
    raw["slots"] = tuple(raw["slots"])
    raw["options"] = tuple(DecisionOption(**o) for o in raw["options"])
    return deepcopy(FIXTURE["state"]), PendingDecision(**raw), list(FIXTURE["chosen"])


def test_retained_cast_rationale_replaces_false_blocking_payoff_with_requirements():
    state, decision, chosen = context()
    before = deepcopy(state)
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = REASON
    planner._last_decision_trace = {}
    planner._note_false_claims(state, decision, chosen)
    actual = planner._last_decision_reasoning
    assert "block the flying Angels" not in actual
    assert "with 7 mana it can fetch" not in actual
    assert "deck's tutor-to-battlefield engine" in actual
    for name, mv, payment in [("Kogla, the Titan Ape", 6, 8), ("Thorn Mammoth", 7, 9)]:
        assert f"{name} has neither flying nor reach" in actual
        assert f"mana value {mv} requires X at least {mv}" in actual
        assert f"printed payment of {payment} mana before cost modifiers" in actual
    assert "Confirm a payable X after cost modifiers" in actual
    assert planner._last_decision_trace["uncorrected_reasoning"] == REASON
    assert chosen == ["idx:2"] and state == before


@pytest.mark.parametrize(
    "text",
    [
        "Finale of Devastation can fetch Titan of Industry to block the flying Angels.",
        "Finale of Devastation can fetch Kogla to fight the flying Angels.",
        "Finale of Devastation can fetch Kogla to block ground attackers.",
        "Finale of Devastation can fetch Kogla to block the flying Angels if it gains reach.",
        "Finale of Devastation can fetch Kogla after equipping it to block the flying Angels.",
        "Finale of Devastation cannot fetch Kogla to block the flying Angels.",
        "Finale of Devastation is a tutor; Chord of Calling can fetch Kogla to block the flying Angels.",
        "Chord of Calling can fetch Kogla to block the flying Angels.",
        "Finale of Devastation with 7 mana can fetch Titan of Industry.",
    ],
)
def test_legal_conditional_other_spell_and_unknown_affordability_controls(text):
    state, decision, chosen = context()
    assert correct_cast_tutor_blockers(text, state, decision, chosen) == (text, [])


@pytest.mark.parametrize(
    "change",
    [
        "no_board",
        "unknown_permanent",
        "unknown_target_rules",
        "reach_grant",
        "block_exception",
        "source_flying_grant",
        "wrong_instance",
        "wrong_group",
        "pass",
        "no_oracle",
    ],
)
def test_unknown_or_external_grants_do_not_prove_false_blocking(change):
    state, decision, chosen = context()
    if change == "no_board":
        state.pop("battlefield")
    elif change == "unknown_permanent":
        state["battlefield"].append({"name": "Unknown", "type_line": "Enchantment"})
    elif change == "unknown_target_rules":
        for key in ["kogla, the titan ape", "thorn mammoth"]:
            state["deck_catalog"][key]["oracle_text"] = ""
    elif change == "reach_grant":
        state["battlefield"].append(
            {"name": "Anthem", "oracle_text": "Creatures you control have reach.", "type_line": "Enchantment"}
        )
    elif change == "block_exception":
        state["battlefield"].append(
            {
                "name": "Exception",
                "oracle_text": "Creatures can block as though they had flying.",
                "type_line": "Enchantment",
            }
        )
    elif change == "source_flying_grant":
        state["hand"][0]["oracle_text"] += " That creature gains flying."
    elif change == "wrong_instance":
        state["hand"][0]["instance_id"] = 999
    elif change == "wrong_group":
        state["hand"][0]["grp_id"] = 999
    elif change == "pass":
        chosen = ["pass"]
    elif change == "no_oracle":
        state["hand"][0]["oracle_text"] = ""
    assert correct_cast_tutor_blockers(REASON, state, decision, chosen) == (REASON, [])


def test_cost_reduction_does_not_make_printed_payment_an_affordability_verdict():
    state, decision, chosen = context()
    state["battlefield"].append(
        {"name": "Discount", "type_line": "Artifact", "oracle_text": "Spells you cast cost {2} less to cast."}
    )
    text, notes = correct_cast_tutor_blockers(REASON, state, decision, chosen)
    assert notes and "before cost modifiers" in text
    assert "cannot afford" not in text and "unpayable" not in text


def test_casting_kind_and_choice_are_required():
    state, decision, chosen = context()
    decision = replace(decision, request_type="Search")
    assert correct_cast_tutor_blockers(REASON, state, decision, chosen) == (REASON, [])


def test_unknown_duplicate_target_facts_abstain():
    state, decision, chosen = context()
    state["hand"].append({"name": "Kogla, the Titan Ape"})
    text = "Finale of Devastation can fetch Kogla to block the flying Angels."
    assert correct_cast_tutor_blockers(text, state, decision, chosen) == (text, [])


def test_named_ground_creature_is_not_the_flying_blocker_in_a_contrast():
    state, decision, chosen = context()
    text = (
        "Finale of Devastation can fetch Kogla for a fight but Titan of Industry to block the flying Angels."
    )
    assert correct_cast_tutor_blockers(text, state, decision, chosen) == (text, [])


def test_creatures_own_blocking_exception_is_not_erased():
    state, decision, chosen = context()
    state["deck_catalog"]["kogla, the titan ape"]["oracle_text"] += (
        " This creature can block as though it had flying."
    )
    text = "Finale of Devastation can fetch Kogla to block the flying Angels."
    assert correct_cast_tutor_blockers(text, state, decision, chosen) == (text, [])


def test_creatures_own_entering_grant_remains_possible():
    state, decision, chosen = context()
    state["deck_catalog"]["kogla, the titan ape"]["oracle_text"] += (
        " When this creature enters, it gains flying until end of turn."
    )
    text = "Finale of Devastation can fetch Kogla to block the flying Angels."
    assert correct_cast_tutor_blockers(text, state, decision, chosen) == (text, [])
