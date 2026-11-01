from arenamcp.combat_loss import LossState, player_hit
from arenamcp.combat_solver import _resolve_attacker, optimal_blocks


def creature(iid, power=1, toughness=1, text="", **kw):
    return dict(
        instance_id=iid, name=f"Creature {iid}", power=power, toughness=toughness, oracle_text=text, **kw
    )


def test_toxic_hit_is_fatal_at_nine_even_at_high_life():
    a = creature(1, text="Toxic 1")
    b = creature(2, power=0)
    for budget in (0, 50000):
        plan = optimal_blocks([a], [b], 100, defender=LossState(9), max_options=budget)
        assert plan.assignments == {2: 1} and not plan.player_loses
    assert optimal_blocks([a], [], 100, defender=LossState(9)).player_loses


def test_poison_totals_enter_memo_and_accumulate_across_attackers():
    attackers = [creature(1, text="Toxic 1"), creature(2, text="Toxic 1")]
    assert not optimal_blocks(attackers, [], 100, defender=LossState(7)).player_loses
    plan = optimal_blocks(attackers, [], 100, defender=LossState(8))
    assert plan.poison_given == 2 and plan.player_loses


def test_double_strike_toxic_counts_each_connected_damage_step():
    a = creature(1, power=4, text="Toxic 2", keywords=["double strike", "trample"])
    assert _resolve_attacker(a, []).poison_given == 4
    # First hit kills the blocker without spill, second hit gives poison once.
    b = creature(2, power=0, toughness=4)
    hit = _resolve_attacker(a, [b])
    assert hit.damage_through == 4 and hit.poison_given == 2
    a["power"] = 0
    assert _resolve_attacker(a, []).poison_given == 0


def test_first_strike_kill_stops_poison_and_bonus_is_not_poison_damage():
    a = creature(1, text="Infect", combat_hit_bonus=4)
    hit = _resolve_attacker(a, [])
    assert hit.poison_given == 1 and hit.damage_through == 4
    assert _resolve_attacker(a, [creature(2, keywords=["first strike"])]).poison_given == 0


def test_infect_keeps_lifelink_but_replaces_life_damage():
    hit = _resolve_attacker(creature(1, power=4, text="Infect", keywords=["lifelink"]), [])
    assert hit.poison_given == 4 and hit.damage_through == 0 and hit.life_gained == 4


def test_mill_is_deferred_loss_on_draw_not_instant_death():
    a = creature(
        1, text="Whenever this creature deals combat damage to a player, that player mills three cards."
    )
    plan = optimal_blocks([a], [], 100, defender=LossState(0, 3))
    assert plan.cards_milled == 3 and plan.draw_loss_pending and not plan.player_loses
    assert LossState(0, 3).hit(0, 3).draw_loses
    assert not LossState(0, None).hit(0, 99).draw_loses


def test_mill_that_many_and_walkers_do_not_trigger_player_effects():
    a = creature(
        1,
        power=3,
        text="Whenever this creature deals combat damage to a player, that player mills that many cards.",
    )
    assert _resolve_attacker(a, []).cards_milled == 3
    a["oracle_text"] += "\nInfect"
    hit = _resolve_attacker(a, [], defending_player=False)
    assert hit.damage_through == 3 and hit.poison_given == hit.cards_milled == 0


def test_does_not_invent_conditional_or_activated_poison_and_mill():
    assert player_hit(creature(1, text="Pay {2}: This creature gains toxic 4 until end of turn."), 1, 1) == (
        1,
        0,
        0,
    )
    assert player_hit(
        creature(
            1,
            text="Whenever this creature deals combat damage to a player, if you control an artifact, that player mills three cards.",
        ),
        1,
        1,
    ) == (1, 0, 0)
    assert player_hit(creature(1, text="Toxic 1\nToxic 2"), 4, 1) == (4, 3, 0)


def board(ours=(), theirs=(), *, our_poison=0, opp_poison=0, library=None, opp_library=None, active=1):
    return {
        "match_id": "combat-loss",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {"turn_number": 6, "active_player": active, "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 30, "counters": {"Poison": our_poison}},
            {"seat_id": 2, "life_total": 30, "counters": {"Poison": opp_poison}},
        ],
        "battlefield": [
            {
                **c,
                "controller_seat_id": seat,
                "owner_seat_id": seat,
                "type_line": "Creature",
                "card_types": ["Creature"],
                "turn_entered_battlefield": 1,
            }
            for seat, team in [(1, ours), (2, theirs)]
            for c in team
        ],
        "hand": [],
        "graveyard": [],
        "stack": [],
        "zones": {
            "library_count": library,
            "library_count_source": "log_zone_membership",
            "opponent_library_count": opp_library,
            "opponent_library_count_source": "bridge_total_card_count",
        },
    }


def test_poison_block_guard_keeps_a_life_saving_chump():
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
    from arenamcp.combat_strategy import wasteful_chump_blocks

    a, b = creature(1, text="Toxic 1", is_attacking=True), creature(2, power=0)
    snapshot = board([b], [a], our_poison=9, active=2)
    plan = ActionPlan(actions=[GameAction(action_type=ActionType.DECLARE_BLOCKERS)])
    ActionPlanner(Mock())._check_block_survival(
        plan, snapshot, {"type": "declare_blockers", "legal_blocker_ids": [2]}
    )
    assert plan.actions[0].blocker_instance_assignments == {2: 1}
    assert not wasteful_chump_blocks(snapshot, {2: 1})


def test_greedy_blocks_earlier_poison_to_survive_later_unblockable_hit():
    attackers = [creature(1, text="Toxic 1"), creature(2, text="Toxic 1", cant_be_blocked=True)]
    plan = optimal_blocks(attackers, [creature(3, power=0)], 100, defender=LossState(8), max_options=0)
    assert plan.assignments == {3: 1} and not plan.player_loses


def test_greedy_can_make_multiple_required_poison_blocks():
    attackers = [creature(i, text="Toxic 2") for i in (1, 2, 3)]
    plan = optimal_blocks(
        attackers, [creature(4, power=0), creature(5, power=0)], 100, defender=LossState(6), max_options=0
    )
    assert len(plan.assignments) == 2 and not plan.player_loses


def test_attack_score_knows_poison_crackback_and_poison_win():
    from arenamcp.combat_solver import evaluate_attack

    a = creature(1, text="Toxic 1")
    assert evaluate_attack([a], [], [], 30, 30, [], state=board([a], opp_poison=9)).opponent_loses
    assert evaluate_attack([], [], [], 30, 30, [a], state=board(theirs=[a], our_poison=9)).lose_to_crackback


def test_known_library_does_not_disable_ordinary_chump_guard():
    from arenamcp.combat_strategy import guarded_block_plan

    attackers, blockers = [creature(1, 4, 4)], [creature(2, 0, 1)]
    ordinary = guarded_block_plan(attackers, blockers, 20)
    known_library = guarded_block_plan(attackers, blockers, 20, defender=LossState(0, 52))
    assert known_library.assignments == ordinary.assignments
