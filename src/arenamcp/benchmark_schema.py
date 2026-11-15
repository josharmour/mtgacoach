"""Offline benchmark's bounded structured extensions, never coercing values.

Each path has its own contract. Unsupported nested structures fail closed. This
is not a general sanitizer and never sends data or fetches card information.
"""

from __future__ import annotations


def integer(v):
    return type(v) is int


def boolean(v):
    return type(v) is bool


def text(v):
    return isinstance(v, str)


def enum(*values):
    allowed = frozenset(values)
    return lambda v: isinstance(v, str) and v in allowed


def nullable(check):
    return lambda v: v is None or check(v)


def sequence(check):
    return lambda v: isinstance(v, list) and all(check(x) for x in v)


def record(fields):
    return lambda v: isinstance(v, dict) and all(k in fields and fields[k](x) for k, x in v.items())


def mapping(keys, values):
    return lambda v: isinstance(v, dict) and all(keys(k) and values(x) for k, x in v.items())


def EMPTY(v):
    return isinstance(v, list) and not v


COLOR = enum(
    "ManaColor_White",
    "ManaColor_Blue",
    "ManaColor_Black",
    "ManaColor_Red",
    "ManaColor_Green",
    "ManaColor_Colorless",
    "ManaColor_Generic",
    "ManaColor_Any",
    "ManaColor_AnyColor",
    "ManaColor_X",
    "White",
    "Blue",
    "Black",
    "Red",
    "Green",
    "Colorless",
    "Generic",
    "Any",
    "AnyColor",
    "X",
    "W",
    "U",
    "B",
    "R",
    "G",
    "C",
)
HIGHLIGHT = enum(
    "Cold",
    "Tepid",
    "Hot",
    "HighlightType_Cold",
    "HighlightType_Tepid",
    "HighlightType_Hot",
    "HighlightType_ReplaceRole",
)
DESIGNATION = enum("Commander", "PlayerSpeed")
REQUEST = enum(
    "ActionsAvailable",
    "CastingTimeOptions",
    "Group",
    "Mulligan",
    "OptionalAction",
    "Search",
    "SelectN",
    "SelectTargets",
)
REQUEST_CLASS = enum(
    "ActionsAvailableRequest",
    "CastingTimeOptionsRequest",
    "GroupRequest",
    "MulliganRequest",
    "OptionalActionMessageRequest",
    "PayCostsRequest",
    "SearchRequest",
    "SelectNRequest",
    "SelectTargetsRequest",
)
ACTION = enum(
    "CastingTimeOption",
    "Cast",
    "Play",
    "Pass",
    "ActivateMana",
    "FloatMana",
    "Activate",
    "Special",
    "PlayMdfc",
    "ActionType_PlayMdfc",
    "CastAdventure",
    "CastOmen",
    *(
        "ActionType_" + v
        for v in (
            "Activate",
            "ActivateMana",
            "Activate_Mana",
            "AssignDamage",
            "Attack",
            "AttackWithGroup",
            "Block",
            "BlockWithGroup",
            "Cast",
            "CastAdventure",
            "CastLeft",
            "CastLeftRoom",
            "CastMDFC",
            "CastOmen",
            "CastPrototype",
            "CastRight",
            "CastRightRoom",
            "CastingOption",
            "ChooseStartingPlayer",
            "CombatCost",
            "Distribute",
            "FloatMana",
            "Make",
            "ManaChoice",
            "MulliganKeep",
            "MulliganMull",
            "None",
            "NumericInput",
            "OpeningHandAction",
            "OrderDamage",
            "OrderTriggeredAbilities",
            "Pass",
            "PayCosts",
            "Play",
            "PlayMDFC",
            "ResolutionCost",
            "SearchLibrary",
            "SelectCounters",
            "SelectN",
            "SelectReplacement",
            "SelectTarget",
            "Special",
        )
    ),
)


def cost_colors(value):
    # Native bridge can stringify its enum list; decode only a bounded list
    # of known mana colors, retaining the original value in the case.
    import json

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return False
    return sequence(COLOR)(value)


MANA_COST = sequence(record({"abilityGrpId": integer, "color": cost_colors, "count": integer}))
MANA_SPEC = record(
    {
        "type": enum(
            "ManaSpecType_AdditionalEffect",
            "ManaSpecType_CantBeCountered",
            "ManaSpecType_FromCreature",
            "ManaSpecType_FromTreasure",
            "ManaSpecType_GainsHaste",
            "ManaSpecType_Predictive",
            "ManaSpecType_Restricted",
        ),
        "abilityGrpId": integer,
    }
)
MANA = record(
    {
        "abilityGrpId": integer,
        "color": COLOR,
        "count": integer,
        "manaId": integer,
        "srcInstanceId": integer,
        "specs": sequence(MANA_SPEC),
        "linkedSelections": EMPTY,
    }
)
PAYMENT = record({"mana": sequence(MANA), "haveTriggersBeenApplied": boolean, "selectedColor": COLOR})
AUTOTAP = record(
    {"abilityGrpId": integer, "instanceId": integer, "manaId": integer, "manaPaymentOption": PAYMENT}
)
CONDITION = record(
    {"abilityGrpId": integer, "colors": sequence(COLOR), "type": enum("ManaPaymentConditionType_Threshold")}
)
AUTOTAP_SOLUTION = record(
    {
        "autoTapActions": sequence(AUTOTAP),
        "manaPaymentConditions": sequence(CONDITION),
        "manaPayments": sequence(record({"manaColor": COLOR})),
    }
)
MANA_SELECTION = record(
    {
        "abilityGrpId": integer,
        "instanceId": integer,
        "options": sequence(PAYMENT),
        "selectionCount": integer,
        "validationType": enum("SelectionValidationType_NonRepeatable"),
    }
)
TARGET = record(
    {
        "targetIdx": integer,
        "targetId": integer,
        "targets": sequence(record({"targetInstanceId": integer, "highlight": HIGHLIGHT})),
    }
)
FACE = record({"grp_id": integer, "name": text, "oracle_text": text, "type_line": text, "mana_cost": text})
PROMPT = record({"promptId": integer, "parameters": EMPTY})
ACTION_CONTEXT = record(
    {
        "type": enum("ActionsAvailable", "actions_available"),
        "requestType": enum("ActionsAvailable"),
        "requestClass": enum("ActionsAvailableRequest"),
        "prompt": PROMPT,
        "sourceId": integer,
        "_bridge_source": boolean,
        "num_actions": integer,
        "action_types": sequence(ACTION),
    }
)


def mulligan_context(value):
    """The native opening-hand prompt carries one numeric card count."""
    fields = {
        "type",
        "mulligan_count",
        "requestType",
        "requestClass",
        "prompt",
        "sourceId",
        "mulliganCount",
        "freeMulliganCount",
        "_bridge_source",
    }
    if type(value) is not dict or set(value) != fields:
        return False
    if (value["type"], value["requestType"], value["requestClass"]) != (
        "mulligan",
        "Mulligan",
        "MulliganRequest",
    ):
        return False
    if (
        value["_bridge_source"] is not True
        or value["sourceId"] != 0
        or not all(
            integer(value[k]) and value[k] >= 0
            for k in ("sourceId", "mulligan_count", "mulliganCount", "freeMulliganCount")
        )
    ):
        return False
    prompt = value["prompt"]
    if (
        not isinstance(prompt, dict)
        or set(prompt) != {"promptId", "parameters"}
        or not integer(prompt["promptId"])
        or prompt["promptId"] != 34
    ):
        return False
    params = prompt["parameters"]
    if type(params) is not list or len(params) != 1 or type(params[0]) is not dict:
        return False
    item = params[0]
    return (
        set(item) == {"parameterName", "type", "value", "valueCase"}
        and (item["parameterName"], item["type"], item["valueCase"])
        == ("NumberOfCards", "Number", "NumberValue")
        and integer(item["value"])
        and 0 <= item["value"] <= 7
        and value["mulligan_count"] == value["mulliganCount"]
    )


def _same_json(value, expected):
    """Compare a recorded mirror with a bounded, typed producer shape (True != 1)."""
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return value.keys() == expected.keys() and all(_same_json(value[k], v) for k, v in expected.items())
    if isinstance(expected, list):
        return len(value) == len(expected) and all(
            _same_json(x, y) for x, y in zip(value, expected, strict=True)
        )
    return value == expected


def _target_cards(state):
    cards = {}
    zones = state.get("zones", {})
    if not isinstance(zones, dict):
        return None
    objects_by_zone = [
        (key, state.get(key, []))
        for key in (
            "stack",
            "battlefield",
            "hand",
            "graveyard",
            "exile",
            "command",
            "pending_selection_objects",
        )
    ]
    objects_by_zone.extend((key, zones.get(key, [])) for key in ("local_graveyard", "opponent_graveyard"))
    for zone, objects in objects_by_zone:
        if type(objects) is not list:
            return None
        for card in objects:
            if type(card) is not dict:
                return None
            iid = card.get("instance_id")
            if type(iid) is int and iid > 0:
                # An ID with competing zone evidence cannot witness any role.
                cards[iid] = (zone, card) if iid not in cards else None
    return cards


def _public_target_card(cards, iid, zone):
    entry = cards.get(iid) if type(iid) is int else None
    if not entry or entry[0] != zone:
        return None
    card = entry[1]
    if (
        not card
        or card.get("zone") not in (None, zone)
        or card.get("face_down_reason") not in (None, 0, "")
        or card.get("face_down")
        or card.get("identity_known") is False
        or card.get("visibility") not in (None, "public")
        or type(card.get("name")) is not str
        or not card["name"]
        or not integer(card.get("grp_id"))
        or card["grp_id"] <= 0
        or card["grp_id"] == 3  # Arena's face-down print, even without the flag.
    ):
        return None
    return card


def _target_menu(candidates, cards):
    if type(candidates) is not list or not 1 <= len(candidates) <= 100:
        return None
    verified, ids, options = [], [], []
    for candidate in candidates:
        if type(candidate) is not dict:
            return None
        iid, highlight = candidate.get("targetInstanceId"), candidate.get("highlight")
        card = _public_target_card(cards, iid, "battlefield")
        if (
            not card
            or card.get("object_kind") != "CARD"
            or iid in ids
            or highlight
            not in (
                "HighlightType_Hot",
                "HighlightType_Tepid",
                "HighlightType_Cold",
            )
        ):
            return None
        ids.append(iid)
        verified.append(
            {"targetInstanceId": iid, "legalAction": "SelectAction_Select", "highlight": highlight}
        )
        options.append(
            {
                "option_id": f"tgt:{iid}",
                "label": card["name"],
                "payable": None,
                "meta": {
                    "instanceId": iid,
                    "grpId": 0,
                    "targetIdx": 1,
                    "highlight": highlight.removeprefix("HighlightType_"),
                },
            }
        )
    return verified, ids, options


def target_context(state, pending):
    """Admit the observed single compulsory target with its full, unchanged public menu."""
    ctx = state.get("decision_context")
    if (
        type(ctx) is not dict
        or state.get("_bridge_request_type") != "SelectTargets"
        or state.get("_bridge_request_class") != "SelectTargetsRequest"
        or pending.get("request_type") != "SelectTargets"
        or state.get("local_seat_id") not in (1, 2)
        or type(state.get("local_seat_id")) is not int
        or not _same_json(state.get("decision_seat_id"), state["local_seat_id"])
    ):
        return False
    cards = _target_cards(state)
    if not cards:
        return False
    source_id = ctx.get("sourceId")
    source = _public_target_card(cards, source_id, "stack")
    if (
        not source
        or type(source.get("oracle_text")) is not str
        or not source["oracle_text"]
        or pending.get("source_label") != source["name"]
    ):
        return False
    origin = source
    extra = {}
    if source.get("object_kind") == "ABILITY":
        origin = _public_target_card(cards, source.get("parent_instance_id"), "battlefield")
        if (
            not origin
            or origin.get("object_kind") != "CARD"
            or type(origin.get("oracle_text")) is not str
            or not origin["oracle_text"]
        ):
            return False
        extra = {
            "source_parent_instance_id": origin["instance_id"],
            "source_card_oracle_text": origin["oracle_text"],
        }
    elif source.get("object_kind") != "CARD":
        return False
    rows = ctx.get("targets")
    if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
        return False
    row = rows[0]
    menu = _target_menu(row.get("targets"), cards)
    if not menu:
        return False
    verified, ids, options = menu
    prompt = row.get("prompt")
    if (
        type(prompt) is not dict
        or type(prompt.get("promptId")) is not int
        or prompt["promptId"] not in (1010, 152, 7087, 1330)
    ):
        return False
    ability = ctx.get("abilityGrpId")
    targeting_ability = row.get("targetingAbilityGrpId")
    if not all(type(n) is int and n > 0 for n in (ability, targeting_ability)):
        return False
    if ability != source["grp_id"] or (source["object_kind"] == "ABILITY" and ability != targeting_ability):
        return False
    abilities = origin.get("ability_grp_ids")
    if (
        type(abilities) is not list
        or not all(integer(gid) for gid in abilities)
        or targeting_ability not in abilities
    ):
        return False
    expected_rows = [
        {
            "targetIdx": 1,
            "minTargets": 1,
            "maxTargets": 1,
            "targets": verified,
            "targetingAbilityGrpId": targeting_ability,
            "targetingPlayer": state["local_seat_id"],
            "prompt": {
                "promptId": prompt["promptId"],
                "parameters": [
                    {
                        "parameterName": "CardId",
                        "type": "ParameterType_Number",
                        "numberValue": origin["instance_id"],
                    }
                ],
            },
        }
    ]
    raw = {"targets": expected_rows, "sourceId": source_id, "abilityGrpId": ability}
    expected = {
        **raw,
        "type": "target_selection",
        "source_id": source_id,
        "source_card": origin["name"],
        "source_oracle_text": source["oracle_text"],
        "raw": raw,
        **extra,
        "requestType": "SelectTargets",
        "requestClass": "SelectTargetsRequest",
        "prompt": {"promptId": 10, "parameters": []},
        "_bridge_source": True,
    }
    slots = [{"target_idx": 1, "min_targets": 1, "max_targets": 1, "selected": 0, "candidate_ids": ids}]
    request_id = pending.get("request_id")
    if (
        type(request_id) is not list
        or len(request_id) != 2
        or not all(type(part) is int and part >= 0 for part in request_id)
        or type(pending.get("can_cancel")) is not bool
        or not _same_json(state.get("_bridge_can_cancel"), pending["can_cancel"])
        or state.get("_bridge_can_pass") is not False
    ):
        return False
    expected_pending = {
        "request_id": request_id,
        "request_type": "SelectTargets",
        "options": options,
        "min_select": 1,
        "max_select": 1,
        "can_pass": False,
        "can_cancel": pending["can_cancel"],
        "source_label": source["name"],
        "slots": slots,
        "min_weight": None,
        "max_weight": None,
    }
    return _same_json(ctx, expected) and _same_json(pending, expected_pending)


CONTROLLED = record(
    {
        "opponent_controlled_by_you": boolean,
        "you_controlled_by_opponent": boolean,
        "deciding_for_opponent": boolean,
    }
)

CARD_EXTENSIONS = {
    "token_creator": text,
    "ability_grp_ids": sequence(integer),
    "related_faces": sequence(FACE),
    "targeting": sequence(integer),
    "death_swing": sequence(integer),
    "_return_credit_blocked": boolean,
    "door_token_seen": boolean,
    "face_down_reason": integer,
    "selection_source_grp_id": integer,
}
PLAYER_EXTENSIONS = {"has_enduring_story": nullable(boolean), "designations": sequence(DESIGNATION)}
ACTION_EXTENSIONS = {
    "actionType": ACTION,
    "manaCost": nullable(MANA_COST),
    "autoTapActions": sequence(AUTOTAP),
    "autoTapSolution": AUTOTAP_SOLUTION,
    "manaPaymentOptions": sequence(PAYMENT),
    "manaPaymentConditions": sequence(CONDITION),
    "manaSelections": sequence(MANA_SELECTION),
    "targets": sequence(TARGET),
    "highlight": HIGHLIGHT,
    "identity_known": boolean,
    "requestClass": REQUEST_CLASS,
    "label": text,
    "facetId": integer,
    "uniqueAbilityId": integer,
    "optionIndex": integer,
    "player_seat": integer,
    "color_id": integer,
    "title_id": integer,
    "subtype_id": integer,
    "numericValue": integer,
    "maxActivations": integer,
    "shouldStop": boolean,
    "manaSpentMatters": boolean,
    "_autotap_lands": sequence(record({"instanceId": nullable(integer), "mana": enum("")})),
    "london_mulligan": boolean,
}


def extended_field(context, key, value):
    fields = {"card": CARD_EXTENSIONS, "player": PLAYER_EXTENSIONS, "meta": ACTION_EXTENSIONS}.get(
        context, {}
    )
    return key in fields and fields[key](value)


def state_field(key, value, card_check):
    if key == "deck_cards":
        return sequence(integer)(value)
    if key == "controlled_turn":
        return CONTROLLED(value)
    if key == "decision_context":
        return value is None or ACTION_CONTEXT(value)
    if key == "_bridge_request_type":
        return nullable(REQUEST)(value)
    if key == "_bridge_request_class":
        return nullable(REQUEST_CLASS)(value)
    if key == "designations":
        return mapping(lambda k: str(k) in {"1", "2"}, sequence(DESIGNATION))(value)
    if key == "zones":
        fields = {
            "opponent_hand_count": integer,
            "library_count": nullable(integer),
            "opponent_library_count": nullable(integer),
            "library_count_source": enum("bridge_total_card_count", "log_zone_membership", "unknown"),
            "local_graveyard": sequence(card_check),
            "opponent_graveyard": sequence(card_check),
        }
        return record(fields)(value)
    if key == "pending_selection_objects":
        return sequence(card_check)(value)
    return False


def legal_labels(value, state):
    """Validate source renderer grammar against names already in card records."""
    import re

    if not isinstance(value, list):
        return False
    names = {"Land", "Spell"}
    for zone in ("hand", "battlefield", "graveyard", "exile", "command", "stack", "deck_cards"):
        for card in state.get(zone) or []:
            if isinstance(card, dict):
                if isinstance(card.get("name"), str):
                    names.add(card["name"])
                for face in card.get("related_faces") or []:
                    if isinstance(face, dict) and isinstance(face.get("name"), str):
                        names.add(face["name"])
    for label in value:
        if not isinstance(label, str):
            return False
        if label == "Pass":
            continue
        if label.startswith("Action: ") and ACTION("ActionType_" + label[len("Action: ") :]):
            continue
        stem = re.sub(r" \[OK\]$", "", label)
        stem = re.sub(r" \[NEED:(?:\{[0-9WUBRGCXYZ/]+\})+\]$", "", stem)
        if stem == "Activate Ability":
            continue
        if any(
            stem.startswith(prefix) and stem[len(prefix) :] in names
            for prefix in ("Play Land: ", "Cast ", "Activate Ability: ")
        ):
            continue
        return False
    return True
