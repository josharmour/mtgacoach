"""2026-10-11: a haste creature whose rules text was not resolved yet must still be an attacker."""

from __future__ import annotations

from arenamcp.rules_engine import RulesEngine, card_has_haste


def _state(oracle: str, keywords: list[str]) -> dict:
    return {
        "players": [{"seat_id": 1, "is_local": True}, {"seat_id": 2}],
        "local_seat_id": 1,
        "turn": {"turn_number": 9, "phase": "Combat", "step": "Step_DeclareAttack", "active_player": 1},
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": ["Frostbite Pyromental", "Titanbones"],
        },
        "battlefield": [
            {
                "name": "Frostbite Pyromental",
                "type_line": "Creature — Elemental",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "turn_entered_battlefield": 9,
                "power": 4,
                "toughness": 4,
                "oracle_text": oracle,
                "keywords": keywords,
            },
            {
                "name": "Titanbones",
                "type_line": "Creature — Giant",
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "turn_entered_battlefield": 5,
                "power": 7,
                "toughness": 4,
                "oracle_text": "Reach",
            },
        ],
    }


def test_haste_is_read_from_the_objects_keywords_when_rules_text_is_missing():
    assert card_has_haste({"oracle_text": "", "keywords": ["trample", "haste"]})
    assert card_has_haste({"oracle_text": "Haste", "keywords": []})
    assert not card_has_haste({"oracle_text": "Trample", "keywords": ["trample"]})
    actions = RulesEngine._get_decision_actions(_state("", ["trample", "haste"]))
    assert actions[0].startswith("Attack with: Frostbite Pyromental")
    assert any(a.startswith("Attack with: Titanbones") for a in actions)
    # Outside the live declare step the local legality filter applies: no haste, no attack.
    stale = _state("", ["trample"])
    stale["turn"]["step"] = "Step_BeginCombat"
    sick = RulesEngine._get_decision_actions(stale)
    assert not any("Pyromental" in a for a in sick)


def test_attack_prompt_carries_this_turns_decisions_and_investments():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(Mock())
    state = _state("Haste\nAt the beginning of the end step, sacrifice this creature.", ["haste"])
    state["match_id"] = "m1"
    state["deck_catalog"] = {}
    decision = SimpleNamespace(
        request_type="ActionsAvailable",
        source_label="",
        options=[SimpleNamespace(option_id="idx:0", label="Cast Blazing Crescendo", meta={})],
    )
    planner._memory().record(
        state, decision, ["idx:0"], "Buff Titanbones to 7/4 so it attacks through blockers"
    )
    prompt = planner._build_action_prompt(
        state,
        trigger="decision_required",
        legal_actions=["Attack with: Titanbones (7/4)"],
        decision_context=state["decision_context"],
    )
    assert "DECISIONS YOU ALREADY MADE THIS TURN" in prompt and "Cast Blazing Crescendo" in prompt
    assert "THIS TURN'S INVESTMENTS" in prompt and "Frostbite Pyromental is sacrificed" in prompt


def test_activated_abilities_with_an_empty_hand_do_not_crash():
    """2026-10-11 Collector Sealed game 2: the ability loop read the hand loop's
    leftover `card`, so an empty hand raised UnboundLocalError and the whole
    legal-action list was lost; with cards in hand it checked the wrong card."""
    creature = {
        "name": "Living Library",
        "type_line": "Creature — Spirit",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "turn_entered_battlefield": 3,
        "oracle_text": "{T}: Draw a card.",
    }
    state = {
        "players": [{"seat_id": 1, "is_local": True, "lands_played": 1}, {"seat_id": 2}],
        "local_seat_id": 1,
        "turn": {"turn_number": 9, "phase": "Phase_Main1", "active_player": 1, "priority_player": 1},
        "hand": [],
        "stack": [],
        "battlefield": [creature],
    }
    assert "Activate Living Library" in RulesEngine.get_legal_actions(state)

    # The haste check reads the creature, not the last card left in hand.
    state["turn"]["turn_number"] = 3
    state["hand"] = [
        {
            "name": "Raging Goblin",
            "type_line": "Creature — Goblin",
            "oracle_text": "Haste",
            "mana_cost": "{R}",
        }
    ]
    assert "Activate Living Library" not in RulesEngine.get_legal_actions(state)
