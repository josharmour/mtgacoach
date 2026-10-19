"""A target the burn on the stack kills together with castable burn from hand is not declined.

2026-10-19 12:29:01 (ledger #110, match e453dfa3 T9): Iroh's Demonstration's
"4 damage to target creature" at the opponent's only creature, a 4/5 Hearth
Elemental, with Tectonic Hazard ({R}: 1 damage to each creature target
opponent controls) in hand and "Cast Tectonic Hazard [OK]" on the menu. The
reasoning ("The 4-damage mode kills the Hearth Elemental ... exactly as
intended") named no follow-up, so the #103 check passed it over and the log
read "Keeping the spell: 4 damage kills no legal opposing target (picked
Hearth Elemental)": the cast was cancelled. 4 + 1 kills a 4/5 for 3 of the
4 mana.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from arenamcp.action_planner import DECLINE_DECISION, ActionPlanner
from arenamcp.decision_memory import stack_burn_combo_on

IROH_MODE = "iroh's demonstration deals 4 damage to target creature."
IROH = {
    "instance_id": 228,
    "name": "Iroh's Demonstration",
    "type_line": "Sorcery",
    "mana_cost": "{1}{R}",
    "controller_seat_id": 2,
    "oracle_text": (
        "Choose one —\n• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
        "• Iroh's Demonstration deals 4 damage to target creature."
    ),
}
HAZARD = {
    "instance_id": 240,
    "name": "Tectonic Hazard",
    "type_line": "Sorcery",
    "mana_cost": "{R}",
    "oracle_text": "Tectonic Hazard deals 1 damage to each creature target opponent controls.",
}
ELEMENTAL = {
    "instance_id": 342,
    "name": "Hearth Elemental",
    "type_line": "Creature — Elemental",
    "power": 4,
    "toughness": 5,
    "controller_seat_id": 1,
}


def _mountain(iid: int) -> dict:
    return {"instance_id": iid, "name": "Mountain", "type_line": "Basic Land — Mountain", "controller_seat_id": 2}


def _state(*, lands: int = 4, hand=(HAZARD,), theirs=(ELEMENTAL,), stack=(IROH,), active: int = 2) -> dict:
    return {
        "turn": {"turn_number": 9, "active_player": active, "phase": "Phase_Main1"},
        "players": [{"seat_id": 2, "is_local": True}, {"seat_id": 1, "is_local": False}],
        "battlefield": [_mountain(280 + n) for n in range(lands)] + list(theirs),
        "hand": list(hand),
        "stack": list(stack),
    }


def _planner(oracle: str = IROH_MODE, source: int = 228) -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._decision_source_oracle = lambda decision, state: oracle
    planner._decision_source_instance = lambda state: source
    planner._last_decision_reasoning = "The 4-damage mode kills the Hearth Elemental, their only clock, exactly as intended."
    return planner


def _decision(*targets: int, source: str = "Iroh's Demonstration"):
    return SimpleNamespace(source_label=source, options=[SimpleNamespace(option_id=f"tgt:{t}") for t in targets])


def test_the_live_iroh_plus_hazard_kill_is_kept(caplog):
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        chosen = _planner()._prefer_lethal_damage_target(_decision(342), _state(), ["tgt:342"])
    assert chosen == ["tgt:342"]
    kept = [r.getMessage() for r in caplog.records if "burn combo" in r.getMessage()]
    assert kept and "Iroh's Demonstration (4) + Tectonic Hazard (1) = 5 damage kills Hearth Elemental" in kept[0]
    assert not [r for r in caplog.records if "kills no legal opposing target" in r.getMessage()]


def test_the_stack_spell_is_found_by_name_when_the_request_has_no_source_id():
    chosen = _planner(source=0)._prefer_lethal_damage_target(_decision(342), _state(), ["tgt:342"])
    assert chosen == ["tgt:342"]


def test_without_the_mana_for_both_it_is_still_declined():
    # Two Mountains: Iroh's (2) leaves nothing for the Hazard (1).
    planner = _planner()
    assert planner._prefer_lethal_damage_target(_decision(342), _state(lands=2), ["tgt:342"]) == [DECLINE_DECISION]


def test_without_burn_in_hand_it_is_still_declined():
    planner = _planner()
    assert planner._prefer_lethal_damage_target(_decision(342), _state(hand=()), ["tgt:342"]) == [DECLINE_DECISION]


def test_a_combo_on_another_creature_does_not_keep_this_target():
    wall = dict(ELEMENTAL, instance_id=343, name="Hearth Wall", power=0, toughness=6)
    state = _state(theirs=(ELEMENTAL, wall))
    # The combo kills the Elemental, not the 0/6 the model picked (4 + 1 < 6).
    assert stack_burn_combo_on(state, 342, source_id=228, damage=4) is not None
    assert stack_burn_combo_on(state, 343, source_id=228, damage=4) is None
    planner = _planner()
    assert planner._prefer_lethal_damage_target(_decision(342, 343), state, ["tgt:343"]) == [DECLINE_DECISION]


def test_a_sorcery_follow_up_does_not_count_on_their_turn():
    prophecy = {
        "instance_id": 229,
        "name": "Fire Prophecy",
        "type_line": "Instant",
        "mana_cost": "{1}{R}",
        "controller_seat_id": 2,
        "oracle_text": "Fire Prophecy deals 3 damage to target creature.",
    }
    four_four = dict(ELEMENTAL, toughness=4)
    planner = _planner(oracle="fire prophecy deals 3 damage to target creature.", source=229)
    ours = _state(theirs=(four_four,), stack=(prophecy,))
    assert planner._prefer_lethal_damage_target(_decision(342, source="Fire Prophecy"), ours, ["tgt:342"]) == [
        "tgt:342"
    ]
    planner = _planner(oracle="fire prophecy deals 3 damage to target creature.", source=229)
    theirs = _state(theirs=(four_four,), stack=(prophecy,), active=1)
    assert planner._prefer_lethal_damage_target(_decision(342, source="Fire Prophecy"), theirs, ["tgt:342"]) == [
        DECLINE_DECISION
    ]


def test_the_chosen_mode_damage_is_what_the_combo_counts():
    # Mode 1 of Iroh's (1 to each) never targets; if a 1-damage mode were picked, 1 + 1 kills nothing.
    assert stack_burn_combo_on(_state(), 342, source_id=228, damage=1) is None
    assert stack_burn_combo_on(_state(), 342, source_id=228, damage=4) is not None
