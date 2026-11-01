"""Retained #300 cast wording; hand rules reconstructed from disclosed saved card."""

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.mode_claims import correct_cast_mode_reasoning

FIXTURE = json.loads((Path(__file__).parent / "fixtures/gameplay_300_charm_cast.json").read_text())
REASON = FIXTURE["reasoning"]


def context():
    raw = deepcopy(FIXTURE["pending_decision"])
    raw["request_id"] = tuple(raw["request_id"])
    raw["slots"] = tuple(raw["slots"])
    raw["options"] = tuple(DecisionOption(**o) for o in raw["options"])
    return {"local_seat_id": 2, "hand": [deepcopy(FIXTURE["card"])]}, PendingDecision(**raw)


def test_retained_cast_claim_through_planner_keeps_choice_and_original():
    state, decision = context()
    before = deepcopy(state)
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = REASON
    planner._last_decision_trace = {}
    chosen = ["idx:1"]
    planner._note_false_claims(state, decision, chosen)
    actual = planner._last_decision_reasoning
    assert "fight mode" not in actual
    assert "one-way" in actual and "before" in actual
    assert "does not establish the selected mode" in actual
    assert "can kill Kinnan" in actual  # No strategic or kill-feasibility verdict.
    assert "only payable play" in actual
    assert planner._last_decision_trace["uncorrected_reasoning"] == REASON
    assert chosen == ["idx:1"] and state == before


@pytest.mark.parametrize(
    "text",
    [
        "Charm's fight mode can kill Kinnan.",
        "Archdruid's Charm's fight mode causes reciprocal damage.",
    ],
)
def test_explicit_current_card_fight_claim(text):
    state, decision = context()
    actual, notes = correct_cast_mode_reasoning(text, state, decision, ["idx:1"])
    assert actual != text and notes
    assert "available mode" in actual and "no reciprocal damage" in actual
    assert "causes reciprocal damage" not in actual


@pytest.mark.parametrize(
    "text",
    [
        "Prey Upon's fight mode can kill Kinnan.",
        "Selesnya Charm's fight mode can kill Kinnan.",
        "If Charm's fight mode were available, we could remove Kinnan.",
        "Charm's fight mode would be good if it existed.",
        "Charm's fight mode is not a real fight.",
        "Charm's damage mode is not a fight.",
        "Charm's fight mode doesn’t cause reciprocal damage.",
        "Charm's land mode helps Prey Upon's damage plan, and its fight mode can kill Kinnan.",
        "Its fight mode can kill Kinnan.",
        "Fight mode can kill Kinnan.",
        "Charm's land mode helps; its fight mode can kill Kinnan.",
        "Charm's fight mode of Prey Upon can kill Kinnan.",
        "Charm's one-way damage mode can kill Kinnan.",
    ],
)
def test_negated_hypothetical_unbound_or_correct_claims_abstain(text):
    state, decision = context()
    assert correct_cast_mode_reasoning(text, state, decision, ["idx:1"]) == (text, [])


@pytest.mark.parametrize(
    "change",
    [
        "missing_rules",
        "wrong_instance",
        "wrong_group",
        "actual_fight",
        "mixed_fight",
        "duplicate_instance",
        "missing_card",
        "not_modal",
        "extra_rules",
        "ambiguous_short_name",
        "pass",
        "multiple",
    ],
)
def test_unknown_mismatched_or_ambiguous_cast_abstains(change):
    state, decision = context()
    card = state["hand"][0]
    chosen = ["idx:1"]
    if change == "missing_rules":
        card["oracle_text"] = ""
    elif change == "wrong_instance":
        card["instance_id"] = 999
    elif change == "wrong_group":
        card["grp_id"] = 999
    elif change == "actual_fight":
        card["oracle_text"] = (
            "Choose one —\n• Target creature you control fights target creature you don't control.\n• Draw a card."
        )
    elif change == "mixed_fight":
        card["oracle_text"] += "\n• Target creature you control fights another target creature."
    elif change == "duplicate_instance":
        state["hand"].append(deepcopy(card))
    elif change == "missing_card":
        state["hand"] = []
    elif change == "not_modal":
        card["oracle_text"] = (
            "Put a +1/+1 counter on target creature you control. It deals damage equal to its power to target creature you don't control."
        )
    elif change == "extra_rules":
        card["oracle_text"] += "\nThe opposing creature then deals damage back."
    elif change == "ambiguous_short_name":
        state["hand"].append({"name": "Another Charm", "instance_id": 999})
    elif change == "pass":
        chosen = ["pass"]
    elif change == "multiple":
        chosen = ["idx:1", "pass"]
    assert correct_cast_mode_reasoning(REASON, state, decision, chosen) == (REASON, [])
