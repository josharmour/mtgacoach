"""#252: the role guard must not count Planar Incision as lasting removal."""

import pytest

from arenamcp import board_assessment as ba


def spell(text):
    return {"name": "Blink spell", "type_line": "Instant", "oracle_text": text}


@pytest.mark.parametrize(
    "text",
    [
        "Exile target artifact or creature, then return it to the battlefield under its owner's control with a +1/+1 counter on it.",
        "Exile target creature. Return that card to the battlefield under its owner's control.",
    ],
)
def test_immediate_return_is_not_removal_or_false_saved_attack(text):
    blink = spell(text)
    enemy = ba._body(
        {
            "name": "Threat",
            "instance_id": 674,
            "power": 6,
            "toughness": 6,
            "controller_seat_id": 2,
            "type_line": "Creature",
        },
        10,
        {},
    )
    assert ba.removal_reach(blink) is None
    assert ba.card_role(blink) not in {"removal", "bounce"}
    assert not ba._kills(blink, enemy)


@pytest.mark.parametrize(
    "text",
    [
        "Exile target creature.",
        "Exile target creature until this enchantment leaves the battlefield.",
        "Exile target creature. Return that card to the battlefield under its owner's control at the beginning of the next end step.",
        "Exile target creature, then return it to the battlefield under your control.",
        "Destroy target creature. Return target creature card from your graveyard to the battlefield.",
    ],
)
def test_actual_exile_delayed_return_and_control_change_are_not_disarmed(text):
    assert ba.removal_reach(spell(text)) == ("destroy", None)


def test_blink_clause_does_not_hide_independent_damage_clause():
    mixed = spell(
        "Exile target creature, then return it to the battlefield under its owner's control. Deal 3 damage to target creature."
    )
    assert ba.removal_reach(mixed) == ("damage", 3)
