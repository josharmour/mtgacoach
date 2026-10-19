"""The coach's fallback target pick for a damage spell is the opponent's creature, not ours.

(RulesEngine's ranking is covered by test_target_selection_disjunction's side table.)
"""

from arenamcp.coach import CoachEngine


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def test_postprocess_fallback_target_selection_harmful():
    """Verify _postprocess_advice fallback chooses opponent target over player's own creature for damage spell."""
    coach = _make_coach()
    game_state = {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "decision_context": {
            "type": "target_selection",
            "source_card": "Smite the Deathless",
            "source_oracle_text": "Smite the Deathless deals 3 damage to target creature.",
        },
        "battlefield": [
            {
                "instance_id": 226,
                "name": "Guttersnipe",
                "type_line": "Creature — Goblin Shaman",
                "power": 2,
                "toughness": 2,
                "owner_seat_id": 1,
                "controller_seat_id": 1,
            },
            {
                "instance_id": 221,
                "name": "Ravening Warg",
                "type_line": "Creature — Wolf",
                "power": 2,
                "toughness": 2,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
            {
                "instance_id": 204,
                "name": "Haunt of the Dead Marshes",
                "type_line": "Creature — Nightmare Elf",
                "power": 1,
                "toughness": 1,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
            },
        ],
    }

    # Invalid advice that doesn't match legal_actions forces postprocessor fallback to pick best legal action
    advice = "Cast Lightning Bolt."
    sanitized = coach._postprocess_advice(advice, game_state)

    # Must NOT pick Guttersnipe (YOURS)
    assert "Guttersnipe" not in sanitized
    assert "Ravening Warg" in sanitized or "Haunt" in sanitized
