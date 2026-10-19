"""bug_20261006_140351: a token Jace with two loyalty abilities, as the bridge served it.

Jace (instance 443) offered two Activate actions: abilityGrpId 208424
"-1: Surveil 1." and 208425 "-3: Draw a card." Both were labelled
"Activate: Jace"; the model said it was drawing, picked idx:1, and the
surveil was submitted.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.fakes import ScriptedBackend, make_engine

from arenamcp.action_planner import ActionPlanner

SURVEIL, DRAW = 208424, 208425
ABILITIES = {SURVEIL: ("-1", "Surveil 1."), DRAW: ("-3", "Draw a card.")}

# The bridge actions at gameStateId 493 / msgId 699, as the mac adapter serialized them.
WITNESS = {"actionType": "Activate", "grpId": 106272, "instanceId": 371, "abilityGrpId": 153236}
JACE_SURVEIL = {"actionType": "Activate", "grpId": 106555, "instanceId": 443, "abilityGrpId": SURVEIL}
JACE_DRAW = {"actionType": "Activate", "grpId": 106555, "instanceId": 443, "abilityGrpId": DRAW}
BRIDGE_ACTIONS = [
    WITNESS,
    JACE_SURVEIL,
    JACE_DRAW,
    {"actionType": "Activate_Mana", "grpId": 106529, "instanceId": 245, "abilityGrpId": 1002},
    {"actionType": "Pass"},
    {"actionType": "FloatMana"},
]
POLL = {
    "has_pending": True,
    "request_type": "ActionsAvailable",
    "request_class": "ActionsAvailableRequest",
    "can_pass": True,
    "game_state_id": 493,
    "msg_id": 699,
    "actions": BRIDGE_ACTIONS,
}
JACE_CARD = {
    "instance_id": 443,
    "grp_id": 106555,
    "name": "Jace",
    "oracle_text": "Surveil 1.\nDraw a card.",
    "type_line": "Token Planeswalker — Jace",
    "card_types": ["Planeswalker"],
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "counters": {"Loyalty": 4},
    "turn_entered_battlefield": 20,
}
WITNESS_CARD = {
    "instance_id": 371,
    "grp_id": 106272,
    "name": "Undulating Witness",
    "oracle_text": "Flying\n{o2}: This creature gets +1/-1 until end of turn.",
    "card_types": ["Creature"],
    "controller_seat_id": 2,
    "owner_seat_id": 2,
    "power": 4,
    "toughness": 6,
    "turn_entered_battlefield": 12,
}
GAME_OBJECTS = {443: JACE_CARD, 371: WITNESS_CARD}


def jace_engine(response: str, poll: dict = POLL):
    """A typed-path engine whose model answers ``response`` while the bridge shows ``poll``."""
    backend = ScriptedBackend(response)
    bridge = MagicMock()
    bridge.connected = True
    bridge.get_pending_actions.return_value = poll
    bridge.submit_action_by_index.return_value = True
    bridge.submit_pass.return_value = True
    engine = make_engine(bridge, ActionPlanner(backend=backend))
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    return engine, bridge, backend


def jace_state() -> dict:
    return {
        "turn": {"turn_number": 20, "phase": "Phase_Main1", "active_player": 2, "priority_player": 2},
        "players": [
            {"seat_id": 1, "life_total": 15},
            {"seat_id": 2, "life_total": 13, "is_local": True},
        ],
        "local_seat_id": 2,
        "battlefield": [JACE_CARD, WITNESS_CARD],
        "hand": [],
        "stack": [],
        "_bridge_connected": True,
    }
