"""Golden unit tests for the On-the-Fly Scryfall Ability Synthesizer."""

from arenamcp.ability_synthesizer import (
    AbilitySynthesizer,
    ActivatedAbility,
    CreateToken,
    Filter,
    ReturnFromGraveyard,
    StaticAbility,
    TriggeredAbility,
)


def test_parse_the_notary_hobbits():
    name = "The Notary Hobbits"
    oracle = (
        "When The Notary Hobbits enters the battlefield, if it's not a token, "
        "create two tokens that are copies of The Notary Hobbits, except the tokens aren't legendary.\n"
        "{T}: Add {C} for each Halfling you control."
    )

    result = AbilitySynthesizer.parse_card(name, oracle)
    assert result.coverage >= 0.90
    assert len(result.abilities) == 2

    # 1. ETB Trigger
    etb = result.abilities[0]
    assert isinstance(etb, TriggeredAbility)
    assert etb.trigger.event == "ETB"
    assert len(etb.effects) == 1
    effect = etb.effects[0]
    assert isinstance(effect, CreateToken)
    assert effect.spec.name == "The Notary Hobbits"
    assert effect.spec.count == 2
    assert effect.spec.copy_of_self is True
    assert effect.spec.not_legendary is True

    # 2. Tap for mana ability
    tap_ab = result.abilities[1]
    assert isinstance(tap_ab, ActivatedAbility)
    assert tap_ab.cost.tap is True
    assert tap_ab.mana_ability is True


def test_parse_samwise_gamgee():
    name = "Samwise Gamgee"
    oracle = (
        "Whenever another nontoken creature enters the battlefield under your control, create a Food token.\n"
        "{2}, Sacrifice three Foods: Return target historic card from your graveyard to your hand."
    )

    result = AbilitySynthesizer.parse_card(name, oracle)
    assert result.coverage >= 0.90
    assert len(result.abilities) == 2

    # 1. Triggered ability
    trig = result.abilities[0]
    assert isinstance(trig, TriggeredAbility)
    assert trig.trigger.event == "ANOTHER_CREATURE_ETB"
    assert trig.trigger.condition == "nontoken"
    assert len(trig.effects) == 1
    eff = trig.effects[0]
    assert isinstance(eff, CreateToken)
    assert eff.spec.name == "Food"

    # 2. Activated sacrifice ability
    act = result.abilities[1]
    assert isinstance(act, ActivatedAbility)
    assert "{2}" in act.cost.mana
    assert act.cost.sacrifice is not None
    assert "food" in act.cost.sacrifice.target.lower()
    assert act.cost.sacrifice.count == 3
    assert len(act.effects) == 1
    assert isinstance(act.effects[0], ReturnFromGraveyard)


def test_parse_gilded_goose():
    name = "Gilded Goose"
    oracle = (
        "Flying\n"
        "When Gilded Goose enters the battlefield, create a Food token.\n"
        "{1}{G}, {T}: Create a Food token.\n"
        "{T}, Sacrifice a Food: Add one mana of any color."
    )

    result = AbilitySynthesizer.parse_card(name, oracle)
    assert result.coverage >= 0.90
    assert len(result.abilities) == 4

    # Keyword: Flying
    assert isinstance(result.abilities[0], StaticAbility)
    assert "Flying" in result.abilities[0].keywords

    # ETB Food
    assert isinstance(result.abilities[1], TriggeredAbility)
    assert result.abilities[1].effects[0].spec.name == "Food"

    # {1}{G}, {T}: Food
    act1 = result.abilities[2]
    assert isinstance(act1, ActivatedAbility)
    assert "{1}{G}" in act1.cost.mana
    assert act1.cost.tap is True

    # Mana ability
    act2 = result.abilities[3]
    assert isinstance(act2, ActivatedAbility)
    assert act2.cost.tap is True
    assert act2.cost.sacrifice is not None
    assert act2.mana_ability is True


def test_parse_adeline():
    name = "Adeline, Resplendent Cathar"
    oracle = (
        "Vigilance\n"
        "Adeline, Resplendent Cathar's power is equal to the number of creatures you control.\n"
        "Whenever you attack, for each opponent, create a 1/1 white Human creature token that's tapped and attacking that player or a planeswalker they control."
    )

    result = AbilitySynthesizer.parse_card(name, oracle)
    assert result.coverage >= 0.90
    assert len(result.abilities) == 3

    # Vigilance
    assert isinstance(result.abilities[0], StaticAbility)
    assert "Vigilance" in result.abilities[0].keywords

    # Attack token trigger
    atk = result.abilities[2]
    assert isinstance(atk, TriggeredAbility)
    assert atk.trigger.event == "ATTACKS"
    assert atk.effects[0].spec.name == "Human"
    assert atk.effects[0].spec.tapped is True
    assert atk.effects[0].spec.attacking is True
