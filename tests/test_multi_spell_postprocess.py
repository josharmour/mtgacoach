"""Unit tests for issue #481 fixes:
- Post-land option formatting (choose one: X or Y)
- Multi-spell total CMC validation in postprocessing
- RulesDB thread safety
"""

from __future__ import annotations

import concurrent.futures
from arenamcp.coach import CoachEngine
from arenamcp.rules_db import RulesDB


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def test_format_post_land_planning_multiple_spells():
    """Verify that multiple spells enabled by a land drop use '(choose one: X or Y)'."""
    coach = _make_coach()
    state = {
        "players": [
            {"seat_id": 1, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "is_local": False},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 8,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": [
            {"name": "Forest", "type_line": "Basic Land — Forest"},
            {"name": "Michelangelo, Mutant BFF", "type_line": "Creature", "mana_cost": "{2}{G}{G}"},
            {"name": "Solid Ground", "type_line": "Enchantment", "mana_cost": "{3}{G}"},
        ],
        "battlefield": [
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "is_tapped": False},
        ],
        "stack": [],
    }

    lines = coach._format_post_land_planning(
        game_state=state,
        local_seat=1,
        valid_moves=["Cast Michelangelo, Mutant BFF", "Cast Solid Ground", "Play Land: Forest"],
        is_my_turn=True,
        phase="Phase_Main1",
    )

    assert len(lines) == 1
    assert "THEN: Play Forest → Cast (choose one: Michelangelo, Mutant BFF or Solid Ground)" in lines[0]


def test_postprocess_strips_multi_spell_when_over_mana_budget():
    """Verify that advice recommending 2x 4-CMC spells with 4 post-land mana is stripped."""
    coach = _make_coach()
    state = {
        "players": [
            {"seat_id": 1, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "is_local": False},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 8,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": [
            {"name": "Forest", "type_line": "Basic Land — Forest"},
            {"name": "Michelangelo, Mutant BFF", "type_line": "Creature", "mana_cost": "{2}{G}{G}"},
            {"name": "Solid Ground", "type_line": "Enchantment", "mana_cost": "{3}{G}"},
        ],
        "battlefield": [
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Forest", "type_line": "Basic Land — Forest", "owner_seat_id": 1, "is_tapped": False},
        ],
        "stack": [],
        "_last_prompt_lines": [
            "THEN: Play Forest → Cast (choose one: Michelangelo, Mutant BFF or Solid Ground)"
        ],
    }

    advice = "Play Forest then cast Michelangelo, and Solid Ground to double counters."
    out = coach._postprocess_advice(advice, state)

    # Should keep single spell and strip impossible secondary spell
    assert "Solid Ground" not in out
    assert "Michelangelo" in out


def test_rules_db_multi_threaded_query():
    """Verify RulesDB query works across worker threads without ProgrammingError."""
    rules_db = RulesDB()

    def _query():
        return rules_db.get_rules_for_situation({"turn": {"phase": "Phase_Combat"}}, "combat_attackers")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_query) for _ in range(8)]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 8


def test_postprocess_strips_summoning_sick_attack_clause():
    """Verify that advice advising 'Play Hero in Training then attack with it' strips the invalid attack clause."""
    coach = _make_coach()
    state = {
        "players": [
            {"seat_id": 1, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "is_local": False},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 5,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": [
            {"name": "Hero in Training", "type_line": "Creature — Human Hero", "mana_cost": "{2}{W}", "oracle_text": "When this creature enters, draw a card."},
        ],
        "battlefield": [
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 1, "is_tapped": False},
        ],
        "stack": [],
    }

    advice = "Play Hero in Training then attack with it."
    out = coach._postprocess_advice(advice, state)

    assert "Hero in Training" in out
    assert "attack" not in out.lower()


def test_postprocess_strips_summoning_sick_attack_clause_with_comma():
    """Verify that card names with commas (e.g. Agent 13, Sharon Carter) are matched and stripped."""
    coach = _make_coach()
    state = {
        "players": [
            {"seat_id": 1, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "is_local": False},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 9,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": [
            {
                "name": "Agent 13, Sharon Carter",
                "type_line": "Legendary Creature — Human Spy Hero",
                "mana_cost": "{2}{W}",
                "oracle_text": "Whenever a creature you control attacks alone, investigate.",
            },
        ],
        "battlefield": [
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 1, "is_tapped": False},
            {"name": "Plains", "type_line": "Basic Land — Plains", "owner_seat_id": 1, "is_tapped": False},
        ],
        "stack": [],
        "legal_actions": ["Cast Agent 13, Sharon Carter [OK]"],
    }

    advice = "Play Agent 13, Sharon Carter then attack alone for a clue."
    out = coach._postprocess_advice(advice, state)

    assert "Agent 13, Sharon Carter" in out
    assert "attack" not in out.lower()

