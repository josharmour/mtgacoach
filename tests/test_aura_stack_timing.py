"""Ledger #291: Feather must wait until Spellbook Vendor is a permanent."""

from copy import deepcopy

import pytest
from tests.test_burn_timing_20261019 import _down

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.play_safety import aura_waits_for_creature


def state():
    return {
        "local_seat_id": 1,
        "turn": {"active_player": 1, "phase": "Phase_Main1", "turn_number": 15},
        "hand": [
            {
                "instance_id": 379,
                "name": "Feather of Flight",
                "type_line": "Enchantment — Aura",
                "oracle_text": "Flash\nEnchant creature\nWhen this Aura enters, draw a card.\nEnchanted creature gets +1/+0 and has flying.",
            }
        ],
        "battlefield": [{"type_line": "Creature — Kithkin", "controller_seat_id": 2}],
        "stack": [
            {"type_line": "Creature — Human Peasant", "name": "Spellbook Vendor", "controller_seat_id": 1}
        ],
    }


def test_feather_waits_then_returns_to_menu_when_vendor_resolves():
    s = state()
    menu = PendingDecision(
        (1, 1),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0", "Cast Feather", True, {"actionType": "ActionType_Cast", "instanceId": 379}
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    planner = _down()
    filtered, pick = planner._hold_instant_burn(menu, s)
    assert filtered.option_ids() == {"pass"}
    assert pick == ["pass"]
    s["battlefield"].extend(s.pop("stack"))
    filtered, _ = planner._hold_instant_burn(menu, s)
    assert "idx:0" in filtered.option_ids()


@pytest.mark.parametrize(
    "change",
    [
        "friendly",
        "unknown_controller",
        "opposing_stack",
        "empty_stack",
        "removal",
        "land_aura",
        "missing_board",
    ],
)
def test_hold_does_not_block_other_plays(change):
    s = deepcopy(state())
    if change == "friendly":
        s["battlefield"][0]["controller_seat_id"] = 1
    elif change == "unknown_controller":
        s["battlefield"][0].pop("controller_seat_id")
    elif change == "opposing_stack":
        s["stack"][0]["controller_seat_id"] = 2
    elif change == "empty_stack":
        s["stack"] = []
    elif change == "removal":
        s["hand"][0]["oracle_text"] = "Enchant creature\nEnchanted creature can't attack or block."
    elif change == "land_aura":
        s["hand"][0]["oracle_text"] = "Enchant land\nEnchanted land has hexproof."
    else:
        s.pop("battlefield")
    assert not aura_waits_for_creature(s, s["hand"][0])
