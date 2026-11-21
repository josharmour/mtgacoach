"""Regression tests for Sprint 4 Batch 1 (S4-1 Eerie/Cast permanent triggers & S4-2 empty-board ETB-removal Aura hold)."""

from __future__ import annotations

import json

from arenamcp.action_planner import ActionPlanner
from arenamcp.board_assessment import assess
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search_moves import _text_permanent_triggers, unmodelled_effect


class _CaptureBackend:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict[str, str]] = []

    def complete(self, system_prompt: str, user_message: str, **kwargs) -> str:
        self.calls.append({"system": system_prompt, "user": user_message})
        return self.reply


class _FailBackend:
    def complete(self, *args, **kwargs):
        raise AssertionError("deterministic path should not call LLM")


def _plains(iid: int, seat: int = 1) -> dict:
    return {
        "instance_id": iid,
        "name": "Plains",
        "type_line": "Basic Land — Plains",
        "oracle_text": "{T}: Add {W}.",
        "card_types": ["CardType_Land"],
        "subtypes": ["SubType_Plains"],
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
    }


def test_s4_1_text_permanent_triggers_models_eerie_and_noncreature_cast():
    eerie = {
        "name": "Loom Scout",
        "type_line": "Creature — Human Scout",
        "mana_cost": "{W}",
        "oracle_text": "Eerie — Whenever an enchantment you control enters and whenever you fully unlock a Room, put a +1/+1 counter on target creature.",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 1,
    }
    prowess_like = {
        "name": "Storm Sprite",
        "type_line": "Creature — Faerie",
        "mana_cost": "{1}{R}",
        "oracle_text": "Flying\nWhenever you cast a noncreature spell, this creature gets +2/+0 until end of turn.",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 2,
    }

    t_eerie = _text_permanent_triggers(eerie)
    assert t_eerie is not None and len(t_eerie) == 1 and t_eerie[0].event == "ENCHANTMENT_ENTERS"
    t_cast = _text_permanent_triggers(prowess_like)
    assert t_cast is not None and len(t_cast) == 1 and t_cast[0].event == "CAST_SPELL"
    for card in (eerie, prowess_like):
        assert unmodelled_effect(card) == ""


def test_s4_1_control_complex_triggers_fail_closed():
    complex_eerie = {
        "name": "Glimmer Oracle",
        "type_line": "Creature — Spirit",
        "oracle_text": "Eerie — Whenever an enchantment you control enters, exile target creature until this leaves the battlefield.",
        "card_types": ["CardType_Creature"],
    }
    optional_trig = {
        "name": "Optional Mentor",
        "type_line": "Creature — Human",
        "oracle_text": "Whenever an enchantment you control enters, you may put a +1/+1 counter on target creature.",
        "card_types": ["CardType_Creature"],
    }
    activated = {
        "name": "Mana Engine",
        "type_line": "Creature — Elf",
        "oracle_text": "Whenever an enchantment you control enters, put a +1/+1 counter on target creature.\n{T}: Add {G}.",
        "card_types": ["CardType_Creature"],
    }
    non_self_pump = {
        "name": "Team Booster",
        "type_line": "Creature — Human",
        "oracle_text": "Whenever you cast a noncreature spell, target creature gets +2/+0 until end of turn.",
        "card_types": ["CardType_Creature"],
    }
    for card in (complex_eerie, optional_trig, activated):
        assert _text_permanent_triggers(card) is None
        assert unmodelled_effect(card) == "its triggered ability"
    assert _text_permanent_triggers(non_self_pump) is None


def test_s4_1_line_search_integration_battlefield_eerie_engine_finds_lethal():
    """Integration: a 1/1 Eerie creature on board + +1/+1 Aura in hand deals 1+1+1 = 3 lethal damage!"""
    eerie_on_board = {
        "instance_id": 10,
        "name": "Loom Scout",
        "type_line": "Creature — Human Scout",
        "mana_cost": "{W}",
        "oracle_text": "Eerie — Whenever an enchantment you control enters and whenever you fully unlock a Room, put a +1/+1 counter on target creature.",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 1,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }
    aura_in_hand = {
        "instance_id": 20,
        "name": "Flash Shield",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": "Flash\nEnchant creature you control\nAs long as this Aura entered this turn, enchanted creature has hexproof.\nEnchanted creature gets +1/+1.",
        "card_types": ["CardType_Enchantment"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 3,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 3, "lands_played": 0},
        ],
        "hand": [aura_in_hand],
        "battlefield": [_plains(1), _plains(2), eerie_on_board],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    res = assess(state).line_search
    assert res is not None and res.best is not None
    assert "Flash Shield" in res.best.steps[0].casts
    assert res.best.outcome == "win"


def test_s4_2_etb_removal_aura_held_on_empty_opposing_board_and_allowed_when_target_exists():
    """bug_20261121_101212: never waste an ETB-exile Aura on an empty opposing board; allow when opp has a target."""
    our_body = {
        "instance_id": 10,
        "name": "Loom Scout",
        "type_line": "Creature — Human Scout",
        "mana_cost": "{W}",
        "oracle_text": "",
        "card_types": ["CardType_Creature"],
        "power": 1,
        "toughness": 1,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }
    removal_aura = {
        "instance_id": 20,
        "name": "Spirit Ward",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{1}{W}",
        "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink and ward {2}.",
        "card_types": ["CardType_Enchantment"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    # 1. Opponent has only lands -> Spirit Ward is deterministically held (Pass without LLM!)
    empty_opp_state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 3,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 20, "lands_played": 0},
        ],
        "hand": [removal_aura],
        "battlefield": [_plains(1), _plains(2), our_body, _plains(3, seat=2)],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    dec = PendingDecision(
        (1, 1),
        "ActionsAvailable",
        (
            DecisionOption(
                "idx:0",
                "Cast Spirit Ward",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 20},
            ),
            DecisionOption("pass", "Pass"),
        ),
    )
    assert ActionPlanner(_FailBackend()).plan_decision_options(dec, empty_opp_state) == ["pass"]

    # 2. Control: opponent has a nonland permanent -> Spirit Ward stays in menu for LLM
    opp_cre = {
        "instance_id": 50,
        "name": "Opp Bear",
        "type_line": "Creature — Bear",
        "oracle_text": "",
        "card_types": ["CardType_Creature"],
        "power": 2,
        "toughness": 2,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
        "turn_entered_battlefield": 2,
    }
    target_state = {**empty_opp_state, "battlefield": [*empty_opp_state["battlefield"], opp_cre]}
    backend = _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Exile Opp Bear."}))
    assert ActionPlanner(backend).plan_decision_options(dec, target_state) == ["idx:0"]
    assert len(backend.calls) >= 1

    # 3. Control (Major #1): lethal on empty opposing board (opp at 2 life, 1/1 + +1/+0 = 2) is NOT held
    lethal_empty_state = {
        **empty_opp_state,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 2, "lands_played": 0},
        ],
    }
    backend_lethal = _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Lethal pump."}))
    assert ActionPlanner(backend_lethal).plan_decision_options(dec, lethal_empty_state) == ["idx:0"]
    assert len(backend_lethal.calls) >= 1

    # 4. Control (Major #3): opposing permanent with missing controller_seat_id fails open (not held)
    unknown_ctrl_perm = {
        "instance_id": 51,
        "name": "Mystery Relic",
        "type_line": "Artifact",
        "oracle_text": "",
        "card_types": ["CardType_Artifact"],
    }
    unk_state = {**empty_opp_state, "battlefield": [*empty_opp_state["battlefield"], unknown_ctrl_perm]}
    backend_unk = _CaptureBackend(json.dumps({"option_ids": ["idx:0"], "reasoning": "Exile relic."}))
    assert ActionPlanner(backend_unk).plan_decision_options(dec, unk_state) == ["idx:0"]
    assert len(backend_unk.calls) >= 1
