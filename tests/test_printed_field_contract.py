import pytest

from arenamcp.ability_synthesizer import Cost
from arenamcp.card_facts import CardFacts, Mode, PrintedCard, validate


@pytest.mark.parametrize("type_line", ["", "Artifact", "Creature — Elf"])
def test_mismatched_printed_type_rejected(type_line):
    record = CardFacts(name="Example", mana_cost="{1}", type_line=type_line)
    problems = validate(record, PrintedCard("Example", mana_cost="{1}", type_line="Artifact Creature — Elf"))
    assert any(p.startswith("type_line:") for p in problems)


def test_normalized_type_and_unknown_modes_preserved():
    record = CardFacts(name="Example", mana_cost="{1}", type_line="  ARTIFACT   CREATURE — Elf ")
    assert (
        validate(record, PrintedCard("Example", mana_cost="{1}", type_line="Artifact Creature — Elf")) == []
    )
    assert record.modes == ()


@pytest.mark.parametrize("printed,cost", [("{0}", ""), ("{1}{R}", ""), ("{1}{R}", "{R}")])
def test_declared_cast_must_retain_printed_cost(printed, cost):
    record = CardFacts(
        name="Example",
        mana_cost=printed,
        type_line="Artifact",
        modes=(Mode(name="cast", cost=Cost(mana=cost)),),
    )
    assert any(
        "cast cost" in p
        for p in validate(record, PrintedCard("Example", mana_cost=printed, type_line="Artifact"))
    )


@pytest.mark.parametrize("printed,cost", [("", ""), ("{0}", "{0}"), ("{1}{R}", " {1}{r} ")])
def test_matching_cast_cost_preserved(printed, cost):
    record = CardFacts(
        name="Example",
        mana_cost=printed,
        type_line="Artifact",
        modes=(Mode(name="cast", cost=Cost(mana=cost)),),
    )
    assert validate(record, PrintedCard("Example", mana_cost=printed, type_line="Artifact")) == []


def test_alternate_mode_not_replaced_by_printed_cost():
    record = CardFacts(
        name="Example",
        mana_cost="{5}",
        type_line="Artifact",
        modes=(Mode(name="alternate", cost=Cost(mana="{1}")),),
    )
    assert validate(record, PrintedCard("Example", mana_cost="{5}", type_line="Artifact")) == []
