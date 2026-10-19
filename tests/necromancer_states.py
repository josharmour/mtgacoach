"""Theoretical Necromancer's activate/cancel loop, bug_20261006_185803 (turn 16).

Its graveyard ability (173944, source 455, targetSourceZoneId 37 = our
graveyard) returns another creature card from our graveyard to hand. Every pick
of our Yuriko (399) was replaced with a cancel, the cancel un-counted the
activation, and the next priority window activated it again. Shared by
test_graveyard_recursion_and_modal_targets and test_oops_emote.
"""

from unittest.mock import MagicMock

from tests.fakes import make_engine

from arenamcp.action_planner import DECLINE_DECISION

LOCAL, OPP = 2, 1

# Arena card DB text (Raw_CardDatabase, 2026-10-06).
NECROMANCER = (
    "{o3oB}, Exile this card from your graveyard: Return another target creature card from your "
    "graveyard to your hand."
)


def card(iid, name, seat, power=None, toughness=None, oracle="", type_line="Creature"):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def players():
    return [
        {"seat_id": OPP, "life_total": 23, "is_local": False},
        {"seat_id": LOCAL, "life_total": 10, "is_local": True},
    ]


def targets_poll(ids, *, game_state_id, msg_id, hot=()):
    """The mac bridge's SelectTargetsRequest shape (mac_bridge_adapter)."""
    slot = [
        {
            "targetInstanceId": iid,
            "targetIdx": 1,
            "grpId": 0,
            "legalAction": "Select",
            "highlight": "Hot" if iid in hot else "Tepid",
        }
        for iid in ids
    ]
    return {
        "has_pending": True,
        "request_type": "SelectTargets",
        "request_class": "SelectTargetsRequest",
        "game_state_id": game_state_id,
        "msg_id": msg_id,
        "can_cancel": True,
        "target_selections": [
            {"targetIdx": 1, "minTargets": 1, "maxTargets": 1, "selectedTargets": 0, "targets": slot}
        ],
        "target_candidates": slot,
    }


def log_request(ids, ability_id, source_id, card_grp, zone=None):
    """selectTargetsReq exactly as Player.log records it."""
    selection = {
        "targetIdx": 1,
        "targets": [
            {
                "targetInstanceId": iid,
                "legalAction": "SelectAction_Select",
                "highlight": "HighlightType_Tepid",
            }
            for iid in ids
        ],
        "minTargets": 1,
        "maxTargets": 1,
        "prompt": {"promptId": 1031, "parameters": [{"parameterName": "CardId", "numberValue": card_grp}]},
        "targetingAbilityGrpId": ability_id,
        "targetingPlayer": LOCAL,
    }
    if zone:
        selection["targetSourceZoneId"] = zone
    return {"targets": [selection], "sourceId": source_id, "abilityGrpId": card_grp}


NECRO_IDS = (271, 399, 408, 449)


def necro_state(*, bridge=True):
    request = log_request(NECRO_IDS, 173944, 455, 106298, zone=37)
    state = {
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 16, "active_player": LOCAL, "phase": "Phase_Main1"},
        "players": players(),
        "battlefield": [
            card(351, "Geist of Saint Thalia", LOCAL, 1, 2),
            card(451, "Mindseeker Oculus", LOCAL, 2, 1),
            card(382, "Yuriko, Blade of the Mighty", OPP, 4, 5),
            card(389, "Greenhouse Propagator", OPP, 2, 3),
            card(437, "Tomik, Orzhov Lawmage", OPP, 2, 1),
        ],
        "graveyard": [
            card(271, "Diviner of Victory", LOCAL, 1, 1),
            card(378, "Theoretical Necromancer", LOCAL, 4, 1, NECROMANCER),
            card(399, "Yuriko, Hope from the Shadows", LOCAL, 1, 1),
            card(408, "Living Library", LOCAL, 0, 4),
            card(449, "Codie, Ravenous Codex", LOCAL, 1, 4),
            card(272, "Teyo, Lightshield Expert", OPP, 1, 1),
        ],
        "hand": [],
        "stack": [
            {
                "instance_id": 455,
                "grp_id": 173944,
                "name": "Theoretical Necromancer ability",
                "type_line": "Ability",
                "object_kind": "ABILITY",
                "oracle_text": NECROMANCER,
                "controller_seat_id": LOCAL,
                "parent_instance_id": 378,
            }
        ],
        "decision_context": {
            "type": "target_selection",
            "source_card": "Theoretical Necromancer",
            "source_id": 455,
            "targets": request["targets"],
            "raw": request,
            "source_parent_instance_id": 378,
            "source_card_oracle_text": NECROMANCER,
            "source_oracle_text": NECROMANCER,
        },
    }
    if bridge:
        # The bridge payload from the bug report (GRE_RequestPayload).
        state["_bridge_request_payload"] = {
            "requestType": "SelectTargets",
            "requestClass": "SelectTargetsRequest",
            "targetSelections": [
                {
                    "targetIdx": 1,
                    "targets": ["{}", "{}", "{}", "{}"],
                    "minTargets": 1,
                    "maxTargets": 1,
                    "selectedTargets": 0,
                    "targetSourceZoneId": 37,
                    "targetingAbilityGrpId": 173944,
                    "targetingPlayer": LOCAL,
                }
            ],
            "abilityGrpId": 173944,
            "sourceId": 455,
        }
    return state


def actions_poll(game_state_id=459):
    return {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "game_state_id": game_state_id,
        "can_pass": True,
        "actions": [
            {
                "actionType": "ActionType_Activate",
                "grpId": 106298,
                "instanceId": 378,
                "abilityGrpId": 173944,
                "manaCost": [{"color": '[ "Generic" ]', "count": 3}, {"color": '[ "Black" ]', "count": 1}],
                "hasAutoTap": True,
                "autoTapActions": [{"instanceId": 367}, {"instanceId": 400}],
            },
            {"actionType": "ActionType_Pass"},
        ],
    }


def loop_engine():
    """The live loop: an engine whose planner declines the targets, then takes the activation again."""
    planner = MagicMock()
    planner.plan_decision_options.side_effect = lambda decision, gs: (
        [DECLINE_DECISION]
        if decision.request_type == "SelectTargets"
        else [o.option_id for o in decision.options if o.option_id != "pass"][:1] or ["pass"]
    )
    bridge = MagicMock()
    bridge.connected = True
    engine = make_engine(bridge, planner)
    engine._request_tracker = MagicMock()
    engine._request_tracker.may_submit.return_value = True
    engine._request_tracker.exhausted.return_value = False
    return engine, bridge, planner


def loop_state(turn=16):
    state = necro_state(bridge=False)
    state["turn"] = {"turn_number": turn, "active_player": LOCAL, "phase": "Phase_Main1"}
    return state
