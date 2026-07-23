"""Game-state model types for arenamcp.

Enums and dataclasses extracted from arenamcp.gamestate (2026-07-23) —
pure data types with no dependency on the GameState state machine.
Re-exported from arenamcp.gamestate for backwards compatibility.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ZoneType(Enum):
    """Zone types in MTGA."""

    BATTLEFIELD = "ZoneType_Battlefield"
    HAND = "ZoneType_Hand"
    GRAVEYARD = "ZoneType_Graveyard"
    EXILE = "ZoneType_Exile"
    LIBRARY = "ZoneType_Library"
    STACK = "ZoneType_Stack"
    LIMBO = "ZoneType_Limbo"
    COMMAND = "ZoneType_Command"
    PENDING = "ZoneType_Pending"
    REVEALED = "ZoneType_Revealed"
    UNKNOWN = "Unknown"


class GameObjectKind(Enum):
    """Game object types from GRE protobuf (GameObjectType enum)."""

    NONE = "GameObjectType_None"
    CARD = "GameObjectType_Card"
    TOKEN = "GameObjectType_Token"
    ABILITY = "GameObjectType_Ability"
    EMBLEM = "GameObjectType_Emblem"
    SPLIT_CARD = "GameObjectType_SplitCard"
    SPLIT_LEFT = "GameObjectType_SplitLeft"
    SPLIT_RIGHT = "GameObjectType_SplitRight"
    REVEALED_CARD = "GameObjectType_RevealedCard"
    TRIGGER_HOLDER = "GameObjectType_TriggerHolder"
    ADVENTURE = "GameObjectType_Adventure"
    MDFC_BACK = "GameObjectType_MDFCBack"
    DISTURB_BACK = "GameObjectType_DisturbBack"
    BOON = "GameObjectType_Boon"
    PROTOTYPE_FACET = "GameObjectType_PrototypeFacet"
    ROOM_LEFT = "GameObjectType_RoomLeft"
    ROOM_RIGHT = "GameObjectType_RoomRight"
    OMEN = "GameObjectType_Omen"
    UNKNOWN = "Unknown"


@dataclass
class GameObject:
    """A game object (card, token, ability, etc.) in the game."""

    instance_id: int
    grp_id: int
    zone_id: int
    owner_seat_id: int
    controller_seat_id: int | None = None
    visibility: str | None = None
    card_types: list[str] = field(default_factory=list)
    subtypes: list[str] = field(default_factory=list)
    power: int | None = None
    toughness: int | None = None
    is_tapped: bool = False
    # For abilities: instance_id of the source permanent
    parent_instance_id: int | None = None
    # For summoning sickness tracking
    turn_entered_battlefield: int = -1
    # True only if this object entered the battlefield via the "play land"
    # action (ZoneTransferReason.PlayLand). Lands put onto the battlefield by
    # spells/abilities (Rampant Growth → "Put", Goldspan → "Resolve" cast,
    # etc.) leave this False so they aren't counted as the turn's land drop.
    entered_via_play_land: bool = False
    # Combat status
    is_attacking: bool = False
    is_blocking: bool = False
    # GRE object kind (Card, Token, Ability, Emblem, MDFC, etc.)
    object_kind: GameObjectKind = GameObjectKind.UNKNOWN
    # Counters on this object: {"counter_type": count}
    counters: dict[str, int] = field(default_factory=dict)
    # ── Phase 1 turbo-charge fields (from GRE annotations) ──
    # Modified stats (actual values after continuous effects)
    modified_power: int | None = None
    modified_toughness: int | None = None
    modified_cost: str | None = None
    modified_colors: list[str] | None = None
    modified_types: list[str] | None = None
    modified_name: str | None = None
    # Granted/lost abilities
    granted_abilities: list[str] = field(default_factory=list)
    removed_abilities: list[str] = field(default_factory=list)
    # Turn-specific state
    damaged_this_turn: bool = False
    crewed_this_turn: bool = False
    saddled_this_turn: bool = False
    # Phasing
    is_phased_out: bool = False
    # Class/Saga level
    class_level: int | None = None
    # Copy source
    copied_from_grp_id: int | None = None
    # Targeting info: list of instance_ids this object is targeting
    targeting: list[int] = field(default_factory=list)
    # Color production (mana abilities)
    color_production: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to simple dict for snapshot serialization."""
        result = {
            "instance_id": self.instance_id,
            "grp_id": self.grp_id,
            "zone_id": self.zone_id,
            "owner_seat_id": self.owner_seat_id,
            "controller_seat_id": self.controller_seat_id,
            "visibility": self.visibility,
            "card_types": self.card_types,
            "subtypes": self.subtypes,
            "power": self.power,
            "toughness": self.toughness,
            "is_tapped": self.is_tapped,
            "turn_entered_battlefield": self.turn_entered_battlefield,
            "is_attacking": self.is_attacking,
            "is_blocking": self.is_blocking,
            "object_kind": self.object_kind.name,
        }
        if self.counters:
            result["counters"] = self.counters
        if self.parent_instance_id is not None:
            result["parent_instance_id"] = self.parent_instance_id
        # Phase 1 turbo-charge fields — only include when set to keep payloads lean
        if self.modified_power is not None:
            result["modified_power"] = self.modified_power
        if self.modified_toughness is not None:
            result["modified_toughness"] = self.modified_toughness
        if self.modified_cost is not None:
            result["modified_cost"] = self.modified_cost
        if self.modified_colors:
            result["modified_colors"] = self.modified_colors
        if self.modified_types:
            result["modified_types"] = self.modified_types
        if self.modified_name:
            result["modified_name"] = self.modified_name
        if self.granted_abilities:
            result["granted_abilities"] = self.granted_abilities
        if self.removed_abilities:
            result["removed_abilities"] = self.removed_abilities
        if self.damaged_this_turn:
            result["damaged_this_turn"] = True
        if self.crewed_this_turn:
            result["crewed_this_turn"] = True
        if self.saddled_this_turn:
            result["saddled_this_turn"] = True
        if self.is_phased_out:
            result["is_phased_out"] = True
        if self.entered_via_play_land:
            result["entered_via_play_land"] = True
        if self.class_level is not None:
            result["class_level"] = self.class_level
        if self.copied_from_grp_id is not None:
            result["copied_from_grp_id"] = self.copied_from_grp_id
        if self.targeting:
            result["targeting"] = self.targeting
        if self.color_production:
            result["color_production"] = self.color_production
        return result


@dataclass
class Zone:
    """A game zone (battlefield, hand, etc.)."""

    zone_id: int
    zone_type: ZoneType
    owner_seat_id: int | None = None
    object_instance_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to simple dict for snapshot."""
        return {
            "zone_id": self.zone_id,
            "zone_type": self.zone_type.name,  # Enum to string
            "owner_seat_id": self.owner_seat_id,
            "object_instance_ids": self.object_instance_ids,
        }


@dataclass
class Player:
    """A player in the game."""

    seat_id: int
    life_total: int = 20
    lands_played: int = 0
    mana_pool: dict[str, int] = field(default_factory=dict)
    team_id: int | None = None
    status: str = ""

    def to_dict(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "life_total": self.life_total,
            "lands_played": self.lands_played,
            "mana_pool": self.mana_pool,
            "team_id": self.team_id,
            "status": self.status,
        }


@dataclass
class TurnInfo:
    """Current turn information."""

    turn_number: int = 0
    active_player: int = 0
    priority_player: int = 0
    phase: str = ""
    step: str = ""

    def to_dict(self) -> dict:
        return {
            "turn_number": self.turn_number,
            "active_player": self.active_player,
            "priority_player": self.priority_player,
            "phase": self.phase,
            "step": self.step,
        }


def _parse_zone_type(value: Any) -> ZoneType:
    """Best-effort ZoneType parser for persisted checkpoint data."""
    if isinstance(value, ZoneType):
        return value
    if isinstance(value, str):
        member = ZoneType.__members__.get(value)
        if member is not None:
            return member
        try:
            return ZoneType(value)
        except ValueError:
            pass
    return ZoneType.UNKNOWN


def _parse_object_kind(value: Any) -> GameObjectKind:
    """Best-effort GameObjectKind parser for persisted checkpoint data."""
    if isinstance(value, GameObjectKind):
        return value
    if isinstance(value, str):
        member = GameObjectKind.__members__.get(value)
        if member is not None:
            return member
        try:
            return GameObjectKind(value)
        except ValueError:
            pass
    return GameObjectKind.UNKNOWN
