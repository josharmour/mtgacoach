"""Resource engines must not be valued as disposable power/toughness in combat."""

import pytest

from arenamcp.coach import CoachEngine
from arenamcp.combat_solver import _material, combat_resource_roles, mana_spending_restriction, optimal_blocks


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


def large_hit_combat():
    """Report 20260929_210547: one 10/9 and three available resource creatures."""
    attacker = creature(651, "Large enchanted attacker", 10, 9, is_attacking=True)
    support = creature(
        450,
        "Ability mana support",
        2,
        2,
        "You may activate abilities of creatures you control as though those creatures had haste.\n"
        "{T}: Add two mana of any one color. Spend this mana only to activate abilities of creature sources.",
    )
    tokens = [
        creature(
            iid,
            f"Halfling token #{index}",
            1,
            1,
            "{T}: Add {C} for each Halfling you control.",
            object_kind="TOKEN",
        )
        for index, iid in enumerate((665, 666), start=1)
    ]
    return [attacker], [support, *tokens]


@pytest.mark.parametrize("max_options", [1, 50000])
def test_ten_damage_at_twenty_four_life_is_worth_one_support_creature(max_options):
    attackers, blockers = large_hit_combat()
    plan = optimal_blocks(attackers, blockers, 24, max_options=max_options)
    assert plan.assignments == {450: 651}
    assert plan.damage_through == 0
    assert plan.blockers_lost_material == _material(blockers[0])
    assert plan.attackers_killed_material == 0


def test_large_hit_can_still_be_taken_with_abundant_life():
    attackers, blockers = large_hit_combat()
    plan = optimal_blocks(attackers, blockers, 50)
    assert plan.assignments == {}
    assert plan.damage_through == 10


def test_trampling_hit_is_not_treated_as_ten_damage_prevented_by_one_blocker():
    attackers, blockers = large_hit_combat()
    attackers[0]["oracle_text"] = "Trample"
    plan = optimal_blocks(attackers, blockers, 24)
    assert plan.assignments == {}
    assert plan.damage_through == 10


def test_illegal_support_block_is_never_recommended():
    attackers, blockers = large_hit_combat()
    plan = optimal_blocks(
        attackers, blockers, 24, blocker_allowed_attackers={450: set(), 665: {651}, 666: {651}}
    )
    assert len(plan.assignments) == 1
    assert next(iter(plan.assignments)) in (665, 666)
    assert plan.damage_through == 0


def test_large_hit_prompt_explains_life_saved_and_restricted_mana():
    attackers, blockers = large_hit_combat()
    context = {
        "type": "declare_blockers",
        "legal_blocker_ids": [card["instance_id"] for card in blockers],
        "attacker_ids": [651],
        "raw_blockers": [
            {"blockerInstanceId": card["instance_id"], "attackerInstanceIds": [651]} for card in blockers
        ],
    }
    prompt = "\n".join(
        CoachEngine.__new__(CoachEngine)._format_block_combat(
            blockers, attackers, {"life_total": 24}, 8, "Phase_Combat", set(), context
        )
    )
    assert "take 10 dmg → 14 life remaining" in prompt
    assert "Computed optimal blocks: Ability mana support blocks Large enchanted attacker" in prompt
    assert "Recommended blocks leave 24 life after 0 combat damage" in prompt
    assert (
        "Mana restriction — Ability mana support: Spend this mana only to activate abilities of creature sources."
        in prompt
    )
    assert mana_spending_restriction(blockers[1]) == ""
