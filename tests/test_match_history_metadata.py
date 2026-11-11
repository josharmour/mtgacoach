import json

from arenamcp.match_history import parse_replay_cosmetics, record_from_game_end


def test_record_from_game_end_extracts_metadata():
    snapshot = {
        "match_id": "match-123",
        "opponent_name": "Sparky_Bot",
        "format_name": "Standard Ranked",
        "event_id": "Standard_Ranked",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn_info": {"turn_number": 7},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 0, "name": "Sparky_Bot"},
        ],
        "battlefield": [
            {
                "instance_id": 101,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "owner_seat_id": 2,
            },
            {
                "instance_id": 102,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "owner_seat_id": 2,
            },
            {
                "instance_id": 103,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "owner_seat_id": 1,
            },
        ],
        "graveyard": [
            {
                "instance_id": 104,
                "name": "Lightning Bolt",
                "mana_cost": "{R}",
                "owner_seat_id": 2,
            }
        ],
    }

    rec = record_from_game_end(
        match_id="match-123",
        result="win",
        game_state_snapshot=snapshot,
    )

    assert rec.match_id == "match-123"
    assert rec.result == "win"
    assert rec.opponent_name == "Sparky_Bot"
    assert rec.format_name == "Standard Ranked"
    assert rec.turns == 7
    assert rec.local_life_final == 20
    assert rec.opponent_life_final == 0
    # Opponent had Island (U), Mountain (R), Lightning Bolt (R)
    assert rec.opponent_colors_seen == ["R", "U"]


def test_parse_replay_cosmetics_reads_the_replay_header(tmp_path):
    header = {"Local": {"ScreenName": "me"}, "Opponent": {"ScreenName": "Sparky_Bot"}, "BattlefieldId": 7}
    replay = tmp_path / "match.rply"
    replay.write_text("#Version 3\n" + json.dumps(header) + '\n{"greToClientEvent": {}}\n', encoding="utf-8")
    assert parse_replay_cosmetics(str(replay)) == header

    not_a_replay = tmp_path / "notes.txt"
    not_a_replay.write_text(json.dumps(header) + "\n", encoding="utf-8")
    assert parse_replay_cosmetics(str(not_a_replay)) is None
    assert parse_replay_cosmetics(str(tmp_path / "missing.rply")) is None


def test_record_from_game_end_records_deck_identity_and_autopilot_flag():
    """Real logged case: evidence bundle 00651f77… (2026-11-10 Ladder loss,
    game 1, autopilot enabled) carries final_state.deck_cards (grpIds) and
    settings autopilot_enabled — the game-end record must preserve both so
    per-deck autopilot results are possible."""
    snapshot = {
        "opponent_name": "Opponent",
        "format_name": "Ladder",
        "turn": {"turn_number": 9},
        "deck_cards": [111] * 4 + [222] * 20 + [333] * 36,
    }

    rec = record_from_game_end("match-deck", "loss", snapshot, autopilot=True)

    assert rec.deck_identity  # stable hash of the deck's card list
    assert rec.autopilot is True

    # The same card list must yield the same identity as the playbook cache
    # key (deck_strategy.deck_identity), so per-deck rows line up with the
    # deck the coach actually prepared for.
    again = record_from_game_end("match-deck-2", "win", dict(snapshot), autopilot=False)
    assert again.deck_identity == rec.deck_identity
    assert again.autopilot is False


def test_legacy_record_leaves_autopilot_unknown_and_has_no_deck_identity():
    """Callers that predate per-deck tracking pass no autopilot flag: the
    record must stay 'unknown' (None), not silently count as autopilot."""
    snapshot = {"opponent_name": "Opponent", "format_name": "Ladder"}

    rec = record_from_game_end("match-old", "win", snapshot)

    assert rec.autopilot is None
    assert rec.deck_identity == ""
    assert rec.deck_name == ""
