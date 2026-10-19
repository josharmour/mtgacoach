"""Regression tests for _postprocess_advice land-cleanup.

The previous implementation used a hardcoded regex that only knew the five
basic land names (Forest/Island/Swamp/Mountain/Plains/"a land"). Modern
decks run snow basics, Triomes, shocks, fetches, Cavern of Souls, etc., and
none of those got stripped when the LLM hallucinated a land play with no
land in hand. Now the cleanup is data-driven against the actual card names
present in the game state.
"""

from __future__ import annotations

import pytest

from arenamcp.coach import CoachEngine


def _state_no_lands_in_hand(extra_zones: dict | None = None) -> dict:
    """A priority-window state with one untapped Mountain so 'Cast Lightning Bolt'
    is actually legal — otherwise the legal-actions hard filter at the end of
    _postprocess_advice rewrites our prose entirely and we can't assert on it.
    """
    state = {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 0},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 4,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": [
            {"name": "Lightning Bolt", "type_line": "Instant", "mana_cost": "{R}"},
        ],
        "battlefield": [
            {
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "is_tapped": False,
                "color_production": ["R"],
            }
        ],
        "graveyard": [],
        "stack": [],
        "exile": [],
    }
    if extra_zones:
        for zone, cards in extra_zones.items():
            state.setdefault(zone, []).extend(cards)
    return state


def _make_coach() -> CoachEngine:
    # The postprocessor doesn't talk to the backend, so we can pass a stub.
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


@pytest.mark.parametrize(
    ("land", "type_line"),
    [
        ("Forest", "Basic Land — Forest"),
        # Modern lands were silently let through by the old basic-only regex.
        ("Spara's Headquarters", "Land — Forest Plains Island"),
        ("Snow-Covered Forest", "Basic Snow Land — Forest"),
    ],
    ids=["basic", "triome", "snow-basic"],
)
def test_strips_land_play_when_no_land_in_hand(land, type_line):
    coach = _make_coach()
    state = _state_no_lands_in_hand(extra_zones={"battlefield": [{"name": land, "type_line": type_line}]})

    out = coach._postprocess_advice(f"Play {land}. Cast Lightning Bolt at their face.", state)

    # The rest of the advice survives as written: had the land play stayed,
    # the legality filter would have replaced the whole line with a
    # [LOCAL FALLBACK] action, which also drops the land name.
    assert out == "Cast Lightning Bolt at their face."


def test_keeps_play_recommendation_when_land_actually_in_hand():
    coach = _make_coach()
    state = _state_no_lands_in_hand()
    state["hand"] = [
        {"name": "Forest", "type_line": "Basic Land — Forest"},
        {"name": "Lightning Bolt", "type_line": "Instant"},
    ]

    out = coach._postprocess_advice(
        "Play Forest, then cast Lightning Bolt.",
        state,
    )

    assert "Play Forest" in out


@pytest.mark.parametrize(
    ("advice", "expected"),
    [
        ("Play a land then cast Lightning Bolt.", "Cast Lightning Bolt"),
        ("Play a land, then cast Lightning Bolt.", "Cast Lightning Bolt"),
        ("Play a land and then cast Lightning Bolt at their face.", "Cast Lightning Bolt at their face."),
        ("Cast Lightning Bolt at their face, then play a land.", "Cast Lightning Bolt at their face"),
    ],
    ids=["then", "comma-then", "and-then", "trailing-then"],
)
def test_strips_generic_a_land_phrase_with_its_joiner(advice, expected):
    """The stripped land play used to leave "then cast Lightning Bolt" (or
    "..., then") behind, shown and spoken as the advice."""
    coach = _make_coach()

    assert coach._postprocess_advice(advice, _state_no_lands_in_hand()) == expected


def test_misspelled_card_name_is_corrected_from_game_state():
    """The fuzzy pass fixes a model typo against card names actually in play
    (it replaced a hardcoded typo dictionary)."""
    coach = _make_coach()

    out = coach._postprocess_advice("Cast Lightnign Bolt at their face.", _state_no_lands_in_hand())

    assert out == "Cast Lightning Bolt at their face."


def test_normalize_game_state_cards():
    coach = _make_coach()
    state = {
        "hand": [
            {
                "instance_id": 1,
                "name": "Unknown (ID: 75553)",
                "type_line": "",
                "card_types": ["Land"],
                "subtypes": ["Forest"],
            },
            {
                "instance_id": 2,
                "name": "Animal Attendant",
                "type_line": "Creature — Human Citizen",
                "card_types": ["Creature"],
                "subtypes": ["Human", "Citizen"],
            },
        ],
        "zones": {
            "battlefield": [
                {
                    "instance_id": 3,
                    "name": "Unknown (ID: 69698)",
                    "type_line": "",
                    "card_types": ["Land"],
                }
            ]
        },
    }

    coach._normalize_game_state_cards(state)

    assert state["hand"][0]["type_line"] == "Land — Forest"
    assert state["hand"][1]["type_line"] == "Creature — Human Citizen"
    assert state["zones"]["battlefield"][0]["type_line"] == "Land"
