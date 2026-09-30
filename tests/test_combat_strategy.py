from arenamcp.combat_strategy import combat_choice


def creature(identity, power, toughness, owner=1, **extra):
    return {
        "instance_id": identity,
        "name": f"Creature {identity}",
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "owner_seat_id": owner,
        "controller_seat_id": owner,
        **extra,
    }


def board(ours, theirs=(), life=20, opponent_life=20, loyalty=3):
    recipients = [
        {"type": "Player", "playerSystemSeatId": 2},
        {"type": "Planeswalker", "planeswalkerInstanceId": 99},
    ]
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": life},
            {"seat_id": 2, "is_local": False, "life_total": opponent_life},
        ],
        "battlefield": [
            *ours,
            *theirs,
            {
                "instance_id": 99,
                "name": "Walker",
                "type_line": "Planeswalker",
                "controller_seat_id": 2,
                "counters": {"Loyalty": loyalty} if loyalty is not None else {},
            },
        ],
        "decision_context": {
            "raw_attackers": [
                {"attackerInstanceId": card["instance_id"], "legalDamageRecipients": recipients}
                for card in ours
            ]
        },
    }


def test_kills_planeswalker_instead_of_low_value_face_damage():
    result = combat_choice(board([creature(10, 3, 3)]))
    assert result.assignments[10]["planeswalkerInstanceId"] == 99
    assert result.planeswalkers_removed == [99]
    assert result.player_damage == 0


def test_prefers_immediate_player_lethal_over_planeswalker():
    result = combat_choice(board([creature(10, 3, 3)], opponent_life=3))
    assert result.assignments[10]["playerSystemSeatId"] == 2
    assert result.player_damage == 3


def test_splits_attack_without_counting_walker_damage_as_player_damage():
    result = combat_choice(board([creature(10, 3, 3), creature(11, 4, 4)]))
    assert result.planeswalkers_removed == [99]
    assert result.player_damage == 4
    assert result.assignments[10]["planeswalkerInstanceId"] == 99
    assert result.assignments[11]["playerSystemSeatId"] == 2


def test_holds_defense_against_lethal_counterattack():
    result = combat_choice(board([creature(10, 3, 4)], [creature(20, 3, 3, owner=2, is_tapped=True)], life=3))
    assert result.assignments == {}


def test_vigilance_can_remove_walker_and_still_defend():
    result = combat_choice(
        board(
            [creature(10, 3, 4, oracle_text="Vigilance")],
            [creature(20, 3, 3, owner=2, is_tapped=True)],
            life=3,
        )
    )
    assert result.planeswalkers_removed == [99]
    assert result.crackback == 0


def test_unknown_loyalty_never_claims_a_planeswalker_kill():
    result = combat_choice(board([creature(10, 3, 3)], loyalty=None))
    assert result.planeswalkers_removed == []
    assert result.player_damage == 3


def test_one_blocker_cannot_block_both_split_attackers():
    result = combat_choice(board([creature(10, 3, 4), creature(11, 3, 4)], [creature(20, 0, 4, owner=2)]))
    assert result.player_damage + 3 * len(result.planeswalkers_removed) > 0


def test_search_budget_is_bounded_and_double_strike_counts():
    result = combat_choice(
        board([creature(10, 3, 3, oracle_text="Double strike")], opponent_life=6), budget=100
    )
    assert result.player_damage == 6
    assert result.assignments[10]["playerSystemSeatId"] == 2


def test_gre_loyalty_updates_are_preserved_across_partial_object_updates():
    from arenamcp.gamestate import GameState

    state = GameState()
    state._update_game_object(
        {"instanceId": 99, "grpId": 100, "type": "GameObjectType_Card", "loyalty": {"value": 6}}
    )
    assert state.game_objects[99].counters["Loyalty"] == 6
    state._update_game_object({"instanceId": 99, "grpId": 100})
    assert state.game_objects[99].counters["Loyalty"] == 6
    state._update_game_object({"instanceId": 99, "grpId": 100, "loyalty": 0})
    assert state.game_objects[99].counters["Loyalty"] == 0
