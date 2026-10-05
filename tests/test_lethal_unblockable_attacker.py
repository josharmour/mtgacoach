"""An attacker nobody can block still deals damage (bug_20261004_233230).

At 25 life the opponent attacked with a 9/9 Construct and an 18/19 flying
Mechtitan. DeclareBlockersReq only listed the Construct (no flyer/reach to
block Mechtitan), the handler cleared Mechtitan's log attackState, and the
planner "absorbed 9" from the Construct: 27 damage, loss. Chumping the
Construct with the Monk token left 7 life.
"""

from __future__ import annotations

from unittest.mock import Mock

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction, plan_fallback_reason
from arenamcp.gamestate import GameObject, GameState, _handle_decision_message

OPPONENT, YOU = 1, 2
CONSTRUCT, MECHTITAN, MONK, DRUID = 976, 1103, 993, 1074
RAW_BLOCKERS = [
    {"blockerInstanceId": MONK, "attackerInstanceIds": [CONSTRUCT], "maxAttackers": 1},
    {"blockerInstanceId": DRUID, "attackerInstanceIds": [CONSTRUCT], "maxAttackers": 1},
]


def test_blockers_request_keeps_the_attacker_no_one_can_block():
    gs = GameState()
    gs.turn_info.active_player = OPPONENT
    names = {1: "Construct", 2: "Mechtitan", 3: "Monk", 4: "Paradise Druid", 5: "Stale"}
    for iid, grp, seat, attacking in (
        (CONSTRUCT, 1, OPPONENT, True),
        (MECHTITAN, 2, OPPONENT, True),
        (MONK, 3, YOU, False),
        (DRUID, 4, YOU, False),
        (7, 5, YOU, True),  # a defender can't be attacking: stale flag
    ):
        gs.game_objects[iid] = GameObject(
            instance_id=iid,
            grp_id=grp,
            zone_id=1,
            owner_seat_id=seat,
            controller_seat_id=seat,
            is_attacking=attacking,
        )
    gs._resolve_card_name = lambda grp_id: names[grp_id]

    _handle_decision_message(
        gs, "GREMessageType_DeclareBlockersReq", {"declareBlockersReq": {"blockers": RAW_BLOCKERS}}
    )

    assert gs.decision_context["attacker_ids"] == [CONSTRUCT, MECHTITAN]
    assert gs.decision_context["attackers"] == ["Construct", "Mechtitan"]
    assert gs.game_objects[MECHTITAN].is_attacking is True
    assert gs.game_objects[7].is_attacking is False


def creature(iid, name, power, toughness, seat, *, attacking=False, oracle=""):
    return {
        "instance_id": iid,
        "name": name,
        "power": power,
        "toughness": toughness,
        "owner_seat_id": seat,
        "controller_seat_id": seat,
        "is_attacking": attacking,
        "oracle_text": oracle,
        "type_line": "Creature",
    }


def board(life=25):
    state = {
        "players": [
            {"seat_id": OPPONENT, "life_total": 19, "is_local": False},
            {"seat_id": YOU, "life_total": life, "is_local": True},
        ],
        "battlefield": [
            creature(CONSTRUCT, "Construct", 9, 9, OPPONENT, attacking=True),
            creature(
                MECHTITAN,
                "Mechtitan",
                18,
                19,
                OPPONENT,
                attacking=True,
                oracle="Flying, vigilance, trample, lifelink, haste",
            ),
            creature(MONK, "Monk", 1, 1, YOU),
            creature(DRUID, "Paradise Druid", 2, 1, YOU),
        ],
    }
    context = {"type": "declare_blockers", "legal_blocker_ids": [MONK, DRUID], "raw_blockers": RAW_BLOCKERS}
    return state, context


def no_blocks():
    return ActionPlan(
        actions=[
            GameAction(action_type=ActionType.DECLARE_BLOCKERS, reasoning="absorb 9 from the Construct")
        ],
        overall_strategy="Preserve both blockers and mana engine; absorb 9 damage from the Construct.",
    )


def test_lethal_no_block_plan_is_replaced_by_a_surviving_chump():
    state, context = board()
    plan = no_blocks()
    ActionPlanner(Mock())._check_block_survival(plan, state, context)
    action = plan.actions[0]
    assert len(action.blocker_instance_assignments) == 1
    assert set(action.blocker_instance_assignments.values()) == {CONSTRUCT}
    assert len(action.blocker_assignments) == 1
    assert plan_fallback_reason(plan) == "planner_lethal_block"
    assert "27 damage at 25 life" in action.reasoning


def test_nonlethal_or_unwinnable_combat_keeps_the_planner_choice():
    for life in (30, 18):  # 27 is survivable at 30; even a chump leaves 18 at 18
        state, context = board(life)
        plan = no_blocks()
        ActionPlanner(Mock())._check_block_survival(plan, state, context)
        assert plan.actions[0].blocker_instance_assignments == {}
        assert plan_fallback_reason(plan) == ""
