"""GRE Action Matcher — resolves high-level GameActions to raw GRE action dicts.

Phase 1 of the RE-driven autopilot refactor.  This module provides:

* ``GREActionRef`` — a compact, serializable reference to a specific raw GRE action.
* ``match_action_to_gre()`` — matches a ``GameAction`` to the best candidate
  from ``legal_actions_raw``.
* ``ACTION_TYPE_MAP`` — maps ``ActionType`` enum values to GRE ``actionType`` strings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from arenamcp.action_planner import ActionType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ActionType → GRE actionType string mapping
# ---------------------------------------------------------------------------

ACTION_TYPE_MAP: dict[ActionType, str] = {
    ActionType.PASS_PRIORITY: "ActionType_Pass",
    ActionType.RESOLVE: "ActionType_Pass",  # Resolve is also a pass in GRE
    ActionType.PLAY_LAND: "ActionType_Play",
    ActionType.CAST_SPELL: "ActionType_Cast",
    ActionType.ACTIVATE_ABILITY: "ActionType_Activate",
    ActionType.DECLARE_ATTACKERS: "ActionType_AttackWithGroup",
    ActionType.DECLARE_BLOCKERS: "ActionType_BlockWithGroup",
    ActionType.SELECT_TARGET: "ActionType_SelectTarget",
    ActionType.SELECT_N: "ActionType_SelectN",
    ActionType.MODAL_CHOICE: "ActionType_ManaChoice",
    ActionType.MULLIGAN_KEEP: "ActionType_MulliganKeep",
    ActionType.MULLIGAN_MULL: "ActionType_MulliganMull",
    ActionType.CLICK_BUTTON: "ActionType_Pass",
    ActionType.ORDER_BLOCKERS: "ActionType_OrderDamage",
    ActionType.ASSIGN_DAMAGE: "ActionType_AssignDamage",
    ActionType.ORDER_COMBAT_DAMAGE: "ActionType_OrderDamage",
    ActionType.PAY_COSTS: "ActionType_PayCosts",
    ActionType.SEARCH_LIBRARY: "ActionType_SearchLibrary",
    ActionType.DISTRIBUTE: "ActionType_Distribute",
    ActionType.NUMERIC_INPUT: "ActionType_NumericInput",
    ActionType.CHOOSE_STARTING_PLAYER: "ActionType_ChooseStartingPlayer",
    ActionType.SELECT_REPLACEMENT: "ActionType_SelectReplacement",
    ActionType.SELECT_COUNTERS: "ActionType_SelectCounters",
    ActionType.CASTING_OPTIONS: "ActionType_CastingOption",
    ActionType.ORDER_TRIGGERS: "ActionType_OrderTriggeredAbilities",
    # DRAFT_PICK has no GRE equivalent (handled via draft protocol)
}


# ---------------------------------------------------------------------------
# GREActionRef dataclass
# ---------------------------------------------------------------------------

@dataclass
class GREActionRef:
    """Compact, serializable reference to a selected GRE action."""

    action_type: str = ""           # e.g. "ActionType_Cast"
    grp_id: int = 0
    instance_id: int = 0
    ability_grp_id: int = 0
    source_id: int = 0
    alternative_grp_id: int = 0
    selection_type: int = 0
    selection: int = 0
    targets: list[dict] = field(default_factory=list)
    raw: Optional[dict] = None

    # -- Serialisation helpers ------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict (omits ``raw`` to keep it compact)."""
        d: dict[str, Any] = {
            "action_type": self.action_type,
            "grp_id": self.grp_id,
            "instance_id": self.instance_id,
        }
        if self.ability_grp_id:
            d["ability_grp_id"] = self.ability_grp_id
        if self.source_id:
            d["source_id"] = self.source_id
        if self.alternative_grp_id:
            d["alternative_grp_id"] = self.alternative_grp_id
        if self.selection_type:
            d["selection_type"] = self.selection_type
        if self.selection:
            d["selection"] = self.selection
        if self.targets:
            d["targets"] = self.targets
        return d

    @classmethod
    def from_raw(cls, raw_action: dict) -> "GREActionRef":
        """Create a ``GREActionRef`` from a raw GRE action dict."""
        targets: list[dict] = []
        for t in raw_action.get("targets", []):
            targets.append({
                "targetInstanceId": t.get("targetInstanceId", 0),
                "targetGrpId": t.get("targetGrpId", 0),
            })
        return cls(
            action_type=raw_action.get("actionType", ""),
            grp_id=raw_action.get("grpId", 0),
            instance_id=raw_action.get("instanceId", 0),
            ability_grp_id=raw_action.get("abilityGrpId", 0),
            source_id=raw_action.get("sourceId", 0),
            alternative_grp_id=raw_action.get("alternativeGrpId", 0),
            selection_type=raw_action.get("selectionType", 0),
            selection=raw_action.get("selection", 0),
            targets=targets,
            raw=raw_action,
        )


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _resolve_card_name(
    grp_id: int,
    game_objects: dict[int, dict],
    scryfall_lookup: Optional[Callable[[int], Optional[str]]],
) -> Optional[str]:
    """Resolve a ``grp_id`` to a card name.

    First checks ``game_objects`` (keyed by instance_id), then falls back to
    ``scryfall_lookup`` if provided.
    """
    # Scan game_objects for matching grp_id
    for obj in game_objects.values():
        obj_grp = obj.get("grp_id") or obj.get("grpId", 0)
        if obj_grp == grp_id:
            name = obj.get("name") or obj.get("card_name")
            if name:
                return name

    # Fallback to scryfall lookup
    if scryfall_lookup and grp_id:
        try:
            name = scryfall_lookup(grp_id)
            if name:
                return name
        except Exception:
            pass

    return None


def _name_matches(wanted: str, candidate: Optional[str]) -> bool:
    """Case-insensitive partial name match.

    ``wanted`` is the name from the ``GameAction`` (e.g. "Shock").
    ``candidate`` is the resolved card name (e.g. "Shock" or "Lightning Bolt // Shock").
    """
    if not wanted or not candidate:
        return False
    w = wanted.strip().lower()
    c = candidate.strip().lower()
    # Exact match
    if w == c:
        return True
    # Partial / substring match (handles split cards, etc.)
    if w in c or c in w:
        return True
    return False


def _resolve_instance_name(
    instance_id: int,
    game_objects: dict[int, dict],
    scryfall_lookup: Optional[Callable[[int], Optional[str]]],
) -> Optional[str]:
    """Get card name for a specific ``instance_id``."""
    obj = game_objects.get(instance_id)
    if obj:
        name = obj.get("name") or obj.get("card_name")
        if name:
            return name
        grp_id = obj.get("grp_id") or obj.get("grpId", 0)
        if grp_id:
            return _resolve_card_name(grp_id, game_objects, scryfall_lookup)
    return None


# ---------------------------------------------------------------------------
# Main matcher
# ---------------------------------------------------------------------------

def match_action_to_gre(
    action: "GameAction",  # noqa: F821  — forward ref avoids circular import
    raw_actions: list[dict],
    game_objects: dict[int, dict],
    scryfall_lookup: Optional[Callable[[int], Optional[str]]] = None,
) -> Optional[GREActionRef]:
    """Match a high-level ``GameAction`` to the best raw GRE action.

    Args:
        action: Parsed ``GameAction`` from the action planner.
        raw_actions: ``legal_actions_raw`` list of raw GRE action dicts.
        game_objects: Mapping of ``instance_id`` -> game object dict.
        scryfall_lookup: Optional callable ``(grp_id) -> card_name``.

    Returns:
        A ``GREActionRef`` if a match is found, otherwise ``None``.
    """
    from arenamcp.action_planner import ActionType  # local import to avoid circular

    if not raw_actions:
        logger.debug("match_action_to_gre: no raw_actions available")
        return None

    atype = action.action_type

    # --- PASS / RESOLVE ------------------------------------------------
    if atype in (ActionType.PASS_PRIORITY, ActionType.RESOLVE, ActionType.CLICK_BUTTON):
        for raw in raw_actions:
            if raw.get("actionType") == "ActionType_Pass":
                ref = GREActionRef.from_raw(raw)
                logger.debug(f"Matched {atype.value} -> ActionType_Pass")
                return ref
        logger.debug(f"No ActionType_Pass found for {atype.value}")
        return None

    # --- PLAY LAND -----------------------------------------------------
    if atype == ActionType.PLAY_LAND:
        for raw in raw_actions:
            if raw.get("actionType") != "ActionType_Play":
                continue
            grp_id = raw.get("grpId", 0)
            card_name = _resolve_card_name(grp_id, game_objects, scryfall_lookup)
            if action.card_name and _name_matches(action.card_name, card_name):
                ref = GREActionRef.from_raw(raw)
                logger.info(f"Matched PLAY_LAND '{action.card_name}' -> grpId={grp_id} '{card_name}'")
                return ref
        # Fallback: if only one Play action, use it
        plays = [r for r in raw_actions if r.get("actionType") == "ActionType_Play"]
        if len(plays) == 1:
            ref = GREActionRef.from_raw(plays[0])
            logger.info(f"Matched PLAY_LAND '{action.card_name}' -> sole ActionType_Play (grpId={plays[0].get('grpId', 0)})")
            return ref
        logger.warning(f"Could not match PLAY_LAND '{action.card_name}' among {len(plays)} Play actions")
        return None

    # --- CAST SPELL ----------------------------------------------------
    if atype == ActionType.CAST_SPELL:
        for raw in raw_actions:
            if raw.get("actionType") != "ActionType_Cast":
                continue
            grp_id = raw.get("grpId", 0)
            card_name = _resolve_card_name(grp_id, game_objects, scryfall_lookup)
            if action.card_name and _name_matches(action.card_name, card_name):
                ref = GREActionRef.from_raw(raw)
                logger.info(f"Matched CAST_SPELL '{action.card_name}' -> grpId={grp_id} '{card_name}'")
                return ref
        # Fallback: if only one Cast action, use it
        casts = [r for r in raw_actions if r.get("actionType") == "ActionType_Cast"]
        if len(casts) == 1:
            ref = GREActionRef.from_raw(casts[0])
            logger.info(f"Matched CAST_SPELL '{action.card_name}' -> sole ActionType_Cast (grpId={casts[0].get('grpId', 0)})")
            return ref
        logger.warning(f"Could not match CAST_SPELL '{action.card_name}' among {len(casts)} Cast actions")
        return None

    # --- ACTIVATE ABILITY ----------------------------------------------
    if atype == ActionType.ACTIVATE_ABILITY:
        for raw in raw_actions:
            if raw.get("actionType") != "ActionType_Activate":
                continue
            # Try to match by source card name
            source_id = raw.get("sourceId", 0) or raw.get("instanceId", 0)
            source_name = _resolve_instance_name(source_id, game_objects, scryfall_lookup)
            if action.card_name and _name_matches(action.card_name, source_name):
                ref = GREActionRef.from_raw(raw)
                logger.info(f"Matched ACTIVATE_ABILITY '{action.card_name}' -> sourceId={source_id} '{source_name}'")
                return ref
        # Fallback: sole activate
        activates = [r for r in raw_actions if r.get("actionType") == "ActionType_Activate"]
        if len(activates) == 1:
            ref = GREActionRef.from_raw(activates[0])
            logger.info(f"Matched ACTIVATE_ABILITY '{action.card_name}' -> sole ActionType_Activate")
            return ref
        logger.warning(f"Could not match ACTIVATE_ABILITY '{action.card_name}' among {len(activates)} Activate actions")
        return None

    # --- DECLARE ATTACKERS ---------------------------------------------
    if atype == ActionType.DECLARE_ATTACKERS:
        for raw in raw_actions:
            if raw.get("actionType") in ("ActionType_AttackWithGroup", "ActionType_Attack"):
                ref = GREActionRef.from_raw(raw)
                logger.info(f"Matched DECLARE_ATTACKERS -> {raw.get('actionType')}")
                return ref
        logger.warning("Could not match DECLARE_ATTACKERS to any attack group action")
        return None

    # --- DECLARE BLOCKERS ----------------------------------------------
    if atype == ActionType.DECLARE_BLOCKERS:
        for raw in raw_actions:
            if raw.get("actionType") in ("ActionType_BlockWithGroup", "ActionType_Block"):
                ref = GREActionRef.from_raw(raw)
                logger.info(f"Matched DECLARE_BLOCKERS -> {raw.get('actionType')}")
                return ref
        logger.warning("Could not match DECLARE_BLOCKERS to any block group action")
        return None

    # --- GENERIC FALLBACK: match by ACTION_TYPE_MAP --------------------
    gre_type = ACTION_TYPE_MAP.get(atype)
    if gre_type:
        candidates = [r for r in raw_actions if r.get("actionType") == gre_type]
        if len(candidates) == 1:
            ref = GREActionRef.from_raw(candidates[0])
            logger.info(f"Matched {atype.value} -> {gre_type} (sole candidate)")
            return ref
        if candidates:
            # For actions with a card_name, try to narrow by name
            if action.card_name:
                for raw in candidates:
                    grp_id = raw.get("grpId", 0)
                    instance_id = raw.get("instanceId", 0) or raw.get("sourceId", 0)
                    card_name = _resolve_card_name(grp_id, game_objects, scryfall_lookup)
                    if not card_name:
                        card_name = _resolve_instance_name(instance_id, game_objects, scryfall_lookup)
                    if _name_matches(action.card_name, card_name):
                        ref = GREActionRef.from_raw(raw)
                        logger.info(f"Matched {atype.value} -> {gre_type} by name '{action.card_name}'")
                        return ref
            # If no name match found, return the first candidate
            ref = GREActionRef.from_raw(candidates[0])
            logger.info(f"Matched {atype.value} -> {gre_type} (first of {len(candidates)} candidates)")
            return ref

    logger.debug(f"No GRE match for action type {atype.value} (gre_type={gre_type})")
    return None
