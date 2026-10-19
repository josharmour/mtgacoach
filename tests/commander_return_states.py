"""2026-10-18 18:18:20-26 (match d58f6da0, turn 12): a damage plan outlived its request.

Mossborn Hydra (#613, 18/18 trample) was blocked by Etali, Primal Conqueror
(#596, lethal 7). Arena asked for the damage split (AssignDamage, gameStateId
338) and the autopilot started a 5 s strategic plan (AssignDamage is not a
typed family). At 18:18:22 the damage was assigned in Arena's own UI (no bridge
submission), Michelangelo died, and Arena asked whether to return him to the
command zone (OptionalAction, gameStateId 339). The plan's assign_damage was then submitted against that
request: "Bridge couldn't handle assign_damage (Mossborn Hydra)", MANUAL
REQUIRED, "[Bridge gap: OptionalAction (type=optional_action)]".

The polls are shaped as the Mac bridge serves them; ids, assigners and the
prompt come from Player.log and bug_20261018_181826.json.
"""

from __future__ import annotations

import json

ASSIGN_DAMAGE_POLL = {
    "ok": True,
    "has_pending": True,
    "request_type": "AssignDamage",
    "request_class": "AssignDamageRequest",
    "game_state_id": 338,
    "msg_id": 489,
    "can_pass": False,
    "assigners": [
        {
            "instanceId": 613,
            "totalDamage": 18,
            "assignments": [
                {"instanceId": 596, "minDamage": 7, "maxDamage": 18, "assignedDamage": 7},
                {"instanceId": 1, "maxDamage": 11, "assignedDamage": 11},
            ],
        }
    ],
}
COMMANDER_RETURN_POLL = {
    "ok": True,
    "has_pending": True,
    "request_type": "OptionalAction",
    "request_class": "OptionalActionMessageRequest",
    "game_state_id": 339,
    "msg_id": 492,
    "can_pass": False,
    "source_instance_id": 14612,
    "prompt": {"promptId": 144, "parameters": [{"parameterName": "CardId", "numberValue": 654}]},
}
# The plan logged at 18:18:26 ("[1/1] assign_damage | Mossborn Hydra"), as model JSON.
DAMAGE_PLAN = json.dumps(
    {
        "actions": [
            {
                "action_type": "assign_damage",
                "card_name": "Mossborn Hydra",
                "reasoning": "kill Etali, trample",
            }
        ],
        "overall_strategy": "All-in race: kill the biggest blocker and push lethal-range trample damage.",
        "voice_advice": "Assign damage.",
    }
)

HYDRA = {
    "instance_id": 613,
    "grp_id": 93820,
    "name": "Mossborn Hydra",
    "type_line": "Creature — Elemental Hydra",
    "oracle_text": "Trample",
    "card_types": ["Creature"],
    "power": 18,
    "toughness": 18,
    "keywords": ["trample"],
    "is_attacking": True,
    "is_tapped": True,
    "owner_seat_id": 2,
    "controller_seat_id": 2,
}
ETALI = {
    "instance_id": 596,
    "grp_id": 84411,
    "name": "Etali, Primal Conqueror",
    "type_line": "Legendary Creature — Elder Dinosaur",
    "card_types": ["Creature"],
    "power": 7,
    "toughness": 7,
    "keywords": ["trample"],
    "is_blocking": True,
    "owner_seat_id": 1,
    "controller_seat_id": 1,
}
MICHELANGELO = {
    "instance_id": 654,
    "grp_id": 100578,
    "name": "Michelangelo, Weirdness to 11",
    "type_line": "Legendary Creature — Mutant Ninja Turtle",
    "card_types": ["Creature"],
    "owner_seat_id": 2,
    "controller_seat_id": 2,
}


def assign_damage_state() -> dict:
    """The log snapshot the 18:18:21 plan was built from (bridge=AssignDamage, no board id)."""
    return {
        "match_id": "d58f6da0-41c8-4313-bbf6-75b046ab5b25",
        "turn": {
            "turn_number": 12,
            "phase": "Phase_Combat",
            "step": "Step_CombatDamage",
            "active_player": 2,
            "priority_player": 2,
        },
        "players": [{"seat_id": 1, "life_total": 22}, {"seat_id": 2, "life_total": 5, "is_local": True}],
        "local_seat_id": 2,
        "battlefield": [HYDRA, ETALI],
        "hand": [],
        "stack": [],
        "graveyard": [],
        "pending_decision": "Assign Damage",
        "decision_context": {"type": "assign_damage"},
        "legal_actions": ["Assign damage (order targets by priority)", "Done"],
        "_bridge_connected": True,
        "_bridge_request_type": "AssignDamage",
        "_bridge_request_class": "AssignDamageRequest",
    }


def commander_return_state() -> dict:
    """The snapshot once Arena asks about Michelangelo's return (18:18:22.957)."""
    state = assign_damage_state()
    state.update(
        battlefield=[HYDRA],
        graveyard=[MICHELANGELO],
        pending_decision="Optional Action",
        decision_context={
            "type": "optional_action",
            "prompt": "Return Michelangelo, Weirdness to 11 to the command zone?",
            "recipient_ids": [654],
            "commander_return": True,
        },
        legal_actions=[],
        _bridge_request_type="OptionalAction",
        _bridge_request_class="OptionalActionMessageRequest",
    )
    return state
