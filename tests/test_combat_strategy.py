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


def test_bounded_search_keeps_player_lethal_when_clean_hit_could_also_kill_a_walker():
    state = board(
        [creature(10, 4, 3, oracle_text="Flying"), creature(11, 3, 3)],
        [creature(20, 2, 2, owner=2)],
        opponent_life=4,
        loyalty=3,
    )
    for budget in (2, 4):
        best = combat_choice(state, budget=budget)
        assert best.bounded and best.player_damage == 4
        assert best.assignments[10]["playerSystemSeatId"] == 2


def test_splits_attack_without_counting_walker_damage_as_player_damage():
    result = combat_choice(board([creature(10, 3, 3), creature(11, 4, 4)]))
    assert result.planeswalkers_removed == [99]
    assert result.player_damage == 4
    assert result.assignments[10]["planeswalkerInstanceId"] == 99
    assert result.assignments[11]["playerSystemSeatId"] == 2


def test_holds_defense_against_lethal_counterattack():
    result = combat_choice(board([creature(10, 3, 4)], [creature(20, 3, 3, owner=2, is_tapped=True)], life=3))
    assert result.assignments == {}


def test_attacking_lifelink_can_pay_for_the_return_attack():
    state = board(
        [creature(10, 4, 4, keywords=["lifelink"])],
        [creature(20, 5, 5, owner=2, is_tapped=True)],
        life=3,
        opponent_life=6,
    )
    state["decision_context"]["raw_attackers"][0]["legalDamageRecipients"] = [
        {"type": "Player", "playerSystemSeatId": 2}
    ]
    result = combat_choice(state)
    assert result.assignments[10]["playerSystemSeatId"] == 2
    assert (result.attack_life_gain, result.crackback) == (4, 5)
    from arenamcp.coach import CoachEngine

    assert "you gain 4 life" in CoachEngine._combat_fallback_line(state, "attackers")

    source = {"instance_id": 30, "controller_seat_id": 2, "oracle_text": "Your opponents can't gain life."}
    state["battlefield"].append(source)
    assert not combat_choice(state).assignments
    source["oracle_text"] = "{R}: Your opponents can't gain life this turn."
    assert combat_choice(state).attack_life_gain == 4
    source["oracle_text"] = "Your opponents can't gain life."
    source["is_phased_out"] = True
    assert combat_choice(state).attack_life_gain == 4

    state["battlefield"][0]["keywords"] = []
    assert not combat_choice(state).assignments


def test_lifelink_on_defense_survives_when_attacking_loses():
    state = board(
        [creature(10, 4, 4, keywords=["lifelink"])],
        [
            creature(20, 4, 4, owner=2, is_tapped=True, keywords=["flying"]),
            creature(21, 4, 4, owner=2, is_tapped=True),
        ],
        life=3,
        opponent_life=12,
    )
    result = combat_choice(state)
    assert not result.assignments
    assert (result.crackback, result.block_life_gain) == (4, 4)


def test_lifelink_candidate_preserves_clean_combined_lethal_at_small_budget():
    state = board(
        [creature(10, 2, 2, keywords=["lifelink"]), creature(11, 2, 2)],
        [creature(20, 10, 10, owner=2, is_tapped=True, keywords=["flying"])],
        life=1,
        opponent_life=4,
    )
    for entry in state["decision_context"]["raw_attackers"]:
        entry["legalDamageRecipients"] = [{"type": "Player", "playerSystemSeatId": 2}]
    assert combat_choice(state, budget=2).player_damage == 4


def test_affordable_full_attack_keeps_lethal_through_one_blocker():
    state = board(
        [creature(10, 2, 2, keywords=["lifelink"]), creature(11, 2, 2), creature(12, 2, 2)],
        [creature(20, 0, 1, owner=2), creature(21, 10, 10, owner=2, is_tapped=True, keywords=["flying"])],
        life=1,
        opponent_life=4,
    )
    for entry in state["decision_context"]["raw_attackers"]:
        entry["legalDamageRecipients"] = [{"type": "Player", "playerSystemSeatId": 2}]
    result = combat_choice(state, budget=5)
    assert result.player_damage >= 4 and len(result.assignments) == 3


def test_early_clean_hit_holds_the_only_defender_against_lethal_flying_damage():
    state = board(
        [creature(10, 4, 4, oracle_text="Flying"), creature(11, 1, 1)],
        [creature(20, 5, 5, owner=2, oracle_text="Flying", is_tapped=True), creature(21, 1, 1, owner=2)],
        life=5,
    )
    for entry in state["decision_context"]["raw_attackers"]:
        entry["legalDamageRecipients"] = [{"type": "Player", "playerSystemSeatId": 2}]
    best = combat_choice(state, budget=2)
    assert best.bounded and not best.assignments
    assert best.crackback < 5


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
    # The 0/4 can stop only one of them, so both go at the 3-loyalty walker.
    assert result.planeswalkers_removed == [99]
    assert {result.assignments[i]["planeswalkerInstanceId"] for i in (10, 11)} == {99}


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
