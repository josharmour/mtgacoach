"""Unit test to verify target selection for Equip abilities (e.g. S.H.I.E.L.D. Spy Kit).

Ensures that equip and beneficial abilities infer must_control='you' and
sort friendly creatures first, rather than prioritizing opponent creatures.
"""

from __future__ import annotations

from arenamcp.rules_engine import RulesEngine


def test_equip_target_selection_prefers_friendly_creatures():
    """Verify S.H.I.E.L.D. Spy Kit equip ability targets friendly creatures (YOURS)."""
    state = {
        "players": [
            {"seat_id": 1, "is_local": True},
            {"seat_id": 2, "is_local": False},
        ],
        "decision_context": {
            "type": "target_selection",
            "source_id": 202,
            "source_card": "S.H.I.E.L.D. Spy Kit",
            "source_oracle_text": "Equipped creature gets +1/+1. Whenever equipped creature attacks alone, untap it and scry 1. Equip {1}",
            "validTargets": [
                {"instanceId": 211},  # Patriot (YOURS)
                {"instanceId": 239},  # Hero in Training (YOURS)
            ],
        },
        "battlefield": [
            {"instance_id": 211, "name": "Patriot, Shield Wielder", "owner_seat_id": 1, "controller_seat_id": 1, "power": 3, "toughness": 3, "type_line": "Creature — Human Hero"},
            {"instance_id": 239, "name": "Hero in Training", "owner_seat_id": 1, "controller_seat_id": 1, "power": 2, "toughness": 2, "type_line": "Creature — Human Hero"},
            {"instance_id": 230, "name": "Hawkeye, Young Avenger", "owner_seat_id": 2, "controller_seat_id": 2, "power": 2, "toughness": 4, "type_line": "Creature — Human Archer Hero"},
        ],
    }

    actions = RulesEngine._get_target_selection_actions(state)
    assert len(actions) > 0
    # Every returned target must be YOURS, not OPP
    for act in actions:
        assert "(OPP)" not in act
        assert "(YOURS)" in act
