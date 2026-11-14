"""Synthetic preparatory renderer controls; not E3 size acceptance."""

import copy
from dataclasses import replace

import pytest

from arenamcp.ability_synthesizer import Cost, UnknownEffect
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.prompt_render import render, source_text
from arenamcp.card_facts.schema import CardBody, CardFacts, EffectEntry, Mode


def sample():
    card = dict(
        name="Air Cadet",
        mana_cost="{2}{U}",
        type_line="Creature — Bird",
        power="2",
        toughness="3",
        oracle_text="Flying",
    )
    record = CardFacts(
        name=card["name"],
        mana_cost=card["mana_cost"],
        type_line=card["type_line"],
        rules_hash=rules_hash(card["name"], card["mana_cost"], card["type_line"], card["oracle_text"]),
        confidence="high",
        body=CardBody(power=2, toughness=3, keywords=("Flying",)),
        modes=(Mode(name="cast", cost=Cost(mana="{2}{U}"), cost_text="{2}{U}"),),
    )
    return card, record, source_text(card)


def test_default_off_and_role_guards_preserve_exact_text():
    card, record, text = sample()
    assert render(card, record, text) == text
    for role in ("option", "permanent", "stack", "hand", "unknown"):
        assert render(card, record, text, enabled=True, role=role) == text


def test_supported_body_preserves_characteristics_without_mutation():
    card, record, text = sample()
    before = copy.deepcopy((card, record))
    out = render(card, record, text, enabled=True, role="stable_deck")
    assert out == "Air Cadet — {2}{U}; Creature — Bird; 2/3; Flying"
    assert len(out) < len(text)
    assert (card, record) == before


@pytest.mark.parametrize(
    "kind",
    [
        "unknown",
        "modal",
        "cost",
        "hash",
        "face",
        "extension",
        "reserved",
        "notes",
        "body",
        "oracle",
        "annotated",
        "failed",
    ],
)
def test_unsupported_information_keeps_original_exactly(kind):
    card, record, text = sample()
    if kind == "unknown":
        record.effects = (EffectEntry(effect=UnknownEffect(text="Draw a card.")),)
    elif kind == "modal":
        record.modes += (Mode(name="evoke"),)
    elif kind == "cost":
        record.modes[0].cost = replace(record.modes[0].cost, life=2)
    elif kind == "hash":
        record.rules_hash = "0" * 64
    elif kind == "face":
        card["related_faces"] = [dict(oracle_text="Draw two cards.")]
    elif kind == "extension":
        record.body._unknown_fields = {"obligation": "Extra cost"}
    elif kind == "reserved":
        record.relationships = {"condition": "Other card required"}
    elif kind == "notes":
        record.notes = "Additional obligation"
    elif kind == "body":
        record.body.power = 4
    elif kind == "oracle":
        card["oracle_text"] = "Flying, ward {2}"
    elif kind == "annotated":
        text += "\nDo not spend this before combat."
    elif kind == "failed":
        record.confidence = "failed"
    assert render(card, record, text, enabled=True, role="stable_deck") == text


def test_missing_record_and_arbitrary_source_keep_all_modal_text():
    text = "Choose one — attack; or defend. Other face: flying. Option 7."
    assert render({}, None, text, enabled=True, role="stable_deck") == text
