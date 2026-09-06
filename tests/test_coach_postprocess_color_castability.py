"""Tests to verify that coach postprocessing filters out uncastable spell recommendations

due to missing color requirements (e.g. recommending red spells with only white lands).
"""

from arenamcp.coach import CoachEngine


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def test_postprocess_filters_uncastable_color_spell():
    """Verify advice recommending a red spell when only white lands exist is stripped."""
    coach = _make_coach()
    game_state = {
        "local_seat_id": 2,
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 20},
            {"seat_id": 2, "is_local": True, "life_total": 20, "lands_played": 0},
        ],
        "turn": {
            "active_player": 2,
            "priority_player": 2,
            "turn_number": 2,
            "phase": "Phase_Main1",
        },
        "battlefield": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "oracle_text": "({T}: Add {W}.)",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
            }
        ],
        "hand": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "oracle_text": "({T}: Add {W}.)",
                "owner_seat_id": 2,
            },
            {
                "name": "Bothersome Noisemaker",
                "type_line": "Creature — Goblin Bard",
                "mana_cost": "{1}{R}",
                "grp_id": 103465,
                "owner_seat_id": 2,
            },
        ],
        "legal_actions": [
            "Play Land: Plains",
            "Pass",
        ],
    }

    advice = "Play Plains then play Bothersome Noisemaker."
    sanitized = coach._postprocess_advice(advice, game_state)
    # Must remove the uncastable red spell suggestion ("then play Bothersome Noisemaker")
    assert "Bothersome Noisemaker" not in sanitized
    assert "Play Plains" in sanitized


def test_postprocess_allows_castable_color_spell():
    """Verify advice recommending a spell matching available colors is preserved."""
    coach = _make_coach()
    game_state = {
        "local_seat_id": 2,
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 20},
            {"seat_id": 2, "is_local": True, "life_total": 20, "lands_played": 0},
        ],
        "turn": {
            "active_player": 2,
            "priority_player": 2,
            "turn_number": 2,
            "phase": "Phase_Main1",
        },
        "battlefield": [
            {
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "oracle_text": "({T}: Add {W}.)",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": False,
            }
        ],
        "hand": [
            {
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "oracle_text": "({T}: Add {R}.)",
                "owner_seat_id": 2,
            },
            {
                "name": "Bothersome Noisemaker",
                "type_line": "Creature — Goblin Bard",
                "mana_cost": "{1}{R}",
                "grp_id": 103465,
                "owner_seat_id": 2,
            },
        ],
        "legal_actions": [
            "Play Land: Mountain",
            "THEN: Play Mountain -> Cast Bothersome Noisemaker",
            "Pass",
        ],
    }

    advice = "Play Mountain then cast Bothersome Noisemaker."
    sanitized = coach._postprocess_advice(advice, game_state)
    assert "Bothersome Noisemaker" in sanitized
