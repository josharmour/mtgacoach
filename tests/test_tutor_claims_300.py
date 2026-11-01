"""#300 Finale exact decisions/rationales; minimal source state reconstruction disclosed."""

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.tutor_claims import correct_tutor_claims

FIXTURE = json.loads((Path(__file__).parent / "fixtures/gameplay_300_finale_tutor.json").read_text())


def context(kind="numeric"):
    raw = deepcopy(FIXTURE[kind + "_decision"])
    raw["request_id"] = tuple(raw["request_id"])
    raw["slots"] = tuple(raw["slots"])
    raw["options"] = tuple(DecisionOption(**o) for o in raw["options"])
    return deepcopy(FIXTURE["state"]), PendingDecision(**raw), list(FIXTURE[kind + "_chosen"])


def test_exact_x4_explanation_cannot_fetch_six_and_seven_mana_creatures():
    state, decision, chosen = context()
    original = deepcopy(state)
    planner = ActionPlanner(Mock())
    planner._last_decision_reasoning = FIXTURE["numeric_reasoning"]
    planner._last_decision_trace = {}
    planner._note_false_claims(state, decision, chosen)
    actual = planner._last_decision_reasoning
    assert "biggest stabilizing creature" not in actual
    assert "block the flying Angels" not in actual
    assert "X<10 gives no alpha-strike bonus" in actual
    assert "At X=4" in actual and "mana value 4 or less" in actual
    for name, value in [("Kogla, the Titan Ape", 6), ("Thorn Mammoth", 7), ("Titan of Industry", 7)]:
        assert f"{name} (mana value {value})" in actual
    assert planner._last_decision_trace["uncorrected_reasoning"] == FIXTURE["numeric_reasoning"]
    assert chosen == ["idx:4"] and state == original


def test_exact_legal_rofellos_search_is_preserved():
    state, decision, chosen = context("search")
    text = FIXTURE["search_reasoning"]
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])
    assert chosen == ["sel:328"] and decision.selection_is_valid(chosen)


def test_search_uses_only_current_offers_not_a_guessed_x():
    state, decision, chosen = context("search")
    state.pop("stack")
    text = "Fetch Titan of Industry to stabilize. Keep the commander for later."
    actual, notes = correct_tutor_claims(text, state, decision, chosen)
    assert "not among this search's offered choices" in actual
    assert "Keep the commander for later." in actual
    assert "X=" not in actual and notes
    assert chosen == ["sel:328"]


@pytest.mark.parametrize(
    "text",
    [
        "X=4 cannot fetch Kogla or Thorn Mammoth.",
        "X=4 fetches no Titan of Industry.",
        "If we choose X=7, we could fetch Titan of Industry.",
        "X=7 fetches Titan of Industry.",
        "Fetch Rofellos instead of Titan of Industry.",
        "Fetch Rofellos rather than Thorn Mammoth.",
        "Fetch Kogla next turn.",
        "Thorn Mammoth would stabilize if we had more mana.",
        "Kogla is in the deck; fetch Rofellos now.",
        "Kogla is in the deck, fetch Rofellos now.",
        "Fetch Rofellos and hold Kogla.",
        "Fetch Rofellos while Kogla waits in hand.",
        "X=4 fetches Rofellos.",
        "X=4 fetches an unknown creature.",
    ],
)
def test_negated_hypothetical_future_and_legal_claims_survive(text):
    state, decision, chosen = context()
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])


@pytest.mark.parametrize(
    "change",
    [
        "no_source",
        "other_source",
        "source_mismatch",
        "missing_rules",
        "not_tutor",
        "wrong_numeric_kind",
        "unselected",
        "other_label",
        "other_child",
        "unknown_cost",
        "conflicting_cost",
    ],
)
def test_missing_or_inconsistent_evidence_abstains(change):
    state, decision, chosen = context()
    text = "X=4 fetches Kogla."
    source = state["stack"][0]
    if change == "no_source":
        state["stack"] = []
    elif change == "other_source":
        source["instance_id"] = 528
    elif change == "source_mismatch":
        decision = replace(decision, source_instance_id=528)
    elif change == "missing_rules":
        state["deck_catalog"]["69611"]["oracle_text"] = ""
    elif change == "not_tutor":
        source["oracle_text"] = "Deal X damage to any target."
    elif change == "unselected":
        chosen = ["missing"]
    elif change in {"wrong_numeric_kind", "other_label", "other_child"}:
        options = list(decision.options)
        option = options[4]
        if change == "wrong_numeric_kind":
            option = replace(option, meta={**option.meta, "choiceKind": "modal"})
        elif change == "other_label":
            option = replace(option, label="Choose 4 cards")
        else:
            option = replace(option, meta={**option.meta, "childIndex": 1})
        options[4] = option
        decision = replace(decision, options=tuple(options))
    elif change == "unknown_cost":
        card = state["deck_catalog"]["kogla, the titan ape"]
        card.pop("cmc")
        card.pop("mana_cost")
    elif change == "conflicting_cost":
        state["hand"] = [{"name": "Kogla, the Titan Ape", "cmc": 2}]
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])


def test_x7_can_fetch_titan_and_exact_limit_is_inclusive():
    state, decision, _ = context()
    text = "X=7 fetches Titan of Industry."
    assert correct_tutor_claims(text, state, decision, ["idx:7"]) == (text, [])
    text = "X=6 fetches Kogla."
    assert correct_tutor_claims(text, state, decision, ["idx:6"]) == (text, [])


def test_unknown_menu_identity_cannot_establish_absence():
    state, decision, chosen = context("search")
    options = list(decision.options)
    options[0] = replace(options[0], meta={**options[0].meta, "identity_known": False})
    decision = replace(decision, options=tuple(options))
    text = "Fetch Titan of Industry."
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])


def test_offered_card_wins_over_an_old_catalog_cost():
    state, decision, chosen = context("search")
    state["deck_catalog"]["rofellos"] = {"name": "Rofellos, Llanowar Emissary", "cmc": 9}
    text = "Fetch Rofellos."
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])


def test_same_short_name_as_a_legal_card_is_ambiguous():
    state, decision, chosen = context()
    state["deck_catalog"]["other"] = {"name": "Titan", "cmc": 2}
    text = "X=4 fetches Titan."
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])


def test_cast_stage_does_not_invent_x_from_available_mana():
    state, decision, chosen = context()
    decision = replace(decision, request_type="ActionsAvailable")
    text = "With 7 mana Finale can fetch Kogla."
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])


def test_known_mana_cost_fallback_does_not_treat_unknown_as_zero():
    state, decision, chosen = context()
    state["deck_catalog"]["kogla, the titan ape"].pop("cmc")
    actual, notes = correct_tutor_claims("X=4 fetches Kogla.", state, decision, chosen)
    assert notes and "mana value 6" in actual


def test_factual_source_rules_are_required_even_with_exact_x():
    state, decision, chosen = context()
    state["deck_catalog"] = {k: v for k, v in state["deck_catalog"].items() if k != "69611"}
    assert correct_tutor_claims(FIXTURE["numeric_reasoning"], state, decision, chosen) == (
        FIXTURE["numeric_reasoning"],
        [],
    )


def test_unknown_search_clause_exception_abstains():
    state, decision, chosen = context()
    state["stack"][0]["oracle_text"] = (
        "Search your library for a creature card with mana value X or less or any creature if you control a Forest."
    )
    text = "X=4 fetches Kogla."
    assert correct_tutor_claims(text, state, decision, chosen) == (text, [])
