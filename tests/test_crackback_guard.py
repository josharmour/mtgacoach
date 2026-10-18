"""The combat plan's lethal-claim correction and crackback guard (2026-10-10 game 3, turn 34)."""

from __future__ import annotations

import logging

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.board_assessment import _CACHE


def creature(iid, name, ctrl, power, toughness, keywords=(), tapped=False, entered=1):
    return {
        "instance_id": iid,
        "grp_id": iid,
        "name": name,
        "owner_seat_id": ctrl,
        "controller_seat_id": ctrl,
        "card_types": ["Creature"],
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "keywords": list(keywords),
        "oracle_text": " ".join(k.capitalize() for k in keywords),
        "is_tapped": tapped,
        "turn_entered_battlefield": entered,
        "summoning_sickness": False,
        "mana_cost": "{2}{U}",
    }


def land(iid, ctrl, name="Island"):
    return {
        "instance_id": iid,
        "grp_id": iid,
        "name": name,
        "owner_seat_id": ctrl,
        "controller_seat_id": ctrl,
        "card_types": ["Land"],
        "type_line": "Basic Land",
        "is_tapped": False,
    }


def turn_34(our_life=4, opp_life=10, their_board=True):
    ours = [
        creature(423, "Cadet", 1, 7, 7),
        creature(525, "Geist of Saint Thalia", 1, 1, 2, ("flying",)),
        creature(565, "Theorix Metamage", 1, 3, 3, ("flying",)),
    ]
    theirs = (
        [
            creature(539, "Danitha, Spear of Agony", 2, 3, 3, ("first strike",)),
            creature(613, "Rank Rat", 2, 2, 2),
            creature(607, "Blessed Ghoul", 2, 1, 1, ("lifelink",)),
            creature(266, "Koth of the Homestead", 2, 4, 5),
        ]
        if their_board
        else []
    )
    lands = [land(900 + i, 1) for i in range(8)] + [land(950 + i, 2, "Plains") for i in range(7)]
    return {
        "match_id": "crackback-test",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 34,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Combat",
            "step": "DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "life_total": our_life, "is_local": True, "lands_played": 1, "mana_pool": {}},
            {"seat_id": 2, "life_total": opp_life, "is_local": False, "lands_played": 1, "mana_pool": {}},
        ],
        "battlefield": ours + theirs + lands,
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "command": [],
        "zones": {"opponent_hand_count": 0, "opponent_library_count": 20, "library_count": 10},
        "legal_actions": [
            "Attack with: Cadet (7/7)",
            "Attack with: Geist of Saint Thalia (1/2)",
            "Attack with: Theorix Metamage (3/3)",
        ],
        "decision_context": {"type": "declare_attackers", "legal_attacker_ids": [423, 525, 565]},
    }


def all_out_plan():
    return ActionPlan(
        actions=[
            GameAction(
                ActionType.DECLARE_ATTACKERS,
                attacker_names=["Cadet", "Geist of Saint Thalia", "Theorix Metamage"],
                attacker_instance_ids=[423, 525, 565],
                attacker_targets={
                    "Cadet": "Opponent",
                    "Geist of Saint Thalia": "Opponent",
                    "Theorix Metamage": "Opponent",
                },
                reasoning="Swing with everything.",
            )
        ],
        overall_strategy="All-out attack for lethal this turn.",
    )


def test_false_lethal_claim_is_corrected_and_blockers_stay_home_at_four_life(caplog):
    _CACHE.clear()
    state = turn_34()
    plan = all_out_plan()
    planner = ActionPlanner(backend=None)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner._check_attack_crackback(plan, state, state["decision_context"])
    assert "Board math: not lethal this turn" in plan.overall_strategy
    assert "Attack plan claims lethal this turn but the board math does not" in caplog.text
    declared = plan.actions[0]
    assert declared.action_type == ActionType.DECLARE_ATTACKERS
    assert len(declared.attacker_names) < 3, declared.attacker_names
    assert len(declared.attacker_instance_ids) == len(declared.attacker_names)
    assert set(declared.attacker_targets) == set(declared.attacker_names)
    assert plan.fallback_reason == "planner_crackback_guard"
    assert "Crackback guard" in caplog.text and "Crackback guard" in declared.reasoning
    assert plan.voice_advice.startswith("Holding ")


def test_a_real_lethal_or_a_safe_crackback_is_left_alone():
    _CACHE.clear()
    planner = ActionPlanner(backend=None)
    # They are at 1 with no blockers: the all-out attack is lethal and stands.
    state = turn_34(opp_life=1, their_board=False)
    plan = all_out_plan()
    planner._check_attack_crackback(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_names == ["Cadet", "Geist of Saint Thalia", "Theorix Metamage"]
    assert plan.fallback_reason == ""
    # At 20 life the same crackback is survivable: the plan stands, only the claim is corrected.
    _CACHE.clear()
    state = turn_34(our_life=20)
    plan = all_out_plan()
    planner._check_attack_crackback(plan, state, state["decision_context"])
    assert plan.actions[0].attacker_names == ["Cadet", "Geist of Saint Thalia", "Theorix Metamage"]
    assert "Board math: not lethal this turn" in plan.overall_strategy
