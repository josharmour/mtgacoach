"""GRE Action Serializer — builds PerformActionResp messages from GREActionRef.

Offline serializer that takes a GREActionRef (or raw action dict) and produces
a properly formatted PerformActionResp message as a JSON-serializable dict.
This is a prerequisite for eventual direct GRE submission (bypassing UI clicks).

The output format mirrors the protobuf JSON representation used by MTGA's
ClientToGREMessage, based on the GRE protobuf message definitions.

This module does NOT send anything over the network.  It only serializes.
"""

from __future__ import annotations

import copy
import logging
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protobuf enum mirrors (ActionType, AutoPassPriority, ClientMessageType)
# ---------------------------------------------------------------------------

class GREActionType(Enum):
    """ActionType enum from GRE protobuf."""
    NONE = "ActionType_None"
    CAST = "ActionType_Cast"
    ACTIVATE = "ActionType_Activate"
    PLAY = "ActionType_Play"
    ACTIVATE_MANA = "ActionType_Activate_Mana"
    PASS = "ActionType_Pass"
    ACTIVATE_TEST = "ActionType_Activate_Test"
    SPECIAL = "ActionType_Special"
    SPECIAL_TURN_FACE_UP = "ActionType_Special_TurnFaceUp"
    RESOLUTION_COST = "ActionType_ResolutionCost"
    CAST_LEFT = "ActionType_CastLeft"
    CAST_RIGHT = "ActionType_CastRight"
    MAKE_PAYMENT = "ActionType_Make_Payment"
    COMBAT_COST = "ActionType_CombatCost"
    OPENING_HAND_ACTION = "ActionType_OpeningHandAction"
    CAST_ADVENTURE = "ActionType_CastAdventure"
    FLOAT_MANA = "ActionType_FloatMana"
    CAST_MDFC = "ActionType_CastMDFC"
    PLAY_MDFC = "ActionType_PlayMDFC"
    SPECIAL_PAYMENT = "ActionType_Special_Payment"
    CAST_PROTOTYPE = "ActionType_CastPrototype"
    CAST_LEFT_ROOM = "ActionType_CastLeftRoom"
    CAST_RIGHT_ROOM = "ActionType_CastRightRoom"
    CAST_OMEN = "ActionType_CastOmen"


class AutoPassPriority(Enum):
    """AutoPassPriority enum from GRE protobuf."""
    NONE = "AutoPassPriority_None"
    NO = "AutoPassPriority_No"
    YES = "AutoPassPriority_Yes"


# All ActionType_Cast* variants
_CAST_ACTION_TYPES = frozenset({
    "ActionType_Cast",
    "ActionType_CastLeft",
    "ActionType_CastRight",
    "ActionType_CastAdventure",
    "ActionType_CastMDFC",
    "ActionType_CastPrototype",
    "ActionType_CastLeftRoom",
    "ActionType_CastRightRoom",
    "ActionType_CastOmen",
})

# Action types that represent playing (not casting) a card
_PLAY_ACTION_TYPES = frozenset({
    "ActionType_Play",
    "ActionType_PlayMDFC",
})


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class SerializationError(Exception):
    """Raised when a GRE action cannot be serialized."""
    pass


class ValidationError(SerializationError):
    """Raised when an action ref fails validation against legal actions."""
    pass


# ---------------------------------------------------------------------------
# Action serialization helpers
# ---------------------------------------------------------------------------

def _serialize_action(raw_action: dict[str, Any]) -> dict[str, Any]:
    """Serialize a raw GRE action dict into the PerformActionResp.Action format.

    The GRE expects a minimal Action message: only fields with non-default
    values should be included.  Zero-valued uint32 fields and False booleans
    are omitted to match protobuf serialization conventions.

    Args:
        raw_action: A raw GRE action dict as stored in ``legal_actions_raw``.

    Returns:
        A cleaned Action dict suitable for PerformActionResp.actions[].
    """
    action: dict[str, Any] = {}

    # --- Required: actionType ---
    action_type = raw_action.get("actionType", "")
    if not action_type:
        raise SerializationError("Action missing 'actionType' field")
    action["actionType"] = action_type

    # --- Identity fields (uint32, omit if 0) ---
    _copy_uint("grpId", raw_action, action)
    _copy_uint("instanceId", raw_action, action)
    _copy_uint("facetId", raw_action, action)
    _copy_uint("abilityGrpId", raw_action, action)
    _copy_uint("sourceId", raw_action, action)
    _copy_uint("alternativeGrpId", raw_action, action)
    _copy_uint("selectionType", raw_action, action)
    _copy_uint("selection", raw_action, action)

    # --- Targeting ---
    targets = raw_action.get("targets")
    if targets:
        action["targets"] = _serialize_targets(targets)

    # --- Mana payment (forward from GRE, not modified) ---
    mana_payment_options = raw_action.get("manaPaymentOptions")
    if mana_payment_options:
        action["manaPaymentOptions"] = copy.deepcopy(mana_payment_options)

    mana_cost = raw_action.get("manaCost")
    if mana_cost:
        action["manaCost"] = copy.deepcopy(mana_cost)

    auto_tap = raw_action.get("autoTapSolution")
    if auto_tap:
        action["autoTapSolution"] = copy.deepcopy(auto_tap)

    # --- Booleans (only include if True) ---
    if raw_action.get("shouldStop"):
        action["shouldStop"] = True

    # --- Extra fields the GRE may include ---
    _copy_uint("uniqueAbilityId", raw_action, action)
    _copy_uint("timingSourceGrpid", raw_action, action)

    return action


def _copy_uint(key: str, src: dict, dst: dict) -> None:
    """Copy a uint field if present and non-zero."""
    val = src.get(key, 0)
    if val:
        dst[key] = int(val)


def _serialize_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Serialize target selection entries.

    Each entry in the GRE ``targets`` list is a TargetSelection message with
    sub-``targets`` containing Target messages.  We preserve the structure but
    clean up zero-valued fields.
    """
    result = []
    for ts in targets:
        entry: dict[str, Any] = {}
        # targetIdx is an index (0 is valid for the first target selection),
        # so include it whenever explicitly present in the source dict.
        if "targetIdx" in ts:
            entry["targetIdx"] = int(ts["targetIdx"])

        sub_targets = ts.get("targets", [])
        if sub_targets:
            serialized_sub = []
            for t in sub_targets:
                st: dict[str, Any] = {}
                _copy_uint("targetInstanceId", t, st)
                _copy_uint("targetGrpId", t, st)  # Non-standard but used in our GREActionRef
                if "legalAction" in t:
                    st["legalAction"] = t["legalAction"]
                if "highlight" in t and t["highlight"]:
                    st["highlight"] = t["highlight"]
                if st:
                    serialized_sub.append(st)
            if serialized_sub:
                entry["targets"] = serialized_sub

        _copy_uint("minTargets", ts, entry)
        _copy_uint("maxTargets", ts, entry)
        _copy_uint("selectedTargets", ts, entry)
        _copy_uint("targetSourceZoneId", ts, entry)
        _copy_uint("targetingAbilityGrpId", ts, entry)
        _copy_uint("targetingPlayer", ts, entry)

        if entry:
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# PerformActionResp builder
# ---------------------------------------------------------------------------

def serialize_perform_action_resp(
    raw_action: dict[str, Any],
    *,
    auto_pass: AutoPassPriority = AutoPassPriority.NONE,
) -> dict[str, Any]:
    """Build a PerformActionResp message from a single raw GRE action.

    This is the core serialization function.  It produces a dict matching the
    protobuf JSON format of ``PerformActionResp``.

    Protobuf fields (from PerformActionResp.cs):
      - actions:          repeated Action   (field 1)
      - autoPassPriority: AutoPassPriority  (field 2)
      - setYield:         SettingStatus     (field 3)
      - appliesTo:        SettingScope      (field 4)
      - mapTo:            SettingKey        (field 5)

    For normal gameplay actions we only populate ``actions`` and optionally
    ``autoPassPriority``.

    Args:
        raw_action: A raw GRE action dict (from legal_actions_raw).
        auto_pass: Optional auto-pass priority setting.

    Returns:
        A JSON-serializable dict representing PerformActionResp.
    """
    action_msg = _serialize_action(raw_action)

    resp: dict[str, Any] = {
        "actions": [action_msg],
    }

    if auto_pass != AutoPassPriority.NONE:
        resp["autoPassPriority"] = auto_pass.value

    return resp


def serialize_perform_action_resp_multi(
    raw_actions: list[dict[str, Any]],
    *,
    auto_pass: AutoPassPriority = AutoPassPriority.NONE,
) -> dict[str, Any]:
    """Build a PerformActionResp with multiple actions (e.g. mana batch).

    Some responses (like batch mana payments) include multiple Action messages
    in a single PerformActionResp.

    Args:
        raw_actions: List of raw GRE action dicts.
        auto_pass: Optional auto-pass priority setting.

    Returns:
        A JSON-serializable dict representing PerformActionResp.
    """
    if not raw_actions:
        raise SerializationError("Cannot serialize empty action list")

    action_msgs = [_serialize_action(a) for a in raw_actions]

    resp: dict[str, Any] = {
        "actions": action_msgs,
    }

    if auto_pass != AutoPassPriority.NONE:
        resp["autoPassPriority"] = auto_pass.value

    return resp


# ---------------------------------------------------------------------------
# ClientToGREMessage builder
# ---------------------------------------------------------------------------

def serialize_client_message(
    raw_action: dict[str, Any],
    *,
    system_seat_id: int,
    game_state_id: int,
    auto_pass: AutoPassPriority = AutoPassPriority.NONE,
) -> dict[str, Any]:
    """Build a complete ClientToGREMessage wrapping a PerformActionResp.

    This is the top-level message that would be sent to the GRE.  It includes
    the envelope fields (type, systemSeatId, gameStateId) plus the
    performActionResp payload.

    Protobuf fields (from ClientToGREMessage.cs):
      - type:                ClientMessageType  (field 1)
      - systemSeatId:        uint32             (field 2)
      - gameStateId:         uint32             (field 3)
      - performActionResp:   PerformActionResp  (field 14, oneof message)

    Args:
        raw_action: A raw GRE action dict.
        system_seat_id: The player's seat ID (1 or 2).
        game_state_id: The current game state ID from the GRE.
        auto_pass: Optional auto-pass priority setting.

    Returns:
        A JSON-serializable dict representing ClientToGREMessage.
    """
    perform_resp = serialize_perform_action_resp(
        raw_action, auto_pass=auto_pass
    )

    msg: dict[str, Any] = {
        "type": "ClientMessageType_PerformActionResp",
        "systemSeatId": system_seat_id,
        "gameStateId": game_state_id,
        "performActionResp": perform_resp,
    }

    return msg


# ---------------------------------------------------------------------------
# GREActionRef integration
# ---------------------------------------------------------------------------

def serialize_from_action_ref(
    action_ref: "GREActionRef",  # noqa: F821
    *,
    system_seat_id: int = 0,
    game_state_id: int = 0,
    auto_pass: AutoPassPriority = AutoPassPriority.NONE,
) -> dict[str, Any]:
    """Serialize a GREActionRef into a PerformActionResp or ClientToGREMessage.

    If ``system_seat_id`` and ``game_state_id`` are provided, returns a full
    ClientToGREMessage envelope.  Otherwise returns just the PerformActionResp.

    Args:
        action_ref: A GREActionRef from the gre_action_matcher.
        system_seat_id: Player seat ID (0 = omit envelope).
        game_state_id: Game state ID (0 = omit envelope).
        auto_pass: Optional auto-pass priority setting.

    Returns:
        A JSON-serializable dict.

    Raises:
        SerializationError: If the action_ref has no raw action data.
    """
    # Prefer the stored raw dict for maximum fidelity
    if action_ref.raw:
        raw = action_ref.raw
    else:
        # Reconstruct from GREActionRef fields
        raw = _action_ref_to_raw(action_ref)

    if system_seat_id and game_state_id:
        return serialize_client_message(
            raw,
            system_seat_id=system_seat_id,
            game_state_id=game_state_id,
            auto_pass=auto_pass,
        )
    else:
        return serialize_perform_action_resp(raw, auto_pass=auto_pass)


def _action_ref_to_raw(action_ref: "GREActionRef") -> dict[str, Any]:  # noqa: F821
    """Reconstruct a raw action dict from GREActionRef fields.

    This is a fallback for when the original raw dict was not preserved.
    """
    raw: dict[str, Any] = {
        "actionType": action_ref.action_type,
    }
    if action_ref.grp_id:
        raw["grpId"] = action_ref.grp_id
    if action_ref.instance_id:
        raw["instanceId"] = action_ref.instance_id
    if action_ref.ability_grp_id:
        raw["abilityGrpId"] = action_ref.ability_grp_id
    if action_ref.source_id:
        raw["sourceId"] = action_ref.source_id
    if action_ref.alternative_grp_id:
        raw["alternativeGrpId"] = action_ref.alternative_grp_id
    if action_ref.selection_type:
        raw["selectionType"] = action_ref.selection_type
    if action_ref.selection:
        raw["selection"] = action_ref.selection
    if action_ref.targets:
        # GREActionRef.targets is a list of {targetInstanceId, targetGrpId} dicts
        # Wrap each into a TargetSelection.targets[].Target structure
        raw["targets"] = [{
            "targets": [
                {"targetInstanceId": t.get("targetInstanceId", 0)}
                for t in action_ref.targets
                if t.get("targetInstanceId", 0)
            ],
        }]
    return raw


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_action_against_legal(
    raw_action: dict[str, Any],
    legal_actions_raw: list[dict[str, Any]],
) -> bool:
    """Check that a raw action matches one of the legal actions from the GRE.

    Matching is done by comparing the identity fields:
    (actionType, grpId, instanceId, abilityGrpId, sourceId, selection).

    Args:
        raw_action: The action to validate.
        legal_actions_raw: The list of legal actions from ActionsAvailableReq.

    Returns:
        True if the action matches a legal action, False otherwise.
    """
    if not legal_actions_raw:
        return False

    action_key = _action_identity_key(raw_action)

    for legal in legal_actions_raw:
        if _action_identity_key(legal) == action_key:
            return True

    return False


def find_matching_legal_action(
    raw_action: dict[str, Any],
    legal_actions_raw: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Find the matching legal action for a given raw action.

    Returns the full legal action dict (with all GRE-provided fields like
    manaPaymentOptions, autoTapSolution, etc.) if found.

    Args:
        raw_action: The action to look up.
        legal_actions_raw: The list of legal actions from ActionsAvailableReq.

    Returns:
        The matching legal action dict, or None if not found.
    """
    if not legal_actions_raw:
        return None

    action_key = _action_identity_key(raw_action)

    for legal in legal_actions_raw:
        if _action_identity_key(legal) == action_key:
            return legal

    return None


def _action_identity_key(action: dict[str, Any]) -> tuple:
    """Extract a hashable identity key from an action dict.

    The identity key uses the fields that uniquely identify a GRE action:
    - actionType: what kind of action
    - grpId: which card definition
    - instanceId: which specific game object
    - abilityGrpId: which ability (for activations)
    - sourceId: source of the ability
    - selectionType + selection: for modal/selection actions
    """
    return (
        action.get("actionType", ""),
        action.get("grpId", 0),
        action.get("instanceId", 0),
        action.get("abilityGrpId", 0),
        action.get("sourceId", 0),
        action.get("selectionType", 0),
        action.get("selection", 0),
    )


# ---------------------------------------------------------------------------
# Validated serialization (combines validation + serialization)
# ---------------------------------------------------------------------------

def serialize_validated(
    raw_action: dict[str, Any],
    legal_actions_raw: list[dict[str, Any]],
    *,
    system_seat_id: int = 0,
    game_state_id: int = 0,
    auto_pass: AutoPassPriority = AutoPassPriority.NONE,
    use_legal_action_data: bool = True,
) -> dict[str, Any]:
    """Serialize an action after validating it against legal actions.

    If ``use_legal_action_data`` is True (default), the matched legal action
    from the GRE is used as the serialization source.  This ensures that
    fields like ``manaPaymentOptions`` and ``autoTapSolution`` — which the
    GRE provides but which GREActionRef may not preserve — are included.

    Args:
        raw_action: The action to serialize.
        legal_actions_raw: Legal actions from the current game state.
        system_seat_id: Player seat ID (0 = PerformActionResp only).
        game_state_id: Game state ID (0 = PerformActionResp only).
        auto_pass: Optional auto-pass priority setting.
        use_legal_action_data: Use the matched legal action for full fidelity.

    Returns:
        A JSON-serializable dict (PerformActionResp or ClientToGREMessage).

    Raises:
        ValidationError: If the action does not match any legal action.
    """
    matched = find_matching_legal_action(raw_action, legal_actions_raw)
    if matched is None:
        action_type = raw_action.get("actionType", "unknown")
        grp_id = raw_action.get("grpId", 0)
        instance_id = raw_action.get("instanceId", 0)
        raise ValidationError(
            f"Action ({action_type}, grpId={grp_id}, instanceId={instance_id}) "
            f"not found in {len(legal_actions_raw)} legal actions"
        )

    source = matched if use_legal_action_data else raw_action

    if system_seat_id and game_state_id:
        return serialize_client_message(
            source,
            system_seat_id=system_seat_id,
            game_state_id=game_state_id,
            auto_pass=auto_pass,
        )
    else:
        return serialize_perform_action_resp(source, auto_pass=auto_pass)


# ---------------------------------------------------------------------------
# Convenience: common action type builders
# ---------------------------------------------------------------------------

def build_pass_action() -> dict[str, Any]:
    """Build a minimal raw action dict for passing priority."""
    return {"actionType": "ActionType_Pass"}


def build_cast_action(
    grp_id: int,
    instance_id: int,
    *,
    ability_grp_id: int = 0,
    auto_tap_solution: Optional[dict] = None,
) -> dict[str, Any]:
    """Build a raw action dict for casting a spell.

    Args:
        grp_id: Card definition ID.
        instance_id: Game object instance ID.
        ability_grp_id: Ability group ID (for adventure, MDFC casts).
        auto_tap_solution: Optional auto-tap solution from the GRE.
    """
    action: dict[str, Any] = {
        "actionType": "ActionType_Cast",
        "grpId": grp_id,
        "instanceId": instance_id,
    }
    if ability_grp_id:
        action["abilityGrpId"] = ability_grp_id
    if auto_tap_solution:
        action["autoTapSolution"] = auto_tap_solution
    return action


def build_play_land_action(grp_id: int, instance_id: int) -> dict[str, Any]:
    """Build a raw action dict for playing a land."""
    return {
        "actionType": "ActionType_Play",
        "grpId": grp_id,
        "instanceId": instance_id,
    }


def build_activate_action(
    instance_id: int,
    ability_grp_id: int,
    *,
    source_id: int = 0,
    grp_id: int = 0,
) -> dict[str, Any]:
    """Build a raw action dict for activating an ability.

    Args:
        instance_id: Game object instance ID.
        ability_grp_id: Ability group ID.
        source_id: Source object ID (often same as instance_id).
        grp_id: Card definition ID.
    """
    action: dict[str, Any] = {
        "actionType": "ActionType_Activate",
        "instanceId": instance_id,
        "abilityGrpId": ability_grp_id,
    }
    if source_id:
        action["sourceId"] = source_id
    if grp_id:
        action["grpId"] = grp_id
    return action


def build_targeted_action(
    action_type: str,
    instance_id: int,
    target_instance_ids: list[int],
    *,
    grp_id: int = 0,
    ability_grp_id: int = 0,
) -> dict[str, Any]:
    """Build a raw action dict with target selections.

    Args:
        action_type: The GRE ActionType string.
        instance_id: Source game object instance ID.
        target_instance_ids: List of target instance IDs.
        grp_id: Card definition ID.
        ability_grp_id: Ability group ID.
    """
    action: dict[str, Any] = {
        "actionType": action_type,
        "instanceId": instance_id,
    }
    if grp_id:
        action["grpId"] = grp_id
    if ability_grp_id:
        action["abilityGrpId"] = ability_grp_id

    if target_instance_ids:
        action["targets"] = [{
            "targets": [
                {"targetInstanceId": tid} for tid in target_instance_ids
            ],
        }]

    return action
