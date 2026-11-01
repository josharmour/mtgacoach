"""#309 / 8b7bed14 T12: equipping in Main2 cannot create an attack window."""

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.game_plan import decision_claim_notes


def state(phase="Phase_Main2"):
    return {
        "local_seat_id": 1,
        "turn": {"turn_number": 12, "active_player": 1, "priority_player": 1, "phase": phase},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
        "battlefield": [
            {
                "instance_id": 1,
                "name": "Lightning Greaves",
                "type_line": "Artifact — Equipment",
                "oracle_text": "Equipped creature has haste and shroud.\nEquip {0}",
                "controller_seat_id": 1,
            },
            {
                "instance_id": 2,
                "name": "Vorinclex",
                "type_line": "Legendary Creature — Phyrexian Praetor",
                "oracle_text": "Trample, reach",
                "controller_seat_id": 1,
                "power": 6,
                "toughness": 6,
            },
        ],
        "_bridge_request_type": "SelectTargets",
    }


@pytest.mark.parametrize("phase", ["Main2", "Phase_Main2", "Ending", "Phase_Ending"])
def test_postcombat_haste_attack_claim_is_flagged(phase):
    notes = decision_claim_notes("Equip Greaves so Vorinclex can attack now.", state(phase))
    assert any("attack declaration" in note and "haste" in note for note in notes)


@pytest.mark.parametrize(
    "text",
    [
        "Equip Greaves for shroud.",
        "Equip Greaves so Vorinclex can attack next turn.",
        "Vorinclex cannot attack now, but shroud protects it.",
        "Vorinclex can't attack this turn.",
        "Haste lets the mana creature tap now.",
        "Vorinclex could attack last turn.",
        "After creating an additional combat phase Vorinclex can attack this turn.",
    ],
)
def test_other_equipment_benefits_and_conditional_attacks_are_preserved(text):
    assert decision_claim_notes(text, state()) == []


def test_main1_haste_can_enable_an_attack():
    assert decision_claim_notes("Equip Greaves so Vorinclex can attack now.", state("Phase_Main1")) == []


def test_explicit_attack_request_overrides_stale_phase():
    game = state()
    game["_bridge_request_type"] = "DeclareAttackers"
    assert decision_claim_notes("Vorinclex can attack now.", game) == []


def test_typed_selection_context_keeps_explicit_phase_constraint():
    context = ActionPlanner(None)._decision_game_context(state(), options_listed=True)
    assert "CURRENT TIMING" in context
    assert "normal attack declaration" in context
    assert "haste" in context and "shroud" in context
