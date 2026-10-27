"""Rules links misclassified or stripped in the October 21/25 Powered Cubes.

These are mechanical relationships from recorded Arena card text, not a claim
that any particular Cube ranking or pick is optimal.
"""

import pytest

from arenamcp.limited_rules import rules_profile, synergy_evidence, synergy_graph


def card(grp_id, name, kind, text):
    return {"grp_id": grp_id, "name": name, "type_line": kind, "oracle_text": text}


STONEFORGE = card(
    35737,
    "Stoneforge Mystic",
    "Creature — Kor Artificer",
    "When this creature enters, you may search your library for an Equipment card, reveal it, "
    "put it into your hand, then shuffle.\n"
    "{o1oW}, {oT}: You may put an Equipment card from your hand onto the battlefield.",
)
CHAINSAW = card(
    92208,
    "Chainsaw",
    "Artifact — Equipment",
    "When this Equipment enters, it deals 3 damage to up to one target creature.\n"
    "Whenever one or more creatures die, put a rev counter on this Equipment.\n"
    "Equipped creature gets +X/+0, where X is the number of rev counters on this Equipment.\n"
    "Equip {o3}",
)
KALDRA = card(
    76186,
    "Kaldra Compleat",
    "Legendary Artifact — Equipment",
    "Living weapon\nIndestructible\n"
    "Equipped creature gets +5/+5 and has first strike, trample, indestructible, haste, and "
    '"Whenever this creature deals combat damage to a creature, exile that creature."\nEquip {o7}',
)
EXTRUDER = card(
    90673,
    "Legion Extruder",
    "Artifact",
    "When this artifact enters, it deals 2 damage to any target.\n"
    "{o2}, {oT}, Sacrifice another artifact: Create a 3/3 colorless Golem artifact creature token.",
)
HOLE = card(
    1,
    "Portable Hole",
    "Artifact",
    "When this artifact enters, exile target nonland permanent an opponent controls with mana value "
    "2 or less until this artifact leaves the battlefield.",
)
SNAPCASTER = card(
    98153,
    "Snapcaster Mage",
    "Creature — Human Wizard",
    "Flash\nWhen this creature enters, target instant or sorcery card in your graveyard gains flashback "
    "until end of turn. The flashback cost is equal to its mana cost.",
)


def mechanics(first, second):
    return {edge["mechanic"] for edge in synergy_evidence(first, second)}


def test_extruder_uses_artifacts_as_sacrifice_fodder_not_entry_triggers():
    # Oct 25 P1P11 incorrectly described Portable Hole as triggering Extruder.
    assert mechanics(HOLE, EXTRUDER) == {"artifact_presence"}
    assert "artifact_entry" not in rules_profile(EXTRUDER)["payoffs"]
    actual_trigger = card(
        3, "Trigger", "Creature", "Whenever an artifact enters under your control, draw a card."
    )
    assert mechanics(HOLE, actual_trigger) == {"artifact_entry"}


@pytest.mark.parametrize(
    "source",
    [
        HOLE,
        card(
            106389,
            "Solitary Cell",
            "Artifact",
            "When this artifact enters, exile target nonland permanent an opponent controls with mana value "
            "3 or less until this artifact leaves the battlefield.\n"
            "{o1}, {oT}, Discard a legendary card: Draw a card.",
        ),
        card(
            3,
            "Holding Vessel",
            "Artifact",
            "When Holding Vessel enters the battlefield, exile target creature until Holding Vessel leaves the battlefield.",
        ),
    ],
)
def test_sacrifice_link_keeps_the_return_from_exile_downside(source):
    edges = synergy_evidence(source, EXTRUDER)
    edge = next(edge for edge in edges if edge["mechanic"] == "artifact_presence")
    assert edge["source_id"] == source["grp_id"]
    assert edge["payoff_id"] == EXTRUDER["grp_id"]
    assert [fact["code"] for fact in edge["cautions"]] == ["sacrifice_ends_exile_until_source_leaves"]
    assert edge["cautions"][0]["kind"] == "rules_fact"
    assert "exiled tokens do not return" in edge["cautions"][0]["detail"]
    assert synergy_graph([source, EXTRUDER]) == edges


def test_exile_caution_does_not_turn_nonconsuming_artifact_synergy_into_a_penalty():
    count = card(2, "Count", "Creature", "This creature gets +1/+0 for each artifact you control.")
    trigger = card(3, "Trigger", "Creature", "Whenever an artifact enters under your control, draw a card.")
    assert "cautions" not in synergy_evidence(HOLE, count)[0]
    assert "cautions" not in synergy_evidence(HOLE, trigger)[0]
    permanent_exile = {**HOLE, "oracle_text": "When this artifact enters, exile target creature."}
    assert "cautions" not in synergy_evidence(permanent_exile, EXTRUDER)[0]
    another_source = {
        **HOLE,
        "oracle_text": "Exile target creature until another target permanent leaves the battlefield.",
    }
    assert "cautions" not in synergy_evidence(another_source, EXTRUDER)[0]


def test_token_producer_is_not_itself_sacrificed_to_supply_artifact_fodder():
    spell = card(
        2,
        "Removal and Treasure",
        "Sorcery",
        "Create a Treasure token. Exile target creature until this permanent leaves the battlefield.",
    )
    assert "cautions" not in synergy_evidence(spell, EXTRUDER)[0]


@pytest.mark.parametrize(
    "mana_text",
    ["{T}: Add {G}.", "{oT}: Add {oCoCoC}.", "{T}, Sacrifice this artifact: Add one mana of any color."],
)
def test_mana_artifact_is_a_resource_tradeoff_not_forbidden_fodder(mana_text):
    mana = card(2, "Mana source", "Artifact", mana_text)
    edge = synergy_evidence(mana, EXTRUDER)[0]
    assert edge["mechanic"] == "artifact_presence"
    assert edge["cautions"][0]["code"] == "sacrifice_spends_mana_source"
    assert edge["cautions"][0]["kind"] == "opportunity_cost"
    etb_mana = {**mana, "oracle_text": "When this artifact enters, add {R}."}
    assert "cautions" not in synergy_evidence(etb_mana, EXTRUDER)[0]
    granted_mana = {
        **mana,
        "type_line": "Artifact — Equipment",
        "oracle_text": 'Equipped creature has "{T}: Add one mana of any color."',
    }
    assert "cautions" not in synergy_evidence(granted_mana, EXTRUDER)[0]


def test_extruder_needs_another_artifact_before_it_supplies_a_body():
    profile = rules_profile(EXTRUDER)
    assert profile["body"] is True
    assert profile["unconditional_body"] is False


def test_artifact_count_and_token_replacement_are_not_entry_triggers():
    count = card(2, "Count", "Creature", "This creature gets +1/+0 for each artifact you control.")
    replacement = card(
        3,
        "Replacement",
        "Creature",
        "If one or more artifact tokens would be created under your control, those tokens plus an "
        "additional Treasure token are created instead.",
    )
    treasure = card(4, "Treasure maker", "Sorcery", "Create a Treasure token.")
    assert mechanics(HOLE, count) == {"artifact_presence"}
    assert mechanics(HOLE, replacement) == set()
    assert mechanics(treasure, replacement) == {"artifact_tokens"}


@pytest.mark.parametrize("equipment", [CHAINSAW, KALDRA])
def test_stoneforge_tutors_and_deploys_actual_equipment_cards(equipment):
    # Both real links were stripped on Oct 25 at 23:01:41 and 23:04:30.
    assert mechanics(STONEFORGE, equipment) == {"equipment_card"}
    assert mechanics(STONEFORGE, HOLE) == set()


def test_restricted_equipment_tutor_is_not_generic_equipment_support():
    restricted = card(
        2,
        "Restricted tutor",
        "Creature",
        "When this creature enters, search your library for an Equipment card with mana value 1 or less, "
        "reveal it, put it into your hand, then shuffle.",
    )
    assert mechanics(restricted, KALDRA) == set()


@pytest.mark.parametrize("kind", ["Instant", "Sorcery"])
def test_snapcaster_has_graveyard_spell_targets_not_generic_permanent_synergy(kind):
    # Lightning Bolt and Consider links were stripped in the Oct 21 Cube.
    spell = card(2, "Spell", kind, "Draw a card.")
    assert mechanics(spell, SNAPCASTER) == {"spell_recursion"}
    assert mechanics(HOLE, SNAPCASTER) == set()


def test_spell_recursion_requires_the_unrestricted_own_graveyard_wording():
    spell = card(2, "Spell", "Instant", "Draw a card.")
    own = card(
        3,
        "Recursion",
        "Enchantment",
        "When this Class becomes level 2, return target instant or sorcery card from your graveyard "
        "to your hand.",
    )
    assert mechanics(spell, own) == {"spell_recursion"}
    for text in (
        "Return target instant or sorcery card from an opponent's graveyard to their hand.",
        "Return target instant or sorcery card with mana value 1 or less from your graveyard to your hand.",
    ):
        assert mechanics(spell, {**own, "oracle_text": text}) == set()


@pytest.mark.parametrize("keyword", ["Living weapon", "For Mirrodin!"])
def test_equipment_body_keywords_supply_token_body_without_changing_card_type(keyword):
    equipment = {**KALDRA, "oracle_text": keyword}
    profile = rules_profile(equipment)
    assert profile["body"] is True
    assert profile["unconditional_body"] is True
    assert "creature_entry" in profile["enables"]
    assert "noncreature_spell" in profile["enables"]
    assert rules_profile(CHAINSAW)["body"] is False
    # Reminder text, when present in another card source, must not double-count.
    with_reminder = {
        **equipment,
        "oracle_text": keyword + " (Create a creature token and attach this Equipment to it.)",
    }
    assert rules_profile(with_reminder) == profile
