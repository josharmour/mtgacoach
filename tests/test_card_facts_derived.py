import copy

import pytest

from arenamcp.ability_synthesizer import Draw, GainLife, UnknownEffect
from arenamcp.card_facts.derived import derive_threat, validate_derived_threat
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.narrative import validate_layers
from arenamcp.card_facts.schema import CardFacts, EffectEntry
from arenamcp.card_facts.validate import PrintedCard


def fixture(text="Draw two cards.", effect=None, type_line="Sorcery"):
    printed = PrintedCard("Test spell", "{2}{U}", type_line, text)
    record = CardFacts(
        name=printed.name,
        mana_cost=printed.mana_cost,
        type_line=type_line,
        rules_hash=rules_hash(printed.name, printed.mana_cost, type_line, text),
        effects=(EffectEntry(effect=effect or Draw(2)),),
        confidence="high",
    )
    return record, printed


@pytest.mark.parametrize(
    "text,effect",
    [("Draw two cards.", Draw(2)), ("Draw a card.", Draw(1)), ("You gain 7 life.", GainLife(7))],
)
def test_exact_projection(text, effect):
    record, printed = fixture(text, effect)
    before = copy.deepcopy(record)
    result = derive_threat(record, printed)
    assert len(result["plan"]) == 1
    assert result["plan"][0]["origin"] == "derived"
    assert result["plan"][0]["evidence"][0]["quote"] == text
    assert not result["clock"] and not result["answers"]
    assert record == before
    assert not validate_derived_threat(result, record, printed)
    assert validate_layers(
        result, {}, {printed.name: text}, card_name=printed.name
    )  # model input still forbidden


@pytest.mark.parametrize(
    "text,effect",
    [
        ("Draw two cards.", Draw(3)),
        ("Draw two cards. Discard a card.", Draw(2)),
        ("If you control a creature, draw two cards.", Draw(2)),
        ("Target player draws two cards.", Draw(2)),
        ("Draw two cards.", UnknownEffect(text="Draw two cards.")),
        ("You gain 7 life.", GainLife(8)),
        ("Draw a card.", Draw(True)),
        ("You gain 1 life.", GainLife(True)),
    ],
)
def test_unsupported(text, effect):
    record, printed = fixture(text, effect)
    assert not derive_threat(record, printed)["plan"]


@pytest.mark.parametrize("change", ["hash", "confidence", "trigger", "conditions", "permanent", "effects"])
def test_refusal(change):
    record, printed = fixture()
    if change == "hash":
        record.rules_hash = "wrong"
    elif change == "confidence":
        record.confidence = "low"
    elif change == "trigger":
        record.effects[0].trigger = "enters"
    elif change == "conditions":
        record.effects[0].conditions = ("if",)
    elif change == "permanent":
        record.type_line = printed.type_line = "Creature"
    elif change == "effects":
        record.effects += (EffectEntry(effect=Draw(2)),)
    assert not derive_threat(record, printed)["plan"]


def test_forgery_rejected():
    record, printed = fixture()
    result = derive_threat(record, printed)
    result["plan"][0]["text"] = "Wins the game immediately."
    assert validate_derived_threat(result, record, printed)


def test_boolean_not_version():
    record, printed = fixture()
    result = derive_threat(record, printed)
    result["version"] = True
    assert validate_derived_threat(result, record, printed)


@pytest.mark.parametrize("field", ["name", "mana_cost", "type_line", "oracle_text"])
@pytest.mark.parametrize("bad", [None, [], {}, True, 3])
def test_malformed_printed_hash_fields_fail_closed(field, bad):
    record, printed = fixture()
    setattr(printed, field, bad)
    assert not derive_threat(record, printed)["plan"]
