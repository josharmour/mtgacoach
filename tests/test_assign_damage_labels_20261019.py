"""Damage-assignment receivers are named from Arena's request, and a bare id names one (ledger #169).

Match 639c5fe1 T13 (2026-10-19 14:55:20, gameStateId 373): Leonardo, Big Brother
13/3 was blocked by two Pests 2/2 (attackInfo.orderedBlockers 544, 545). Arena
asked:

    {"instanceId": 530, "totalDamage": 13, "assignments": [
        {"instanceId": 544, "minDamage": 2, "assignedDamage": 2},
        {"instanceId": 545, "minDamage": 2, "assignedDamage": 11}]}

The model answered {"544": 2, "545": 11}. ``_ID_SUFFIX`` wanted "[544]", so the
split was refused ("model split names '544', which is not in Arena's request")
and the deterministic one sent; the narration labelled 545, missing from the
snapshot, as the opponent: "Assign 2 to Pest and 11 to the opponent".
"""

from __future__ import annotations

from unittest.mock import Mock

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.damage_assignment import (
    model_split,
    plan_assigners,
    receiver_label,
    request_blockers,
    spoken_split,
    submitted_distribution,
)

US, THEM = 1, 2
REQUEST = {
    "instanceId": 530,
    "totalDamage": 13,
    "assignments": [
        {"instanceId": 544, "minDamage": 2, "assignedDamage": 2},
        {"instanceId": 545, "minDamage": 2, "assignedDamage": 11},
    ],
}
LEONARDO = {
    "instance_id": 530,
    "name": "Leonardo, Big Brother",
    "type_line": "Legendary Creature — Mutant Ninja Turtle",
    "card_types": ["Creature"],
    "controller_seat_id": US,
    "owner_seat_id": US,
    "power": 13,
    "toughness": 3,
    "is_attacking": True,
    "attack_target_id": THEM,
}


def pest(iid):
    return {
        "instance_id": iid,
        "name": "Pest",
        "type_line": "Token Creature — Pest",
        "card_types": ["Creature"],
        "controller_seat_id": THEM,
        "owner_seat_id": THEM,
        "power": 2,
        "toughness": 2,
        "is_blocking": True,
        "oracle_text": "When this creature dies, you gain 1 life.",
    }


def t13_state(*, with_second_pest=False):
    """The snapshot the narration read: the second blocking Pest is not in it."""
    battlefield = [LEONARDO, pest(544)] + ([pest(545)] if with_second_pest else [])
    return {
        "match_id": "639c5fe1",
        "local_seat_id": US,
        "players": [
            {"seat_id": US, "life_total": 25, "is_local": True},
            {"seat_id": THEM, "life_total": 18, "is_local": False},
        ],
        "turn": {"turn_number": 13, "active_player": US, "phase": "Combat", "step": "CombatDamage"},
        "battlefield": battlefield,
        "decision_context": {"type": "assign_damage", "raw": {"damageAssigners": [REQUEST]}},
    }


def as_map(split):
    return {entry["instanceId"]: entry["damage"] for entry in split}


def test_the_models_bare_ids_name_arenas_receivers():
    assigners, source = plan_assigners([REQUEST], t13_state(), {"544": 2, "545": 11})
    assert source == "model"
    assert as_map(assigners[0]["assignments"]) == {544: 2, 545: 11}
    assert model_split(REQUEST["assignments"], {"id:544": 2, "545": 11}, [], 13) is not None


def test_a_bare_id_arena_did_not_list_is_still_refused():
    assert model_split(REQUEST["assignments"], {"544": 2, "546": 11}, [], 13) is None


def test_a_blocker_missing_from_the_snapshot_is_never_the_opponent():
    state = t13_state()
    assigners, _ = plan_assigners([REQUEST], state, {"544": 2, "545": 11})
    split = submitted_distribution(assigners, state, [REQUEST])
    assert split == {"Pest [544]": 2, "a blocking creature [545]": 11}
    assert spoken_split(split) == "2 to Pest and 11 to a blocking creature"
    # With the card in the snapshot it is named.
    assert receiver_label(545, t13_state(with_second_pest=True), blockers=frozenset({545})) == "Pest [545]"


def test_request_blockers_read_lethal_entries_and_the_attackers_ordered_blockers():
    state = t13_state()
    assert request_blockers([REQUEST], state) == {544, 545}
    # A trample quarry (no minDamage) is the player, not a blocker.
    trample = {
        "instanceId": 530,
        "totalDamage": 13,
        "assignments": [
            {"instanceId": 544, "minDamage": 2, "assignedDamage": 2},
            {"instanceId": THEM, "maxDamage": 11, "assignedDamage": 11},
        ],
    }
    assert request_blockers([trample], state) == {544}
    # Blockers recorded on the attacker count without a lethal amount.
    state["battlefield"][0] = {**LEONARDO, "ordered_blockers": [{"instanceId": 544}, {"instanceId": 545}]}
    bare = {"instanceId": 530, "totalDamage": 13, "assignments": [{"instanceId": 545}]}
    assert request_blockers([bare], state) == {544, 545}


def test_the_bridge_narrates_the_second_pest():
    pilot = _BridgeSubmitMixin()
    pilot._gre_bridge = Mock()
    pilot._gre_bridge.get_pending_actions.return_value = {
        "ok": True,
        "has_pending": True,
        "request_type": "AssignDamage",
        "request_class": "AssignDamageRequest",
        "assigners": [
            {
                "instanceId": 530,
                "totalDamage": 13,
                "assignments": [
                    {"instanceId": 544, "minDamage": 2, "maxDamage": 0, "assignedDamage": 2},
                    {"instanceId": 545, "minDamage": 2, "maxDamage": 0, "assignedDamage": 11},
                ],
            }
        ],
    }
    pilot._gre_bridge.submit_assign_damage.return_value = True
    pilot._log_execution_path = Mock()
    pilot._gre_bridge_failed_methods = set()
    action = GameAction(ActionType.ASSIGN_DAMAGE, distribution={"544": 2, "545": 11})

    result = pilot._try_gre_bridge_assign_damage(action, t13_state())

    assert result is not None and result.success
    (assigners,) = pilot._gre_bridge.submit_assign_damage.call_args.args
    assert as_map(assigners[0]["assignments"]) == {544: 2, 545: 11}
    assert result.submitted_action.override_note == ""  # the model's split was used
    spoken = ActionPlan(actions=[result.submitted_action]).spoken_actions()
    assert spoken == "Assign 2 to Pest and 11 to a blocking creature."
    assert "opponent" not in spoken
