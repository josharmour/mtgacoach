"""Current minimal false-lethal reproduction for #284; not a full old-match replay."""

import pytest

from arenamcp.combat_loss import LossState
from arenamcp.combat_solver import _resolve_attacker, evaluate_attack, optimal_blocks


def creature(i, p, t, text="", **extra):
    return dict(
        instance_id=i,
        name=f"Creature {i}",
        type_line="Creature",
        power=p,
        toughness=t,
        oracle_text=text,
        **extra,
    )


@pytest.mark.parametrize("max_options", [50_000, 0])
def test_blocking_lifelink_prevents_exact_lethal_in_exhaustive_and_greedy(max_options):
    attacker = creature(1, 6, 6, "Trample")
    blocker = creature(2, 2, 2, "Lifelink")
    plan = optimal_blocks([attacker], [blocker], 4, max_options=max_options)
    assert plan.assignments == {2: 1}
    assert plan.damage_through == 4 and plan.blocker_life_gained == 2
    assert plan.net_life_lost == 2 and plan.death_drain == 0
    assert "lifelink gains you 2" in plan.explanation
    attack = evaluate_attack([attacker], [], [blocker], 4, 20, [])
    assert attack.score < 10_000
    assert attack.damage_through == 4 and attack.opponent_life_gained == 2


def test_multiple_attackers_share_the_defending_players_life():
    attackers = [creature(1, 6, 6, "Trample"), creature(3, 1, 1, "Flying")]
    plan = optimal_blocks(attackers, [creature(2, 2, 2, "Lifelink")], 5)
    assert plan.assignments == {2: 1} and plan.net_life_lost == 3


def test_regular_damage_lifelink_cannot_rescue_first_strike_lethal_elsewhere():
    attackers = [creature(1, 4, 4, "Flying\nFirst strike"), creature(3, 1, 3)]
    blocker = creature(2, 2, 2, "Lifelink")
    plan = optimal_blocks(attackers, [blocker], 4)
    assert plan.net_life_lost >= 4
    assert evaluate_attack(attackers, [], [blocker], 4, 20, []).score == 10_000


def test_first_strike_lifelink_elsewhere_can_save_regular_damage_lethal():
    attackers = [creature(1, 4, 4, "Flying"), creature(3, 1, 3)]
    blocker = creature(2, 2, 2, "Lifelink\nFirst strike")
    plan = optimal_blocks(attackers, [blocker], 4)
    assert plan.assignments == {2: 3} and plan.net_life_lost == 2


@pytest.mark.parametrize("text", ["Trample\nFirst strike", "Trample\nDouble strike"])
def test_lifelinker_killed_before_dealing_damage_cannot_save_player(text):
    attacker, blocker = creature(1, 6, 6, text), creature(2, 2, 2, "Lifelink")
    assert _resolve_attacker(attacker, [blocker]).blocker_life_gained == 0
    assert optimal_blocks([attacker], [blocker], 4).blocker_life_gained == 0
    assert evaluate_attack([attacker], [], [blocker], 4, 20, []).score == 10_000


def test_double_strike_blocker_gains_only_for_damage_actually_dealt():
    attacker, blocker = creature(1, 6, 6, "Trample"), creature(2, 2, 4, "Double strike\nLifelink")
    plan = optimal_blocks([attacker], [blocker], 2)
    assert plan.assignments == {2: 1} and plan.blocker_life_gained == 4
    assert plan.net_life_lost == -2
    killed_first = _resolve_attacker(creature(1, 2, 2), [blocker])
    assert killed_first.blocker_life_gained == 2


def test_no_invented_gain_and_no_illegal_block():
    attacker = creature(1, 6, 6, "Trample")
    blocker = creature(2, 2, 2)
    assert evaluate_attack([attacker], [], [blocker], 4, 20, []).score == 10_000
    blocker["oracle_text"] = "Lifelink"
    plan = optimal_blocks([attacker], [blocker], 4, blocker_allowed_attackers={2: set()})
    assert not plan.assignments and plan.blocker_life_gained == 0


@pytest.mark.parametrize(
    "defender,text",
    [
        (LossState(poison=9), "Trample\nToxic 1"),
        (
            LossState(library=1),
            "Trample\nWhenever this creature deals combat damage to a player, that player mills one card.",
        ),
    ],
)
def test_lifelink_does_not_cure_poison_or_failed_draw(defender, text):
    plan = optimal_blocks([creature(1, 6, 6, text)], [creature(2, 2, 2, "Lifelink")], 4, defender=defender)
    assert plan.player_loses or plan.draw_loss_pending
    assert plan.score < -1000


def test_lifelink_does_not_restore_planeswalker_loyalty():
    plan = optimal_blocks(
        [creature(1, 6, 6, "Trample")], [creature(2, 2, 2, "Lifelink")], 4, defending_player=False
    )
    assert plan.blocker_life_gained == 0 and plan.net_life_lost >= 4


def test_recovery_credit_available_when_lifelink_keeps_player_alive():
    plan = optimal_blocks(
        [creature(1, 6, 6, "Trample")],
        [creature(2, 2, 2, "Lifelink")],
        4,
        recovery_credit=lambda dead: 3 if 2 in dead else 0,
    )
    assert plan.assignments == {2: 1} and plan.recovery_credit == 3


def test_death_drain_and_blocker_lifelink_remain_separate():
    plan = optimal_blocks(
        [creature(1, 6, 6, "Trample")], [creature(2, 2, 2, "Lifelink", death_swing=[-1, 1])], 4
    )
    assert plan.blocker_life_gained == 2 and plan.death_drain == 1
    assert plan.net_life_lost == 3


def test_our_lifelink_blocker_is_counted_in_crackback_survival():
    ours = creature(2, 2, 2, "Lifelink")
    attack = evaluate_attack([], [ours], [], 20, 4, [creature(1, 6, 6, "Trample")])
    assert attack.worst_case_crackback == 2 and attack.score > -1000


def test_coach_reports_lifelink_adjusted_remaining_life():
    from arenamcp.coach import CoachEngine

    ours = [creature(2, 2, 2, "Lifelink", controller_seat_id=2)]
    theirs = [creature(1, 6, 6, "Trample", controller_seat_id=1, is_attacking=True)]
    local = dict(seat_id=2, is_local=True, life_total=4)
    context = {
        "type": "declare_blockers",
        "legal_blocker_ids": [2],
        "attacker_ids": [1],
        "raw_blockers": [{"blockerInstanceId": 2, "attackerInstanceIds": [1]}],
    }
    state = {
        "players": [local, dict(seat_id=1, life_total=20)],
        "battlefield": ours + theirs,
        "decision_context": context,
    }
    prompt = "\n".join(
        CoachEngine.__new__(CoachEngine)._format_block_combat(
            ours, theirs, local, 7, "Combat", set(), context, state
        )
    )
    assert "Recommended blocks leave 2 life" in prompt
    assert "Best blocks → take 4 dmg → DEAD" not in prompt


def test_g03_innocence_block_is_no_longer_rejected_after_ignoring_its_lifelink():
    # Retained audit G03: d1bad78e T7, Innocence275 can block Kaito247.
    blocker = creature(275, 2, 1, "Lifelink\nWhenever one or more other creatures enter, draw a card.")
    attacker = creature(247, 4, 5)
    plan = optimal_blocks([attacker], [blocker], 16, blocker_allowed_attackers={275: {247}})
    assert plan.assignments == {275: 247}
    assert plan.damage_through == 0 and 16 - plan.net_life_lost == 18
