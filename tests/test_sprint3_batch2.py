"""Regression tests for Sprint 3 Batch 2 (S3-5 combat attack calibration, S3-6 generic text buff-Aura modelling)."""

from __future__ import annotations

from arenamcp.combat_strategy import combat_choice
from arenamcp.line_search_moves import _text_buff_aura, unmodelled_effect


def _creature(iid: int, name: str, p: int, t: int, seat: int = 1, oracle: str = "") -> dict:
    return {
        "instance_id": iid,
        "name": name,
        "power": p,
        "toughness": t,
        "type_line": "Creature",
        "card_types": ["CardType_Creature"],
        "oracle_text": oracle,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }


def _combat_state(ours: list[dict], theirs: list[dict], our_life: int = 18, opp_life: int = 20) -> dict:
    return {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": our_life},
            {"seat_id": 2, "is_local": False, "life_total": opp_life},
        ],
        "battlefield": [*ours, *theirs],
        "hand": [],
        "stack": [],
        "graveyard": [],
        "exile": [],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": [c["name"] for c in ours],
            "legal_attacker_ids": [c["instance_id"] for c in ours],
            "raw_attackers": [
                {
                    "attackerInstanceId": c["instance_id"],
                    "instanceId": c["instance_id"],
                    "legalDamageRecipients": [{"playerSystemSeatId": 2}],
                }
                for c in ours
            ],
        },
    }


def test_s3_5_outnumber_attack_and_even_trade_initiative():
    """S3-5: outnumbering boards attack instead of holding; 2/2 vs 2/2 takes clean-trade initiative."""
    # 3/2 + 2/2 vs 2/2: attacks (sending the 2/2 to force an even trade without trading down the 3/2)
    ours = [_creature(1, "Vanguard", 3, 2, 1), _creature(2, "Scout", 2, 2, 1)]
    theirs = [_creature(3, "Guard", 2, 2, 2)]
    choice = combat_choice(_combat_state(ours, theirs, our_life=18, opp_life=20))
    assert choice is not None and len(choice.assignments) >= 1

    # 2/2 + 2/2 vs 2/2: both swing for 2 damage + clean trade
    ours_wide = [_creature(1, "Scout A", 2, 2, 1), _creature(2, "Scout B", 2, 2, 1)]
    choice_wide = combat_choice(_combat_state(ours_wide, theirs, our_life=18, opp_life=20))
    assert choice_wide is not None and len(choice_wide.assignments) == 2

    ours_even = [_creature(1, "Bear A", 2, 2, 1)]
    theirs_even = [_creature(3, "Bear B", 2, 2, 2)]
    choice_even = combat_choice(_combat_state(ours_even, theirs_even, our_life=20, opp_life=20))
    assert choice_even is not None and 1 in choice_even.assignments


def test_s3_5_control_chump_attack_into_bigger_wall_still_holds():
    """Control: attacking a 2/2 into a 3/3 that eats it for zero blockers killed still holds."""
    ours = [_creature(1, "Small Bear", 2, 2, 1)]
    theirs = [_creature(3, "Big Wall", 3, 3, 2)]
    choice = combat_choice(_combat_state(ours, theirs, our_life=20, opp_life=20))
    assert choice is not None and choice.assignments == {}


def test_s3_6_text_buff_aura_models_simple_cantrip_exile_and_scaling_auras():
    """S3-6: generic buff/cantrip/exile/scaling Auras are modelled deterministically without CardFacts."""
    simple_flash = {
        "name": "Flash Shield",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Flash\nEnchant creature you control\nAs long as this Aura entered this turn, enchanted creature has hexproof.\nEnchanted creature gets +1/+1.",
    }
    cantrip = {
        "name": "Winged Blessing",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Flash\nEnchant creature\nWhen this Aura enters, draw a card.\nEnchanted creature gets +1/+0 and has flying.",
    }
    exile_aura = {
        "name": "Spirit Ward",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink and ward {2}.",
    }
    scaling = {
        "name": "Radiant Plate",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1 for each enchantment you control and has first strike.",
    }

    assert _text_buff_aura(simple_flash) == ((1, 1), 0, False)
    assert _text_buff_aura(cantrip) == ((1, 0), 1, False)
    assert _text_buff_aura(exile_aura) == ((1, 0), 0, True)
    assert _text_buff_aura(scaling, our_enchantments=2) == ((3, 3), 0, False)
    for card in (simple_flash, cantrip, exile_aura, scaling):
        assert unmodelled_effect(card) == ""


def test_s3_6_control_negative_or_complex_auras_fail_closed():
    """Control: Pacifism, activated Auras, and triggered Auras still fail closed as unmodelled."""
    pacifism = {
        "name": "Quiet Cage",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature can't attack or block.",
    }
    activated = {
        "name": "Arcane Lens",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1 and has '{T}: Draw a card.'",
    }
    triggered = {
        "name": "War Banner",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.\nWhenever enchanted creature attacks, draw a card.",
    }
    dies_trigger = {
        "name": "Last Gift",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.\nWhen enchanted creature dies, draw two cards.",
    }
    conditional_buff = {
        "name": "Threshold Mantle",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nAs long as you have seven or more cards in your graveyard, enchanted creature gets +3/+3.",
    }
    other_scaling = {
        "name": "Forest Cloak",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1 for each Forest you control.",
    }
    for card in (pacifism, activated, triggered, dies_trigger, conditional_buff, other_scaling):
        assert _text_buff_aura(card) is None
        assert unmodelled_effect(card) == "its effect"


def test_s3_6_line_search_integration_models_scaling_and_exile_auras():
    """Integration: Moves._variants via line_search sees battlefield enchantments and ETB exile."""
    from arenamcp.board_assessment import assess

    plains = lambda iid: {  # noqa: E731
        "instance_id": iid,
        "name": "Plains",
        "type_line": "Basic Land — Plains",
        "oracle_text": "{T}: Add {W}.",
        "card_types": ["CardType_Land"],
        "subtypes": ["SubType_Plains"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
    }
    ench_perm = {
        "instance_id": 20,
        "name": "Sun Shrine",
        "type_line": "Enchantment",
        "oracle_text": "",
        "card_types": ["CardType_Enchantment"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    scaling_aura = {
        "instance_id": 30,
        "name": "Radiant Plate",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{W}",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1 for each enchantment you control and has first strike.",
        "card_types": ["CardType_Enchantment"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    # 2/2 attacker + 1 existing enchantment on board -> Radiant Plate gives +2/+2 (4 power = lethal vs 4 life!)
    state = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 4,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 18, "lands_played": 1},
            {"seat_id": 2, "is_local": False, "life_total": 4, "lands_played": 0},
        ],
        "hand": [scaling_aura],
        "battlefield": [plains(1), plains(2), ench_perm, _creature(10, "Knight", 2, 2, 1)],
        "stack": [],
        "graveyard": [],
        "exile": [],
    }
    res = assess(state).line_search
    assert res is not None and res.best is not None
    assert "Radiant Plate" in res.best.steps[0].casts
    assert res.best.outcome == "win"

    # ETB-exile buff Aura removes opponent's 4/4 blocker so our 3/3 swings for lethal vs 4 life!
    exile_aura = {
        "instance_id": 31,
        "name": "Spirit Ward",
        "type_line": "Enchantment — Aura",
        "mana_cost": "{1}{W}",
        "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink and ward {2}.",
        "card_types": ["CardType_Enchantment"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }
    state_exile = {
        **state,
        "hand": [exile_aura],
        "battlefield": [
            plains(1),
            plains(2),
            _creature(10, "Knight", 3, 3, 1),
            _creature(11, "Wall", 4, 4, 2),
        ],
    }
    res2 = assess(state_exile).line_search
    assert res2 is not None and res2.best is not None
    assert "Spirit Ward" in res2.best.steps[0].casts
    assert res2.best.outcome == "win"
