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
