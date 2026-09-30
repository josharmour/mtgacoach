"""Resource engines must not be valued as disposable power/toughness in combat."""

import pytest

from arenamcp.coach import CoachEngine
from arenamcp.combat_solver import _material, combat_resource_roles, optimal_blocks


def creature(instance_id, name, power, toughness, text="", **extra):
    return {
        "instance_id": instance_id,
        "name": name,
        "power": power,
        "toughness": toughness,
        "oracle_text": text,
        "type_line": "Creature",
        **extra,
    }


def mana_engine_combat():
    attacker = creature(
        100,
        "Wary Zone Guard",
        5,
        5,
        "This creature enters tapped.\n"
        "Survival — At the beginning of your second main phase, if this creature is tapped, "
        "return up to one target land card from your graveyard to the battlefield. "
        "This creature perpetually gets +1/+1.",
        is_attacking=True,
    )
    blockers = [
        creature(
            200,
            "Badgermole Cub",
            2,
            2,
            "When this creature enters, earthbend 1.\n"
            "Whenever you tap a creature for mana, add an additional {oG}.",
        )
    ] + [
        creature(
            300 + index,
            f"The Notary Hobbits #{index + 1}",
            1,
            1,
            "When The Notary Hobbits enter, if they're not a token, create two tokens "
            "that are copies of them, except the tokens aren't legendary.\n"
            "{oT}: Add {oC} for each Halfling you control.",
            object_kind="TOKEN" if index else "CARD",
        )
        for index in range(3)
    ]
    return [attacker], blockers


def test_preserve_four_creature_mana_engine_instead_of_trading_for_one_five_five():
    attackers, blockers = mana_engine_combat()
    plan = optimal_blocks(attackers, blockers, 21)
    assert plan.assignments == {}
    assert plan.damage_through == 5
    assert plan.blockers_lost_material == 0


@pytest.mark.parametrize(
    "text",
    [
        "{T}: Add {G}.",
        "{oT}: Add {oCoCoC}.",
        "{oT}: Add {oG} for each Elf you control.",
        "{T}: Add X mana of any one color, where X is the number of creatures you control.",
        "Whenever you tap a creature for mana, add an additional {G}.",
        "{T}: Draw a card.",
        "At the beginning of your upkeep, create a 1/1 creature token.",
        "{T}: Return target creature card from your graveyard to your hand.",
    ],
)
def test_ongoing_abilities_change_a_safe_trade_into_preserving_resources(text):
    attacker = creature(10, "Attacking Bear", 2, 2)
    blocker = creature(20, "Resource Bear", 2, 2, text)
    vanilla = {**blocker, "oracle_text": ""}
    assert optimal_blocks([attacker], [vanilla], 21).assignments == {20: 10}
    assert optimal_blocks([attacker], [blocker], 21).assignments == {}


def test_survival_still_wins_over_preserving_the_mana_engine():
    attackers, blockers = mana_engine_combat()
    plan = optimal_blocks(attackers, blockers, 5)
    assert plan.damage_through == 0
    assert len(plan.assignments) == 1
    assert plan.attackers_killed_material == 0


def test_ability_tokens_are_not_treated_like_vanilla_tokens():
    attacker = creature(10, "Large attacker", 5, 5)
    mana_token = creature(20, "Mana token", 1, 1, "{T}: Add {G}.", object_kind="TOKEN")
    plain_token = creature(30, "Plain token", 1, 1, object_kind="TOKEN")
    plan = optimal_blocks([attacker], [mana_token, plain_token], 5)
    assert plan.assignments == {30: 10}


@pytest.mark.parametrize("life,should_block", [(21, False), (5, True)])
def test_bounded_fallback_also_compares_chumping_to_taking_damage(life, should_block):
    attackers, blockers = mana_engine_combat()
    plan = optimal_blocks(attackers, blockers, life, max_options=1)
    assert bool(plan.assignments) is should_block
    assert plan.damage_through == (0 if should_block else 5)


def test_engine_value_is_symmetric_for_opposing_creatures():
    attacker = creature(10, "Opposing mana creature", 2, 2, "{T}: Add {G}.")
    blocker = creature(20, "Plain Bear", 2, 2)
    plan = optimal_blocks([attacker], [blocker], 21)
    assert plan.assignments == {20: 10}
    assert plan.attackers_killed_material > plan.blockers_lost_material


@pytest.mark.parametrize(
    "text",
    [
        "When this creature enters, draw a card.",
        "When this creature enters, create two 1/1 creature tokens.",
        "When this creature enters, add {G}{G}{G}.",
        'When this creature enters, create a 1/1 creature token with "{T}: Add {G}."',
        "{T}, Sacrifice this creature: Add {G}.",
    ],
)
def test_spent_etbs_and_sacrificing_the_source_are_not_repeatable_engines(text):
    assert not combat_resource_roles(creature(10, "One shot", 1, 1, text))


def test_duplicate_oracle_text_does_not_multiply_engine_value():
    text = "Whenever you tap a creature for mana, add an additional {oG}."
    single = creature(10, "Mana amplifier", 2, 2, text)
    duplicated = {**single, "oracle_text": "\n".join([text] * 3)}
    assert _material(single) == _material(duplicated)


def test_resource_value_does_not_depend_on_card_name_or_token_status():
    original = creature(10, "Unknown card", 1, 1, "{oT}: Add {oC} for each Halfling you control.")
    renamed = {**original, "name": "Another name", "object_kind": "TOKEN"}
    assert _material(original) == _material(renamed)


def test_prompt_explains_nonlethal_damage_and_resource_preservation():
    attackers, blockers = mana_engine_combat()
    context = {
        "type": "declare_blockers",
        "legal_blocker_ids": [card["instance_id"] for card in blockers],
        "attacker_ids": [100],
    }
    lines = CoachEngine.__new__(CoachEngine)._format_block_combat(
        blockers, attackers, {"life_total": 21}, 9, "Phase_Combat", set(), context
    )
    prompt = "\n".join(lines)
    assert "take 5 dmg → 16 life remaining" in prompt
    assert "Computed optimal blocks: no blocks" in prompt
    assert "Resource creatures:" in prompt
    assert "mana production" in prompt
    assert "ability to rebuild next turn" in prompt
