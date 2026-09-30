"""Engine payment solutions outrank local mana estimates in every prompt section."""

from copy import deepcopy

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.coach import CoachEngine
from arenamcp.decisions import build_pending_decision


def _spell(cost="{10}", instance_id=101):
    return {
        "name": "Test Spell",
        "instance_id": instance_id,
        "grp_id": 123,
        "mana_cost": cost,
        "type_line": "Creature",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }


def _cast(**payment):
    return {"actionType": "Cast", "grpId": 123, "instanceId": 101, **payment}


def _state(actions, *, hand=None):
    return {
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}],
        "turn": {"turn_number": 8, "active_player": 1, "priority_player": 1, "phase": "Phase_Main1"},
        "battlefield": [],
        "hand": [_spell()] if hand is None else hand,
        "stack": [],
        "_bridge_request_type": "ActionsAvailable",
        "_bridge_actions": actions,
        "_bridge_can_pass": True,
        "legal_actions": ["Cast Test Spell [NEED:10]", "Pass"],
    }


@pytest.mark.parametrize("payment", [{"hasAutoTap": True}, {"autoTapSolution": {}}])
@pytest.mark.parametrize("cost", ["{10}", "{G}{G}{G}", "{X}{G}", None])
def test_engine_payment_overrides_local_mana_and_printed_cost(payment, cost, monkeypatch):
    coach = CoachEngine.__new__(CoachEngine)

    def no_mana_estimate(*args):
        pytest.fail("Live castability must not calculate a local mana pool")

    monkeypatch.setattr(coach, "_format_mana_info", no_mana_estimate)
    context = coach._format_game_context(_state([_cast(**payment)], hand=[_spell(cost)]), for_planner=True)

    assert "Legal: Cast Test Spell [OK]" in context
    assert "[S,OK]" in context
    assert "NEED:" not in context
    assert "X=0" not in context
    assert "Arena's current payment solutions" in context


def test_engine_unpayable_overrides_sufficient_local_mana():
    state = _state([_cast(hasAutoTap=False)], hand=[_spell("{0}")])
    state["legal_actions"] = ["Cast Test Spell [OK]", "Pass"]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state)

    assert "Legal: Pass" in context
    assert "[S,CANNOT AUTO-PAY]" in context
    assert "[S,OK]" not in context


@pytest.mark.parametrize("request_type", ["ActionsAvailable", "SelectTargets", "PayCosts"])
@pytest.mark.parametrize("actions", [[], None])
def test_fresh_empty_or_non_cast_request_never_reuses_stale_castability(request_type, actions):
    state = _state(actions)
    state["_bridge_request_type"] = request_type
    state["legal_actions_raw"] = [_cast(hasAutoTap=True)]
    coach = CoachEngine.__new__(CoachEngine)
    context = coach._format_game_context(state, for_planner=True)

    assert coach._resolve_raw_legal_actions(state) == []
    assert "[S,NOT AVAILABLE]" in context
    assert "[S,OK]" not in context
    if request_type == "ActionsAvailable":
        assert "Legal: Pass" in context


def test_payability_is_per_instance_and_accepts_any_payable_alternative():
    state = _state(
        [_cast(hasAutoTap=False), _cast(autoTapSolution={})],
        hand=[_spell(), _spell(instance_id=102)],
    )
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)

    assert "Test Spell #1 {10} [S,OK]" in context
    assert "Test Spell #2 {10} [S,NOT AVAILABLE]" in context


@pytest.mark.parametrize("payment", [{"hasAutoTap": True}, {"autoTapSolution": {}}])
def test_post_filter_cannot_strip_engine_approved_cast_for_estimated_mana(payment):
    coach = CoachEngine.__new__(CoachEngine)
    moves = ["Cast Test Spell [OK]", "Cast Other Spell", "Pass"]
    lines = ["=== GAME ===", "Legal: " + ", ".join(moves), "LegalGRE: []"]
    coach._post_filter_uncastable_legal_moves(
        lines, moves, [_cast(**payment)], set(), {"Test Spell"}, _state([])
    )

    assert "Cast Test Spell [OK]" in lines[1]
    assert '"instanceId":101' in lines[2]
    assert '"hasAutoTap":true' in lines[2]


def test_mana_ability_text_survives_planner_compaction():
    state = _state([_cast(hasAutoTap=True)])
    state["battlefield"] = [
        {
            "name": "Variable Mana Creature",
            "type_line": "Creature — Elf",
            "owner_seat_id": 1,
            "turn_entered_battlefield": 2,
            "oracle_text": "{oT}: Add {oG} for each Elf you control.",
        }
    ]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert "for each Elf you control" in context


def test_local_estimate_remains_available_without_engine_menu():
    state = _state([])
    del state["_bridge_request_type"]
    del state["_bridge_actions"]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert "NEED:10" in context


def test_vehicle_crew_text_and_current_creature_status_remain_visible():
    state = _state([])
    state["battlefield"] = [
        {
            "name": "Test Vehicle",
            "type_line": "Artifact — Vehicle",
            "card_types": ["CardType_Artifact", "CardType_Creature"],
            "owner_seat_id": 1,
            "turn_entered_battlefield": 2,
            "oracle_text": "Crew 4",
        }
    ]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert "Crew 4" in context
    assert "CREATURE NOW" in context


def test_old_equipment_keeps_haste_and_equip_cost_visible_to_planner():
    state = _state([])
    state["battlefield"] = [
        {
            "name": "Test Boots",
            "type_line": "Artifact — Equipment",
            "owner_seat_id": 1,
            "turn_entered_battlefield": 2,
            "oracle_text": "Equipped creature has haste and shroud.\nEquip {o0}",
        }
    ]
    context = CoachEngine.__new__(CoachEngine)._format_game_context(state, for_planner=True)
    assert "Equipped creature has haste and shroud" in context
    assert "Equip" in context


def test_typed_prompt_uses_current_decision_instead_of_stale_snapshot():
    class CapturingBackend:
        def complete(self, system, user, *args, **kwargs):
            self.prompt = user
            self.system = system
            return '{"option_ids": ["idx:0"]}'

    state = _state([])
    previous = deepcopy(state)
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [_cast(hasAutoTap=True)],
        },
        resolve_name=lambda grp_id: "Test Spell",
    )
    backend = CapturingBackend()
    assert ActionPlanner(backend=backend).plan_decision_options(decision, state) == ["idx:0"]
    assert "Arena confirms payable now" in backend.prompt
    assert "Legal: Cast Test Spell [OK]" in backend.prompt
    assert "[S,OK]" in backend.prompt
    assert "NEED:" not in backend.prompt
    assert "payable individually now" in backend.system
    assert state == previous
