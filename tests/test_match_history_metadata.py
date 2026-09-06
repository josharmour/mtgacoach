from arenamcp.match_history import record_from_game_end


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
