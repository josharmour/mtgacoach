"""Regression tests for _postprocess_advice land-cleanup.

The previous implementation used a hardcoded regex that only knew the five
basic land names (Forest/Island/Swamp/Mountain/Plains/"a land"). Modern
decks run snow basics, Triomes, shocks, fetches, Cavern of Souls, etc., and
none of those got stripped when the LLM hallucinated a land play with no
land in hand. Now the cleanup is data-driven against the actual card names
present in the game state.
"""

from __future__ import annotations

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


def test_strips_basic_land_play_when_no_land_in_hand():
    coach = _make_coach()
    state = _state_no_lands_in_hand(
        extra_zones={"battlefield": [{"name": "Forest", "type_line": "Basic Land — Forest"}]}
    )

    out = coach._postprocess_advice(
        "Play Forest. Cast Lightning Bolt at their face.",
        state,
    )

    assert "Play Forest" not in out
    assert "Lightning Bolt" in out


def test_strips_triome_play_when_no_land_in_hand():
    """Modern lands like Triomes were silently let through by the old basic-only regex."""
    coach = _make_coach()
    state = _state_no_lands_in_hand(
        extra_zones={
            "battlefield": [
                {
                    "name": "Spara's Headquarters",
                    "type_line": "Land — Forest Plains Island",
                }
            ]
        }
    )

    out = coach._postprocess_advice(
        "Play Spara's Headquarters. Cast Lightning Bolt.",
        state,
    )

    assert "Spara's Headquarters" not in out
    assert "Lightning Bolt" in out


def test_strips_snow_basic_when_no_land_in_hand():
    coach = _make_coach()
    state = _state_no_lands_in_hand(
        extra_zones={
            "battlefield": [
                {"name": "Snow-Covered Forest", "type_line": "Basic Snow Land — Forest"}
            ]
        }
    )

    out = coach._postprocess_advice(
        "Play Snow-Covered Forest, then untap.",
        state,
    )

    assert "Snow-Covered Forest" not in out


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


def test_strips_generic_a_land_phrase():
    coach = _make_coach()
    state = _state_no_lands_in_hand()

    out = coach._postprocess_advice(
        "Play a land then cast Lightning Bolt.",
        state,
    )

    assert "Play a land" not in out
    # Don't assert on the rest of the prose — the legal-action filter can
    # replace it when nothing legal is mentioned. The point of this test is
    # that the generic "a land" phrase is recognized and stripped.


def test_no_typo_dict_means_typoed_card_passes_through_unchanged():
    """The hardcoded typo_fixes dict is gone. We assert removal: the previous
    Gemma-3N quirks list no longer rewrites prose. A typo with no matching
    card in game state stays as-is rather than getting corrected to one of
    the dict's hardcoded targets."""
    coach = _make_coach()
    state = _state_no_lands_in_hand()

    # "brerak out" was in the old typo_fixes dict; with no Break Out card
    # actually in state, the response should pass through.
    out = coach._postprocess_advice(
        "Cast brerak out at their face.",
        state,
    )

    assert "Break Out" not in out, (
        "the hardcoded typo_fixes dict should be gone — fuzzy matching alone "
        "shouldn't invent card names that aren't present in game state"
    )


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
            }
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
        }
    }
    
    coach._normalize_game_state_cards(state)
    
    assert state["hand"][0]["type_line"] == "Land — Forest"
    assert state["hand"][1]["type_line"] == "Creature — Human Citizen"
    assert state["zones"]["battlefield"][0]["type_line"] == "Land"

