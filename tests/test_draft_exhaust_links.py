"""Recorded DFT Exhaust partners are real activation links, not artifact ETBs."""

import pytest

from arenamcp.limited_rules import synergy_evidence

HIVE = dict(
    grp_id=1,
    type_line="Artifact — Vehicle",
    oracle_text="Vigilance\nWhenever you activate an exhaust ability, create a 1/1 colorless Thopter artifact creature token with flying.\nCrew 1",
)


@pytest.mark.parametrize("cost", ["{3}", "{o4oU}"])
def test_exhaust_activation_supplies_hive_even_with_arena_symbols(cost):
    source = dict(
        grp_id=2,
        type_line="Creature",
        oracle_text=f"Exhaust — {cost}: Put two +1/+1 counters on this creature.",
    )
    assert {"source_id": 2, "payoff_id": 1, "mechanic": "exhaust"} in synergy_evidence(source, HIVE)


@pytest.mark.parametrize(
    "text",
    [
        "Whenever you activate an exhaust ability, draw a card.",
        "Exhaust abilities you activate cost {1} less to activate.",
        "{3}: Put a +1/+1 counter on this creature.",
    ],
)
def test_payoff_discount_and_ordinary_activation_do_not_invent_exhaust_ability(text):
    source = dict(grp_id=2, type_line="Creature", oracle_text=text)
    assert not any(e["mechanic"] == "exhaust" for e in synergy_evidence(source, HIVE))
