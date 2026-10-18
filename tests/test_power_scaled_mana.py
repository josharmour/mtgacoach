"""Mana scaled by our creatures' powers (pro YFRA game, 2026-10-18).

Loot, the Nexus: "{T}: Choose a color. Add one mana of that color for each
different power among creatures you control." ``combat_recovery.mana_options``
failed its fullmatch and gave Loot 0 mana, so the mana-reach note and the
recovery forecast never counted it.
"""

from __future__ import annotations

from tests.pro_yfra_states import card

from arenamcp.combat_recovery import mana_options


def test_loot_adds_one_mana_per_different_power():
    loot = card(219, "Loot, the Nexus", 2)  # 1/3
    beast = card(276, "Beast", 2)  # 4/4
    crafter = card(200, "Heartwood Crafter", 2)  # 1/1: a repeated power adds nothing
    options = mana_options(loot, [loot, beast, crafter])
    assert options and max(sum(o) for o in options) == 2
    # one colour of our choice: every colour option is single-coloured
    assert all(sum(1 for n in o if n) == 1 for o in options)
    assert len(options) == 5


def test_greatest_power_mana():
    druid = {"name": "Big Druid", "type_line": "Creature — Elf Druid", "power": 2,
             "oracle_text": "{T}: Add an amount of {G} equal to the greatest power among creatures you control."}  # fmt: skip
    beast = card(276, "Beast", 2)
    assert mana_options(druid, [druid, beast]) == ((0, 0, 0, 0, 4, 0),)
