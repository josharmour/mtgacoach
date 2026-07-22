"""Unit tests for enhanced advice-matching logic in coach.py.
"""

from __future__ import annotations
from typing import Any
from arenamcp.coach import CoachEngine


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0
        def complete(self, *a, **k):
            return ""
    return CoachEngine(backend=_Stub())


def _make_state(legal_actions: list[str], hand: list[dict] | None = None) -> dict[str, Any]:
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 4,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": hand or [],
        "battlefield": [],
        "graveyard": [],
        "stack": [],
        "exile": [],
        "legal_actions": legal_actions,
    }


def test_match_partial_cast_card_name():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Cast Michelangelo, Weirdness to 11 [OK]"],
        hand=[{"name": "Michelangelo, Weirdness to 11", "type_line": "Creature"}]
    )

    # Cast Michelangelo is a substring of Michelangelo, Weirdness to 11, so it should match
    out = coach._postprocess_advice("Cast Michelangelo to build your board.", state)
    assert "Cast Michelangelo" in out
    # Ensure it didn't get overridden by fallback
    assert "Cast Michelangelo, Weirdness to 11" not in out


def test_match_partial_activate_card_name():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Activate Bristly Bill, Spine Sower"],
    )

    out = coach._postprocess_advice("Activate Bristly Bill now.", state)
    assert "Activate Bristly Bill" in out


def test_match_play_land_partial_name():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Play Land: Spara's Headquarters"],
        hand=[{"name": "Spara's Headquarters", "type_line": "Land"}]
    )

    out = coach._postprocess_advice("Play Spara's Headquarters to get colors.", state)
    assert "Play Spara's Headquarters" in out


def test_match_generic_attack():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Declare Attackers: Bristly Bill, Spine Sower, Michelangelo, Weirdness to 11"],
    )

    out = coach._postprocess_advice("Attack with all creatures — 8 damage is damage.", state)
    assert "Attack with all creatures" in out


def test_match_generic_block():
    coach = _make_coach()
    # Mock defending turn
    state = _make_state(
        legal_actions=["Block with: Bristly Bill, Spine Sower"],
    )
    state["turn"]["active_player"] = 2  # opponent's turn
    state["turn"]["priority_player"] = 1

    out = coach._postprocess_advice("Block with everything to survive.", state)
    assert "Block with everything" in out


def test_match_negative_attack_is_not_overridden():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Declare Attackers: Bristly Bill, Spine Sower", "Pass"],
    )

    out = coach._postprocess_advice("Don't attack. Hold back to block.", state)
    # Don't attack is a passthrough phrase, so it should remain
    assert "Don't attack" in out


def test_match_need_mana_tag_stripping_and_fallback():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Cast Planar Incision [OK]", "Cast Northern Air Temple [NEED:B]", "Pass"],
        hand=[
            {"name": "Northern Air Temple", "type_line": "Enchantment"},
            {"name": "Planar Incision", "type_line": "Instant"},
        ]
    )

    out = coach._postprocess_advice("Cast Northern Air Temple to establish your first Shrine.", state)
    # Ensure Northern Air Temple was recognized (regex stripped [NEED:B])
    assert "Northern Air Temple" in out
    # Ensure it did NOT fall back to blind-casting Planar Incision
    assert "Planar Incision" not in out

