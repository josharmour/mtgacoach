"""Unit tests for enhanced advice-matching logic in coach.py."""

from __future__ import annotations

from typing import Any

import pytest

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
        hand=[{"name": "Michelangelo, Weirdness to 11", "type_line": "Creature"}],
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
        hand=[{"name": "Spara's Headquarters", "type_line": "Land"}],
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


def test_need_tag_is_ignored_when_matching_and_no_other_spell_is_substituted():
    """Matching strips the [OK]/[NEED:x] markers, so advice naming a
    [NEED:B] spell counts as matching its legal action and is NOT replaced
    by the [OK] Planar Incision (the old failure: blind-casting a different
    spell). Note the tension with the prompt contract, which forbids
    recommending [NEED] cards at all (coach_prompts.py, CRITICAL MANA RULES):
    the advice to cast the unpayable spell survives here."""
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Cast Planar Incision [OK]", "Cast Northern Air Temple [NEED:B]", "Pass"],
        hand=[
            {"name": "Northern Air Temple", "type_line": "Enchantment"},
            {"name": "Planar Incision", "type_line": "Instant"},
        ],
    )

    out = coach._postprocess_advice("Cast Northern Air Temple to establish your first Shrine.", state)
    assert out == "Cast Northern Air Temple to establish your first Shrine."


def test_equip_target_in_hand_fixed_to_battlefield_creature():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Activate Ability: Well-Worn Spatula [OK]", "Cast Eagle of the Great Shelf", "Pass"],
        hand=[{"name": "Eagle of the Great Shelf", "type_line": "Creature — Bird Soldier"}],
    )
    state["battlefield"] = [
        {
            "name": "Bothersome Noisemaker",
            "type_line": "Creature — Goblin Bard",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        },
        {
            "name": "Well-Worn Spatula",
            "type_line": "Artifact — Equipment",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        },
    ]

    out = coach._postprocess_advice("Equip Spatula to Eagle of the Great Shelf.", state)
    # Target in hand (Eagle of the Great Shelf) should be corrected to battlefield creature (Bothersome Noisemaker)
    assert "Bothersome Noisemaker" in out
    assert "Eagle of the Great Shelf" not in out


def test_equip_target_in_hand_no_battlefield_creature():
    coach = _make_coach()
    state = _make_state(
        legal_actions=["Activate Ability: Well-Worn Spatula [OK]", "Cast Eagle of the Great Shelf", "Pass"],
        hand=[{"name": "Eagle of the Great Shelf", "type_line": "Creature — Bird Soldier"}],
    )
    state["battlefield"] = [
        {
            "name": "Well-Worn Spatula",
            "type_line": "Artifact — Equipment",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
        },
    ]

    out = coach._postprocess_advice("Equip Spatula to Eagle of the Great Shelf.", state)
    # With no creatures on battlefield, Equip to hand card should be rejected and fall back to legal action
    assert "Equip Spatula to Eagle of the Great Shelf" not in out


def _short_on_mana_state() -> dict[str, Any]:
    """2 Mountains on the battlefield + 1 in hand = 3 mana: a 5-mana Eagle can't follow."""
    state = _make_state(
        legal_actions=[
            "Cast Eagle of the Great Shelf",
            "Cast Esgaroth Garrison",
            "Activate Ability: Well-Worn Spatula [OK]",
            "Play Land: Mountain",
            "Pass",
        ],
        hand=[
            {"name": "Mountain", "type_line": "Basic Land — Mountain"},
            {"name": "Eagle of the Great Shelf", "type_line": "Creature — Bird Soldier", "mana_cost": "{4}{W}"},
        ],
    )
    mountain = {
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
        "is_tapped": False,
    }
    state["battlefield"] = [dict(mountain), dict(mountain)]
    return state


def _wrong_colors_state() -> dict[str, Any]:
    """Only Plains (on the battlefield and in hand): a {1}{R} spell can't follow."""
    plains = {"name": "Plains", "type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)"}
    return {
        "local_seat_id": 2,
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 20},
            {"seat_id": 2, "is_local": True, "life_total": 20, "lands_played": 0},
        ],
        "turn": {"active_player": 2, "priority_player": 2, "turn_number": 2, "phase": "Phase_Main1"},
        "battlefield": [dict(plains, owner_seat_id=2, controller_seat_id=2, is_tapped=False)],
        "hand": [
            dict(plains, owner_seat_id=2),
            {
                "name": "Bothersome Noisemaker",
                "type_line": "Creature — Goblin Bard",
                "mana_cost": "{1}{R}",
                "grp_id": 103465,
                "owner_seat_id": 2,
            },
        ],
        "legal_actions": ["Play Land: Plains", "Pass"],
    }


@pytest.mark.parametrize(
    ("make_state", "advice", "land_play", "spell"),
    [
        (_short_on_mana_state, "Play Mountain then cast Eagle of the Great Shelf.", "Play Mountain", "Eagle"),
        (_wrong_colors_state, "Play Plains then play Bothersome Noisemaker.", "Play Plains", "Noisemaker"),
    ],
    ids=["too-little-mana", "wrong-color"],
)
def test_spell_that_cannot_follow_the_land_drop_is_stripped(make_state, advice, land_play, spell):
    """Land-then-spell advice keeps the land drop and drops a spell the
    post-land mana can't pay for, whether short on total mana or on colour.
    (The castable counterpart is in test_coach_postprocess_color_castability.)"""
    out = _make_coach()._postprocess_advice(advice, make_state())

    assert spell not in out
    assert land_play in out
