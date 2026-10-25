from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_hit import loses_on_combat_hit
from arenamcp.combat_solver import evaluate_attack, optimal_blocks
from arenamcp.combat_strategy import guarded_block_plan, wasteful_chump_blocks

TRIGGER = "Whenever this creature deals combat damage to a player, that player loses the game."
ODIN = (
    'I — Destroy target creature.\nII — Zantetsuken — This creature gains "'
    + TRIGGER
    + '"\nIII — Draw two cards.'
)


def creature(iid, power, toughness, seat, **kw):
    return dict(
        instance_id=iid,
        name=f"Creature {iid}",
        power=power,
        toughness=toughness,
        type_line="Creature",
        card_types=["Creature"],
        controller_seat_id=seat,
        owner_seat_id=seat,
        **kw,
    )


def board():
    return [
        creature(372, 5, 3, 2, oracle_text=ODIN, counters={"Lore": 2}, is_attacking=True),
        creature(101, 2, 1, 1),
        creature(102, 1, 1, 1),
    ]


@pytest.mark.parametrize("max_options", [50000, 0])
def test_odin_requires_a_chump_even_at_high_life(max_options):
    a, *b = board()
    plan = optimal_blocks([a], b, 100, max_options=max_options)
    assert plan.assignments and not plan.player_loses
    assert plan.damage_through == 0


def test_saga_grant_not_active_before_chapter_two_and_cache_tracks_it():
    a, *b = board()
    b = b[:1]  # no profitable gang-block without the terminal ability
    a["counters"]["Lore"] = 1
    assert not loses_on_combat_hit(a)
    assert not optimal_blocks([a], b, 100).assignments
    a["counters"]["Lore"] = 2
    assert optimal_blocks([a], b, 100).assignments


def test_live_grant_without_oracle_text():
    assert loses_on_combat_hit({"ability_grp_ids": [19443]})
    assert not loses_on_combat_hit(
        {"oracle_text": 'Pay {3}: This creature gains "' + TRIGGER + '" until end of turn.'}
    )


def test_trample_hit_still_loses_even_if_attacker_dies():
    a = creature(1, 5, 3, 2, oracle_text=TRIGGER, keywords=["trample"])
    b = creature(2, 3, 3, 1)
    plan = optimal_blocks([a], [b], 100)
    assert plan.player_loses


def test_first_strike_kill_prevents_hit():
    a = creature(1, 5, 3, 2, oracle_text=TRIGGER, keywords=["trample"])
    b = creature(2, 3, 3, 1, keywords=["first strike"])
    plan = optimal_blocks([a], [b], 100)
    assert plan.assignments == {2: 1} and not plan.player_loses


def test_zero_power_does_not_trigger_loss():
    a = creature(1, 0, 3, 2, oracle_text=TRIGGER)
    plan = optimal_blocks([a], [], 100)
    assert not plan.player_loses


def test_guard_and_chump_policy_keep_a_life_saving_block():
    a, *b = board()
    state = {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 8},
        ],
        "battlefield": [a, *b],
    }
    context = {"type": "declare_blockers", "legal_blocker_ids": [101, 102]}
    plan = ActionPlan(actions=[GameAction(action_type=ActionType.DECLARE_BLOCKERS)])
    ActionPlanner(Mock())._check_block_survival(plan, state, context)
    blocks = plan.actions[0].blocker_instance_assignments
    assert blocks and plan.fallback_reason == "planner_lethal_block"
    assert "combat-hit trigger" in plan.actions[0].reasoning
    assert not wasteful_chump_blocks(state, blocks)
    assert guarded_block_plan([a], b, 20).assignments


def test_attack_evaluation_keeps_terminal_outcome_separate_from_damage():
    odin, *blockers = board()
    loss = evaluate_attack([], [], [], 100, 100, [odin])
    assert loss.lose_to_crackback and loss.worst_case_crackback == 5
    assert loss.score == -1000
    win = evaluate_attack([odin], [], [], 100, 100, [])
    assert win.opponent_loses and win.damage_through == 5
    assert win.score == 10000
