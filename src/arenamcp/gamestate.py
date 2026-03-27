"""MTGA game state tracking from parsed log events.

This module provides the GameState class that maintains a complete
snapshot of the current game state from parsed MTGA log events.
"""

import copy
import json
import logging
import threading
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
    # ── Phase 1 turbo-charge fields (from GRE annotations) ──
    # Modified stats (actual values after continuous effects)
    modified_power: Optional[int] = None
    modified_toughness: Optional[int] = None
    modified_cost: Optional[str] = None
    modified_colors: Optional[list[str]] = None
    modified_types: Optional[list[str]] = None
    modified_name: Optional[str] = None
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
    class_level: Optional[int] = None
    # Copy source
    copied_from_grp_id: Optional[int] = None
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
    team_id: Optional[int] = None
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


class GameState:
    """Maintains complete game state from parsed MTGA log events.

    This class tracks zones, game objects, players, and turn information
    as they are updated via GameStateMessage events from the log parser.

    Snapshot architecture
    ---------------------
    There is a single canonical snapshot pipeline:

    1. **_published_snapshot** -- An immutable ``dict`` rebuilt (under
       ``_state_lock``) every time mutable state changes.  All readers
       go through ``get_published_snapshot()`` which returns either a
       deep-copy or a read-only reference.

    2. **get_snapshot()** -- A convenience layer on top of (1) that
       enriches game-object dicts with card names (for the TUI).

    3. **_game_end_data** -- When a game ends, ``prepare_for_game_end()``
       atomically captures the result and a copy of ``_published_snapshot``
       into this single dict, then sets ``game_ended_event``.  The coaching
       loop calls ``consume_game_end()`` to retrieve and clear it in one
       step.
    """

    # Fields that are reset when a new match starts.  Grouped here so that
    # ``__init__`` and ``reset()`` stay in sync automatically.
    # Entries whose value is a *type* (``dict``, ``list``, ``set``) get a
    # fresh instance; everything else is used as a literal default.
    _RESETTABLE_DEFAULTS: dict[str, Any] = {
        # Core game data
        "zones": dict,
        "game_objects": dict,
        "players": dict,
        # Local player tracking
        "local_seat_id": None,
        "_seat_source": 0,
        # Opponent card history
        "played_cards": dict,
        "_seen_instances": set,
        # Combat step tracking
        "_pending_combat_steps": list,
        "_last_combat_step_time": 0,
        # Stack update tracking
        "_last_stack_update_time": 0,
        # Untap prevention
        "_untap_prevention": set,
        "_in_untap_step": False,
        # Decision tracking
        "pending_decision": None,
        "decision_seat_id": None,
        "decision_context": None,
        "decision_timestamp": 0,
        "last_cleared_decision": None,
        "legal_actions": list,
        "legal_actions_raw": list,
        # Match tracking
        "match_id": None,
        # Deck list
        "deck_cards": list,
        # Annotation-derived event tracking
        "recent_events": list,
        "damage_taken": dict,
        "revealed_cards": dict,
        # ── Phase 1 turbo-charge state ──
        # Designations: seat_id -> set of active designations (monarch, initiative, city's blessing, day/night)
        "designations": dict,
        # Dungeon status: seat_id -> {"dungeon": name, "room": room_name}
        "dungeon_status": dict,
        # Action history: rolling buffer of recent player actions
        "action_history": list,
        # Timer state from GRE
        "timer_state": dict,
        # Sideboard cards (populated between BO3 games from SubmitDeckReq)
        "sideboard_cards": list,
    }

    def _apply_field_defaults(self) -> None:
        """Set every resettable field to its default value.

        For entries whose value is a *type* (``dict``, ``list``, ``set``),
        a fresh instance is created.  For everything else the value is
        used directly.
        """
        for attr, default in self._RESETTABLE_DEFAULTS.items():
            if isinstance(default, type):
                setattr(self, attr, default())
            else:
                setattr(self, attr, default)

    def __init__(self) -> None:
        """Initialize empty game state."""
        # Apply all resettable field defaults
        self._apply_field_defaults()

        # Non-resettable fields (survive across matches)
        self.turn_info: TurnInfo = TurnInfo()
        self._max_recent_events: int = 50

        # Persists across reset() so the coaching loop can read it after
        # match ends.  Cleared only by consume_game_end().
        self.last_game_result: Optional[str] = None  # "win", "loss", "draw", or None

        # ── Cross-thread game-end signaling ──
        # The parser thread calls prepare_for_game_end() which atomically
        # stores result + final snapshot into _game_end_data and sets
        # game_ended_event.  The coaching loop calls consume_game_end()
        # to retrieve and clear it.
        self.game_ended_event: threading.Event = threading.Event()
        self._game_end_data: Optional[dict[str, Any]] = None

        # Backward-compatible alias -- standalone.py and server.py read
        # _pre_reset_snapshot directly in a few places.
        self._pre_reset_snapshot: Optional[dict] = None

        # Published immutable snapshot for lock-safe readers
        self._state_lock = threading.RLock()
        self._published_snapshot: dict[str, Any] = {}
        self.publish_snapshot()

    def reset(self) -> None:
        """Reset the game state for a new match.

        Clears all per-game fields via ``_apply_field_defaults()``.
        Fields that must survive across matches (``last_game_result``,
        ``game_ended_event``, ``_game_end_data``, ``_pre_reset_snapshot``)
        are intentionally preserved -- they are consumed by the coaching
        loop after the reset.
        """
        logger.info("Resetting GameState for new match")
        self._apply_field_defaults()
        self.turn_info = TurnInfo()
        self.publish_snapshot()

    def prepare_for_game_end(self) -> None:
        """Capture final state and infer result BEFORE reset().

        Called by the IntermissionReq handler (parser thread) right before
        ``reset()`` wipes the board.  Atomically stores the result and a
        copy of the current published snapshot into ``_game_end_data``,
        then sets ``game_ended_event`` so the coaching loop detects it
        immediately.
        """
        with self._state_lock:
            # 1. Infer game result from the strongest available end-state signal
            if not self.last_game_result:
                inferred = (
                    self._infer_result_from_player_status_locked()
                    or self._infer_result_from_life_totals_locked()
                )
                self._record_match_result_locked(inferred, "prepare_for_game_end")

            # 2. Capture the published snapshot (already built, no enrichment
            #    overhead).  Deep-copy so it is immune to the upcoming reset().
            final_snapshot = copy.deepcopy(self._published_snapshot)

            # 3. Bundle result + snapshot atomically
            self._game_end_data = {
                "result": self.last_game_result,
                "snapshot": final_snapshot,
            }
            # Keep backward-compatible alias for external readers
            self._pre_reset_snapshot = final_snapshot

        # 4. Signal the coaching loop (Event.set is thread-safe on its own)
        self.game_ended_event.set()
        logger.info(f"Game-end prepared: result={self.last_game_result}, snapshot={'yes' if final_snapshot else 'no'}")

    def consume_game_end(self) -> tuple[Optional[str], Optional[dict]]:
        """Consume the game-end signal and return (result, snapshot).

        Called by the coaching loop after detecting ``game_ended_event``.
        Clears the event and all persistent game-end fields atomically so
        they don't re-trigger.

        Returns:
            Tuple of (result, final_snapshot) where result is "win",
            "loss", or None, and final_snapshot is the board state dict
            captured before reset().
        """
        with self._state_lock:
            data = self._game_end_data
            if data is not None:
                result = data.get("result")
                snapshot = data.get("snapshot")
            else:
                # Fallback: read legacy fields for callers that set them
                # individually (e.g. finalMatchResult in server.py).
                result = self.last_game_result
                snapshot = self._pre_reset_snapshot

            # Clear all game-end state
            self._game_end_data = None
            self._pre_reset_snapshot = None
            self.last_game_result = None

        self.game_ended_event.clear()
        return result, snapshot

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

    def _get_local_team_id_locked(self) -> Optional[int]:
        """Return the local player's team ID, if known."""
        if self.local_seat_id is None:
            return None
        player = self.players.get(self.local_seat_id)
        if player is None:
            return None
        return player.team_id

    def get_local_team_id(self) -> Optional[int]:
        """Thread-safe accessor for the local player's team ID."""
        with self._state_lock:
            return self._get_local_team_id_locked()

    def _record_match_result_locked(self, result: Optional[str], source: str) -> Optional[str]:
        """Persist a concrete game result under the state lock."""
        if not result:
            return None
        if result != self.last_game_result:
            if self.last_game_result is None:
                logger.info("Detected game result: %s (%s)", result, source)
            else:
                logger.info(
                    "Updated game result: %s -> %s (%s)",
                    self.last_game_result,
                    result,
                    source,
                )
            self.last_game_result = result
        return result

    def _infer_result_from_life_totals_locked(self) -> Optional[str]:
        """Infer win/loss from lethal life totals when seat identities are known."""
        if self.local_seat_id is None:
            return None
        for seat_id, player in self.players.items():
            if player.life_total <= 0:
                if seat_id == self.local_seat_id:
                    logger.info(
                        "Inferred game result from life totals: loss (seat %s life=%s)",
                        seat_id,
                        player.life_total,
                    )
                    return "loss"
                logger.info(
                    "Inferred game result from life totals: win (opponent seat %s life=%s)",
                    seat_id,
                    player.life_total,
                )
                return "win"
        return None

    def _infer_result_from_player_status_locked(self) -> Optional[str]:
        """Infer win/loss from terminal player statuses."""
        if self.local_seat_id is None:
            return None

        local_player = self.players.get(self.local_seat_id)
        if local_player and ("Loss" in local_player.status or "Defeat" in local_player.status):
            logger.info(
                "Inferred game result from player status: loss (seat %s status=%s)",
                self.local_seat_id,
                local_player.status,
            )
            return "loss"

        for seat_id, player in self.players.items():
            if seat_id == self.local_seat_id:
                continue
            if "Loss" in player.status or "Defeat" in player.status:
                logger.info(
                    "Inferred game result from player status: win (seat %s status=%s)",
                    seat_id,
                    player.status,
                )
                return "win"
        return None

    def _resolve_scope_result_locked(self, result_data: dict) -> Optional[str]:
        """Resolve MTGA result payloads into win/loss/draw for the local player."""
        result_str = str(result_data.get("result", "") or "")
        winning_team_id = result_data.get("winningTeamId")
        seat_id = result_data.get("seatId")

        if "Draw" in result_str:
            return "draw"

        if winning_team_id is not None:
            local_team_id = self._get_local_team_id_locked()
            if local_team_id is not None:
                return "win" if winning_team_id == local_team_id else "loss"

        if seat_id is not None and self.local_seat_id is not None:
            if "Loss" in result_str:
                return "loss" if seat_id == self.local_seat_id else "win"
            if "Win" in result_str:
                return "win" if seat_id == self.local_seat_id else "loss"

        if "Loss" in result_str and seat_id is None and winning_team_id is None:
            return "loss"
        if "Win" in result_str and seat_id is None and winning_team_id is None:
            return "win"
        return None

    def set_result_from_payload(self, result_data: Optional[dict], source: str) -> Optional[str]:
        """Resolve and persist an MTGA result payload."""
        if not result_data:
            return None
        with self._state_lock:
            result = self._resolve_scope_result_locked(result_data)
            return self._record_match_result_locked(result, source)

    def _resolve_end_annotation_locked(
        self,
        ann_type: str,
        affected_ids: list[int],
    ) -> Optional[str]:
        """Resolve win/loss annotations relative to the local seat/team."""
        local_seat_id = self.local_seat_id
        local_team_id = self._get_local_team_id_locked()
        other_team_ids = {
            player.team_id
            for player in self.players.values()
            if player.team_id is not None and player.team_id != local_team_id
        }

        if ann_type == "AnnotationType_LossOfGame":
            if local_seat_id is not None and local_seat_id in affected_ids:
                return "loss"
            if local_team_id is not None and local_team_id in affected_ids:
                return "loss"
            if self.opponent_seat_id is not None and self.opponent_seat_id in affected_ids:
                return "win"
            if other_team_ids.intersection(affected_ids):
                return "win"
        elif ann_type == "AnnotationType_WinTheGame":
            if local_seat_id is not None and local_seat_id in affected_ids:
                return "win"
            if local_team_id is not None and local_team_id in affected_ids:
                return "win"
            if self.opponent_seat_id is not None and self.opponent_seat_id in affected_ids:
                return "loss"
            if other_team_ids.intersection(affected_ids):
                return "loss"

        if not affected_ids:
            return "loss" if "Loss" in ann_type else "win"
        return None

    def _process_game_info(self, game_info: dict) -> None:
        """Consume gameInfo metadata that can signal terminal state."""
        match_id = game_info.get("matchID") or game_info.get("matchId")
        if match_id and self.match_id is None:
            self.match_id = match_id

        results = game_info.get("results", [])
        if results:
            ordered_results = sorted(
                results,
                key=lambda row: 0 if row.get("scope") == "MatchScope_Game" else 1,
            )
            for row in ordered_results:
                scope = row.get("scope", "")
                if scope not in ("MatchScope_Game", "MatchScope_Match", ""):
                    continue
                result = self._resolve_scope_result_locked(row)
                if self._record_match_result_locked(result, f"gameInfo.{scope or 'unknown'}"):
                    break

        if not self.last_game_result:
            inferred = self._infer_result_from_player_status_locked()
            self._record_match_result_locked(inferred, "gameInfo.player_status")

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

    # Grace period (seconds) before clearing the stack on phase transitions.
    # If the stack was modified very recently, the entries might be real
    # (delayed log flush), not stale ghosts.
    _STACK_CLEAR_GRACE_S = 2.0

    def _clear_stale_stack(self, force: bool = False) -> None:
        """Clear the stack zone on turn/phase boundaries.

        In Magic, the stack is always empty when a new turn begins.
        MTGA often doesn't send zone updates for resolved triggered/activated
        abilities, leaving ghost entries that cause the formatter to show
        Legal: NONE (can't cast at sorcery speed with non-empty stack).

        Args:
            force: If True, clear immediately (turn boundaries). If False,
                   respect grace period for recently-updated stacks (phase transitions).
        """
        for zone in self.zones.values():
            if zone.zone_type == ZoneType.STACK and zone.object_instance_ids:
                # On phase transitions (not turn boundaries), defer clearing
                # if the stack was just updated — likely real, not stale.
                if not force and self._last_stack_update_time:
                    age = time.time() - self._last_stack_update_time
                    if age < self._STACK_CLEAR_GRACE_S:
                        logger.debug(
                            f"Deferring stack clear — last update {age:.1f}s ago "
                            f"(grace={self._STACK_CLEAR_GRACE_S}s)"
                        )
                        return

                count = len(zone.object_instance_ids)
                zone.object_instance_ids = []
                logger.info(
                    f"Cleared {count} stale stack entries "
                    f"({'forced' if force else 'phase transition'})"
                )

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
        snap = self.get_published_snapshot(deep_copy=False)
        return list(snap.get("pending_combat_steps", []))

    def _build_raw_snapshot_locked(self) -> dict:
        """Build a complete serializable snapshot from mutable state.

        Must be called with ``self._state_lock`` held.
        """
        opponent_seat = self.opponent_seat_id

        players_list = []
        for p in self.players.values():
            p_dict = p.to_dict()
            p_dict["is_local"] = (p.seat_id == self.local_seat_id)
            players_list.append(p_dict)

        revealed = {}
        for seat_id, grp_ids in self.revealed_cards.items():
            revealed[seat_id] = list(grp_ids)

        return {
            "match_id": self.match_id,
            "local_seat_id": self.local_seat_id,
            "opponent_seat_id": opponent_seat,
            "turn_info": self.turn_info.to_dict(),
            "players": players_list,
            "zones": {
                "battlefield": [obj.to_dict() for obj in self.battlefield],
                "my_hand": [obj.to_dict() for obj in self.hand] if self.local_seat_id else [],
                "opponent_hand_count": len(self.get_objects_in_zone(ZoneType.HAND, opponent_seat)) if opponent_seat else 0,
                "stack": [obj.to_dict() for obj in self.stack],
                "graveyard": [obj.to_dict() for obj in self.graveyard],
                "exile": [obj.to_dict() for obj in self.get_objects_in_zone(ZoneType.EXILE)],
                "command": [obj.to_dict() for obj in self.command],
                "library_count": len(self.get_objects_in_zone(ZoneType.LIBRARY, self.local_seat_id)) if self.local_seat_id else "?",
            },
            "pending_decision": self.pending_decision,
            "decision_seat_id": self.decision_seat_id,
            "decision_context": self.decision_context,
            "last_cleared_decision": self.last_cleared_decision,
            "legal_actions": list(self.legal_actions),
            "legal_actions_raw": copy.deepcopy(self.legal_actions_raw),
            "pending_combat_steps": self._pending_combat_steps.copy(),
            "recent_events": self.recent_events[-10:],
            "damage_taken": dict(self.damage_taken),
            "revealed_cards": revealed,
            "last_game_result": self.last_game_result,
            "deck_cards": list(self.deck_cards),
            # ── Phase 1 turbo-charge fields ──
            "designations": {seat: list(desigs) for seat, desigs in self.designations.items()} if self.designations else {},
            "dungeon_status": dict(self.dungeon_status) if self.dungeon_status else {},
            "timer_state": dict(self.timer_state) if self.timer_state else {},
            "action_history": list(self.action_history[-20:]) if self.action_history else [],
            "sideboard_cards": list(self.sideboard_cards) if self.sideboard_cards else [],
        }

    def publish_snapshot(self) -> None:
        """Publish a consistent immutable snapshot for readers."""
        with self._state_lock:
            self._published_snapshot = self._build_raw_snapshot_locked()

    def get_published_snapshot(self, deep_copy: bool = True) -> dict:
        """Return the latest published snapshot.

        Args:
            deep_copy: When True (default), return a deep copy suitable for
                callers that may mutate data. When False, return a read-only
                reference for fast internal reads.
        """
        with self._state_lock:
            if deep_copy:
                return copy.deepcopy(self._published_snapshot)
            return self._published_snapshot

    def get_snapshot(self) -> dict:
        """Get a TUI-friendly snapshot with lazy card-name enrichment.

        Reads only from the published immutable snapshot to avoid mixed-frame
        reads while parser updates are in flight.
        """
        raw = self.get_published_snapshot()

        def enrich_obj(data: dict) -> dict:
            enriched = dict(data)
            grp_id = int(enriched.get("grp_id", 0) or 0)
            if grp_id:
                try:
                    from arenamcp import server
                    card_info = server.get_card_info(grp_id)
                    enriched["name"] = card_info.get("name", f"Unknown ({grp_id})")
                    enriched["type_line"] = card_info.get("type_line", "")
                    enriched["mana_cost"] = card_info.get("mana_cost", "")
                except Exception as e:
                    logger.debug(f"Card info lookup failed for grp_id={grp_id}: {e}")
                    enriched["name"] = f"Unknown ({grp_id})"
                    enriched["type_line"] = ""
            else:
                enriched.setdefault("name", "Unknown")
                enriched.setdefault("type_line", "")
            return enriched

        zones = raw.get("zones", {})
        raw["zones"] = {
            "battlefield": [enrich_obj(o) for o in zones.get("battlefield", [])],
            "my_hand": [enrich_obj(o) for o in zones.get("my_hand", [])],
            "opponent_hand_count": zones.get("opponent_hand_count", 0),
            "stack": [enrich_obj(o) for o in zones.get("stack", [])],
            "graveyard": [enrich_obj(o) for o in zones.get("graveyard", [])],
            "exile": [enrich_obj(o) for o in zones.get("exile", [])],
            "command": [enrich_obj(o) for o in zones.get("command", [])],
            "library_count": zones.get("library_count", "?"),
        }
        return raw

    def clear_pending_combat_steps(self) -> None:
        """Clear the pending combat steps after processing."""
        with self._state_lock:
            self._pending_combat_steps.clear()
            self._published_snapshot = self._build_raw_snapshot_locked()

    def _clear_action_window(self, reason: str) -> None:
        """Clear stale local action/priority state for a new game window."""
        ctx_type = (self.decision_context or {}).get("type", "")
        if ctx_type in {
            "actions_available",
            "declare_attackers",
            "declare_blockers",
            "assign_damage",
            "order_combat_damage",
        } and self.pending_decision:
            logger.info("Clearing stale decision '%s' (%s)", self.pending_decision, reason)
            self.last_cleared_decision = self.pending_decision
            self.pending_decision = None
            self.decision_seat_id = None
            self.decision_context = None
            self.decision_timestamp = 0

        if self.legal_actions or self.legal_actions_raw:
            logger.info(
                "Clearing stale legal actions (%d summarized, %d raw) (%s)",
                len(self.legal_actions),
                len(self.legal_actions_raw),
                reason,
            )
            self.legal_actions = []
            self.legal_actions_raw = []
    
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
        with self._state_lock:
            if source >= self._seat_source:
                self.local_seat_id = seat_id
                self._seat_source = source
                source_name = self.get_seat_source_name()
                logger.info(f"Set local_seat_id to {seat_id} (Source: {source_name})")
            else:
                logger.info(f"Ignored seat update to {seat_id} (Source {source} < Current {self._seat_source})")
            self._published_snapshot = self._build_raw_snapshot_locked()

    def reset_local_player(self, force: bool = False) -> None:
        """Reset local_seat_id logic.

        Args:
            force: If True, reset EVERYTHING (used for full restart).
                   If False, only reset INFERRED (1) sources.
                   System (2) and User (3) are preserved across game resets (e.g. BO3).
        """
        with self._state_lock:
            if force or self._seat_source <= 1:
                self.local_seat_id = None
                self._seat_source = 0
                logger.info("Reset local_seat_id (cleared)")
            else:
                logger.info(f"Preserving local_seat_id={self.local_seat_id} (Source: {self.get_seat_source_name()})")
            self._published_snapshot = self._build_raw_snapshot_locked()

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
        with self._state_lock:
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

            # Process gameInfo after players so team/status data is available.
            game_info = message.get("gameInfo")
            if game_info:
                self._process_game_info(game_info)

            # Process annotations (damage, counters, zone transfers, reveals, etc.)
            annotations = message.get("annotations", [])
            if annotations:
                self._process_annotations(annotations)
            persistent_annotations = message.get("persistentAnnotations", [])
            if persistent_annotations:
                self._process_annotations(persistent_annotations)

            # Clear untap step flag after processing all objects in this message
            self._in_untap_step = False

            # MEMORY OPTIMIZATION: Periodically clean up stale objects
            # Objects can accumulate when tokens die or cards are exiled from exile
            if len(self.game_objects) > 200:  # Only cleanup when dict gets large
                self._cleanup_stale_objects()

            self._published_snapshot = self._build_raw_snapshot_locked()

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

        # Track stack update time for delay-tolerant clearing
        if zone_type == ZoneType.STACK and object_instance_ids:
            self._last_stack_update_time = time.time()

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

        if "teamId" in player_data:
            team_id = player_data["teamId"]
        elif existing:
            team_id = existing.team_id
        else:
            team_id = None

        if "status" in player_data:
            status = player_data["status"]
        elif existing:
            status = existing.status
        else:
            status = ""

        player = Player(
            seat_id=seat_id,
            life_total=life_total,
            lands_played=lands_played,
            mana_pool=mana_pool,
            team_id=team_id,
            status=status,
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
        except Exception as e:
            logger.debug(f"Card name resolution failed for grp_id={grp_id}: {e}")
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
            if isinstance(ann_types, (str, int)):
                ann_types = [str(ann_types)]
            elif isinstance(ann_types, list):
                ann_types = [str(t) for t in ann_types]
            details = ann.get("details", [])
            affected_ids = ann.get("affectedIds", [])
            if isinstance(affected_ids, int):
                affected_ids = [affected_ids]
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
                    # Player took an action — also record in action history buffer
                    event = {
                        "type": "user_action",
                        "details": detail_map,
                        "affected_ids": affected_ids,
                    }
                    self._add_event(event)
                    # Build a concise action history entry
                    action_type = detail_map.get("actionType", "")
                    grp_id = detail_map.get("grpId", 0)
                    seat = detail_map.get("seatId", 0)
                    card_name = self._resolve_card_name(grp_id) if grp_id else ""
                    history_entry = {
                        "turn": self.turn_info.turn_number,
                        "phase": self.turn_info.phase,
                        "seat": seat,
                        "action": str(action_type).replace("ActionType_", "") if action_type else "unknown",
                        "card": card_name,
                    }
                    self.action_history.append(history_entry)
                    # Cap at 50 entries
                    if len(self.action_history) > 50:
                        self.action_history = self.action_history[-50:]

                elif ann_type == "AnnotationType_Scry":
                    self._add_event({
                        "type": "scry",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type in ("AnnotationType_LossOfGame", "AnnotationType_WinTheGame"):
                    result = self._resolve_end_annotation_locked(ann_type, affected_ids)
                    self._add_event({
                        "type": "game_end",
                        "result": result or "unknown",
                        "affected_ids": affected_ids,
                    })
                    # Persist result so it survives reset() for post-match analysis
                    self._record_match_result_locked(result, ann_type)

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

                # ── Phase 1 turbo-charge: new annotation handlers ──

                elif ann_type == "AnnotationType_TargetSpec":
                    # Spell/ability targeting — links source to target instance IDs
                    source_id = detail_map.get("sourceId", 0)
                    target_ids = detail_map.get("targetIds", affected_ids)
                    if isinstance(target_ids, int):
                        target_ids = [target_ids]
                    source_obj = self.game_objects.get(source_id)
                    if source_obj:
                        source_obj.targeting = list(target_ids)
                    target_names = []
                    for tid in target_ids:
                        tobj = self.game_objects.get(tid)
                        if tobj:
                            target_names.append(self._resolve_card_name(tobj.grp_id))
                        else:
                            # Target might be a player seat
                            target_names.append(f"Player#{tid}" if tid in self.players else f"#{tid}")
                    self._add_event({
                        "type": "target_spec",
                        "source": self._resolve_card_name(source_obj.grp_id) if source_obj else f"#{source_id}",
                        "source_id": source_id,
                        "targets": target_names,
                        "target_ids": list(target_ids),
                    })

                elif ann_type == "AnnotationType_PredictedDirectDamage":
                    # GRE's own combat damage prediction
                    self._add_event({
                        "type": "predicted_damage",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_ModifiedPower":
                    # Actual power after continuous effects
                    new_power = detail_map.get("value", detail_map.get("power"))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and new_power is not None:
                            obj.modified_power = int(new_power) if not isinstance(new_power, int) else new_power

                elif ann_type == "AnnotationType_ModifiedCost":
                    cost_str = detail_map.get("value", detail_map.get("cost", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and cost_str:
                            obj.modified_cost = str(cost_str)

                elif ann_type == "AnnotationType_ModifiedColor":
                    colors = detail_map.get("colors", detail_map.get("value", []))
                    if isinstance(colors, str):
                        colors = [colors]
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and colors:
                            obj.modified_colors = list(colors)

                elif ann_type == "AnnotationType_ModifiedType":
                    types = detail_map.get("types", detail_map.get("value", []))
                    if isinstance(types, str):
                        types = [types]
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and types:
                            obj.modified_types = list(types)

                elif ann_type == "AnnotationType_ModifiedName":
                    name = detail_map.get("value", detail_map.get("name", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and name:
                            obj.modified_name = str(name)

                elif ann_type == "AnnotationType_LayeredEffect":
                    # Active continuous effect (anthem, debuff, etc.)
                    self._add_event({
                        "type": "layered_effect",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type in ("AnnotationType_AddAbility", "AnnotationType_DynamicAbility"):
                    ability = detail_map.get("abilityGrpId", detail_map.get("ability", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and ability:
                            ability_str = str(ability)
                            if ability_str not in obj.granted_abilities:
                                obj.granted_abilities.append(ability_str)
                    self._add_event({
                        "type": "ability_added",
                        "affected_ids": affected_ids,
                        "ability": str(ability),
                    })

                elif ann_type == "AnnotationType_RemoveAbility":
                    ability = detail_map.get("abilityGrpId", detail_map.get("ability", ""))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and ability:
                            ability_str = str(ability)
                            if ability_str not in obj.removed_abilities:
                                obj.removed_abilities.append(ability_str)
                    self._add_event({
                        "type": "ability_removed",
                        "affected_ids": affected_ids,
                        "ability": str(ability),
                    })

                elif ann_type == "AnnotationType_DamagedThisTurn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.damaged_this_turn = True

                elif ann_type == "AnnotationType_CrewedThisTurn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.crewed_this_turn = True

                elif ann_type == "AnnotationType_SaddledThisTurn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.saddled_this_turn = True

                elif ann_type == "AnnotationType_PhasedOut":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.is_phased_out = True
                    self._add_event({
                        "type": "phased_out",
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_PhasedIn":
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.is_phased_out = False
                    self._add_event({
                        "type": "phased_in",
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_ClassLevel":
                    level = detail_map.get("level", detail_map.get("value", 1))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj:
                            obj.class_level = int(level) if not isinstance(level, int) else level
                    self._add_event({
                        "type": "class_level",
                        "affected_ids": affected_ids,
                        "level": level,
                    })

                elif ann_type == "AnnotationType_DungeonStatus":
                    dungeon = detail_map.get("dungeon", "")
                    room = detail_map.get("room", "")
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        owner = obj.owner_seat_id if obj else (affected_ids[0] if affected_ids else 0)
                        if owner:
                            self.dungeon_status[owner] = {"dungeon": dungeon, "room": room}
                    self._add_event({
                        "type": "dungeon_status",
                        "dungeon": dungeon,
                        "room": room,
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_SuspendLike":
                    # Cards in exile with time counters (suspend, foretell, etc.)
                    self._add_event({
                        "type": "suspend_like",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type in ("AnnotationType_LinkedDamage", "AnnotationType_DamageSource"):
                    # Damage attribution — which source dealt what
                    source_id = detail_map.get("sourceId", 0)
                    source_obj = self.game_objects.get(source_id)
                    self._add_event({
                        "type": "damage_attribution",
                        "sub_type": ann_type.replace("AnnotationType_", ""),
                        "source": self._resolve_card_name(source_obj.grp_id) if source_obj else f"#{source_id}",
                        "source_id": source_id,
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_SupplementalText":
                    self._add_event({
                        "type": "supplemental_text",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_ColorProduction":
                    colors = detail_map.get("colors", detail_map.get("value", []))
                    if isinstance(colors, str):
                        colors = [colors]
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and colors:
                            obj.color_production = list(colors)

                elif ann_type == "AnnotationType_CopiedObject":
                    source_grp = detail_map.get("sourceGrpId", detail_map.get("grpId", 0))
                    for obj_id in affected_ids:
                        obj = self.game_objects.get(obj_id)
                        if obj and source_grp:
                            obj.copied_from_grp_id = int(source_grp)
                    self._add_event({
                        "type": "copied_object",
                        "affected_ids": affected_ids,
                        "source_grp_id": source_grp,
                    })

                elif ann_type in ("AnnotationType_Designation", "AnnotationType_GainDesignation"):
                    designation = detail_map.get("designation", detail_map.get("value", ""))
                    for obj_id in affected_ids:
                        # Designations apply to players (seat IDs)
                        seat = obj_id if obj_id in self.players else None
                        if seat is None:
                            obj = self.game_objects.get(obj_id)
                            seat = obj.controller_seat_id if obj else None
                        if seat:
                            if seat not in self.designations:
                                self.designations[seat] = set()
                            self.designations[seat].add(str(designation))
                    self._add_event({
                        "type": "designation_gained",
                        "designation": designation,
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_LoseDesignation":
                    designation = detail_map.get("designation", detail_map.get("value", ""))
                    for obj_id in affected_ids:
                        seat = obj_id if obj_id in self.players else None
                        if seat is None:
                            obj = self.game_objects.get(obj_id)
                            seat = obj.controller_seat_id if obj else None
                        if seat and seat in self.designations:
                            self.designations[seat].discard(str(designation))
                    self._add_event({
                        "type": "designation_lost",
                        "designation": designation,
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_BoonInfo":
                    self._add_event({
                        "type": "boon",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_Vote":
                    self._add_event({
                        "type": "vote",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_Shuffle":
                    self._add_event({
                        "type": "shuffle",
                        "affected_ids": affected_ids,
                    })

                elif ann_type == "AnnotationType_DieRoll":
                    self._add_event({
                        "type": "die_roll",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_ObjectIdChanged":
                    self._add_event({
                        "type": "object_id_changed",
                        "affected_ids": affected_ids,
                        "details": detail_map,
                    })

                elif ann_type == "AnnotationType_NewTurnStarted":
                    # Reset turn-specific flags on all game objects
                    for obj in self.game_objects.values():
                        obj.damaged_this_turn = False
                        obj.crewed_this_turn = False
                        obj.saddled_this_turn = False
                        obj.targeting.clear()

                elif ann_type in (
                    "AnnotationType_None",
                    "AnnotationType_Attachment",
                    "AnnotationType_ObjectsSelected",
                    "AnnotationType_PendingEffect",
                    "AnnotationType_Qualification",
                    "AnnotationType_Haunt",
                    "AnnotationType_LoopCount",
                    "AnnotationType_GroupedIds",
                    "AnnotationType_SyntheticEvent",
                    "AnnotationType_TurnPermanent",
                    "AnnotationType_LinkInfo",
                    "AnnotationType_CopyException",
                    "AnnotationType_AbilityExhausted",
                    "AnnotationType_ManaDetails",
                    "AnnotationType_RemoveAttachment",
                    "AnnotationType_ShouldntPlay",
                    "AnnotationType_TextChange",
                    "AnnotationType_AssignDamageConfirmation",
                ):
                    # Known but not actionable for coaching — skip silently
                    pass

                else:
                    logger.debug("Unhandled annotation type: %s (affected: %s, details: %s)",
                                 ann_type, affected_ids, detail_map)

    def _update_turn_info(self, turn_data: dict) -> None:
        """Update turn info from message data.

        Args:
            turn_data: TurnInfo dict from GameStateMessage.
        """
        prev_turn = self.turn_info.turn_number
        prev_active = self.turn_info.active_player
        prev_priority = self.turn_info.priority_player
        prev_phase = self.turn_info.phase
        prev_step = self.turn_info.step

        new_turn = turn_data.get("turnNumber", prev_turn)

        # Detect new game: turn number resets to 1 or decreases significantly
        if prev_turn > 3 and new_turn <= 1:
            logger.info(f"New game detected (turn {prev_turn} -> {new_turn}) - Performing Search & Destroy on old state.")
            
            # FULL RESET of all zones, objects, players
            self.reset()
            
            # reset() makes turn_number 0, which is fine as we overwrite it below

        # Check if active player is explicitly in the update
        explicit_active = "activePlayer" in turn_data
        
        if new_turn != prev_turn:
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
                other_seat = self.opponent_seat_id if prev_active == self.local_seat_id else self.local_seat_id
                if other_seat is not None and prev_active != 0:
                    new_active = other_seat
                    logger.info(f"Turn change ({prev_turn}->{new_turn}) without activePlayer. Inferred active_player={new_active} (alternating from {prev_active}).")
                elif prev_active != 0:
                    # Can't determine other seat but have a valid previous value — keep it
                    new_active = prev_active
                    logger.warning(f"Turn change ({prev_turn}->{new_turn}) without activePlayer. Keeping previous active_player={prev_active} (opponent seat unknown).")
                else:
                    # Previous was already 0 (unknown) — nothing to alternate from.
                    # Guess local player (turn 1 is usually the play-first player,
                    # but any value > 0 is better than 0).
                    new_active = self.local_seat_id or 0
                    logger.warning(f"Turn change ({prev_turn}->{new_turn}) without activePlayer. Previous was 0, guessing local_seat={new_active}.")
        else:
            # Turn didn't change, use update or keep existing
            new_active = turn_data.get("activePlayer", prev_active)

        # Clear stale pending combat steps when turn or active player changes
        # Must check BEFORE updating turn_info
        if new_turn != prev_turn or new_active != prev_active:
            if self._pending_combat_steps:
                logger.debug(f"Clearing {len(self._pending_combat_steps)} stale pending combat steps (turn/active changed)")
                self._pending_combat_steps.clear()
        
        # UNTAP STEP: When turn changes, untap all permanents controlled by the new active player.
        # Skip permanents in _untap_prevention — those that MTGA explicitly kept tapped last turn
        # (e.g. creatures with "can't become untapped" from Blossombind-style effects).
        # After blanket untap, _in_untap_step is set so that object diffs in this same message
        # can update _untap_prevention for the NEXT turn's blanket untap.
        if new_turn != prev_turn and new_active != 0:
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
        if new_turn != prev_turn:
            for player in self.players.values():
                player.lands_played = 0

        turn_changed = new_turn != prev_turn
        new_priority = turn_data.get("priorityPlayer", prev_priority)
        self.turn_info.turn_number = new_turn
        self.turn_info.active_player = new_active
        self.turn_info.priority_player = new_priority
        if turn_changed:
            # Clear stale stack entries on turn change (forced — stack is always
            # empty at turn boundaries in Magic).
            self._clear_stale_stack(force=True)
            # Turn changed, reset phase/step if not provided (prevent stale phase leakage)
            if "phase" in turn_data:
                new_phase = turn_data["phase"]
            else:
                new_phase = "Phase_Beginning"
                logger.debug(f"Turn change ({prev_turn}->{new_turn}) without phase. Resetting to Phase_Beginning.")
            
            if "step" in turn_data:
                new_step = turn_data["step"]
            else:
                new_step = "Step_Untap"
        else:
            # Same turn, preserve existing if missing
            new_phase = turn_data.get("phase", prev_phase)
            new_step = turn_data.get("step", prev_step)

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
        if new_phase != prev_phase:
            logger.debug(f"Phase change: {prev_phase} -> {new_phase}")
            # Clear stale stack on phase transitions to Main phases.
            # The stack must be empty before any phase change in Magic.
            # Main phases are when advice matters most (cast spells, play lands).
            # Use grace period: if the stack was just updated, defer clearing
            # to avoid discarding real entries from delayed log writes.
            if "Main" in new_phase:
                self._clear_stale_stack(force=False)
            # If step is not explicitly in the update AND we didn't just reset it above,
            # we should clear it to avoid stale steps (e.g. Step_Draw in Phase_Main1)
            # But if we just set it to Untap above, keep it.
            if "step" not in turn_data and new_turn == prev_turn:
                new_step = ""
                logger.debug("Resetting step due to phase change")

        # Track combat steps as they happen (for event-driven triggers)
        if "Combat" in new_phase and new_step != prev_step:
            if "DeclareAttack" in new_step or "DeclareBlock" in new_step:
                # Store step with active player info for trigger generation
                self._pending_combat_steps.append({
                    "step": new_step,
                    "active_player": self.turn_info.active_player,
                    "turn": new_turn
                })
                self._last_combat_step_time = time.time()
                logger.info(f"Queued combat step: {new_step} (active_player={self.turn_info.active_player})")

        turn_window_changed = any((
            new_turn != prev_turn,
            new_active != prev_active,
            new_priority != prev_priority,
            new_phase != prev_phase,
            new_step != prev_step,
        ))
        if turn_window_changed:
            self._clear_action_window(
                reason=(
                    f"window change to turn={new_turn} active={new_active} "
                    f"priority={new_priority} phase={new_phase} step={new_step or '-'}"
                )
            )

        self.turn_info.phase = new_phase
        self.turn_info.step = new_step
        logger.debug(f"Updated turn info: turn {self.turn_info.turn_number}, phase {self.turn_info.phase}, step {self.turn_info.step}")


def _auto_clear_stale_decision(game_state: GameState) -> bool:
    """Auto-clear stale decisions whose source is no longer on the stack.

    Returns True if the decision was cleared (snapshot_dirty should be set).
    """
    MIN_DECISION_HOLD_S = 5
    _decision_type = (game_state.decision_context or {}).get("type", "")
    _skip_auto_clear = _decision_type in ("actions_available", "pay_costs")
    if not (game_state.pending_decision
            and game_state.pending_decision != "Mulligan"
            and game_state.decision_context
            and not _skip_auto_clear):
        return False

    import time as _time
    decision_age = (
        _time.time() - game_state.decision_timestamp
        if game_state.decision_timestamp else 999
    )
    source_id = game_state.decision_context.get("source_id")
    should_clear = False
    if source_id is not None:
        still_on_stack = any(
            obj.instance_id == source_id for obj in game_state.stack
        )
        if not still_on_stack:
            if decision_age >= MIN_DECISION_HOLD_S:
                should_clear = True
                logger.info(
                    f"Auto-clearing stale decision '{game_state.pending_decision}' "
                    f"(source {source_id} no longer on stack, age={decision_age:.1f}s)"
                )
            else:
                logger.debug(
                    f"Decision '{game_state.pending_decision}' source left stack "
                    f"but holding ({decision_age:.1f}s < {MIN_DECISION_HOLD_S}s)"
                )
    elif game_state.decision_timestamp:
        is_busy = (
            "Combat" in game_state.turn_info.phase
            or len(game_state.stack) > 0
        )
        no_source_timeout = 25 if is_busy else 15
        if decision_age > no_source_timeout:
            should_clear = True
            logger.info(
                f"Auto-clearing stale decision '{game_state.pending_decision}' "
                f"(no source_id, age={decision_age:.0f}s, "
                f"timeout={no_source_timeout}s)"
            )
    if should_clear:
        game_state.pending_decision = None
        game_state.decision_seat_id = None
        game_state.decision_context = None
        game_state.decision_timestamp = 0
        return True
    return False


def _set_simple_decision(game_state: GameState, label: str, dec_type: str,
                         raw: Optional[dict] = None) -> None:
    """Set a simple pending decision with optional raw data."""
    import time as _time
    logger.info(f"Captured Decision: {label}")
    game_state.pending_decision = label
    game_state.decision_timestamp = _time.time()
    ctx: dict[str, Any] = {"type": dec_type}
    if raw is not None:
        ctx["raw"] = raw
    game_state.decision_context = ctx


def _handle_decision_message(game_state: GameState, msg_type: str,
                             msg: dict) -> bool:
    """Handle a GRE decision message. Returns True if snapshot_dirty should be set."""
    import time as _time

    if msg_type in ("GREMessageType_MulliganReq", "GREMessageType_SubmitDeckReq"):
        logger.info(f"Captured Decision: Mulligan Check ({msg_type})")
        if game_state.turn_info.turn_number > 1:
            logger.info(f"Mulligan Request detected at Turn {game_state.turn_info.turn_number} -> Resetting Ghost State.")
            game_state.reset()
        # ── Phase 1: Extract sideboard from SubmitDeckReq (BO3 between games) ──
        if msg_type == "GREMessageType_SubmitDeckReq":
            submit_req = msg.get("submitDeckReq", {})
            deck_data = submit_req.get("deck", {})
            sideboard = deck_data.get("sideboardCards", deck_data.get("sideboard", []))
            if sideboard:
                game_state.sideboard_cards = list(sideboard)
                logger.info(f"Captured sideboard: {len(sideboard)} cards")
        game_state.pending_decision = "Mulligan"
        game_state.decision_seat_id = game_state.local_seat_id
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {"type": "mulligan"}
        return False

    elif msg_type == "GREMessageType_IntermissionReq":
        logger.info(f"IntermissionReq received (turn {game_state.turn_info.turn_number}) - game over/transition")
        intermission_result = msg.get("intermissionReq", {}).get("result")
        if intermission_result:
            game_state.set_result_from_payload(intermission_result, "IntermissionReq")
        game_state.prepare_for_game_end()
        game_state.reset()
        game_state.pending_decision = None
        game_state.decision_seat_id = None
        game_state.decision_context = None
        game_state.decision_timestamp = 0
        return False

    elif msg_type == "GREMessageType_PromptReq":
        prompt_text = msg.get("promptReq", {}).get("prompt", {}).get("text", "Action Required")
        logger.info(f"Captured Decision: Prompt ({prompt_text})")
        if game_state.pending_decision == "Mulligan":
            logger.info("Skipping PromptReq — Mulligan decision already active")
        else:
            game_state.pending_decision = prompt_text
            game_state.decision_timestamp = _time.time()
            game_state.decision_context = {"type": "prompt", "text": prompt_text}
        return False

    elif msg_type == "GREMessageType_SelectTargetsReq":
        req = msg.get("selectTargetsReq", {})
        source_id = req.get("sourceId")
        source_card = None
        if source_id:
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
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "target_selection",
            "source_card": source_card,
            "source_id": source_id,
        }
        return False

    elif msg_type == "GREMessageType_SelectNReq":
        return _handle_select_n_req(game_state, msg)

    elif msg_type == "GREMessageType_GroupReq":
        req = msg.get("groupReq", {})
        if game_state.turn_info.turn_number < 1:
            logger.info(f"Captured Decision: Mulligan Bottom Cards ({msg_type})")
            game_state.pending_decision = "Mulligan Bottom"
            game_state.decision_seat_id = game_state.local_seat_id
            game_state.decision_timestamp = _time.time()
            game_state.decision_context = {"type": "mulligan_bottom", "raw": dict(req)}
        else:
            _set_simple_decision(game_state, "Group Selection", "group_selection", dict(req))
        return False

    elif msg_type == "GREMessageType_GroupOptionReq":
        req = msg.get("groupOptionReq", {})
        options = req.get("options", [])
        logger.info(f"Captured Decision: Choose Mode ({len(options)} options)")
        game_state.pending_decision = "Choose Mode"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {"type": "modal_choice", "num_options": len(options), "options": options}
        return False

    elif msg_type == "GREMessageType_ActionsAvailableReq":
        return _handle_actions_available(game_state, msg)

    elif msg_type == "GREMessageType_ConnectResp":
        connect_resp = msg.get("connectResp", {})
        deck_cards = connect_resp.get("deckMessage", {}).get("deckCards", [])
        if deck_cards:
            game_state.deck_cards = deck_cards
            logger.info(f"Captured deck list from ConnectResp: {len(deck_cards)} cards")
        return False

    elif msg_type == "GREMessageType_DeclareAttackersReq":
        req = msg.get("declareAttackersReq", {})
        legal_attackers = req.get("attackers", req.get("qualifiedAttackers", []))
        attacker_names, attacker_ids = [], []
        for atk in legal_attackers:
            obj_id = atk if isinstance(atk, int) else atk.get("instanceId", atk.get("attackerInstanceId", 0))
            obj = game_state.game_objects.get(obj_id)
            if obj:
                attacker_names.append(game_state._resolve_card_name(obj.grp_id))
                attacker_ids.append(obj_id)
        logger.info(f"Captured Decision: Declare Attackers ({len(attacker_names)} legal)")
        game_state.pending_decision = "Declare Attackers"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "declare_attackers", "legal_attackers": attacker_names,
            "legal_attacker_ids": attacker_ids, "raw_attackers": legal_attackers,
        }
        return False

    elif msg_type == "GREMessageType_DeclareBlockersReq":
        req = msg.get("declareBlockersReq", {})
        legal_blockers = req.get("blockers", req.get("qualifiedBlockers", []))
        blocker_names, blocker_ids = [], []
        for blk in legal_blockers:
            obj_id = blk if isinstance(blk, int) else blk.get("instanceId", blk.get("blockerInstanceId", 0))
            obj = game_state.game_objects.get(obj_id)
            if obj:
                blocker_names.append(game_state._resolve_card_name(obj.grp_id))
                blocker_ids.append(obj_id)
        logger.info(f"Captured Decision: Declare Blockers ({len(blocker_names)} legal)")
        game_state.pending_decision = "Declare Blockers"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "declare_blockers", "legal_blockers": blocker_names,
            "legal_blocker_ids": blocker_ids, "raw_blockers": legal_blockers,
        }
        return False

    elif msg_type == "GREMessageType_AssignDamageReq":
        _set_simple_decision(game_state, "Assign Damage", "assign_damage",
                             dict(msg.get("assignDamageReq", {})))
        return False

    elif msg_type == "GREMessageType_OrderCombatDamageReq":
        req = msg.get("orderCombatDamageReq", msg.get("orderDamageReq", {}))
        _set_simple_decision(game_state, "Order Combat Damage", "order_combat_damage", dict(req))
        return False

    elif msg_type == "GREMessageType_PayCostsReq":
        return _handle_pay_costs(game_state, msg)

    elif msg_type == "GREMessageType_SearchReq":
        req = msg.get("searchReq", {})
        logger.info(f"Captured Decision: Search (zone {req.get('zoneId', 0)})")
        game_state.pending_decision = "Search Library"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {"type": "search", "zone_id": req.get("zoneId", 0), "raw": dict(req)}
        return False

    elif msg_type == "GREMessageType_DistributionReq":
        req = msg.get("distributionReq", {})
        total = req.get("amount", req.get("total", 0))
        source_id = req.get("sourceId", 0)
        source_obj = game_state.game_objects.get(source_id)
        source_name = game_state._resolve_card_name(source_obj.grp_id) if source_obj else "Unknown"
        logger.info(f"Captured Decision: Distribute {total} (source: {source_name})")
        game_state.pending_decision = "Distribute"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "distribution", "source_card": source_name,
            "source_id": source_id, "total": total, "raw": dict(req),
        }
        return False

    elif msg_type == "GREMessageType_NumericInputReq":
        req = msg.get("numericInputReq", {})
        source_id = req.get("sourceId", 0)
        source_obj = game_state.game_objects.get(source_id)
        source_name = game_state._resolve_card_name(source_obj.grp_id) if source_obj else "Unknown"
        logger.info(f"Captured Decision: Numeric Input for {source_name} ({req.get('min', 0)}-{req.get('max', 0)})")
        game_state.pending_decision = "Choose Number"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "numeric_input", "source_card": source_name,
            "source_id": source_id, "min": req.get("min", 0), "max": req.get("max", 0),
        }
        return False

    elif msg_type == "GREMessageType_ChooseStartingPlayerReq":
        _set_simple_decision(game_state, "Choose Play/Draw", "choose_starting_player")
        return False

    elif msg_type == "GREMessageType_SelectReplacementReq":
        _set_simple_decision(game_state, "Select Replacement", "select_replacement",
                             dict(msg.get("selectReplacementReq", {})))
        return False

    elif msg_type == "GREMessageType_SelectNGroupReq":
        _set_simple_decision(game_state, "Select from Group", "select_n_group",
                             dict(msg.get("selectNGroupReq", {})))
        return False

    elif msg_type == "GREMessageType_SelectFromGroupsReq":
        _set_simple_decision(game_state, "Select from Groups", "select_from_groups",
                             dict(msg.get("selectFromGroupsReq", {})))
        return False

    elif msg_type == "GREMessageType_SearchFromGroupsReq":
        _set_simple_decision(game_state, "Search from Groups", "search_from_groups",
                             dict(msg.get("searchFromGroupsReq", {})))
        return False

    elif msg_type == "GREMessageType_CastingTimeOptionsReq":
        _set_simple_decision(game_state, "Choose Casting Option", "casting_time_options",
                             dict(msg.get("castingTimeOptionsReq", {})))
        return False

    elif msg_type == "GREMessageType_SelectCountersReq":
        _set_simple_decision(game_state, "Select Counters", "select_counters",
                             dict(msg.get("selectCountersReq", {})))
        return False

    elif msg_type == "GREMessageType_RevealHandReq":
        req = msg.get("revealHandReq", {})
        logger.info(f"Hand reveal for seats: {req.get('systemSeatIds', [])}")
        return False

    elif msg_type == "GREMessageType_OrderReq":
        _set_simple_decision(game_state, "Order Triggers", "order_triggers",
                             dict(msg.get("orderReq", {})))
        return False

    elif msg_type == "GREMessageType_GatherReq":
        _set_simple_decision(game_state, "Gather", "gather",
                             dict(msg.get("gatherReq", {})))
        return False

    elif msg_type == "GREMessageType_OptionalActionMessage":
        opt = msg.get("optionalActionMessage", msg.get("prompt", {}))
        prompt_text = ""
        if isinstance(opt, dict):
            prompt_text = (
                opt.get("prompt", {}).get("text", "")
                if isinstance(opt.get("prompt"), dict)
                else str(opt.get("prompt", ""))
            )
        logger.info(f"Captured Decision: Optional Action ({prompt_text[:60]})")
        game_state.pending_decision = "Optional Action"
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "optional_action", "prompt": prompt_text,
            "raw": {k: v for k, v in msg.items() if k != "type"},
        }
        return False

    return False  # Unhandled message type


def _handle_select_n_req(game_state: GameState, msg: dict) -> bool:
    """Handle SelectNReq messages (scry, discard, choose creature, etc.)."""
    import time as _time
    req = msg.get("selectNReq", {})
    context_data = req.get("context", {})
    num_to_select = req.get("count", 1)
    min_select = req.get("minCount", num_to_select)
    max_select = req.get("maxCount", num_to_select)
    option_ids = req.get("ids", [])

    context_str = str(context_data).lower()
    prior_prompt = ""
    if (game_state.pending_decision
            and game_state.decision_context
            and game_state.decision_context.get("type") == "prompt"):
        prior_prompt = game_state.decision_context.get("text", "").lower()
        context_str = f"{context_str} {prior_prompt}"

    # Determine selection type
    _type_map = [
        ("discard", "discard", "Discard"), ("sacrifice", "sacrifice", "Sacrifice"),
        ("exile", "exile", "Exile"), ("destroy", "destroy", "Destroy"),
        ("return", "return", "Return"), ("scry", "scry", "Scry"),
        ("surveil", "surveil", "Surveil"), ("mill", "mill", "Mill"),
        ("explore", "explore", "Explore"), ("creature", "choose_creature", "Choose Creature"),
        ("land", "choose_land", "Choose Land"), ("enchantment", "choose_enchantment", "Choose Enchantment"),
        ("artifact", "choose_artifact", "Choose Artifact"), ("permanent", "choose_permanent", "Choose Permanent"),
        ("choose", "choose", "Choose"),
    ]
    selection_type = "select_n"
    decision_text = "Select Items"
    for keyword, sel_type, dec_text in _type_map:
        if keyword in context_str:
            selection_type = sel_type
            decision_text = dec_text
            break
    else:
        if prior_prompt:
            decision_text = game_state.decision_context.get("text", "Select Items")

    # Resolve option IDs to card names
    option_cards: list[str] = []
    for oid in option_ids[:20]:
        obj = game_state.game_objects.get(oid)
        if obj and obj.grp_id:
            try:
                from arenamcp import server
                info = server.get_card_info(obj.grp_id)
                option_cards.append(info.get("name", f"Card#{obj.grp_id}"))
            except Exception:
                option_cards.append(f"Card#{obj.grp_id}")

    logger.info(f"Captured Decision: {decision_text} ({num_to_select} items, type={selection_type}, options={option_cards or option_ids[:5]})")
    game_state.pending_decision = decision_text
    game_state.decision_timestamp = _time.time()
    game_state.decision_context = {
        "type": selection_type, "count": num_to_select,
        "min": min_select, "max": max_select,
        "context_raw": context_data, "prior_prompt": prior_prompt or None,
        "option_cards": option_cards or None,
    }
    return False


def _handle_actions_available(game_state: GameState, msg: dict) -> bool:
    """Handle ActionsAvailableReq messages. Returns True if snapshot_dirty."""
    import time as _time
    req = msg.get("actionsAvailableReq", {})
    raw_actions = req.get("actions", [])

    # ── Phase 1: Enrich raw actions with AutoTap + ability metadata ──
    enriched_actions = []
    for action in raw_actions:
        enriched = copy.deepcopy(action)
        # Extract AutoTap castability — the game engine's own mana solver
        autotap = action.get("autoTapSolution")
        if autotap is not None:
            enriched["_castable"] = True
            # Parse tap actions if present
            tap_actions = autotap.get("autoTapActions", [])
            if tap_actions:
                enriched["_autotap_lands"] = [
                    {"instanceId": ta.get("instanceId"), "mana": ta.get("manaProduced", "")}
                    for ta in tap_actions
                ]
        elif action.get("actionType") in ("ActionType_Cast", "ActionType_Activate"):
            # No autotap solution but it's a spell/ability — may need manual mana
            enriched["_castable"] = action.get("assumeCanBePaidFor", False)

        # Extract ability metadata
        if action.get("abilityGrpId"):
            enriched["_ability_grp_id"] = action["abilityGrpId"]
        if action.get("sourceId"):
            enriched["_source_id"] = action["sourceId"]
        if action.get("alternativeGrpId"):
            enriched["_alternative_grp_id"] = action["alternativeGrpId"]

        # Mana cost structured extraction
        mana_cost = action.get("manaCost", [])
        if mana_cost:
            _MANA_ABBREV = {
                "ManaColor_White": "W", "ManaColor_Blue": "U",
                "ManaColor_Black": "B", "ManaColor_Red": "R",
                "ManaColor_Green": "G", "ManaColor_Colorless": "C",
                "ManaColor_Any": "X",
            }
            cost_parts = []
            for mc in mana_cost:
                colors = mc.get("color", [])
                count = mc.get("count", 1)
                if colors:
                    symbols = [_MANA_ABBREV.get(c, c) for c in colors]
                    cost_parts.append(f"{count}{''.join(symbols)}")
                else:
                    cost_parts.append(str(count))
            enriched["_mana_cost_str"] = "".join(cost_parts)

        enriched_actions.append(enriched)

    game_state.legal_actions_raw = enriched_actions

    legal_list = []
    for action in raw_actions:
        atype = action.get("actionType", "")
        if atype == "ActionType_Pass":
            legal_list.append("Pass")
        elif atype == "ActionType_Play":
            name = "Land"
            if action.get("grpId"):
                try:
                    from arenamcp import server
                    info = server.get_card_info(action["grpId"])
                    name = info.get("name", "Land")
                except Exception: pass
            legal_list.append(f"Play Land: {name}")
        elif atype == "ActionType_Cast":
            name = "Spell"
            if action.get("grpId"):
                try:
                    from arenamcp import server
                    info = server.get_card_info(action["grpId"])
                    name = info.get("name", "Spell")
                except Exception: pass
            # Include castability from AutoTap
            castable = action.get("autoTapSolution") is not None
            suffix = " [OK]" if castable else ""
            legal_list.append(f"Cast {name}{suffix}")
        elif atype == "ActionType_Activate":
            name = ""
            if action.get("grpId"):
                try:
                    from arenamcp import server
                    info = server.get_card_info(action["grpId"])
                    name = info.get("name", "")
                except Exception: pass
            if name:
                legal_list.append(f"Activate Ability: {name}")
            else:
                legal_list.append("Activate Ability")
        else:
            legal_list.append(f"Action: {atype.replace('ActionType_', '')}")

    if legal_list:
        game_state.legal_actions = legal_list
        logger.info(f"Captured {len(legal_list)} legal actions from GRE: {legal_list}")

    # Correct priority/active player
    if raw_actions and game_state.local_seat_id is not None:
        local = game_state.local_seat_id
        action_types = {a.get("actionType", "") for a in raw_actions}
        # Only correct active_player when GRE offers ActionType_Play (land drop).
        # Land drops are strictly sorcery-speed and main-phase-only, so they
        # reliably indicate it's our turn.  ActionType_Cast is NOT reliable
        # because the GRE sends it for instants too (e.g. responding to an
        # opponent's spell on THEIR turn).  Using Cast here caused bug #49:
        # the system flipped active_player to local when we only had instant-
        # speed responses, making the coach suggest sorceries on opp's turn.
        has_land_play = "ActionType_Play" in action_types
        stack_empty = not game_state.get_objects_in_zone(ZoneType.STACK)
        if has_land_play and stack_empty:
            if game_state.turn_info.active_player != local:
                logger.warning(
                    f"Active player correction: GRE offered land play "
                    f"but active_player={game_state.turn_info.active_player} "
                    f"(local={local}). Correcting to {local}."
                )
                game_state.turn_info.active_player = local
        if game_state.turn_info.priority_player != local:
            game_state.turn_info.priority_player = local

    if raw_actions:
        action_types = {a.get("actionType", "") for a in raw_actions}
        if action_types == {"ActionType_Pass"}:
            decision_label = "Priority (Pass Only)"
        elif "ActionType_Cast" in action_types or "ActionType_Play" in action_types:
            decision_label = "Priority"
        elif "ActionType_AttackWithGroup" in action_types or "ActionType_Attack" in action_types:
            decision_label = "Declare Attackers"
        elif "ActionType_BlockWithGroup" in action_types or "ActionType_Block" in action_types:
            decision_label = "Declare Blockers"
        else:
            decision_label = "Choose Action"
        game_state.pending_decision = decision_label
        game_state.decision_timestamp = _time.time()
        game_state.decision_context = {
            "type": "actions_available",
            "num_actions": len(raw_actions),
            "action_types": sorted(action_types),
        }
        return True
    return False


def _handle_pay_costs(game_state: GameState, msg: dict) -> bool:
    """Handle PayCostsReq messages."""
    import time as _time
    req = msg.get("payCostsReq", {})
    mana_cost = req.get("manaCost", [])
    source_id = 0
    for mc in mana_cost:
        oid = mc.get("objectId", 0)
        if oid:
            source_id = oid
            break
    source_obj = game_state.game_objects.get(source_id) if source_id else None
    source_name = game_state._resolve_card_name(source_obj.grp_id) if source_obj else "Unknown"

    _MANA_ABBREV = {
        "ManaColor_White": "W", "ManaColor_Blue": "U",
        "ManaColor_Black": "B", "ManaColor_Red": "R",
        "ManaColor_Green": "G", "ManaColor_Colorless": "C",
        "ManaColor_Any": "Any",
    }
    mana_parts = []
    for mc in mana_cost:
        colors = mc.get("color", [])
        count = mc.get("count", 1)
        if colors:
            symbols = [_MANA_ABBREV.get(c, c) for c in colors]
            mana_parts.append(f"{count}x{''.join(symbols)}")
        else:
            mana_parts.append(f"{count}")
    mana_str = ", ".join(mana_parts) if mana_parts else "unknown"

    # ── Phase 1: Extract full AutoTap solution ──
    autotap_req = req.get("autoTapActionsReq", {})
    autotap_solutions = autotap_req.get("autoTapSolutions", [])
    has_autotap = bool(autotap_solutions)
    autotap_info = None
    if has_autotap and isinstance(autotap_solutions, list) and autotap_solutions:
        first_solution = autotap_solutions[0]
        tap_actions = first_solution.get("autoTapActions", [])
        autotap_info = {
            "lands_to_tap": [
                {"instanceId": ta.get("instanceId"), "mana": ta.get("manaProduced", "")}
                for ta in tap_actions
            ],
            "num_lands": len(tap_actions),
        }

    logger.info(f"Captured Decision: Pay Costs (source: {source_name}, mana: {mana_str}, autotap={has_autotap})")
    game_state.pending_decision = "Pay Costs"
    game_state.decision_timestamp = _time.time()
    game_state.decision_context = {
        "type": "pay_costs", "source_card": source_name,
        "source_id": source_id if source_id else None,
        "mana_cost": mana_str, "has_autotap": has_autotap,
        "autotap_solution": autotap_info,
    }
    return False


# Known decision Req types handled by _handle_decision_message
_KNOWN_REQ_TYPES = frozenset([
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
    "GREMessageType_ConnectResp", "GREMessageType_OptionalActionMessage",
])

_DECISION_RESPONSE_TYPES = frozenset([
    "GREMessageType_SelectTargetsResp", "GREMessageType_SubmitTargetsResp",
    "GREMessageType_SelectNResp", "GREMessageType_GroupOptionResp",
    "GREMessageType_GroupResp", "GREMessageType_OptionalActionResp",
    "GREMessageType_SubmitDeckResp", "GREMessageType_PromptResp",
    "GREMessageType_SubmitAttackersResp", "GREMessageType_SubmitBlockersResp",
    "GREMessageType_AssignDamageConfirmation", "GREMessageType_OrderDamageConfirmation",
])


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
        snapshot_dirty = False

        def apply_game_state_update(msg_payload: dict) -> None:
            """Apply a state diff/full update and publish exactly once."""
            nonlocal snapshot_dirty
            game_state.update_from_message(msg_payload)
            snapshot_dirty = False

        for msg in messages:
            msg_type = msg.get("type", "")
            seat_hint = msg.get("systemSeatId")
            if seat_hint is None:
                system_seat_ids = msg.get("systemSeatIds", [])
                if isinstance(system_seat_ids, list) and len(system_seat_ids) == 1:
                    seat_hint = system_seat_ids[0]
            if (
                isinstance(seat_hint, int)
                and (
                    game_state.local_seat_id != seat_hint
                    or getattr(game_state, "_seat_source", 0) < 2
                )
            ):
                game_state.set_local_seat_id(seat_hint, source=2)

            if (
                msg_type.endswith("Req")
                or msg_type in ("GREMessageType_ConnectResp", "GREMessageType_OptionalActionMessage")
                or ("Resp" in msg_type and game_state.pending_decision)
            ):
                snapshot_dirty = True

            # Handle GameStateMessage
            if msg_type == "GREMessageType_GameStateMessage":
                game_state_msg = msg.get("gameStateMessage")
                if game_state_msg:
                    apply_game_state_update(game_state_msg)
                    if _auto_clear_stale_decision(game_state):
                        snapshot_dirty = True
            
            # Dispatch known decision/action message types to helpers
            elif msg_type in _KNOWN_REQ_TYPES:
                if _handle_decision_message(game_state, msg_type, msg):
                    snapshot_dirty = True

            elif msg_type == "GREMessageType_QueuedGameStateMessage":
                game_state_msg = msg.get("gameStateMessage")
                if game_state_msg:
                    apply_game_state_update(game_state_msg)

            elif msg_type == "GREMessageType_UIMessage":
                pass  # Hover/highlight UI events

            elif msg_type == "GREMessageType_TimerStateMessage":
                # ── Phase 1: Parse chess clock / timer data ──
                timer_msg = msg.get("timerStateMessage", msg.get("timerState", {}))
                if timer_msg:
                    timers = timer_msg.get("timers", [])
                    timer_data = {}
                    for timer in timers:
                        player_id = timer.get("playerId", timer.get("seatId", 0))
                        timer_type = timer.get("type", timer.get("timerType", ""))
                        remaining = timer.get("timeRemainingMs", timer.get("durationMs", 0))
                        behavior = timer.get("behavior", "")
                        if player_id:
                            timer_data[player_id] = {
                                "time_remaining_ms": remaining,
                                "timer_type": timer_type,
                                "behavior": behavior,
                                "is_ticking": timer.get("isTicking", False),
                            }
                    # Also check for top-level fields
                    if not timer_data:
                        for key in ("player1Timer", "player2Timer"):
                            t = timer_msg.get(key, {})
                            if t:
                                timer_data[key] = {
                                    "time_remaining_ms": t.get("timeRemainingMs", 0),
                                    "timer_type": t.get("type", ""),
                                }
                    if timer_data:
                        game_state.timer_state = timer_data

            # Fallback for unknown Req types
            elif msg_type.endswith("Req") and msg_type not in _KNOWN_REQ_TYPES:
                import time as _time
                logger.warning(f"Unknown GRE Req type: {msg_type} - treating as pending decision")
                game_state.pending_decision = f"Unknown Decision ({msg_type})"
                game_state.decision_timestamp = _time.time()
                game_state.decision_context = {
                    "type": "unknown_req",
                    "gre_type": msg_type,
                    "raw_message": {k: v for k, v in msg.items() if k != "type"},
                }

            elif "Resp" in msg_type:
                if game_state.pending_decision and msg_type in _DECISION_RESPONSE_TYPES:
                    if game_state.pending_decision == "Mulligan" and msg_type != "GREMessageType_SubmitDeckResp":
                        pass
                    else:
                        logger.debug(f"Clearing decision '{game_state.pending_decision}' due to {msg_type}")
                        game_state.last_cleared_decision = game_state.pending_decision
                        game_state.pending_decision = None
                        game_state.decision_seat_id = None
                        game_state.decision_context = None
                        game_state.decision_timestamp = 0

        # Also handle direct GameStateMessage events (legacy format)
        if "gameObjects" in payload or "zones" in payload or "players" in payload:
            apply_game_state_update(payload)

        # Publish once for decision/action-only messages that mutate state
        # without a full/diff GameState update.
        if snapshot_dirty:
            game_state.publish_snapshot()

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
    log_path: Optional[str] = None,
) -> None:
    """Save current match state for recovery after restart.

    Writes match_id, local_seat_id, log_offset, log identity metadata,
    turn info, and timestamp to ~/.arenamcp/last_match.json.

    Args:
        game_state: Current GameState instance.
        log_offset: Current file position in the log file.
        log_path: Path to the log file (for session identity validation on resume).
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

    # Attach log identity for session-aware resume validation
    if log_path:
        try:
            log_p = Path(log_path)
            if log_p.exists():
                stat = log_p.stat()
                state["log_identity"] = {
                    "path": str(log_p),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
        except OSError:
            pass

    try:
        MATCH_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(MATCH_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        logger.debug(f"Saved match state: turn {state['turn_number']}, offset {log_offset}")
    except Exception as e:
        logger.warning(f"Failed to save match state: {e}")


def validate_log_identity(
    saved_state: dict[str, Any],
    current_log_path: Optional[str] = None,
) -> str:
    """Validate whether saved resume state matches the current log session.

    Compares saved log identity metadata against the current log file to
    determine if the offset is safe to resume from.

    Args:
        saved_state: Previously saved match state dict.
        current_log_path: Path to the current log file.

    Returns:
        One of:
        - "resume_same_session": offset is valid, same log session
        - "fresh_log_after_restart": log was recreated, offset invalid
        - "resume_invalid_path": log path changed or missing
        - "resume_no_identity": no log identity saved (legacy state), allow resume
        - "resume_append_mode_ambiguous": file grew but mtime changed significantly
    """
    saved_identity = saved_state.get("log_identity")
    if not saved_identity:
        logger.info("No log identity in saved state (legacy format) — allowing resume")
        return "resume_no_identity"

    if not current_log_path:
        logger.warning("No current log path provided for identity check")
        return "resume_invalid_path"

    try:
        current_path = Path(current_log_path)
        if not current_path.exists():
            logger.warning(f"Current log file does not exist: {current_path}")
            return "resume_invalid_path"

        stat = current_path.stat()
        saved_path = saved_identity.get("path", "")
        saved_size = saved_identity.get("size", 0)
        saved_mtime = saved_identity.get("mtime", 0)

        # Path mismatch (different log file entirely)
        if str(current_path) != saved_path:
            logger.info(
                f"Log path changed: saved={saved_path}, current={current_path}"
            )
            return "resume_invalid_path"

        # File is smaller than saved size → file was recreated (MTGA restart)
        if stat.st_size < saved_size:
            logger.info(
                f"Log file shrank: saved_size={saved_size}, current_size={stat.st_size} "
                "— fresh log after restart"
            )
            return "fresh_log_after_restart"

        # mtime jumped significantly backward or forward (>2s tolerance) AND
        # file is smaller → strong signal of recreation
        # If file grew and mtime advanced, that's normal append behavior.
        mtime_delta = abs(stat.st_mtime - saved_mtime)

        # File is same size or larger, mtime advanced normally → same session
        if stat.st_size >= saved_size and mtime_delta < 300:
            logger.info(
                f"Log identity matches: size {saved_size}->{stat.st_size}, "
                f"mtime delta {mtime_delta:.1f}s — resuming same session"
            )
            return "resume_same_session"

        # File grew but mtime jumped a lot — could be appendlog mode
        if stat.st_size >= saved_size and mtime_delta >= 300:
            logger.warning(
                f"Log identity ambiguous: size grew ({saved_size}->{stat.st_size}) "
                f"but mtime delta is {mtime_delta:.0f}s — possible appendlog mode"
            )
            return "resume_append_mode_ambiguous"

        # Default: treat as fresh
        logger.info("Log identity does not match saved state — treating as fresh")
        return "fresh_log_after_restart"

    except OSError as e:
        logger.warning(f"Error checking log identity: {e}")
        return "resume_invalid_path"


def load_match_state() -> Optional[dict[str, Any]]:
    """Load saved match state if valid (< 30 min old, status active).

    Returns:
        Dict with match_id, local_seat_id, log_offset, turn_number, phase,
        timestamp, status, and optionally log_identity. Or None if no valid
        state exists.
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
