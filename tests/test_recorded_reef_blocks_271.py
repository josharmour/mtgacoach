"""#271: Player-prev.log gsid856 has two 2/2 Hares, not 1-power Hares.

Two blockers stop at most 6 of 9 power; at 3 life every assignment is lethal.
The higher-life control confirms the survival override still chooses Reef.
"""

from unittest.mock import Mock

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.combat_solver import optimal_blocks


def state(life):
    def card(iid, name, power, toughness, seat):
        return dict(
            instance_id=iid,
            name=name,
            power=power,
            toughness=toughness,
            controller_seat_id=seat,
            owner_seat_id=seat,
            type_line="Creature",
            is_attacking=seat == 1,
        )

    attackers = [
        card(455, "Restless Reef", 4, 4, 1),
        card(751, "Etrata, Deadly Fugitive", 1, 4, 1),
        card(877, "Hare Apparent", 2, 2, 1),
        card(891, "Hare Apparent", 2, 2, 1),
    ]
    for c in attackers[:2]:
        c["keywords"] = ["deathtouch"]
    blockers = [card(825, "Leonardo, Big Brother", 2, 3, 2), card(883, "Hare Apparent", 2, 2, 2)]
    context = dict(
        type="declare_blockers",
        legal_blocker_ids=[825, 883],
        raw_blockers=[
            dict(blockerInstanceId=b["instance_id"], attackerInstanceIds=[455, 877, 891, 751], maxAttackers=1)
            for b in blockers
        ],
    )
    board = dict(players=[dict(seat_id=2, is_local=True, life_total=life)], battlefield=attackers + blockers)
    return board, context, attackers, blockers


def test_recorded_three_life_board_has_no_surviving_blocks():
    _, _, attackers, blockers = state(3)
    result = optimal_blocks(attackers, blockers, 3)
    assert result.damage_through >= 3


def test_survival_guard_does_replace_hare_blocks_when_survival_is_possible():
    board, context, _, _ = state(4)
    plan = ActionPlan(
        actions=[GameAction(ActionType.DECLARE_BLOCKERS, blocker_instance_assignments={825: 877, 883: 891})]
    )
    ActionPlanner(Mock())._check_block_survival(plan, board, context)
    assert 455 in plan.actions[0].blocker_instance_assignments.values()
    assert plan.fallback_reason == "planner_lethal_block"
