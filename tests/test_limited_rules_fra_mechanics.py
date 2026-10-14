"""FRA mechanics in the rules-text synergy check (2026-10-13 Contender Draft).

The draft advisor stripped ten of the model's synergy claims as having "no
rules-text link"; three were real (Awaken the Inferno and Artifist Acumen feed
Saheeli's noncreature-spell Thopters, Semester Foreseer's surveil lets
Surveillance Phantasm attack). Card text below is Arena's, which omits
reminder text.
"""

import pytest

from arenamcp.limited_rules import rules_profile, synergy_evidence

FRA = {
    "Saheeli, Jewel of Avishkar": (
        "Legendary Creature - Human Artificer",
        "Thopters you control have haste.\nWhenever you cast a noncreature spell, create a 1/1 colorless "
        "Thopter artifact creature token with flying.",
    ),
    "Awaken the Inferno": (
        "Sorcery",
        "Awaken the Inferno deals 6 damage to target creature or planeswalker an opponent controls. Put a "
        "+1/+1 counter on up to one target creature you control.\nBasic landcycling {o2}",
    ),
    "Artifist Acumen": (
        "Sorcery",
        "Creatures you control gain first strike until end of turn.\nDraw a card.",
    ),
    "Semester Foreseer": (
        "Creature - Human Wizard",
        "This creature enters prepared.\nWhen this creature enters, surveil 1.",
    ),
    "Surveillance Phantasm": (
        "Creature - Bird Illusion",
        "Defender\nFlying\nVigilance\nAs long as you've scried or surveilled this turn, this creature can "
        "attack as though it didn't have defender.\n{o3oU}: Surveil 1.",
    ),
    "Fblthp, Impossibly Lost": (
        "Legendary Creature - Homunculus",
        "When one or more of your opponents are dealt combat damage during your turn, draw two cards.",
    ),
    "Afterthought Sentry": (
        "Artifact Creature - Construct",
        "{o2}: This creature gains flying until end of turn.",
    ),
    "Mindseeker Oculus": ("Creature - Homunculus", "When this creature enters, empower Jace 4."),
    "Countersculpt": (
        "Instant",
        "As an additional cost to cast this spell, behold a Jace or pay {o1}.\nCounter target spell. "
        "Empower Jace 1.",
    ),
    "Proft, Consulting Detective": (
        "Legendary Creature - Human Detective",
        "Whenever you scry or surveil, you may pay {o2}. If you do, put a +1/+1 counter on Proft and draw a card.",
    ),
    "Innovative Commons": (
        "Land",
        "This land enters tapped unless you control a planeswalker.\n{oT}: Add {oU} or {oR}.",
    ),
    "Cryotheory Adept": ("Creature - Human Wizard", "Prowess"),
    "Theorix Metamage": (
        "Creature - Shade Wizard",
        "This creature enters prepared.\nThreshold — This creature gets +1/+0 and has flying as long as there "
        "are seven or more cards in your graveyard.",
    ),
    "Eye of Jace": (
        "Artifact",
        "At the beginning of your upkeep, surveil 1. Then if there are seven or more cards in your graveyard, "
        "sacrifice this artifact.",
    ),
    "Inspired Tethermage": (
        "Creature - Human Wizard",
        "{o6}: Empower Jace 2.\nWhenever you put one or more loyalty counters on a planeswalker, put a +1/+1 "
        "counter on this creature.",
    ),
}


def card(name: str, grp_id: int = 0) -> dict:
    type_line, text = FRA[name]
    return {
        "grp_id": grp_id or hash(name) & 0xFFFF,
        "name": name,
        "type_line": type_line,
        "oracle_text": text,
    }


def mechanics(first: str, second: str) -> set[tuple[str, bool]]:
    return {
        (edge["mechanic"], bool(edge.get("indirect")))
        for edge in synergy_evidence(card(first, 1), card(second, 2))
    }


@pytest.mark.parametrize(
    "spell", ["Awaken the Inferno", "Artifist Acumen", "Countersculpt", "Semester Foreseer"]
)
def test_noncreature_spells_and_prepared_creatures_feed_saheeli(spell):
    assert ("noncreature_spell", False) in mechanics(spell, "Saheeli, Jewel of Avishkar")


def test_prowess_is_a_noncreature_spell_payoff():
    assert ("noncreature_spell", False) in mechanics("Artifist Acumen", "Cryotheory Adept")


def test_scried_or_surveilled_this_turn_is_a_payoff_condition():
    assert ("surveil", False) in mechanics("Semester Foreseer", "Surveillance Phantasm")


def test_empower_jace_links_to_behold_and_planeswalker_lands_and_surveil_via_the_token():
    assert ("jace", False) in mechanics("Mindseeker Oculus", "Countersculpt")
    assert ("planeswalker", False) in mechanics("Mindseeker Oculus", "Innovative Commons")
    assert ("loyalty", False) in mechanics("Mindseeker Oculus", "Inspired Tethermage")
    # The Jace token's [-1]: Surveil 1 is real support for Proft, but indirect.
    assert mechanics("Mindseeker Oculus", "Proft, Consulting Detective") == {("surveil", True)}


def test_self_mill_and_surveil_fill_threshold():
    assert ("graveyard", False) in mechanics("Eye of Jace", "Theorix Metamage")


@pytest.mark.parametrize(
    "first,second",
    [
        ("Fblthp, Impossibly Lost", "Saheeli, Jewel of Avishkar"),  # a creature is not a noncreature spell
        ("Afterthought Sentry", "Saheeli, Jewel of Avishkar"),  # an artifact creature neither
        ("Innovative Commons", "Saheeli, Jewel of Avishkar"),  # Saheeli is a creature, not a planeswalker
        ("Fblthp, Impossibly Lost", "Proft, Consulting Detective"),  # drawing is not scrying
    ],
)
def test_claims_without_a_rules_link_still_fail(first, second):
    assert mechanics(first, second) == set()


def test_reminder_text_does_not_change_the_profile():
    arena = card("Mindseeker Oculus")
    scryfall = {
        **arena,
        "oracle_text": arena["oracle_text"]
        + " (Put four loyalty counters on a Jace token you control. If you don’t control one, first "
        'create a blue Jace planeswalker token with "[−1]: Surveil 1" and "[−3]: Draw a card.")',
    }
    assert rules_profile(arena) == rules_profile(scryfall)
