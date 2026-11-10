"""Tests for the card-facts record schema, hashing and validator.

Records are built by hand for Fury, Minsc & Boo, Chrome Mox and Gemstone
Caverns from their printed text (Scryfall oracle text, fetched 2026-11-10;
Fury's evoke line is "Evoke—Exile a red card from your hand."). Validation
failure cases use those same cards plus Griselbrand ("Flying, lifelink"),
whose 7/7 body and flying/lifelink keywords are the printed facts the coach
mis-modelled in bug report bug_20261109_212434.
"""

from __future__ import annotations

import hashlib

import pytest

from arenamcp.ability_synthesizer import Cost, Damage, UnknownEffect
from arenamcp.card_facts import (
    EFFECT_TYPES,
    CardBody,
    CardFacts,
    EffectEntry,
    ManaProduction,
    Mode,
    PrintedCard,
    card_facts_from_dict,
    card_facts_to_dict,
    from_json,
    rules_hash,
    to_json,
    validate,
)


# ── Hand-built records from printed card text ────────────────────────────────


def fury_record() -> CardFacts:
    return CardFacts(
        name="Fury",
        mana_cost="{3}{R}{R}",
        type_line="Creature — Elemental Incarnation",
        body=CardBody(power="3", toughness="3", keywords=("Double strike",)),
        modes=(
            Mode(name="cast", cost=Cost(mana="{3}{R}{R}"), stays_on_battlefield=True),
            Mode(
                name="evoke",
                cost_text="Exile a red card from your hand.",
                stays_on_battlefield=False,
            ),
        ),
        effects=(
            EffectEntry(
                trigger="ENTERS_THE_BATTLEFIELD_OR_EVOKE",
                effect=Damage(
                    amount=4,
                    target="divided among any number of target creatures"
                    " and/or planeswalkers",
                ),
            ),
        ),
        compiled_by="hand",
        compiled_at="2026-11-10",
        confidence="high",
    )


def minsc_boo_record() -> CardFacts:
    return CardFacts(
        name="Minsc & Boo, Timeless Heroes",
        mana_cost="{2}{R}{G}",
        type_line="Legendary Planeswalker — Minsc",
        body=CardBody(loyalty="3"),
        modes=(
            Mode(name="+1", stays_on_battlefield=True),
            Mode(name="-2", stays_on_battlefield=True),
        ),
        effects=(
            EffectEntry(
                trigger="ENTERS_THE_BATTLEFIELD_OR_UPKEEP",
                effect=UnknownEffect(
                    text="you may create Boo, a legendary 1/1 red Hamster"
                    " creature token with trample and haste"
                ),
            ),
            EffectEntry(
                trigger="",
                effect=UnknownEffect(
                    text="+1: Put three +1/+1 counters on up to one target"
                    " creature with trample or haste"
                ),
            ),
            EffectEntry(
                trigger="",
                effect=UnknownEffect(
                    text="-2: Sacrifice a creature. When you do, Minsc & Boo"
                    " deals X damage to any target, where X is that creature's"
                    " power. If the sacrificed creature was a Hamster, draw X"
                    " cards"
                ),
            ),
        ),
        compiled_by="hand",
        compiled_at="2026-11-10",
        confidence="high",
    )


def chrome_mox_record() -> CardFacts:
    return CardFacts(
        name="Chrome Mox",
        mana_cost="{0}",
        type_line="Artifact",
        modes=(
            Mode(
                name="activate",
                cost=Cost(tap=True),
                stays_on_battlefield=True,
            ),
        ),
        effects=(
            EffectEntry(
                trigger="ENTERS_THE_BATTLEFIELD",
                effect=UnknownEffect(
                    text="Imprint — you may exile a nonartifact, nonland card"
                    " from your hand"
                ),
            ),
            EffectEntry(
                trigger="ACTIVATED",
                effect=UnknownEffect(
                    text="Add one mana of any of the exiled card's colors"
                ),
            ),
        ),
        mana=(ManaProduction(mana="any of the exiled card's colors"),),
        compiled_by="hand",
        compiled_at="2026-11-10",
        confidence="high",
    )


def gemstone_caverns_record() -> CardFacts:
    return CardFacts(
        name="Gemstone Caverns",
        mana_cost="",
        type_line="Legendary Land",
        modes=(
            Mode(name="activate", cost=Cost(tap=True), stays_on_battlefield=True),
        ),
        effects=(
            EffectEntry(
                trigger="ACTIVATED",
                effect=UnknownEffect(text="Add {C}."),
            ),
            EffectEntry(
                trigger="ACTIVATED_WITH_LUCK_COUNTER",
                effect=UnknownEffect(text="instead add one mana of any color"),
            ),
        ),
        mana=(
            ManaProduction(mana="{C}"),
            ManaProduction(mana="one mana of any color", condition="has a luck counter on it"),
        ),
        compiled_by="hand",
        compiled_at="2026-11-10",
        confidence="high",
    )


PRINTED_CARDS = {
    "Fury": PrintedCard(
        name="Fury",
        mana_cost="{3}{R}{R}",
        type_line="Creature — Elemental Incarnation",
        oracle_text=(
            "Double strike\nWhen this creature enters, it deals 4 damage divided"
            " as you choose among any number of target creatures and/or"
            " planeswalkers.\nEvoke—Exile a red card from your hand."
        ),
        power="3",
        toughness="3",
    ),
    "Minsc & Boo, Timeless Heroes": PrintedCard(
        name="Minsc & Boo, Timeless Heroes",
        mana_cost="{2}{R}{G}",
        type_line="Legendary Planeswalker — Minsc",
        oracle_text=(
            "When Minsc & Boo enters and at the beginning of your upkeep, you may"
            " create Boo, a legendary 1/1 red Hamster creature token with trample"
            " and haste.\n+1: Put three +1/+1 counters on up to one target"
            " creature with trample or haste.\n−2: Sacrifice a creature. When you"
            " do, Minsc & Boo deals X damage to any target, where X is that"
            " creature's power. If the sacrificed creature was a Hamster, draw X"
            " cards.\nMinsc & Boo, Timeless Heroes can be your commander."
        ),
        loyalty="3",
    ),
    "Chrome Mox": PrintedCard(
        name="Chrome Mox",
        mana_cost="{0}",
        type_line="Artifact",
        oracle_text=(
            "Imprint — When this artifact enters, you may exile a nonartifact,"
            " nonland card from your hand.\n{T}: Add one mana of any of the"
            " exiled card's colors."
        ),
    ),
    "Gemstone Caverns": PrintedCard(
        name="Gemstone Caverns",
        mana_cost="",
        type_line="Legendary Land",
        oracle_text=(
            "If this card is in your opening hand and you're not the starting"
            " player, you may begin the game with Gemstone Caverns on the"
            " battlefield with a luck counter on it. If you do, exile a card from"
            " your hand.\n{T}: Add {C}. If Gemstone Caverns has a luck counter on"
            " it, instead add one mana of any color."
        ),
    ),
}


@pytest.mark.parametrize(
    "builder", [fury_record, minsc_boo_record, chrome_mox_record, gemstone_caverns_record],
    ids=["fury", "minsc-boo", "chrome-mox", "gemstone-caverns"],
)
def test_hand_built_records_validate_clean_against_printed_card(builder):
    record = builder()
    assert validate(record, PRINTED_CARDS[record.name]) == []


# ── Round-tripping ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "builder", [fury_record, minsc_boo_record, chrome_mox_record, gemstone_caverns_record],
    ids=["fury", "minsc-boo", "chrome-mox", "gemstone-caverns"],
)
def test_json_round_trip_preserves_the_record(builder):
    record = builder()
    restored = from_json(to_json(record))
    assert card_facts_to_dict(restored) == card_facts_to_dict(record)
    # The typed effect survives: Fury's Damage keeps amount and target.
    if record.name == "Fury":
        effect = restored.effects[0].effect
        assert isinstance(effect, Damage)
        assert effect.amount == 4


def test_unknown_fields_are_preserved_on_load():
    """An older client must survive a newer record: extra keys round-trip."""
    data = card_facts_to_dict(fury_record())
    data["future_layer"] = {"something": "new"}
    data["body"]["future_stat"] = 7
    data["modes"][0]["future_mode_field"] = True
    restored = card_facts_from_dict(data)
    reencoded = card_facts_to_dict(restored)
    assert reencoded["future_layer"] == {"something": "new"}
    assert reencoded["body"]["future_stat"] == 7
    assert reencoded["modes"][0]["future_mode_field"] is True


def test_every_synthesizer_effect_type_is_representable():
    """The registry must cover each effect class the ability synthesizer defines."""
    expected_tags = {
        "CreateToken", "GainLife", "Draw", "Damage", "Destroy", "Exile",
        "Counter", "Pump", "AddMana", "PutCounters", "ReturnFromGraveyard",
        "Bounce", "Unknown",
    }
    assert set(EFFECT_TYPES) == expected_tags


# ── Hashing stability ────────────────────────────────────────────────────────


def test_rules_hash_is_stable_across_case_and_whitespace():
    """Same printed content in different spacing/case must hash identically."""
    base = rules_hash("Fury", "{3}{R}{R}", "Creature — Elemental Incarnation", "Double strike")
    messy = rules_hash(
        "  fury ",
        "{3}{r}{r}\n",
        "creature   — elemental incarnation\t",
        "\nDOUBLE STRIKE  ",
    )
    assert base == messy


def test_rules_hash_matches_the_documented_recipe_independently():
    """Recompute the documented recipe by hand and compare."""
    payload = "\n".join([
        "fury".casefold(),
        "{3}{r}{r}".casefold(),
        "creature — elemental incarnation".casefold(),
        "double strike".casefold(),
    ])
    expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert rules_hash("Fury", "{3}{R}{R}", "Creature — Elemental Incarnation", "Double strike") == expected


def test_rules_hash_changes_when_any_input_changes():
    h = rules_hash("Fury", "{3}{R}{R}", "Creature — Elemental Incarnation", "Double strike")
    assert rules_hash("Furry", "{3}{R}{R}", "Creature — Elemental Incarnation", "Double strike") != h
    assert rules_hash("Fury", "{2}{R}{R}", "Creature — Elemental Incarnation", "Double strike") != h
    assert rules_hash("Fury", "{3}{R}{R}", "Creature — Elemental Incarnation", "") != h


# ── Validation failures ──────────────────────────────────────────────────────


def test_wrong_toughness_is_reported():
    record = fury_record()
    record.body.toughness = "4"  # printed 3/3
    problems = validate(record, PRINTED_CARDS["Fury"])
    assert any("toughness" in p and "'4'" in p for p in problems)


def test_missing_keyword_is_reported():
    record = fury_record()
    record.body.keywords = ()  # printed text states Double strike
    problems = validate(record, PRINTED_CARDS["Fury"])
    assert any("'double strike'" in p and "missing" in p for p in problems)


def test_evoke_marked_as_staying_on_the_battlefield_is_reported():
    """Evoke removes the permanent (CR 702.37a); marking it as staying is wrong."""
    record = fury_record()
    record.modes = (
        record.modes[0],
        Mode(name="evoke", cost_text="Exile a red card from your hand.", stays_on_battlefield=True),
    )
    problems = validate(record, PRINTED_CARDS["Fury"])
    assert any("evoke" in p and "staying on the battlefield" in p for p in problems)


def test_cast_mode_cost_disagreeing_with_printed_cost_is_reported():
    record = fury_record()
    record.modes = (Mode(name="cast", cost=Cost(mana="{2}{R}{R}")),)
    problems = validate(record, PRINTED_CARDS["Fury"])
    assert any("cast cost" in p for p in problems)


def test_griselbrand_missing_both_stated_keywords_is_reported():
    """bug_20261109_212434: the coach mis-modelled Griselbrand (7/7 flying)."""
    griselbrand = PrintedCard(
        name="Griselbrand",
        mana_cost="{4}{B}{B}{B}{B}",
        type_line="Legendary Creature — Demon",
        oracle_text="Flying, lifelink\nPay 7 life: Draw seven cards.",
        power="7",
        toughness="7",
    )
    record = CardFacts(
        name="Griselbrand",
        mana_cost="{4}{B}{B}{B}{B}",
        type_line="Legendary Creature — Demon",
        body=CardBody(power="7", toughness="7"),  # keywords forgotten
    )
    problems = validate(record, griselbrand)
    assert any("'flying'" in p for p in problems)
    assert any("'lifelink'" in p for p in problems)


def test_keyword_not_stated_anywhere_is_reported():
    record = chrome_mox_record()
    record.body.keywords = ("Flying",)  # printed text never mentions flying
    problems = validate(record, PRINTED_CARDS["Chrome Mox"])
    assert any("'flying'" in p and "not stated" in p for p in problems)


def test_granted_keywords_are_not_demanded_of_the_card_itself():
    """Minsc & Boo grants trample/haste to the Boo token; the card itself has
    neither keyword and a correct record must not be penalised for omitting
    them."""
    problems = validate(minsc_boo_record(), PRINTED_CARDS["Minsc & Boo, Timeless Heroes"])
    assert not any("keyword" in p for p in problems)


def test_validate_never_raises_on_broken_records():
    record = fury_record()
    record.body.power = object()  # type: ignore[assignment]  # nonsense value
    record.modes = (Mode(name=None),)  # type: ignore[arg-type]
    problems = validate(record, PRINTED_CARDS["Fury"])
    assert isinstance(problems, list) and problems


def test_validate_flags_unknown_effect_without_original_sentence():
    record = fury_record()
    record.effects = (EffectEntry(trigger="", effect=UnknownEffect(text="")),)
    problems = validate(record, PRINTED_CARDS["Fury"])
    assert any("original sentence" in p for p in problems)
