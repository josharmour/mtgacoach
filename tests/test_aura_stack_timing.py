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


@pytest.mark.parametrize(
    "kind,text,wait",
    [
        ("Sorcery", "Create two 1/1 red Goblin creature tokens.", True),
        ("Enchantment — Saga", "I — Create a 3/3 green Beast creature token.\nII — Draw a card.", True),
        ("Enchantment — Saga", "I — Scry 1.\nII — Create a 3/3 green Beast creature token.", False),
        ("Artifact", "{T}: Create a 1/1 green Saproling creature token.", False),
    ],
)
def test_wait_checks_what_the_current_spell_produces(kind, text, wait):
    s = state()
    s["stack"] = [{"type_line": kind, "controller_seat_id": 1, "oracle_text": text}]
    assert aura_waits_for_creature(s, s["hand"][0]) is wait


def test_controller_restrictions_and_independent_entry_reward():
    from tests.test_targeting_fallback_decline import _decision, _planner

    from arenamcp.action_planner import DECLINE_DECISION
    from arenamcp.target_effects import creature_aura_hosts

    s = state()
    s["battlefield"][0]["instance_id"] = 54
    aura = dict(
        s["hand"][0],
        name="Scout's Boon",
        oracle_text="Enchant creature\nWhen this enchantment enters, scry 2.\nEnchanted creature has flying.",
    )
    s["stack"] = [aura]
    s["decision_context"] = {"source_id": aura["instance_id"]}
    p = _planner()
    p._target_highlights = lambda *args: {54: "Cold"}
    assert creature_aura_hosts(aura, s["battlefield"], 1) == s["battlefield"]
    assert p._gate_harmful_llm_target_picks(_decision([54]), s, ["tgt:54"]) == ["tgt:54"]
    aura["oracle_text"] = aura["oracle_text"].replace("When this enchantment enters, scry 2.\n", "")
    assert p._gate_harmful_llm_target_picks(_decision([54]), s, ["tgt:54"]) == [DECLINE_DECISION]
    aura["oracle_text"] = "Enchant creature you control"
    assert creature_aura_hosts(aura, s["battlefield"], 1) == []
