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


SNAPCASTER = {
    "instance_id": 470,
    "name": "Snapcaster Mage",
    "mana_cost": "{1}{U}",
    "type_line": "Creature — Human Wizard",
    "card_types": ["Creature"],
    "power": 2,
    "toughness": 1,
    "oracle_text": (
        "Flash\nWhen Snapcaster Mage enters, target instant or sorcery card in your graveyard "
        "gains flashback until end of turn. The flashback cost is equal to its mana cost."
    ),
}

CRYPTIC = {
    "instance_id": 236,
    "name": "Cryptic Command",
    "mana_cost": "{1}{U}{U}{U}",
    "type_line": "Instant",
    "card_types": ["Instant"],
    "owner_seat_id": 1,
}

OPT = {
    "instance_id": 237,
    "name": "Opt",
    "mana_cost": "{U}",
    "type_line": "Instant",
    "card_types": ["Instant"],
    "owner_seat_id": 1,
}


def _snapcaster_board(untapped_lands: int, graveyard: list[dict]) -> dict:
    return {
        "turn": {"turn_number": 9, "phase": "Phase_Main1", "step": "", "active_player": 1},
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 14},
            {"seat_id": 2, "is_local": False, "life_total": 17},
        ],
        "hand": [SNAPCASTER],
        "graveyard": list(graveyard),
        "battlefield": [
            {
                "instance_id": 100 + i,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "card_types": ["Land"],
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": False,
                "oracle_text": "{T}: Add {U}.",
            }
            for i in range(untapped_lands)
        ],
    }


def test_snapcaster_with_unpayable_graveyard_target_warns_and_refutes_claim():
    from arenamcp.action_planner import ActionPlanner, board_math_option_pick
    from arenamcp.cast_notes import graveyard_flashback_note, unpayable_graveyard_flashback
    from arenamcp.decisions import DecisionOption, PendingDecision
    from arenamcp.game_plan import decision_claim_notes

    # 2 mana open ({1}{U}): casting Snapcaster Mage leaves 0 mana, cannot pay Cryptic Command ({1}{U}{U}{U}).
    board_2_mana = _snapcaster_board(2, [CRYPTIC])
    info = unpayable_graveyard_flashback(board_2_mana, SNAPCASTER)
    assert info is not None and info["reason"] == "unpayable_targets" and info["mana_after"] == 0
    note = graveyard_flashback_note(board_2_mana, SNAPCASTER)
    assert "FLASHBACK UNTIL END OF TURN ONLY" in note
    assert "leaves 0 mana" in note and "Cryptic Command ({1}{U}{U}{U})" in note

    # Empty graveyard also warns.
    empty_note = graveyard_flashback_note(_snapcaster_board(2, []), SNAPCASTER)
    assert "no instant or sorcery card to target" in empty_note

    # With 3 mana open and Opt ({U}) in graveyard (1 mana left >= 1), no warning.
    assert graveyard_flashback_note(_snapcaster_board(3, [CRYPTIC, OPT]), SNAPCASTER) == ""
    # With 6 mana open and Cryptic Command ({1}{U}{U}{U}) in graveyard (4 mana left >= 4), no warning.
    assert graveyard_flashback_note(_snapcaster_board(6, [CRYPTIC]), SNAPCASTER) == ""

    # False-claim check catches claiming Snapcaster can target/preserve Cryptic Command with 0 mana left.
    claims = decision_claim_notes(
        "With 2 mana open, casting Snapcaster Mage develops a 2/1 body and can target Cryptic Command "
        "in the graveyard to preserve flashback value.",
        board_2_mana,
        cast_ids=[470],
    )
    assert any("Snapcaster Mage grants flashback only until end of turn" in c for c in claims)

    # Second-look concern and board-math fallback hold Snapcaster Mage in Main1 when flashback is unpayable.
    decision = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(option_id="pass", label="Pass", meta={"actionType": "ActionType_Pass"}),
            DecisionOption(
                option_id="cast:470",
                label="Cast Snapcaster Mage [Cost: 1U]",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 470, "grpId": 789},
            ),
        ),
    )
    planner = ActionPlanner(backend=None)
    concerns = planner._second_look_concerns(
        decision,
        board_2_mana,
        ["cast:470"],
        "Casting Snapcaster Mage to target Cryptic Command for flashback.",
    )
    assert any(kind == "wasted_flashback" for kind, _text in concerns)
    picked, _why = board_math_option_pick(decision, board_2_mana)
    assert picked == ["pass"]
