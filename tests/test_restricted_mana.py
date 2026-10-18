"""Restricted mana (pro YFRA game, 2026-10-18, match 1 turn 7).

Heartwood Crafter: "{T}: Add {C}. This mana can't be spent to cast spells
from your hand." The rules engine counted it like any source, so Garruk
({3}{G}{G}) looked castable from hand with three lands, the Heartwood token
and the Crafter; the pro started that cast and had to cancel it.
"""

from __future__ import annotations

from copy import deepcopy

from tests.pro_yfra_states import M1_T7

from arenamcp.rules_engine import RulesEngine

GARRUK = "{3}{G}{G}"


def test_crafter_mana_does_not_cast_garruk_from_hand():
    pool = RulesEngine._get_mana_pool(deepcopy(M1_T7), 2)
    assert pool["total"] == 4
    assert not RulesEngine._can_afford(GARRUK, pool)
    assert RulesEngine._can_afford("{1}{W}", pool)  # Refute Destiny, what the pro cast instead
    assert pool["restricted"] == [{"name": "Heartwood Crafter", "restriction": "hand"}]


def test_crafter_mana_pays_for_a_cast_from_exile():
    pool = RulesEngine._get_mana_pool(deepcopy(M1_T7), 2, from_hand=False)
    assert pool["total"] == 5
    assert RulesEngine._can_afford(GARRUK, pool)


def test_spend_only_mana_never_counts_and_free_mana_still_does():
    restriction = RulesEngine._mana_restriction
    assert restriction("{T}: Add {C}. Spend this mana only to cast artifact spells.") == "only"
    assert restriction("{T}: Add {C}.\n{T}: Add one mana of any color. Spend this mana only to cast a creature spell.") == ""
    assert restriction("{T}: Add {G}.") == ""
