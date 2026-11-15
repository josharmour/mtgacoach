"""N1 preparation: printed Griselbrand evidence from bug_20261109_212434.

Other cards below are explicitly synthetic source-contract fixtures, not
semantic acceptance records or a model-generated cohort.
"""

import copy

import pytest

from arenamcp.card_facts.narrative import attach_layers, build_layer_prompt, validate_layers
from arenamcp.card_facts.schema import card_facts_from_dict, card_facts_to_dict

SOURCES = {
    "Griselbrand": "Flying, lifelink\nPay 7 life: Draw seven cards.",
    "Fixture Payoff": "Whenever you draw a card, gain 1 life.",
}


def claim(card="Griselbrand", quote="Pay 7 life: Draw seven cards."):
    return {
        "text": "Pay 7 life to draw seven cards.",
        "origin": "model",
        "evidence": [{"card": card, "quote": quote}],
    }


def threat():
    return {"version": 1, "plan": [claim()], "clock": [], "answers": []}


def links():
    c = claim()
    c["text"] = "Drawing cards enables the other card's life-gain trigger."
    c["evidence"].append({"card": "Fixture Payoff", "quote": SOURCES["Fixture Payoff"]})
    return {"version": 1, "links": [{"card": "Fixture Payoff", "role": "enabler", "claim": c}]}


def test_valid_layers_roundtrip_without_mutating_sources():
    record = {"name": "Griselbrand", "threat": {}, "relationships": {}}
    t, r = threat(), links()
    original = copy.deepcopy((record, t, r, SOURCES))
    result = attach_layers(record, t, r, SOURCES)
    assert result["threat"] == t
    assert card_facts_to_dict(card_facts_from_dict(result))["relationships"] == r
    result["threat"]["plan"][0]["text"] = "mutated"
    assert (record, t, r, SOURCES) == original


@pytest.mark.parametrize("value", [None, [], "invalid", 1, {"version": True}, {"version": 2}, {"version": 1}])
def test_invalid_layers_rejected(value):
    assert validate_layers(value, {}, SOURCES, card_name="Griselbrand")
    assert validate_layers({}, value, SOURCES, card_name="Griselbrand")


@pytest.mark.parametrize(
    "field,value",
    [
        ("origin", "derived"),
        ("text", ""),
        ("evidence", []),
        ("evidence", None),
        ("evidence", [{"card": "Unknown", "quote": "x"}]),
        ("evidence", [{"card": "Griselbrand", "quote": "Draw eight cards."}]),
        ("evidence", [None]),
    ],
)
def test_claim_failures(field, value):
    t = threat()
    t["plan"][0][field] = value
    with pytest.raises(ValueError):
        attach_layers({"name": "Griselbrand"}, t, {}, SOURCES)


@pytest.mark.parametrize(
    "field,value",
    [
        ("card", []),
        ("card", "Griselbrand"),
        ("card", "Unknown"),
        ("role", []),
        ("role", "best_deck"),
        ("claim", None),
    ],
)
def test_invalid_relationships(field, value):
    r = links()
    r["links"][0][field] = value
    assert validate_layers({}, r, SOURCES, card_name="Griselbrand")


def test_pair_must_cite_both_sources():
    r = links()
    r["links"][0]["claim"]["evidence"].pop()
    assert validate_layers({}, r, SOURCES, card_name="Griselbrand")


def test_threat_cannot_borrow_another_cards_ability():
    t = threat()
    t["plan"] = [claim("Fixture Payoff", SOURCES["Fixture Payoff"])]
    assert validate_layers(t, {}, SOURCES, card_name="Griselbrand")


def test_legacy_empty_layers_supported():
    assert validate_layers({}, {}, SOURCES, card_name="Griselbrand") == []


def test_prompt_is_deterministic_and_preserves_source_text():
    prompt = build_layer_prompt("Griselbrand", SOURCES)
    assert prompt == build_layer_prompt("Griselbrand", dict(reversed(list(SOURCES.items()))))
    assert "Pay 7 life: Draw seven cards." in prompt
    assert "Do not label claims derived" in prompt
    assert "additional cards" in prompt
    with pytest.raises(ValueError):
        build_layer_prompt("Absent", SOURCES)
