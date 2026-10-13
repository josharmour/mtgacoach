"""Timing notes for casts during combat and loot modes with an empty hand (2026-10-10 FRA game 3)."""

from __future__ import annotations

from arenamcp.cast_notes import combat_cast_note, loot_mode_note

DUELIST = {"name": "Divining Duelist", "card_types": ["Creature"], "oracle_text": "Flash"}
GASP = {"name": "Last Gasp", "card_types": ["Instant"]}


def state(phase="Combat", step="FirstStrikeDamage", active=1, local=2, hand=()):
    return {
        "turn": {"turn_number": 20, "phase": phase, "step": step, "active_player": active},
        "local_seat_id": local,
        "hand": list(hand),
    }


def test_creature_cast_after_blocks_in_their_combat_cannot_block():
    note = combat_cast_note(state(), DUELIST)
    assert "CANNOT block this combat" in note
    assert combat_cast_note(state(step="DeclareBlock"), DUELIST)  # Arena's own step name
    assert combat_cast_note(state(step="Step_CombatDamage"), DUELIST)
    assert combat_cast_note(state(), GASP) == ""  # an instant is unaffected
    assert combat_cast_note(state(step="DeclareAttack"), DUELIST) == ""  # before blocks it can still block
    assert combat_cast_note(state(phase="Main1", step="None"), DUELIST) == ""
    assert "cannot attack this combat" in combat_cast_note(state(active=2), DUELIST)


def test_loot_mode_with_an_empty_hand_is_a_mill():
    label = "Mode 3: Draw a card, then discard a card."
    note = loot_mode_note(label, state(hand=()))
    assert "your hand has 0 card(s)" in note and "no card is gained" in note
    assert loot_mode_note(label, state(hand=({"name": "Island"},))) == ""
    assert loot_mode_note("Mode 1: Tap target creature.", state(hand=())) == ""
    assert "you keep 1 of the 2 drawn" in loot_mode_note(
        "Draw two cards, then discard a card.", state(hand=())
    )


# ── 2026-10-11 10:44: a pump cast precombat, then no attack ──────────────────

CRESCENDO = {
    "name": "Blazing Crescendo",
    "type_line": "Instant",
    "oracle_text": "Target creature gets +3/+1 until end of turn. Exile the top card of your library.",
}


def test_pump_instant_in_our_precombat_main_gets_a_timing_note():
    from arenamcp.cast_notes import trick_timing_note

    ours = state(phase="Main1", step="", active=1, local=1)
    assert "combat trick cast BEFORE combat" in trick_timing_note(ours, CRESCENDO)
    assert trick_timing_note(state(phase="Main2", step="", active=1, local=1), CRESCENDO) == ""
    assert trick_timing_note(state(phase="Combat", step="DeclareBlock", active=1, local=1), CRESCENDO) == ""
    assert trick_timing_note(state(phase="Main1", step="", active=2, local=1), CRESCENDO) == ""  # their turn
    assert trick_timing_note(ours, GASP) == ""  # removal is not a pump
    assert trick_timing_note(ours, DUELIST) == ""


def test_investments_name_end_step_sacrifices_and_this_turns_pumps():
    from arenamcp.cast_notes import turn_investments_note

    board = {
        "turn": {"turn_number": 9, "phase": "Combat", "step": "DeclareAttack", "active_player": 1},
        "local_seat_id": 1,
        "deck_catalog": {106511: {"power": "4"}, 106372: {"power": "4"}},
        "battlefield": [
            {
                "name": "Frostbite Pyromental",
                "grp_id": 106372,
                "type_line": "Creature — Elemental",
                "controller_seat_id": 1,
                "turn_entered_battlefield": 9,
                "power": 4,
                "toughness": 4,
                "oracle_text": "Haste\nAt the beginning of the end step, sacrifice this creature.",
            },
            {
                "name": "Titanbones, Towering Heart",
                "grp_id": 106511,
                "type_line": "Creature — Giant",
                "controller_seat_id": 1,
                "turn_entered_battlefield": 5,
                "power": 7,
                "toughness": 4,
                "oracle_text": "Reach",
            },
            {"name": "Forest", "grp_id": 1, "type_line": "Basic Land", "controller_seat_id": 1},
            {
                "name": "Cadet",
                "grp_id": 2,
                "type_line": "Creature",
                "controller_seat_id": 2,
                "power": 9,
                "toughness": 9,
            },
        ],
    }
    note = turn_investments_note(board)
    assert "Frostbite Pyromental is sacrificed at the beginning of this end step" in note
    assert "Titanbones, Towering Heart is pumped to 7/4" in note and "printed 4" in note
    assert "Cadet" not in note
    assert turn_investments_note({**board, "turn": {**board["turn"], "active_player": 2}}) == ""
    quiet = {**board, "battlefield": [board["battlefield"][2]]}
    assert turn_investments_note(quiet) == ""
