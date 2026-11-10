"""Gemstone Caverns makes coloured mana only with its luck counter (match b55e160b, 2026-11-08)."""

from arenamcp.land_mana import land_colors
from arenamcp.mulligan_policy import castable_with, hand_card

CAVERNS = {
    "name": "Gemstone Caverns",
    "type_line": "Legendary Land",
    "oracle_text": (
        "If this card is in your opening hand and you're not the starting player, you may begin the game "
        "with this card on the battlefield with a luck counter on it. If you do, exile a card from your hand.\n"
        "{T}: Add {C}. If this land has a luck counter on it, instead add one mana of any color."
    ),
}
MOUNTAIN = {"name": "Mountain", "type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)"}
HALFLING = {"name": "Delighted Halfling", "type_line": "Creature — Halfling Citizen", "mana_cost": "{G}",
            "oracle_text": "{T}: Add {C}."}  # fmt: skip


def test_caverns_without_a_luck_counter_is_colourless():
    assert land_colors(CAVERNS) == frozenset()
    assert land_colors({**CAVERNS, "counters": {}}) == frozenset()
    assert land_colors({**CAVERNS, "color_production": ["White", "Blue", "Black", "Red", "Green"]}) == frozenset()


def test_caverns_with_its_luck_counter_makes_any_colour():
    for counters in ({"Luck": 1}, {"CounterType_Luck": 1}):
        assert land_colors({**CAVERNS, "counters": counters}) == frozenset("WUBRG")


def test_b55e160b_halfling_is_not_castable_off_mountain_and_caverns():
    lands = [hand_card(MOUNTAIN), hand_card(CAVERNS)]
    assert not castable_with(hand_card(HALFLING), lands)
    assert castable_with(hand_card(HALFLING), [hand_card(MOUNTAIN), hand_card({**CAVERNS, "counters": {"Luck": 1}})])


def test_unconditional_any_colour_lands_are_unchanged():
    town = {"name": "Starting Town", "type_line": "Land",
            "oracle_text": "{T}: Add {C}.\n{T}, Pay 1 life: Add one mana of any color."}  # fmt: skip
    assert land_colors(town) == frozenset("WUBRG")
