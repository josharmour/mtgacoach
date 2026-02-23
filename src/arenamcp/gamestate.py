"""MTGA game state tracking from parsed log events.

This module provides the GameState class that maintains a complete
snapshot of the current game state from parsed MTGA log events.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


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
    controller_seat_id: Optional[int] = None
    visibility: Optional[str] = None
    card_types: list[str] = field(default_factory=list)
    subtypes: list[str] = field(default_factory=list)
    power: Optional[int] = None
    toughness: Optional[int] = None
    is_tapped: bool = False
    # For abilities: instance_id of the source permanent
    parent_instance_id: Optional[int] = None
    # For summoning sickness tracking
    turn_entered_battlefield: int = -1
    # Combat status
    is_attacking: bool = False
    is_blocking: bool = False
    # GRE object kind (Card, Token, Ability, Emblem, MDFC, etc.)
    object_kind: GameObjectKind = GameObjectKind.UNKNOWN
    # Counters on this object: {"counter_type": count}
    counters: dict[str, int] = field(default_factory=dict)
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
        return result


@dataclass
class Zone:
    """A game zone (battlefield, hand, etc.)."""
    zone_id: int
    zone_type: ZoneType
    owner_seat_id: Optional[int] = None
    object_instance_ids: list[int] = field(default_factory=list)
    def to_dict(self) -> dict:
        """Convert to simple dict for snapshot."""
        return {
            "zone_id": self.zone_id,
            "zone_type": self.zone_type.name, # Enum to string
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
    def to_dict(self) -> dict:
        return {
            "seat_id": self.seat_id,
            "life_total": self.life_total,
            "lands_played": self.lands_played,
            "mana_pool": self.mana_pool,
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


class GameState:
    """Maintains complete game state from parsed MTGA log events.

    This class tracks zones, game objects, players, and turn information
    as they are updated via GameStateMessage events from the log parser.
    """

    def __init__(self) -> None:
        """Initialize empty game state."""
        # Core state dictionaries
        self.zones: dict[int, Zone] = {}
        self.game_objects: dict[int, GameObject] = {}
        self.players: dict[int, Player] = {}
        self.turn_info: TurnInfo = TurnInfo()

        # Local player tracking
        self.local_seat_id: Optional[int] = None
        # Source of the seat ID: 0=None, 1=Inferred, 2=System (MatchCreated), 3=User (F8)
        self._seat_source: int = 0
        
        # Opponent card history tracking
        self.played_cards: dict[int, list[int]] = {}  # seat_id -> list of grp_ids
        self._seen_instances: set[int] = set()  # Track instances to avoid double-counting

        # Combat step tracking - captures steps that happen between polls
        # This ensures fast combat phases aren't missed
        self._pending_combat_steps: list[str] = []

        # Untap prevention tracking: instance_ids that MTGA explicitly kept tapped
        # during the untap step (e.g., creatures with Blossombind "can't become untapped").
        # These are skipped during blanket untap on subsequent turns.
        self._untap_prevention: set[int] = set()
        self._in_untap_step: bool = False  # True during first message of a turn change

        # Decision tracking (for Mulligan advice, etc.)
        self.pending_decision: Optional[str] = None  # e.g. "Mulligan"
        self.decision_seat_id: Optional[int] = None
        # PHASE 1: Enhanced decision context
        self.decision_context: Optional[dict] = None  # Rich context: source_card, options, etc.
        self.decision_timestamp: float = 0  # Track when decision was set
        self.legal_actions: list[str] = [] # List of exact legal actions from GRE

        # Match tracking (to avoid stale state across matches)
        self.match_id: Optional[str] = None

        # Deck list captured from ConnectResp
        self.deck_cards: list[int] = []

        # --- Annotation-derived event tracking ---
        # Recent game events from GRE annotations (ring buffer, max 50)
        self.recent_events: list[dict] = []
        self._max_recent_events: int = 50
        # Persists across reset() so the coaching loop can read it after match ends
        self.last_game_result: Optional[str] = None  # "win", "loss", or None
        # Cumulative damage tracking per player this game: {seat_id: total_damage_taken}
        self.damage_taken: dict[int, int] = {}
        # Cards revealed to us by opponent (from CardRevealed/InstanceRevealedToOpponent)
        self.revealed_cards: dict[int, set[int]] = {}  # seat_id -> set of grp_ids

    def reset(self) -> None:
        """Reset the game state to essentially empty (for new match)."""
        logger.info("Resetting GameState for new match")
        self.zones.clear()
        self.game_objects.clear()
        self.players.clear()
        self.turn_info = TurnInfo()
        
        # Reset local player seat logic (but keep User/System if we want persistence?)
        # Actually for a NEW match, System seat ID will change, so we should clear it.
        # But User override (F8) might be intended to persist? 
        # Usually seat ID changes every match, so User override might be wrong for next match.
        # Better to reset seat source to 0 or keep it if it's 3?
        # Let's trust reset_local_player(force=False) logic which keeps User/System.
        # But wait, if System ID is from OLD match, it's invalid.
        # So for a full match reset, we probably want to clear System source too.
        # Always reset seat ID for a new match. 
        # Even if user manually set it (Source 3) in previous match, 
        # it is likely invalid for the new match.
        self.local_seat_id = None
        self._seat_source = 0
            
        self.played_cards.clear()
        self._seen_instances.clear()
        self._pending_combat_steps.clear()
        self._untap_prevention.clear()
        self._in_untap_step = False
        self.pending_decision = None
        self.decision_seat_id = None
        self.decision_context = None
        self.decision_timestamp = 0
        self.legal_actions = []
        self.deck_cards = []
        self.recent_events = []
        self.damage_taken = {}
        self.revealed_cards = {}
        # NOTE: last_game_result is intentionally NOT cleared here.
        # The coaching loop reads it after reset() to detect the match outcome.
        # It is cleared by the coaching loop once consumed.

    # Backward compatibility for _seat_manually_set
    @property
    def _seat_manually_set(self) -> bool:
        return self._seat_source == 3

    @property
    def opponent_seat_id(self) -> Optional[int]:
        """Get the opponent's seat ID (the seat that isn't local player)."""
        if self.local_seat_id is None:
            return None
        # In 2-player games, opponent is the other seat
        for seat_id in self.players:
            if seat_id != self.local_seat_id:
                return seat_id
        return None

    def get_objects_in_zone(
        self,
        zone_type: ZoneType,
        owner: Optional[int] = None
    ) -> list[GameObject]:
        """Get all game objects in zones of a specific type.

        Args:
            zone_type: The type of zone to query.
            owner: Optional seat ID to filter by owner.

        Returns:
            List of GameObjects in matching zones.
        """
        result = []
        for zone in self.zones.values():
            if zone.zone_type != zone_type:
                continue
            if owner is not None and zone.owner_seat_id != owner:
                continue
            for instance_id in zone.object_instance_ids:
                if instance_id in self.game_objects:
                    obj = self.game_objects[instance_id]
                    # Cross-check: object's zone_id must match this zone.
                    # Arena diff updates may update the object's zone_id before
                    # the zone's member list, causing stale entries (e.g. resolved
                    # spells appearing on both stack and battlefield).
                    if obj.zone_id == zone.zone_id:
                        result.append(obj)
        return result

    def get_player_objects(self, seat_id: int) -> list[GameObject]:
        """Get all game objects owned by a specific player.

        Args:
            seat_id: The player's seat ID.

        Returns:
            List of GameObjects owned by the player.
        """
        return [
            obj for obj in self.game_objects.values()
            if obj.owner_seat_id == seat_id
        ]

    def _clear_stale_stack(self) -> None:
        """Clear the stack zone on turn boundaries.

        In Magic, the stack is always empty when a new turn begins.
        MTGA often doesn't send zone updates for resolved triggered/activated
        abilities, leaving ghost entries that cause the formatter to show
        Legal: NONE (can't cast at sorcery speed with non-empty stack).
        """
        for zone in self.zones.values():
            if zone.zone_type == ZoneType.STACK and zone.object_instance_ids:
                count = len(zone.object_instance_ids)
                zone.object_instance_ids = []
                logger.info(f"Cleared {count} stale stack entries on turn change")

    def _cleanup_stale_objects(self) -> None:
        """Remove game objects that are no longer in any zone.
        
        This prevents memory accumulation during long games where many
        tokens are created and destroyed.
        """
        # Collect all instance IDs currently in zones
        live_ids: set[int] = set()
        for zone in self.zones.values():
            live_ids.update(zone.object_instance_ids)
        
        # Find and remove stale objects
        stale_ids = [oid for oid in self.game_objects if oid not in live_ids]
        for oid in stale_ids:
            del self.game_objects[oid]
        
        if stale_ids:
            logger.debug(f"Cleaned up {len(stale_ids)} stale game objects")

    @property
    def battlefield(self) -> list[GameObject]:
        """Get all objects on the battlefield."""
        return self.get_objects_in_zone(ZoneType.BATTLEFIELD)

    @property
    def hand(self) -> list[GameObject]:
        """Get objects in all hands (filtered by local player if set)."""
        if self.local_seat_id is not None:
            # First try zone-based filter
            result = self.get_objects_in_zone(ZoneType.HAND, self.local_seat_id)
            if result:
                return result
            # Fallback: get all hand cards where the card's owner matches local player
            # (handles case where zone.owner_seat_id is None but card has owner set)
            all_hand = self.get_objects_in_zone(ZoneType.HAND)
            return [obj for obj in all_hand if obj.owner_seat_id == self.local_seat_id]
        return self.get_objects_in_zone(ZoneType.HAND)

    @property
    def graveyard(self) -> list[GameObject]:
        """Get all objects in graveyards."""
        return self.get_objects_in_zone(ZoneType.GRAVEYARD)

    @property
    def stack(self) -> list[GameObject]:
        """Get all objects on the stack."""
        return self.get_objects_in_zone(ZoneType.STACK)

    @property
    def command(self) -> list[GameObject]:
        """Get all objects in the command zone."""
        return self.get_objects_in_zone(ZoneType.COMMAND)

    def get_opponent_played_cards(self) -> list[int]:
        """Get list of grp_ids of cards opponent has revealed.

        Returns:
            List of grp_ids (arena card IDs) that opponent has played.
        """
        if self.opponent_seat_id is None:
            return []
        return self.played_cards.get(self.opponent_seat_id, [])

    def get_pending_combat_steps(self) -> list[dict]:
        """Get combat steps that occurred since last check.

        Returns list of dicts with 'step', 'active_player', 'turn' keys.
        This allows catching fast combat phases that happen between polls.
        """
        return self._pending_combat_steps.copy()

    def get_snapshot(self) -> dict:
        """Get a complete, serializable snapshot of the current game state.
        
        This 'flattens' the state into a single JSON-ready dictionary, 
        serving as the 'State Anchor' for the middleware.
        """
        # Resolve owners
        local_player = self.players.get(self.local_seat_id) if self.local_seat_id else None
        opponent_seat = self.opponent_seat_id
        
        # Enrich objects with card names for TUI display
        def enrich_obj(obj):
            """Add card name to object dict for TUI."""
            data = obj.to_dict()
            # Lazy import to avoid circular dependency
            from arenamcp import server
            card_info = server.get_card_info(obj.grp_id)
            data["name"] = card_info.get("name", f"Unknown ({obj.grp_id})")
            data["type_line"] = card_info.get("type_line", "")
            return data
        
        # Serialize zones of interest
        # We group by meaningful logical zones rather than just raw zone IDs
        
        # Manually serialize players to include is_local flag for TUI
        players_list = []
        for p in self.players.values():
            p_dict = p.to_dict()
            p_dict["is_local"] = (p.seat_id == self.local_seat_id)
            players_list.append(p_dict)
            
        # Serialize revealed cards (sets -> lists for JSON)
        revealed = {}
        for seat_id, grp_ids in self.revealed_cards.items():
            revealed[seat_id] = list(grp_ids)

        snapshot = {
            "match_id": self.match_id,
            "local_seat_id": self.local_seat_id,
            "opponent_seat_id": opponent_seat,
            "turn_info": self.turn_info.to_dict(),
            "players": players_list,
            "zones": {
                "battlefield": [enrich_obj(obj) for obj in self.battlefield],
                "my_hand": [enrich_obj(obj) for obj in self.hand] if self.local_seat_id else [],
                "opponent_hand_count": len(self.get_objects_in_zone(ZoneType.HAND, opponent_seat)) if opponent_seat else 0,
                "stack": [enrich_obj(obj) for obj in self.stack],
                "graveyard": [enrich_obj(obj) for obj in self.graveyard],
                "exile": [enrich_obj(obj) for obj in self.get_objects_in_zone(ZoneType.EXILE)],
                "command": [enrich_obj(obj) for obj in self.command],
            },
            "pending_decision": self.pending_decision,
            "decision_seat_id": self.decision_seat_id,
            "decision_context": self.decision_context,
            "legal_actions": self.legal_actions,
            # Annotation-derived data
            "recent_events": self.recent_events[-10:],  # Last 10 events for display
            "damage_taken": self.damage_taken,
            "revealed_cards": revealed,
            "last_game_result": self.last_game_result,
        }
        return snapshot

    def clear_pending_combat_steps(self) -> None:
        """Clear the pending combat steps after processing."""
        self._pending_combat_steps.clear()
    
    def get_seat_source_name(self) -> str:
        """Get human-readable name of the seat ID source."""
        if self._seat_source == 0: return "None"
        if self._seat_source == 1: return "Inferred"
        if self._seat_source == 2: return "System"
        if self._seat_source == 3: return "User"
        return "Unknown"

    def set_local_seat_id(self, seat_id: int, source: int = 2) -> None:
        """Explicitly set the local player's seat ID if source priority allows.
        
        Source levels:
        1: Inferred (from hand visibility)
        2: System (from MatchCreated events)
        3: User (Manual override via F8)

        Args:
            seat_id: The local player's seat ID.
            source: Priority level (default 2=System).
        """
        # Only overwrite if new source is >= current source
        if source >= self._seat_source:
            self.local_seat_id = seat_id
            self._seat_source = source
            source_name = self.get_seat_source_name()
            logger.info(f"Set local_seat_id to {seat_id} (Source: {source_name})")
        else:
            logger.info(f"Ignored seat update to {seat_id} (Source {source} < Current {self._seat_source})")

    def reset_local_player(self, force: bool = False) -> None:
        """Reset local_seat_id logic.
        
        Args:
            force: If True, reset EVERYTHING (used for full restart).
                   If False, only reset INFERRED (1) sources. 
                   System (2) and User (3) are preserved across game resets (e.g. BO3).
        """
        if force or self._seat_source <= 1:
            self.local_seat_id = None
            self._seat_source = 0
            logger.info("Reset local_seat_id (cleared)")
        else:
            logger.info(f"Preserving local_seat_id={self.local_seat_id} (Source: {self.get_seat_source_name()})")

    def ensure_local_seat_id(self) -> None:
        """Ensure local_seat_id is set by inferring from existing data.

        Called by server before returning game state to ensure is_local
        is correctly determined. Uses hand zone with cards that have
        known grp_ids (you can see your own cards but not opponent's).
        """
        if self.local_seat_id is not None:
            return  # Already set

        # Try to infer from hand zones that have cards with VISIBLE grp_ids
        # Opponent's hand zone may have instance_ids but grp_id=0 (hidden)
        for zone in self.zones.values():
            if zone.zone_type != ZoneType.HAND:
                continue
            if zone.owner_seat_id is None:
                continue
            if not zone.object_instance_ids:
                continue

            # Check if ANY card in this hand has a known grp_id (not 0)
            has_visible_card = False
            for instance_id in zone.object_instance_ids:
                obj = self.game_objects.get(instance_id)
                if obj and obj.grp_id != 0:
                    has_visible_card = True
                    break

            if has_visible_card:
                # Use source=1 (Inferred)
                self.set_local_seat_id(zone.owner_seat_id, source=1)
                return

        # Fallback: log that we couldn't determine local player
        logger.debug("Could not infer local_seat_id - no hand zone with visible grp_ids found")

    def update_from_message(self, message: dict) -> None:
        """Update game state from a GameStateMessage payload.

        Handles both full and incremental (diff) updates from the game.
        All updates are treated as upserts (create or update).

        Args:
            message: The GameStateMessage dict from parsed log event.
        """
        # Extract type (full vs diff) - not currently used but logged
        msg_type = message.get("type", "Unknown")
        logger.debug(f"Processing GameStateMessage type: {msg_type}")

        # Update turn info FIRST so that zone updates use the correct turn number
        turn_info = message.get("turnInfo")
        if turn_info:
            self._update_turn_info(turn_info)

        # Update game objects
        game_objects = message.get("gameObjects", [])
        for obj_data in game_objects:
            self._update_game_object(obj_data)

        # Update zones
        zones = message.get("zones", [])
        for zone_data in zones:
            self._update_zone(zone_data)

        # Update players
        players = message.get("players", [])
        for player_data in players:
            self._update_player(player_data)
        
        # Ensure lands_played is correct even when Arena omits player data
        self._infer_lands_played()

        # Process annotations (damage, counters, zone transfers, reveals, etc.)
        annotations = message.get("annotations", [])
        if annotations:
            self._process_annotations(annotations)

        # Clear untap step flag after processing all objects in this message
        self._in_untap_step = False

        # MEMORY OPTIMIZATION: Periodically clean up stale objects
        # Objects can accumulate when tokens die or cards are exiled from exile
        if len(self.game_objects) > 200:  # Only cleanup when dict gets large
            self._cleanup_stale_objects()

    def _update_game_object(self, obj_data: dict) -> None:
        """Update or create a game object from message data.

        Args:
            obj_data: Game object dict from GameStateMessage.
        """
        instance_id = obj_data.get("instanceId")
        if instance_id is None:
            return

        existing_obj = self.game_objects.get(instance_id)

        # Helper to get value from update or fallback to existing
        def get_val(key, default):
            if existing_obj:
                # If existing, prefer update if key exists, else existing attr
                # We need to map key string to attr name sometimes
                return obj_data.get(key, default)
                # Wait, this logic is tricky if I want "if key in obj_data".
            return obj_data.get(key, default)

        # Better merge logic:
        # 1. Start with defaults or existing values
        if existing_obj:
            grp_id = existing_obj.grp_id
            zone_id = existing_obj.zone_id
            owner_seat_id = existing_obj.owner_seat_id
            controller_seat_id = existing_obj.controller_seat_id
            visibility = existing_obj.visibility
            power = existing_obj.power
            toughness = existing_obj.toughness
            is_tapped = existing_obj.is_tapped
            card_types = existing_obj.card_types
            subtypes = existing_obj.subtypes
            parent_instance_id = existing_obj.parent_instance_id
            turn_entered_battlefield = existing_obj.turn_entered_battlefield
            is_attacking = existing_obj.is_attacking
            is_blocking = existing_obj.is_blocking
            object_kind = existing_obj.object_kind
            counters = existing_obj.counters.copy()
        else:
            grp_id = 0
            zone_id = 0
            owner_seat_id = 0
            controller_seat_id = None
            visibility = None
            power = None
            toughness = None
            is_tapped = False
            card_types = []
            subtypes = []
            parent_instance_id = None
            turn_entered_battlefield = -1
            is_attacking = False
            is_blocking = False
            object_kind = GameObjectKind.UNKNOWN
            counters = {}

        # 2. Overwrite with present data
        if "grpId" in obj_data: grp_id = obj_data["grpId"]
        if "zoneId" in obj_data: zone_id = obj_data["zoneId"]
        if "ownerSeatId" in obj_data: owner_seat_id = obj_data["ownerSeatId"]
        if "controllerSeatId" in obj_data: controller_seat_id = obj_data["controllerSeatId"]
        if "visibility" in obj_data: visibility = obj_data["visibility"]
        
        if "power" in obj_data:
             p = obj_data["power"]
             power = p.get("value") if isinstance(p, dict) else p
        
        if "toughness" in obj_data:
             t = obj_data["toughness"]
             toughness = t.get("value") if isinstance(t, dict) else t

        if "isTapped" in obj_data:
            is_tapped = obj_data["isTapped"]
            # Track untap prevention: if MTGA says a permanent is still tapped
            # during the untap step, it has an untap restriction (e.g. Blossombind).
            # Skip blanket-untapping it on future turns.
            if self._in_untap_step:
                if is_tapped:
                    self._untap_prevention.add(instance_id)
                else:
                    self._untap_prevention.discard(instance_id)
        if "parentId" in obj_data: parent_instance_id = obj_data["parentId"]

        if "isAttacking" in obj_data: is_attacking = bool(obj_data["isAttacking"])
        if "isBlocking" in obj_data: is_blocking = bool(obj_data["isBlocking"])

        if "cardTypes" in obj_data:
            card_types = list(obj_data["cardTypes"])

        if "subtypes" in obj_data:
            subtypes = []
            for st in obj_data["subtypes"]:
                clean_subtype = st.replace("SubType_", "") if isinstance(st, str) else str(st)
                subtypes.append(clean_subtype)

        # Parse GRE object kind (Card, Token, Ability, Emblem, MDFC, etc.)
        if "type" in obj_data:
            kind_str = obj_data["type"]
            try:
                object_kind = GameObjectKind(kind_str)
            except ValueError:
                object_kind = GameObjectKind.UNKNOWN
                logger.debug(f"Unknown GameObjectType: {kind_str}")

        # Parse counters on this object
        if "counters" in obj_data:
            counters = {}
            for counter_data in obj_data["counters"]:
                ctype = counter_data.get("type", counter_data.get("counterType", "unknown"))
                ccount = counter_data.get("count", 1)
                counters[ctype] = ccount

        game_object = GameObject(
            instance_id=instance_id,
            grp_id=grp_id,
            zone_id=zone_id,
            owner_seat_id=owner_seat_id,
            controller_seat_id=controller_seat_id,
            visibility=visibility,
            card_types=card_types,
            subtypes=subtypes,
            power=power,
            toughness=toughness,
            is_tapped=is_tapped,
            parent_instance_id=parent_instance_id,
            turn_entered_battlefield=turn_entered_battlefield,
            is_attacking=is_attacking,
            is_blocking=is_blocking,
            object_kind=object_kind,
            counters=counters,
        )


        self.game_objects[instance_id] = game_object
        logger.debug(f"Updated game object {instance_id} (grpId={grp_id})")

        # Track cards when first seen in non-library zones (reveals card identity)
        self._track_played_card(game_object)

    def _track_played_card(self, game_object: GameObject) -> None:
        """Track a card as played/revealed if first seen in non-library zone.

        This records the grp_id (card identity) when a card instance is
        first observed outside of the library, which indicates the card
        has been revealed to both players.

        Args:
            game_object: The game object to potentially track.
        """
        # Skip if already seen this instance
        if game_object.instance_id in self._seen_instances:
            return

        # Skip if grp_id is 0 (unknown/hidden card)
        if game_object.grp_id == 0:
            return

        # Determine zone type for this object
        zone = self.zones.get(game_object.zone_id)
        if zone is None:
            # Zone not yet known; can't determine if revealed
            return

        # Only track cards in non-library zones (where identity is revealed)
        # Library cards are hidden; once they move elsewhere, they're revealed
        non_library_zones = {
            ZoneType.BATTLEFIELD,
            ZoneType.HAND,
            ZoneType.GRAVEYARD,
            ZoneType.EXILE,
            ZoneType.STACK,
            ZoneType.COMMAND,
            ZoneType.REVEALED,
        }

        if zone.zone_type not in non_library_zones:
            return

        # Mark as seen
        self._seen_instances.add(game_object.instance_id)

        # Add to played cards for this owner
        owner = game_object.owner_seat_id
        if owner not in self.played_cards:
            self.played_cards[owner] = []
        self.played_cards[owner].append(game_object.grp_id)
        logger.debug(f"Tracked played card: owner={owner}, grpId={game_object.grp_id}")

    def _update_zone(self, zone_data: dict) -> None:
        """Update or create a zone from message data.

        Args:
            zone_data: Zone dict from GameStateMessage.
        """
        zone_id = zone_data.get("zoneId")
        if zone_id is None:
            return

        existing_zone = self.zones.get(zone_id)

        # Helper to get value from update or preserve existing
        def get_val(key, default, existing_attr=None):
            if key in zone_data:
                return zone_data[key]
            if existing_zone and existing_attr is not None:
                return getattr(existing_zone, existing_attr)
            return default

        # Zone Type
        if "type" in zone_data:
            zone_type_str = zone_data["type"]
            try:
                zone_type = ZoneType(zone_type_str)
            except ValueError:
                zone_type = ZoneType.UNKNOWN
                logger.debug(f"Unknown zone type: {zone_type_str}")
        elif existing_zone:
            zone_type = existing_zone.zone_type
        else:
            zone_type = ZoneType.UNKNOWN

        # Owner Seat ID
        # Note: ownerSeatId can be None in JSON or missing.
        # If explicit null in JSON -> we set to None.
        # If missing -> we preserve existing.
        if "ownerSeatId" in zone_data:
            owner_seat_id = zone_data["ownerSeatId"]
        elif existing_zone:
            owner_seat_id = existing_zone.owner_seat_id
        else:
            owner_seat_id = None

        # Object Instance IDs
        # Critical: If missing, must preserve existing list to avoid wiping zone
        if "objectInstanceIds" in zone_data:
            object_instance_ids = zone_data["objectInstanceIds"]
        elif existing_zone:
            object_instance_ids = existing_zone.object_instance_ids
        else:
            object_instance_ids = []

        zone = Zone(
            zone_id=zone_id,
            zone_type=zone_type,
            owner_seat_id=owner_seat_id,
            object_instance_ids=object_instance_ids,
        )

        self.zones[zone_id] = zone
        logger.debug(f"Updated zone {zone_id} ({zone_type.name})")

        # Track battlefield entry for summoning sickness
        if zone_type == ZoneType.BATTLEFIELD:
            current_turn = self.turn_info.turn_number
            for instance_id in object_instance_ids:
                obj = self.game_objects.get(instance_id)
                if obj and obj.turn_entered_battlefield == -1:
                    obj.turn_entered_battlefield = current_turn
                    logger.debug(f"Object {instance_id} entered battlefield on turn {current_turn}")

        # Infer local player from hand visibility
        # Only infer if hand zone has cards with known grp_ids (not 0)
        # Opponent's hand has instance_ids but grp_id=0 (hidden cards)
        # NEVER override a manually set seat
        if zone_type == ZoneType.HAND and owner_seat_id is not None and object_instance_ids:
            if self.local_seat_id is None and not self._seat_manually_set:
                # Check if any card in this zone has a visible grp_id
                has_visible_card = False
                for instance_id in object_instance_ids:
                    obj = self.game_objects.get(instance_id)
                    if obj and obj.grp_id != 0:
                        has_visible_card = True
                        break

                if has_visible_card:
                    self.local_seat_id = owner_seat_id
                    logger.info(f"Inferred local player as seat {owner_seat_id} from hand zone with visible grp_ids")

    def _update_player(self, player_data: dict) -> None:
        """Update or create a player from message data.

        Handles incremental updates by preserving existing values when
        fields are not present in the update message.

        Args:
            player_data: Player dict from GameStateMessage.
        """
        seat_id = player_data.get("seatId") or player_data.get("systemSeatNumber")
        if seat_id is None:
            return

        # Get existing player to preserve values not in this update
        existing = self.players.get(seat_id)

        # Only update life_total if explicitly provided in the message
        # This fixes the bug where diff messages without lifeTotal would reset to 20
        if "lifeTotal" in player_data:
            life_total = player_data["lifeTotal"]
        elif existing:
            life_total = existing.life_total
        else:
            life_total = 20  # Default for new players

        # Same for lands_played - preserve if not in update
        if "landsPlayedThisTurn" in player_data:
            lands_played = player_data["landsPlayedThisTurn"]
        elif existing:
            lands_played = existing.lands_played
        else:
            lands_played = 0

        # FALLBACK: Arena doesn't always track landsPlayedThisTurn correctly
        # Infer by counting lands that entered battlefield this turn
        if lands_played == 0 and self.turn_info.turn_number > 0:
            current_turn = self.turn_info.turn_number
            inferred_lands = 0
            for obj in self.battlefield:
                if (obj.owner_seat_id == seat_id and
                    obj.controller_seat_id == seat_id and
                    self._is_land_object(obj) and
                    obj.turn_entered_battlefield == current_turn):
                    inferred_lands += 1
                    logger.debug(f"Inferred land: grp_id={obj.grp_id} entered turn {current_turn} for seat {seat_id}")

            if inferred_lands > 0:
                lands_played = inferred_lands
                logger.info(f"Inferred lands_played={lands_played} for seat {seat_id} (Arena reported 0)")

        # Extract mana pool if present, otherwise preserve existing
        # GRE uses "color" field with values like "ManaColor_Green" → map to WUBRG/C
        _MANA_COLOR_MAP = {
            "ManaColor_White": "W", "ManaColor_Blue": "U",
            "ManaColor_Black": "B", "ManaColor_Red": "R",
            "ManaColor_Green": "G", "ManaColor_Colorless": "C",
            "ManaColor_Any": "Any",
        }
        if "manaPool" in player_data:
            mana_pool = {}
            for mana_data in player_data["manaPool"]:
                raw_color = mana_data.get("color", mana_data.get("type", ""))
                mana_type = _MANA_COLOR_MAP.get(raw_color, raw_color or "unknown")
                mana_count = mana_data.get("count", 0)
                mana_pool[mana_type] = mana_pool.get(mana_type, 0) + mana_count
        elif existing:
            mana_pool = existing.mana_pool
        else:
            mana_pool = {}

        player = Player(
            seat_id=seat_id,
            life_total=life_total,
            lands_played=lands_played,
            mana_pool=mana_pool,
        )

        self.players[seat_id] = player
        logger.debug(f"Updated player {seat_id} (life={life_total}, lands={lands_played})")

    def _is_land_object(self, obj: GameObject) -> bool:
        """Check if a game object is a land, with fallback for missing card_types.

        Arena diff messages may create new instances without cardTypes.
        Falls back to checking other objects with the same grp_id.
        """
        if obj.card_types:
            return any("Land" in ct for ct in obj.card_types)
        # Fallback: check if another instance with the same grp_id has land card_types
        if obj.grp_id:
            for other in self.game_objects.values():
                if other.grp_id == obj.grp_id and other.card_types:
                    return any("Land" in ct for ct in other.card_types)
        return False

    def _infer_lands_played(self) -> None:
        """Infer lands_played for all players by counting lands that entered this turn.

        Arena doesn't always include player data in diff messages, so
        lands_played can stay at 0 even after a land enters the battlefield.
        This runs after every game state message to correct that.
        """
        if self.turn_info.turn_number <= 0:
            return

        current_turn = self.turn_info.turn_number
        for seat_id, player in self.players.items():
            if player.lands_played > 0:
                continue  # Already tracked (from Arena data or previous inference)

            inferred_lands = 0
            for obj in self.battlefield:
                if (obj.owner_seat_id == seat_id and
                    obj.controller_seat_id == seat_id and
                    self._is_land_object(obj) and
                    obj.turn_entered_battlefield == current_turn):
                    inferred_lands += 1

            if inferred_lands > 0:
                player.lands_played = inferred_lands
                logger.info(f"Inferred lands_played={inferred_lands} for seat {seat_id} (post-message)")

    def _add_event(self, event: dict) -> None:
        """Add a game event to the recent_events ring buffer.

        Args:
            event: Event dict with at minimum a 'type' key.
        """
        event["turn"] = self.turn_info.turn_number
        event["phase"] = self.turn_info.phase
        self.recent_events.append(event)
        if len(self.recent_events) > self._max_recent_events:
            self.recent_events.pop(0)

    def _resolve_card_name(self, grp_id: int) -> str:
        """Resolve a grp_id to a card name, with fallback."""
        if not grp_id:
            return "Unknown"
        try:
            from arenamcp import server
            info = server.get_card_info(grp_id)
            return info.get("name", f"Card#{grp_id}")
        except Exception:
            return f"Card#{grp_id}"

    def _process_annotations(self, annotations: list[dict]) -> None:
        """Process GRE annotations from a GameStateMessage.

        Annotations carry the 'why' behind state changes: damage, counters,
        zone transfers, reveals, resolution lifecycle, etc.

        Args:
            annotations: List of annotation dicts from GameStateMessage.
        """
        for ann in annotations:
            ann_types = ann.get("type", [])
            if isinstance(ann_types, str):
                ann_types = [ann_types]
            details = ann.get("details", [])
            affected_ids = ann.get("affectedIds", [])
            # Build a quick detail lookup
            # GRE protobuf repeated fields (valueInt32, valueInt64) may arrive
            # as lists instead of scalars — unwrap single-element lists.
            detail_map = {}
            for d in details:
                key = d.get("key", "")
                if key:
                    raw = d.get("valueInt32", d.get("valueString", d.get("valueInt64", "")))
                    # Unwrap single-element lists to scalar (protobuf repeated fields)
                    if isinstance(raw, list):
                        raw = raw[0] if len(raw) == 1 else (sum(raw) if raw and all(isinstance(x, (int, float)) for x in raw) else raw)
                    detail_map[key] = raw

            for ann_type in ann_types:
                if ann_type == "AnnotationType_DamageDealt":
                    # Track damage: who dealt how much to whom
                    _raw_dmg = detail_map.get("damage", 0)
                    damage_amount = _raw_dmg if isinstance(_raw_dmg, int) else int(_raw_dmg[0]) if isinstance(_raw_dmg, list) and _raw_dmg else 0
                    source_id = detail_map.get("sourceId", 0)
                    target_id = detail_map.get("targetId", affected_ids[0] if affected_ids else 0)
                    # If target is a player seat, track cumulative damage
                    for seat_id in self.players:
                        if target_id == seat_id:
                            self.damage_taken[seat_id] = self.damage_taken.get(seat_id, 0) + damage_amount
                    # Resolve source card name for event log
                    source_obj = self.game_objects.get(source_id)
                    source_name = self._resolve_card_name(source_obj.grp_id) if source_obj else f"#{source_id}"
                    self._add_event({
                        "type": "damage_dealt",
                        "source": source_name,
                        "source_id": source_id,
                        "amount": damage_amount,
                        "target_id": target_id,
                    })

                elif ann_type == "AnnotationType_ZoneTransfer":
                    # Card moved zones (died, bounced, exiled, etc.)
                    zone_src = detail_map.get("zone_src", "")
                    zone_dest = detail_map.get("zone_dest", "")
                    category = detail_map.get("category", "")
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event({
                                "type": "zone_transfer",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                                "from_zone": zone_src,
                                "to_zone": zone_dest,
                                "category": category,
                            })

                elif ann_type in ("AnnotationType_CounterAdded", "AnnotationType_CounterRemoved"):
                    counter_type = detail_map.get("counterType", "unknown")
                    _raw_cnt = detail_map.get("counterCount", detail_map.get("count", 1))
                    counter_count = _raw_cnt if isinstance(_raw_cnt, int) else int(_raw_cnt[0]) if isinstance(_raw_cnt, list) and _raw_cnt else 1
                    is_added = "Added" in ann_type
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            # Update counter state on the object
                            current = obj.counters.get(counter_type, 0)
                            if is_added:
                                obj.counters[counter_type] = current + counter_count
                            else:
                                obj.counters[counter_type] = max(0, current - counter_count)
                                if obj.counters[counter_type] == 0:
                                    del obj.counters[counter_type]
                            self._add_event({
                                "type": "counter_added" if is_added else "counter_removed",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                                "counter_type": counter_type,
                                "amount": counter_count,
                            })

                elif ann_type == "AnnotationType_ControllerChanged":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        new_controller = detail_map.get("controllerId", 0)
                        if obj:
                            self._add_event({
                                "type": "controller_changed",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                                "new_controller": new_controller,
                            })

                elif ann_type in ("AnnotationType_CardRevealed",
                                  "AnnotationType_InstanceRevealedToOpponent",
                                  "AnnotationType_RevealedCardCreated"):
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and obj.grp_id:
                            owner = obj.owner_seat_id
                            if owner not in self.revealed_cards:
                                self.revealed_cards[owner] = set()
                            self.revealed_cards[owner].add(obj.grp_id)
                            self._add_event({
                                "type": "card_revealed",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                                "owner_seat": owner,
                            })

                elif ann_type == "AnnotationType_ResolutionStart":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event({
                                "type": "resolution_start",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                            })

                elif ann_type == "AnnotationType_ResolutionComplete":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event({
                                "type": "resolution_complete",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                            })

                elif ann_type in ("AnnotationType_TokenCreated", "AnnotationType_TokenDeleted"):
                    is_created = "Created" in ann_type
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            self._add_event({
                                "type": "token_created" if is_created else "token_deleted",
                                "card": self._resolve_card_name(obj.grp_id),
                                "instance_id": obj_id,
                            })

                elif ann_type == "AnnotationType_TriggeringObject":
                    # Links a triggered ability to its source
                    source_id = detail_map.get("sourceId", affected_ids[0] if affected_ids else 0)
                    trigger_id = detail_map.get("triggerId", 0)
                    source_obj = self.game_objects.get(source_id)
                    if source_obj:
                        self._add_event({
                            "type": "trigger",
                            "source": self._resolve_card_name(source_obj.grp_id),
                            "source_id": source_id,
                            "trigger_id": trigger_id,
                        })

                elif ann_type == "AnnotationType_ManaPaid":
                    # Mana payment details
                    self._add_event({
                        "type": "mana_paid",
                        "details": detail_map,
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_UserActionTaken":
                    # Player took an action (fixes "can't see what player chose" gap)
                    self._add_event({
                        "type": "user_action",
                        "details": detail_map,
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_Scry":
                    self._add_event({
                        "type": "scry",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type in ("AnnotationType_LossOfGame", "AnnotationType_WinTheGame"):
                    result = "loss" if "Loss" in ann_type else "win"
                    self._add_event({
                        "type": "game_end",
                        "result": result,
                        "affected_ids": affected_ids,
                    })
                    # Persist result so it survives reset() for post-match analysis
                    self.last_game_result = result

                elif ann_type == "AnnotationType_ModifiedLife":
                    self._add_event({
                        "type": "life_changed",
                        "details": detail_map,
                        "affected_ids": affected_ids,
                    })

                elif ann_type in ("AnnotationType_CoinFlip", "AnnotationType_ChoiceResult"):
                    self._add_event({
                        "type": "random_result",
                        "sub_type": ann_type.replace("AnnotationType_", ""),
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_FaceDown":
                    for obj_id in affected_ids:
                        self._add_event({
                            "type": "face_down",
                            "instance_id": obj_id,
                        })

                elif ann_type in ("AnnotationType_CreateAttachment",
                                  "AnnotationType_AttachmentCreated"):
                    self._add_event({
                        "type": "attachment",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

    def _update_turn_info(self, turn_data: dict) -> None:
        """Update turn info from message data.

        Args:
            turn_data: TurnInfo dict from GameStateMessage.
        """
        new_turn = turn_data.get("turnNumber", self.turn_info.turn_number)

        # Detect new game: turn number resets to 1 or decreases significantly
        if self.turn_info.turn_number > 3 and new_turn <= 1:
            logger.info(f"New game detected (turn {self.turn_info.turn_number} -> {new_turn}) - Performing Search & Destroy on old state.")
            
            # FULL RESET of all zones, objects, players
            self.reset()
            
            # reset() makes turn_number 0, which is fine as we overwrite it below

        # Check if active player is explicitly in the update
        explicit_active = "activePlayer" in turn_data
        
        if new_turn != self.turn_info.turn_number:
            # Turn changed
            if explicit_active:
                new_active = turn_data["activePlayer"]
            else:
                # Turn changed without explicit activePlayer in this diff.
                # In a 2-player game, turns normally alternate between seats.
                # Infer the new active player by swapping to the other seat.
                # This is correct for normal turns and only wrong for Extra Turns
                # (which are rare and will be corrected by the next diff that
                # includes activePlayer).  Previously we set active_player=0
                # which guaranteed wrong "whose turn" advice until a follow-up
                # diff arrived — often too late, causing the coach to think it
                # was always the opponent's turn.
                prev_active = self.turn_info.active_player
                other_seat = self.opponent_seat_id if prev_active == self.local_seat_id else self.local_seat_id
                if other_seat is not None and prev_active != 0:
                    new_active = other_seat
                    logger.info(f"Turn change ({self.turn_info.turn_number}->{new_turn}) without activePlayer. Inferred active_player={new_active} (alternating from {prev_active}).")
                elif prev_active != 0:
                    # Can't determine other seat but have a valid previous value — keep it
                    new_active = prev_active
                    logger.warning(f"Turn change ({self.turn_info.turn_number}->{new_turn}) without activePlayer. Keeping previous active_player={prev_active} (opponent seat unknown).")
                else:
                    # Previous was already 0 (unknown) — nothing to alternate from.
                    # Guess local player (turn 1 is usually the play-first player,
                    # but any value > 0 is better than 0).
                    new_active = self.local_seat_id or 0
                    logger.warning(f"Turn change ({self.turn_info.turn_number}->{new_turn}) without activePlayer. Previous was 0, guessing local_seat={new_active}.")
        else:
            # Turn didn't change, use update or keep existing
            new_active = turn_data.get("activePlayer", self.turn_info.active_player)

        # Clear stale pending combat steps when turn or active player changes
        # Must check BEFORE updating turn_info
        if new_turn != self.turn_info.turn_number or new_active != self.turn_info.active_player:
            if self._pending_combat_steps:
                logger.debug(f"Clearing {len(self._pending_combat_steps)} stale pending combat steps (turn/active changed)")
                self._pending_combat_steps.clear()
        
        # UNTAP STEP: When turn changes, untap all permanents controlled by the new active player.
        # Skip permanents in _untap_prevention — those that MTGA explicitly kept tapped last turn
        # (e.g. creatures with "can't become untapped" from Blossombind-style effects).
        # After blanket untap, _in_untap_step is set so that object diffs in this same message
        # can update _untap_prevention for the NEXT turn's blanket untap.
        if new_turn != self.turn_info.turn_number and new_active != 0:
            self._in_untap_step = True
            # Clean up _untap_prevention: remove instance_ids no longer on battlefield
            battlefield_ids = set()
            for obj in self.game_objects.values():
                zone = self.zones.get(obj.zone_id)
                if zone and zone.zone_type == ZoneType.BATTLEFIELD:
                    battlefield_ids.add(obj.instance_id)
            self._untap_prevention &= battlefield_ids

            untapped_count = 0
            skipped_count = 0
            for obj in self.game_objects.values():
                controller = obj.controller_seat_id if obj.controller_seat_id else obj.owner_seat_id
                if controller == new_active and obj.is_tapped:
                    zone = self.zones.get(obj.zone_id)
                    if zone and zone.zone_type == ZoneType.BATTLEFIELD:
                        if obj.instance_id in self._untap_prevention:
                            skipped_count += 1
                        else:
                            obj.is_tapped = False
                            untapped_count += 1
            if untapped_count > 0 or skipped_count > 0:
                msg = f"Untap step: untapped {untapped_count} permanents for seat {new_active}"
                if skipped_count > 0:
                    msg += f" (skipped {skipped_count} with untap prevention)"
                logger.info(msg)
        
        # Reset lands_played to 0 for all players when the turn changes.
        # GRE diff messages often omit landsPlayedThisTurn, so _update_player
        # falls back to existing.lands_played — carrying over the stale value
        # from the previous turn. Resetting here ensures a clean slate.
        if new_turn != self.turn_info.turn_number:
            for player in self.players.values():
                player.lands_played = 0

        turn_changed = new_turn != self.turn_info.turn_number
        self.turn_info.turn_number = new_turn
        self.turn_info.active_player = new_active
        self.turn_info.priority_player = turn_data.get("priorityPlayer", self.turn_info.priority_player)
        if turn_changed:
            # Clear stale stack entries on turn change.
            # In Magic, the stack is always empty when a new turn begins.
            # MTGA often doesn't send zone updates to remove resolved abilities
            # (triggered/activated), leaving ghost entries that cause
            # Legal: NONE and "pass priority" advice on the player's turn.
            self._clear_stale_stack()
            # Turn changed, reset phase/step if not provided (prevent stale phase leakage)
            if "phase" in turn_data:
                new_phase = turn_data["phase"]
            else:
                new_phase = "Phase_Beginning"
                logger.debug(f"Turn change ({self.turn_info.turn_number}->{new_turn}) without phase. Resetting to Phase_Beginning.")
            
            if "step" in turn_data:
                new_step = turn_data["step"]
            else:
                new_step = "Step_Untap"
        else:
            # Same turn, preserve existing if missing
            new_phase = turn_data.get("phase", self.turn_info.phase)
            new_step = turn_data.get("step", self.turn_info.step)

        # Safety: auto-clear mulligan decision once the game has started (turn >= 1)
        # SubmitDeckResp is client→server so it never reaches the GRE handler;
        # this auto-clear is the primary mechanism for clearing mulligans.
        if self.pending_decision in ("Mulligan", "Mulligan Bottom") and new_turn >= 1:
            logger.info(f"Auto-clearing Mulligan decision (game started, turn {new_turn})")
            self.pending_decision = None
            self.decision_seat_id = None
            self.decision_context = None
            self.decision_timestamp = 0

        # Detect phase change within same turn (or handled by reset above)
        if new_phase != self.turn_info.phase:
            logger.debug(f"Phase change: {self.turn_info.phase} -> {new_phase}")
            # Clear stale stack on phase transitions to Main phases.
            # The stack must be empty before any phase change in Magic.
            # Main phases are when advice matters most (cast spells, play lands).
            if "Main" in new_phase:
                self._clear_stale_stack()
            # If step is not explicitly in the update AND we didn't just reset it above,
            # we should clear it to avoid stale steps (e.g. Step_Draw in Phase_Main1)
            # But if we just set it to Untap above, keep it.
            if "step" not in turn_data and new_turn == self.turn_info.turn_number:
                new_step = ""
                logger.debug("Resetting step due to phase change")

        # Track combat steps as they happen (for event-driven triggers)
        if "Combat" in new_phase and new_step != self.turn_info.step:
            if "DeclareAttack" in new_step or "DeclareBlock" in new_step:
                # Store step with active player info for trigger generation
                self._pending_combat_steps.append({
                    "step": new_step,
                    "active_player": self.turn_info.active_player,
                    "turn": new_turn
                })
                logger.info(f"Queued combat step: {new_step} (active_player={self.turn_info.active_player})")

        self.turn_info.phase = new_phase
        self.turn_info.step = new_step
        logger.debug(f"Updated turn info: turn {self.turn_info.turn_number}, phase {self.turn_info.phase}, step {self.turn_info.step}")


def create_game_state_handler(game_state: GameState) -> Callable[[dict], None]:
    """Create an event handler that updates a GameState from GreToClientEvent.

    The handler extracts GameStateMessage from GreToClientEvent payloads
    and updates the provided GameState object.

    Args:
        game_state: The GameState instance to update.

    Returns:
        A handler function suitable for LogParser.register_handler().

    Example:
        game_state = GameState()
        handler = create_game_state_handler(game_state)
        parser.register_handler('GreToClientEvent', handler)
    """
    def handler(payload: dict) -> None:
        # GreToClientEvent contains greToClientMessages array
        gre_event = payload.get("greToClientEvent", {})
        messages = gre_event.get("greToClientMessages", [])

        # Process each message in the array
        for msg in messages:
            msg_type = msg.get("type", "")

            # Handle GameStateMessage
            if msg_type == "GREMessageType_GameStateMessage":
                game_state_msg = msg.get("gameStateMessage")
                if game_state_msg:
                    game_state.update_from_message(game_state_msg)
                    # Auto-clear stale decisions whose source is no longer on the stack.
                    # Client→server responses (SelectTargetsResp etc.) never reach this
                    # handler, so we detect resolution by checking if the source left the stack.
                    if (game_state.pending_decision
                            and game_state.pending_decision != "Mulligan"
                            and game_state.decision_context):
                        source_id = game_state.decision_context.get("source_id")
                        should_clear = False
                        if source_id is not None:
                            still_on_stack = any(
                                obj.instance_id == source_id for obj in game_state.stack
                            )
                            if not still_on_stack:
                                should_clear = True
                                logger.info(
                                    f"Auto-clearing stale decision '{game_state.pending_decision}' "
                                    f"(source {source_id} no longer on stack)"
                                )
                        elif game_state.decision_timestamp:
                            # Decisions without source_id (prompt, scry, etc.):
                            # clear after 15s — player has certainly responded by then
                            import time
                            age = time.time() - game_state.decision_timestamp
                            if age > 15:
                                should_clear = True
                                logger.info(
                                    f"Auto-clearing stale decision '{game_state.pending_decision}' "
                                    f"(no source_id, age={age:.0f}s)"
                                )
                        if should_clear:
                            game_state.pending_decision = None
                            game_state.decision_seat_id = None
                            game_state.decision_context = None
                            game_state.decision_timestamp = 0
            
            # Handle Mulligan (MulliganReq or legacy SubmitDeckReq)
            elif msg_type in ("GREMessageType_MulliganReq", "GREMessageType_SubmitDeckReq"):
                # MulliganReq = MTGA asking a player to keep/mulligan.
                # Even if systemSeatIds targets the opponent, both players decide
                # simultaneously — so always set the mulligan decision.
                logger.info(f"Captured Decision: Mulligan Check ({msg_type})")

                # If we get a Mulligan request while deeply into a game (Turn > 1),
                # it implies a restart/rematch that happened before the 'Turn 1' update arrived.
                if game_state.turn_info.turn_number > 1:
                     logger.info(f"Mulligan Request detected at Turn {game_state.turn_info.turn_number} -> Resetting Ghost State.")
                     game_state.reset()

                game_state.pending_decision = "Mulligan"
                game_state.decision_seat_id = game_state.local_seat_id
                import time
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {"type": "mulligan"}

            # Handle IntermissionReq (end-of-game / sideboard transition)
            elif msg_type == "GREMessageType_IntermissionReq":
                # IntermissionReq = game over / BO3 sideboard. Reset state but do NOT
                # set a Mulligan decision — the actual mulligan SubmitDeckReq will come
                # later if a new game starts.
                logger.info(f"IntermissionReq received (turn {game_state.turn_info.turn_number}) - game over/transition")
                game_state.reset()
                # Clear any stale decision from the finished game
                game_state.pending_decision = None
                game_state.decision_seat_id = None
                game_state.decision_context = None
                game_state.decision_timestamp = 0
                
            elif msg_type == "GREMessageType_PromptReq":
                import time
                prompt_text = msg.get("promptReq", {}).get("prompt", {}).get("text", "Action Required")
                logger.info(f"Captured Decision: Prompt ({prompt_text})")
                # Don't overwrite Mulligan with a generic PromptReq — the
                # mulligan prompt often arrives as a PromptReq right after the
                # MulliganReq and would clobber the specific "Mulligan" state
                # that the coach relies on for keep/mull advice.
                if game_state.pending_decision == "Mulligan":
                    logger.info("Skipping PromptReq — Mulligan decision already active")
                else:
                    game_state.pending_decision = prompt_text
                    game_state.decision_timestamp = time.time()
                    game_state.decision_context = {"type": "prompt", "text": prompt_text}
                
            elif msg_type == "GREMessageType_SelectTargetsReq":
                # PHASE 1: Capture rich context for target selection
                import time
                req = msg.get("selectTargetsReq", {})
                source_id = req.get("sourceId")
                source_card = None
                if source_id:
                    # Try to find the source card on the stack
                    for obj in game_state.stack:
                        if obj.instance_id == source_id:
                            try:
                                from arenamcp import server
                                card_info = server.get_card_info(obj.grp_id)
                                source_card = card_info.get("name", f"Unknown ({obj.grp_id})")
                            except Exception:
                                source_card = f"Unknown ({obj.grp_id})"
                            break
                
                logger.info(f"Captured Decision: Select Targets (source: {source_card or 'unknown'})")
                game_state.pending_decision = "Select Targets"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "target_selection",
                    "source_card": source_card,
                    "source_id": source_id,
                }
                
            elif msg_type == "GREMessageType_SelectNReq":
                # PHASE 1: Capture rich context for N-selection (discard, scry, etc.)
                import time
                req = msg.get("selectNReq", {})
                context_data = req.get("context", {})
                num_to_select = req.get("count", 1)
                min_select = req.get("minCount", num_to_select)
                max_select = req.get("maxCount", num_to_select)
                
                # Try to determine the type of selection (discard, scry, etc.)
                # This is heuristic-based on MTGA's context strings
                context_str = str(context_data).lower()
                if "discard" in context_str:
                    selection_type = "discard"
                elif "scry" in context_str:
                    selection_type = "scry"
                elif "surveil" in context_str:
                    selection_type = "surveil"
                else:
                    selection_type = "select_n"
                
                logger.info(f"Captured Decision: Select {num_to_select} Items ({selection_type})")
                game_state.pending_decision = "Select Items"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": selection_type,
                    "count": num_to_select,
                    "min": min_select,
                    "max": max_select,
                    "context_raw": context_data,
                }
                
            elif msg_type == "GREMessageType_GroupReq":
                # London Mulligan "choose cards to put on bottom" step,
                # or other group selection during the game.
                import time
                req = msg.get("groupReq", {})
                if game_state.turn_info.turn_number < 1:
                    # During mulligan phase, this is "select cards to bottom"
                    logger.info(f"Captured Decision: Mulligan Bottom Cards ({msg_type})")
                    game_state.pending_decision = "Mulligan Bottom"
                    game_state.decision_seat_id = game_state.local_seat_id
                    game_state.decision_timestamp = time.time()
                    game_state.decision_context = {
                        "type": "mulligan_bottom",
                        "raw": {k: v for k, v in req.items()},
                    }
                else:
                    logger.info(f"Captured Decision: Group Selection ({msg_type})")
                    game_state.pending_decision = "Group Selection"
                    game_state.decision_timestamp = time.time()
                    game_state.decision_context = {
                        "type": "group_selection",
                        "raw": {k: v for k, v in req.items()},
                    }

            elif msg_type == "GREMessageType_GroupOptionReq":
                # PHASE 1: Capture modal spell options
                import time
                req = msg.get("groupOptionReq", {})
                options = req.get("options", [])

                logger.info(f"Captured Decision: Choose Mode ({len(options)} options)")
                game_state.pending_decision = "Choose Mode"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "modal_choice",
                    "num_options": len(options),
                    "options": options,
                }

            elif msg_type == "GREMessageType_ActionsAvailableReq":
                # PHASE 1: Capture the exact list of actions Arena says are legal right now.
                # This is the "Ground Truth" for the autopilot.
                req = msg.get("actionsAvailableReq", {})
                raw_actions = req.get("actions", [])
                
                legal_list = []
                for action in raw_actions:
                    atype = action.get("actionType", "")
                    # Human-friendly mapping
                    if atype == "ActionType_Pass":
                        legal_list.append("Pass")
                    elif atype == "ActionType_Play":
                        # Try to resolve card name if grpId is present
                        name = "Land"
                        if action.get("grpId"):
                            try:
                                from arenamcp import server
                                info = server.get_card_info(action["grpId"])
                                name = info.get("name", "Land")
                            except: pass
                        legal_list.append(f"Play Land: {name}")
                    elif atype == "ActionType_Cast":
                        name = "Spell"
                        if action.get("grpId"):
                            try:
                                from arenamcp import server
                                info = server.get_card_info(action["grpId"])
                                name = info.get("name", "Spell")
                            except: pass
                        legal_list.append(f"Cast {name}")
                    elif atype == "ActionType_Activate":
                        legal_list.append(f"Activate Ability")
                    else:
                        # Fallback for other types
                        clean_type = atype.replace("ActionType_", "")
                        legal_list.append(f"Action: {clean_type}")
                
                if legal_list:
                    game_state.legal_actions = legal_list
                    logger.info(f"Captured {len(legal_list)} legal actions from GRE: {legal_list}")

            elif msg_type == "GREMessageType_ConnectResp":
                connect_resp = msg.get("connectResp", {})
                deck_cards = connect_resp.get("deckMessage", {}).get("deckCards", [])
                if deck_cards:
                    game_state.deck_cards = deck_cards
                    logger.info(f"Captured deck list from ConnectResp: {len(deck_cards)} cards")

            # --- Combat Decision Messages ---

            elif msg_type == "GREMessageType_DeclareAttackersReq":
                import time
                req = msg.get("declareAttackersReq", {})
                legal_attackers = req.get("attackers", req.get("qualifiedAttackers", []))
                attacker_names = []
                for atk in legal_attackers:
                    obj_id = atk if isinstance(atk, int) else atk.get("instanceId", atk.get("attackerInstanceId", 0))
                    obj = game_state.game_objects.get(obj_id)
                    if obj:
                        attacker_names.append(game_state._resolve_card_name(obj.grp_id))
                logger.info(f"Captured Decision: Declare Attackers ({len(attacker_names)} legal)")
                game_state.pending_decision = "Declare Attackers"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "declare_attackers",
                    "legal_attackers": attacker_names,
                    "raw_attackers": legal_attackers,
                }

            elif msg_type == "GREMessageType_DeclareBlockersReq":
                import time
                req = msg.get("declareBlockersReq", {})
                legal_blockers = req.get("blockers", req.get("qualifiedBlockers", []))
                blocker_names = []
                for blk in legal_blockers:
                    obj_id = blk if isinstance(blk, int) else blk.get("instanceId", blk.get("blockerInstanceId", 0))
                    obj = game_state.game_objects.get(obj_id)
                    if obj:
                        blocker_names.append(game_state._resolve_card_name(obj.grp_id))
                logger.info(f"Captured Decision: Declare Blockers ({len(blocker_names)} legal)")
                game_state.pending_decision = "Declare Blockers"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "declare_blockers",
                    "legal_blockers": blocker_names,
                    "raw_blockers": legal_blockers,
                }

            elif msg_type == "GREMessageType_AssignDamageReq":
                import time
                req = msg.get("assignDamageReq", {})
                logger.info("Captured Decision: Assign Damage")
                game_state.pending_decision = "Assign Damage"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "assign_damage",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_OrderCombatDamageReq":
                import time
                req = msg.get("orderCombatDamageReq", msg.get("orderDamageReq", {}))
                logger.info("Captured Decision: Order Combat Damage")
                game_state.pending_decision = "Order Combat Damage"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "order_combat_damage",
                    "raw": {k: v for k, v in req.items()},
                }

            # --- Additional Decision Messages ---

            elif msg_type == "GREMessageType_PayCostsReq":
                import time
                req = msg.get("payCostsReq", {})
                source_id = req.get("sourceId", 0)
                source_obj = game_state.game_objects.get(source_id)
                source_name = game_state._resolve_card_name(source_obj.grp_id) if source_obj else "Unknown"
                logger.info(f"Captured Decision: Pay Costs (source: {source_name})")
                game_state.pending_decision = "Pay Costs"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "pay_costs",
                    "source_card": source_name,
                    "source_id": source_id,
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_SearchReq":
                import time
                req = msg.get("searchReq", {})
                zone_id = req.get("zoneId", 0)
                logger.info(f"Captured Decision: Search (zone {zone_id})")
                game_state.pending_decision = "Search Library"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "search",
                    "zone_id": zone_id,
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_DistributionReq":
                import time
                req = msg.get("distributionReq", {})
                total = req.get("amount", req.get("total", 0))
                source_id = req.get("sourceId", 0)
                source_obj = game_state.game_objects.get(source_id)
                source_name = game_state._resolve_card_name(source_obj.grp_id) if source_obj else "Unknown"
                logger.info(f"Captured Decision: Distribute {total} (source: {source_name})")
                game_state.pending_decision = "Distribute"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "distribution",
                    "source_card": source_name,
                    "source_id": source_id,
                    "total": total,
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_NumericInputReq":
                import time
                req = msg.get("numericInputReq", {})
                source_id = req.get("sourceId", 0)
                source_obj = game_state.game_objects.get(source_id)
                source_name = game_state._resolve_card_name(source_obj.grp_id) if source_obj else "Unknown"
                min_val = req.get("min", 0)
                max_val = req.get("max", 0)
                logger.info(f"Captured Decision: Numeric Input for {source_name} ({min_val}-{max_val})")
                game_state.pending_decision = "Choose Number"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "numeric_input",
                    "source_card": source_name,
                    "source_id": source_id,
                    "min": min_val,
                    "max": max_val,
                }

            elif msg_type == "GREMessageType_ChooseStartingPlayerReq":
                import time
                logger.info("Captured Decision: Choose Starting Player (Play or Draw)")
                game_state.pending_decision = "Choose Play/Draw"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "choose_starting_player",
                }

            elif msg_type == "GREMessageType_SelectReplacementReq":
                import time
                req = msg.get("selectReplacementReq", {})
                logger.info("Captured Decision: Select Replacement Effect")
                game_state.pending_decision = "Select Replacement"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "select_replacement",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_SelectNGroupReq":
                import time
                req = msg.get("selectNGroupReq", {})
                logger.info("Captured Decision: Select N from Group")
                game_state.pending_decision = "Select from Group"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "select_n_group",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_SelectFromGroupsReq":
                import time
                req = msg.get("selectFromGroupsReq", {})
                logger.info("Captured Decision: Select from Groups")
                game_state.pending_decision = "Select from Groups"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "select_from_groups",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_SearchFromGroupsReq":
                import time
                req = msg.get("searchFromGroupsReq", {})
                logger.info("Captured Decision: Search from Groups")
                game_state.pending_decision = "Search from Groups"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "search_from_groups",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_CastingTimeOptionsReq":
                import time
                req = msg.get("castingTimeOptionsReq", {})
                logger.info("Captured Decision: Casting Time Options (alternative cost)")
                game_state.pending_decision = "Choose Casting Option"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "casting_time_options",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_SelectCountersReq":
                import time
                req = msg.get("selectCountersReq", {})
                logger.info("Captured Decision: Select Counters")
                game_state.pending_decision = "Select Counters"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "select_counters",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_RevealHandReq":
                # Opponent's hand is being revealed - track for strategy
                req = msg.get("revealHandReq", {})
                seat_ids = req.get("systemSeatIds", [])
                logger.info(f"Hand reveal for seats: {seat_ids}")
                # The actual revealed cards arrive via GameStateMessage + CardRevealed annotations

            elif msg_type == "GREMessageType_OrderReq":
                import time
                req = msg.get("orderReq", {})
                logger.info("Captured Decision: Order (triggered ability ordering)")
                game_state.pending_decision = "Order Triggers"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "order_triggers",
                    "raw": {k: v for k, v in req.items()},
                }

            elif msg_type == "GREMessageType_GatherReq":
                import time
                req = msg.get("gatherReq", {})
                logger.info("Captured Decision: Gather")
                game_state.pending_decision = "Gather"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "gather",
                    "raw": {k: v for k, v in req.items()},
                }

            # --- Fallback for truly unknown Req types ---

            elif msg_type.endswith("Req") and msg_type not in (
                "GREMessageType_MulliganReq", "GREMessageType_SubmitDeckReq",
                "GREMessageType_IntermissionReq", "GREMessageType_PromptReq",
                "GREMessageType_SelectTargetsReq", "GREMessageType_SelectNReq",
                "GREMessageType_GroupReq", "GREMessageType_GroupOptionReq",
                "GREMessageType_ActionsAvailableReq",
                "GREMessageType_DeclareAttackersReq", "GREMessageType_DeclareBlockersReq",
                "GREMessageType_AssignDamageReq", "GREMessageType_OrderCombatDamageReq",
                "GREMessageType_PayCostsReq", "GREMessageType_SearchReq",
                "GREMessageType_DistributionReq", "GREMessageType_NumericInputReq",
                "GREMessageType_ChooseStartingPlayerReq", "GREMessageType_SelectReplacementReq",
                "GREMessageType_SelectNGroupReq", "GREMessageType_SelectFromGroupsReq",
                "GREMessageType_SearchFromGroupsReq", "GREMessageType_CastingTimeOptionsReq",
                "GREMessageType_SelectCountersReq", "GREMessageType_RevealHandReq",
                "GREMessageType_OrderReq", "GREMessageType_GatherReq",
            ):
                import time
                logger.warning(f"Unknown GRE Req type: {msg_type} - treating as pending decision")
                game_state.pending_decision = f"Unknown Decision ({msg_type})"
                game_state.decision_timestamp = time.time()
                game_state.decision_context = {
                    "type": "unknown_req",
                    "gre_type": msg_type,
                    "raw_message": {k: v for k, v in msg.items() if k != "type"},
                }

            elif "Resp" in msg_type:
                # PHASE 3: Only clear decisions on actual decision responses, not generic responses
                # Only clear on explicit decision submission responses
                decision_response_types = [
                    "GREMessageType_SelectTargetsResp",
                    "GREMessageType_SubmitTargetsResp",
                    "GREMessageType_SelectNResp",
                    "GREMessageType_GroupOptionResp",
                    "GREMessageType_SubmitDeckResp",  # Mulligan
                    "GREMessageType_PromptResp",
                    "GREMessageType_SubmitAttackersResp",
                    "GREMessageType_SubmitBlockersResp",
                    "GREMessageType_AssignDamageConfirmation",
                    "GREMessageType_OrderDamageConfirmation",
                ]
                
                if game_state.pending_decision and msg_type in decision_response_types:
                    # Don't auto-clear Mulligan (handled separately)
                    if game_state.pending_decision == "Mulligan" and msg_type != "GREMessageType_SubmitDeckResp":
                        pass
                    else:
                        logger.debug(f"Clearing decision '{game_state.pending_decision}' due to {msg_type}")
                        game_state.pending_decision = None
                        game_state.decision_seat_id = None
                        game_state.decision_context = None
                        game_state.decision_timestamp = 0

            elif msg_type == "GREMessageType_TimerStateMessage":
                pass

        # Also handle direct GameStateMessage events (legacy format)
        if "gameObjects" in payload or "zones" in payload or "players" in payload:
            game_state.update_from_message(payload)
        
        # Record frame if recording is active
        try:
            from arenamcp.match_validator import record_frame
            snapshot = game_state.get_snapshot()
            record_frame(payload, snapshot)
        except ImportError:
            pass  # match_validator not available
        except Exception as e:
            logger.debug(f"Frame recording skipped: {e}")

    return handler


def create_recording_handler(
    game_state: GameState,
    recording: "MatchRecording"  # Forward reference to avoid circular import
) -> Callable[[dict], None]:
    """Create a handler that updates GameState AND records frames for validation.
    
    This wraps the base handler to also capture raw messages alongside
    our parsed snapshots for post-match comparison.
    
    Args:
        game_state: The GameState instance to update.
        recording: The MatchRecording to add frames to.
        
    Returns:
        A handler function suitable for LogParser.register_handler().
    """
    base_handler = create_game_state_handler(game_state)
    
    def recording_handler(payload: dict) -> None:
        # First, apply the update via base handler
        base_handler(payload)
        
        # Then, record the frame with current snapshot
        try:
            snapshot = game_state.get_snapshot()
            recording.add_frame(payload, snapshot)
        except Exception as e:
            logger.warning(f"Failed to record frame: {e}")
    
    return recording_handler


# --- Match State Persistence ---

MATCH_STATE_PATH = Path.home() / ".arenamcp" / "last_match.json"
MATCH_STATE_MAX_AGE = 1800  # 30 minutes


def save_match_state(
    game_state: GameState,
    log_offset: int = 0,
) -> None:
    """Save current match state for recovery after restart.

    Writes match_id, local_seat_id, log_offset, turn info, and timestamp
    to ~/.arenamcp/last_match.json.

    Args:
        game_state: Current GameState instance.
        log_offset: Current file position in the log file.
    """
    if not game_state.match_id:
        return

    state = {
        "match_id": game_state.match_id,
        "local_seat_id": game_state.local_seat_id,
        "log_offset": log_offset,
        "turn_number": game_state.turn_info.turn_number,
        "phase": game_state.turn_info.phase,
        "timestamp": time.time(),
        "status": "active",
    }

    try:
        MATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MATCH_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logger.debug(f"Saved match state: turn {state['turn_number']}, offset {log_offset}")
    except Exception as e:
        logger.warning(f"Failed to save match state: {e}")


def load_match_state() -> Optional[dict[str, Any]]:
    """Load saved match state if valid (< 30 min old, status active).

    Returns:
        Dict with match_id, local_seat_id, log_offset, turn_number, phase,
        timestamp, status. Or None if no valid state exists.
    """
    if not MATCH_STATE_PATH.exists():
        return None

    try:
        with open(MATCH_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Validate age
        age = time.time() - state.get("timestamp", 0)
        if age > MATCH_STATE_MAX_AGE:
            logger.info(f"Match state too old ({age:.0f}s > {MATCH_STATE_MAX_AGE}s)")
            return None

        # Validate status
        if state.get("status") != "active":
            logger.info(f"Match state status is '{state.get('status')}', not active")
            return None

        logger.info(
            f"Loaded match state: match={state.get('match_id')}, "
            f"turn={state.get('turn_number')}, offset={state.get('log_offset')}"
        )
        return state

    except Exception as e:
        logger.warning(f"Failed to load match state: {e}")
        return None


def mark_match_ended() -> None:
    """Mark the saved match state as ended."""
    if not MATCH_STATE_PATH.exists():
        return

    try:
        with open(MATCH_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)

        state["status"] = "ended"
        state["ended_at"] = time.time()

        with open(MATCH_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logger.info("Marked match state as ended")
    except Exception as e:
        logger.warning(f"Failed to mark match ended: {e}")

