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
    *(
        "ActionType_" + v
        for v in (
            "Activate",
            "ActivateMana",
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
    )
)
MANA_COST = sequence(record({"abilityGrpId": integer, "color": sequence(COLOR), "count": integer}))
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
    {"targetIdx": integer, "targets": sequence(record({"targetInstanceId": integer, "highlight": HIGHLIGHT}))}
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
    "_autotap_lands": sequence(integer),
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
