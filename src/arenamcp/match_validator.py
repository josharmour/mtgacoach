"""Match Recording and Validation System.

Records raw Arena messages during gameplay and allows post-match comparison
to validate our game state parsing accuracy.

Key concepts:
- Raw Arena JSON messages are the "ground truth"
- Our GameState parser interprets these messages
- This system captures both and allows comparison to find parsing bugs
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MatchFrame:
    """A single frame of match state - raw message + our interpretation."""
    timestamp: datetime
    frame_number: int
    
    # Raw Arena data
    raw_message: dict
    message_type: str  # e.g., "GameStateType_Full", "GameStateType_Diff"
    
    # Our parsed interpretation (snapshot at this point)
    parsed_snapshot: Optional[dict] = None
    
    # Extracted key state from Arena (for comparison)
    arena_turn: int = 0
    arena_phase: str = ""
    arena_step: str = ""
    arena_active_player: int = 0
    arena_priority_player: int = 0
    arena_life_totals: dict = field(default_factory=dict)
    arena_battlefield_count: int = 0
    arena_stack_count: int = 0
    arena_hand_counts: dict = field(default_factory=dict)  # seat_id -> card count
    arena_graveyard_counts: dict = field(default_factory=dict)
    arena_attackers: list = field(default_factory=list)  # instanceIds
    arena_blockers: list = field(default_factory=list)   # instanceIds
    arena_available_actions: list = field(default_factory=list)  # action types
    
    # Card-level tracking (grpId -> {name, instanceId, owner, zone})
    arena_battlefield_cards: list = field(default_factory=list)  # [{grpId, instanceId, owner}]
    arena_hand_cards: list = field(default_factory=list)
    arena_stack_cards: list = field(default_factory=list)
    arena_graveyard_cards: list = field(default_factory=list)


@dataclass
class MatchRecording:
    """Complete recording of a match for validation."""
    match_id: str
    start_time: datetime
    frames: list[MatchFrame] = field(default_factory=list)

    # Match metadata
    your_seat_id: Optional[int] = None
    opponent_name: Optional[str] = None
    result: Optional[str] = None  # "win", "loss", "draw"
    end_time: Optional[datetime] = None
    
    # Advice tracking for post-match analysis
    advice_events: list[dict] = field(default_factory=list)
    
    def add_advice_event(self, trigger: str, advice: str, game_context: str, 
                         parsed_turn: int, parsed_phase: str) -> None:
        """Record when advice was given for post-match analysis."""
        self.advice_events.append({
            "timestamp": datetime.now().isoformat(),
            "frame_number": len(self.frames) - 1,  # Current frame
            "trigger": trigger,
            "advice": advice,
            "game_context_snippet": game_context[:500] if game_context else None,
            "parsed_turn": parsed_turn,
            "parsed_phase": parsed_phase,
        })
    
    def add_frame(self, raw_message: dict, parsed_snapshot: Optional[dict] = None) -> MatchFrame:
        """Add a new frame to the recording."""
        frame_num = len(self.frames)
        
        # Extract key data from raw Arena message
        gsm = raw_message.get("greToClientEvent", {}).get("greToClientMessages", [{}])[0]
        game_state_msg = gsm.get("gameStateMessage", {})
        
        msg_type = game_state_msg.get("type", "Unknown")
        turn_info = game_state_msg.get("turnInfo", {})
        
        # Extract life totals from players array
        life_totals = {}
        for player in game_state_msg.get("players", []):
            seat_id = player.get("systemSeatNumber") or player.get("seatId")
            life = player.get("lifeTotal")
            if seat_id and life is not None:
                life_totals[seat_id] = life
        
        # Build object lookup from gameObjects (grpId, name, owner)
        objects_by_id = {}
        for obj in game_state_msg.get("gameObjects", []):
            instance_id = obj.get("instanceId")
            if instance_id:
                objects_by_id[instance_id] = {
                    "instanceId": instance_id,
                    "grpId": obj.get("grpId", 0),
                    "name": obj.get("name", "Unknown"),
                    "owner": obj.get("ownerSeatId", 0),
                    "controller": obj.get("controllerSeatId", 0),
                    "type": obj.get("type", ""),
                    "power": obj.get("power", {}).get("value") if obj.get("power") else None,
                    "toughness": obj.get("toughness", {}).get("value") if obj.get("toughness") else None,
                }
        
        # Count objects by zone and collect card details
        bf_count = 0
        stack_count = 0
        hand_counts = {}
        graveyard_counts = {}
        
        battlefield_cards = []
        hand_cards = []
        stack_cards = []
        graveyard_cards = []
        
        for zone in game_state_msg.get("zones", []):
            zone_type = zone.get("type", "")
            obj_ids = zone.get("objectInstanceIds", [])
            owner = zone.get("ownerSeatId", 0)
            
            if zone_type == "ZoneType_Battlefield":
                bf_count += len(obj_ids)
                for oid in obj_ids:
                    if oid in objects_by_id:
                        battlefield_cards.append(objects_by_id[oid])
            elif zone_type == "ZoneType_Stack":
                stack_count += len(obj_ids)
                for oid in obj_ids:
                    if oid in objects_by_id:
                        stack_cards.append(objects_by_id[oid])
            elif zone_type == "ZoneType_Hand":
                hand_counts[owner] = len(obj_ids)
                for oid in obj_ids:
                    if oid in objects_by_id:
                        card = objects_by_id[oid].copy()
                        card["owner"] = owner
                        hand_cards.append(card)
            elif zone_type == "ZoneType_Graveyard":
                graveyard_counts[owner] = len(obj_ids)
                for oid in obj_ids:
                    if oid in objects_by_id:
                        graveyard_cards.append(objects_by_id[oid])
        
        # Extract attackers and blockers from annotations
        attackers = []
        blockers = []
        for annotation in game_state_msg.get("annotations", []):
            ann_type = annotation.get("type", [])
            if "AnnotationType_Attacking" in ann_type:
                attackers.extend(annotation.get("affectedIds", []))
            elif "AnnotationType_Blocking" in ann_type:
                blockers.extend(annotation.get("affectedIds", []))
        
        # Extract available actions
        available_actions = []
        for action in game_state_msg.get("actions", []):
            action_info = action.get("action", {})
            action_type = action_info.get("actionType", "")
            if action_type:
                available_actions.append(action_type)
        
        frame = MatchFrame(
            timestamp=datetime.now(),
            frame_number=frame_num,
            # OPTIMIZATION: Only store raw_message every 10 frames to reduce memory
            # The raw message can be very large and storing it every frame causes memory bloat
            raw_message=raw_message if frame_num % 10 == 0 else {},
            message_type=msg_type,
            parsed_snapshot=parsed_snapshot,
            arena_turn=turn_info.get("turnNumber", 0),
            arena_phase=turn_info.get("phase", ""),
            arena_step=turn_info.get("step", ""),
            arena_active_player=turn_info.get("activePlayer", 0),
            arena_priority_player=turn_info.get("priorityPlayer", 0),
            arena_life_totals=life_totals,
            arena_battlefield_count=bf_count,
            arena_stack_count=stack_count,
            arena_hand_counts=hand_counts,
            arena_graveyard_counts=graveyard_counts,
            arena_attackers=attackers,
            arena_blockers=blockers,
            arena_available_actions=list(set(available_actions)),  # Dedupe
            arena_battlefield_cards=battlefield_cards,
            arena_hand_cards=hand_cards,
            arena_stack_cards=stack_cards,
            arena_graveyard_cards=graveyard_cards,
        )
        
        self.frames.append(frame)
        return frame
    
    def save(self, output_dir: Path) -> Path:
        """Save the recording to disk."""
        output_dir.mkdir(parents=True, exist_ok=True)
        
        filename = f"match_{self.match_id}_{self.start_time.strftime('%Y%m%d_%H%M%S')}.json"
        output_path = output_dir / filename
        
        # Convert to serializable format
        data = {
            "match_id": self.match_id,
            "start_time": self.start_time.isoformat(),
            "your_seat_id": self.your_seat_id,
            "opponent_name": self.opponent_name,
            "result": self.result,
            "frame_count": len(self.frames),
            "advice_events": self.advice_events,  # Include recorded advice
            "frames": [
                {
                    "frame_number": f.frame_number,
                    "timestamp": f.timestamp.isoformat(),
                    "message_type": f.message_type,
                    "arena_turn": f.arena_turn,
                    "arena_phase": f.arena_phase,
                    "arena_step": f.arena_step,
                    "arena_active_player": f.arena_active_player,
                    "arena_priority_player": f.arena_priority_player,
                    "arena_life_totals": f.arena_life_totals,
                    "arena_battlefield_count": f.arena_battlefield_count,
                    "arena_stack_count": f.arena_stack_count,
                    "arena_hand_counts": f.arena_hand_counts,
                    "arena_graveyard_counts": f.arena_graveyard_counts,
                    "arena_attackers": f.arena_attackers,
                    "arena_blockers": f.arena_blockers,
                    "arena_available_actions": f.arena_available_actions,
                    "arena_battlefield_cards": f.arena_battlefield_cards,
                    "arena_hand_cards": f.arena_hand_cards,
                    "arena_stack_cards": f.arena_stack_cards,
                    "arena_graveyard_cards": f.arena_graveyard_cards,
                    "raw_message": f.raw_message,
                    "parsed_snapshot": f.parsed_snapshot,
                }
                for f in self.frames
            ]
        }
        
        with open(output_path, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        
        logger.info(f"Saved match recording: {output_path}")
        return output_path

    @classmethod
    def load(cls, path: Path) -> "MatchRecording":
        """Load a recording from disk."""
        with open(path) as f:
            data = json.load(f)

        recording = cls(
            match_id=data["match_id"],
            start_time=datetime.fromisoformat(data["start_time"]),
            your_seat_id=data.get("your_seat_id"),
            opponent_name=data.get("opponent_name"),
            result=data.get("result"),
        )

        # Load advice events
        recording.advice_events = data.get("advice_events", [])

        # Load frames
        for f_data in data.get("frames", []):
            frame = MatchFrame(
                timestamp=datetime.fromisoformat(f_data["timestamp"]),
                frame_number=f_data["frame_number"],
                raw_message=f_data.get("raw_message", {}),
                message_type=f_data.get("message_type", ""),
                parsed_snapshot=f_data.get("parsed_snapshot"),
                arena_turn=f_data.get("arena_turn", 0),
                arena_phase=f_data.get("arena_phase", ""),
                arena_step=f_data.get("arena_step", ""),
                arena_active_player=f_data.get("arena_active_player", 0),
                arena_priority_player=f_data.get("arena_priority_player", 0),
                arena_life_totals=f_data.get("arena_life_totals", {}),
                arena_battlefield_count=f_data.get("arena_battlefield_count", 0),
                arena_stack_count=f_data.get("arena_stack_count", 0),
                arena_hand_counts=f_data.get("arena_hand_counts", {}),
                arena_graveyard_counts=f_data.get("arena_graveyard_counts", {}),
                arena_attackers=f_data.get("arena_attackers", []),
                arena_blockers=f_data.get("arena_blockers", []),
                arena_available_actions=f_data.get("arena_available_actions", []),
                arena_battlefield_cards=f_data.get("arena_battlefield_cards", []),
                arena_hand_cards=f_data.get("arena_hand_cards", []),
                arena_stack_cards=f_data.get("arena_stack_cards", []),
                arena_graveyard_cards=f_data.get("arena_graveyard_cards", []),
            )
            recording.frames.append(frame)

        return recording


@dataclass
class ValidationResult:
    """Result of comparing Arena data vs our parsing."""
    frame_number: int
    field: str
    arena_value: Any
    parsed_value: Any
    turn: int = 0  # Turn number when discrepancy occurred
    severity: str = "warning"  # "info", "warning", "error"
    
    def __str__(self):
        return f"[{self.severity.upper()}] Turn {self.turn} (Frame {self.frame_number}): {self.field} - Arena={self.arena_value}, Parsed={self.parsed_value}"


class MatchValidator:
    """Validates our parsing against Arena's ground truth."""
    
    def validate_recording(self, recording: MatchRecording) -> list[ValidationResult]:
        """Compare all frames and return discrepancies."""
        results = []
        
        # Phase adjacency map (phases that naturally follow each other)
        phase_order = [
            "Phase_Beginning", "Phase_Main1", "Phase_Combat", 
            "Phase_Main2", "Phase_Ending"
        ]
        
        def phases_are_adjacent(p1: str, p2: str) -> bool:
            """Check if two phases are adjacent (off by 1)."""
            if not p1 or not p2:
                return True  # Can't compare empty phases
            try:
                idx1 = phase_order.index(p1) if p1 in phase_order else -1
                idx2 = phase_order.index(p2) if p2 in phase_order else -1
                if idx1 < 0 or idx2 < 0:
                    return False
                return abs(idx1 - idx2) <= 1
            except:
                return False
        
        for frame in recording.frames:
            if frame.parsed_snapshot is None:
                continue
            
            # Get turn info from our snapshot (uses "turn_info" not "turn")
            turn_info = frame.parsed_snapshot.get("turn_info", {})
            zones = frame.parsed_snapshot.get("zones", {})
            
            parsed_turn = turn_info.get("turn_number", 0)
            parsed_phase = turn_info.get("phase", "")
            
            # Skip frames where Arena doesn't report turn info (diff messages)
            if frame.arena_turn == 0:
                continue
            
            # Compare turn number - allow off-by-one (timing tolerance)
            turn_diff = abs(parsed_turn - frame.arena_turn)
            if turn_diff > 0:
                severity = "error" if turn_diff > 1 else "warning"
                results.append(ValidationResult(
                    frame_number=frame.frame_number,
                    field="turn_number",
                    arena_value=frame.arena_turn,
                    parsed_value=parsed_turn,
                    turn=frame.arena_turn,
                    severity=severity
                ))
            
            # Compare phase - adjacent phases are acceptable (timing tolerance)
            if frame.arena_phase and parsed_phase != frame.arena_phase:
                if phases_are_adjacent(frame.arena_phase, parsed_phase):
                    severity = "info"  # Adjacent phase is just timing, not an error
                else:
                    severity = "warning"
                results.append(ValidationResult(
                    frame_number=frame.frame_number,
                    field="phase",
                    arena_value=frame.arena_phase,
                    parsed_value=parsed_phase,
                    turn=frame.arena_turn,
                    severity=severity
                ))
            
            # Compare life totals
            for seat_id, arena_life in frame.arena_life_totals.items():
                parsed_players = frame.parsed_snapshot.get("players", [])
                parsed_life = None
                for p in parsed_players:
                    if p.get("seat_id") == seat_id:
                        parsed_life = p.get("life_total")
                        break
                
                if parsed_life is not None and parsed_life != arena_life:
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field=f"life_total_seat_{seat_id}",
                        arena_value=arena_life,
                        parsed_value=parsed_life,
                        turn=frame.arena_turn,
                        severity="error"
                    ))
            
            # Compare battlefield count (our snapshot uses zones.battlefield)
            parsed_bf = zones.get("battlefield", [])
            if frame.arena_battlefield_count > 0:
                bf_diff = abs(len(parsed_bf) - frame.arena_battlefield_count)
                if bf_diff > 0:
                    # Allow small differences (timing during resolution)
                    severity = "error" if bf_diff > 2 else "warning"
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field="battlefield_count",
                        arena_value=frame.arena_battlefield_count,
                        parsed_value=len(parsed_bf),
                        turn=frame.arena_turn,
                        severity=severity
                    ))
            
            # Compare priority player (CRITICAL for AI timing)
            parsed_priority = turn_info.get("priority_player", 0)
            if frame.arena_priority_player and parsed_priority != frame.arena_priority_player:
                results.append(ValidationResult(
                    frame_number=frame.frame_number,
                    field="priority_player",
                    arena_value=frame.arena_priority_player,
                    parsed_value=parsed_priority,
                    turn=frame.arena_turn,
                    severity="warning"  # Priority can shift rapidly
                ))
            
            # Compare active player (whose turn is it)
            parsed_active = turn_info.get("active_player", 0)
            if frame.arena_active_player and parsed_active != frame.arena_active_player:
                # If turn number is also off, this is likely a timing issue, not a parsing bug
                severity = "warning" if turn_diff > 0 else "error"
                results.append(ValidationResult(
                    frame_number=frame.frame_number,
                    field="active_player",
                    arena_value=frame.arena_active_player,
                    parsed_value=parsed_active,
                    turn=frame.arena_turn,
                    severity=severity
                ))
            
            # Compare stack count (spells being cast)
            parsed_stack = zones.get("stack", [])
            if frame.arena_stack_count > 0 or len(parsed_stack) > 0:
                stack_diff = abs(len(parsed_stack) - frame.arena_stack_count)
                if stack_diff > 0:
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field="stack_count",
                        arena_value=frame.arena_stack_count,
                        parsed_value=len(parsed_stack),
                        turn=frame.arena_turn,
                        severity="warning"
                    ))
            
            # Compare hand counts per player
            parsed_players = frame.parsed_snapshot.get("players", [])
            for seat_id, arena_hand_count in frame.arena_hand_counts.items():
                # Find parsed hand count - this requires checking zones
                parsed_hand = zones.get("hand", [])  # Our hand only
                # For now, just check our own hand
                local_player = next((p for p in parsed_players if p.get("is_local")), None)
                if local_player and seat_id == local_player.get("seat_id"):
                    if len(parsed_hand) != arena_hand_count:
                        hand_diff = abs(len(parsed_hand) - arena_hand_count)
                        results.append(ValidationResult(
                            frame_number=frame.frame_number,
                            field=f"hand_count_seat_{seat_id}",
                            arena_value=arena_hand_count,
                            parsed_value=len(parsed_hand),
                            turn=frame.arena_turn,
                            severity="warning" if hand_diff <= 1 else "error"
                        ))
            
            # Check for attackers (during combat)
            if frame.arena_attackers:
                # We should have attackers tracked somewhere
                # For now, just log if Arena shows attackers
                pass  # TODO: Compare with parsed attackers when available
            
            # CARD-LEVEL VALIDATION: Compare specific cards on battlefield
            if frame.arena_battlefield_cards:
                arena_bf_grpids = set(c.get("grpId") for c in frame.arena_battlefield_cards if c.get("grpId"))
                parsed_bf_grpids = set(c.get("grp_id") for c in parsed_bf if c.get("grp_id"))
                
                # Cards in Arena but not in our parsed state (missing)
                missing = arena_bf_grpids - parsed_bf_grpids
                if missing:
                    missing_names = [c.get("name") for c in frame.arena_battlefield_cards if c.get("grpId") in missing]
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field="battlefield_missing_cards",
                        arena_value=list(missing_names),
                        parsed_value=[],
                        turn=frame.arena_turn,
                        severity="error"
                    ))
                
                # Cards in our parsed state but not in Arena (phantom)
                phantom = parsed_bf_grpids - arena_bf_grpids
                if phantom:
                    phantom_names = [c.get("name") for c in parsed_bf if c.get("grp_id") in phantom]
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field="battlefield_phantom_cards",
                        arena_value=[],
                        parsed_value=list(phantom_names),
                        turn=frame.arena_turn,
                        severity="error"
                    ))
            
            # CARD-LEVEL VALIDATION: Compare hand cards
            if frame.arena_hand_cards:
                # Filter to local player's hand
                parsed_hand = zones.get("hand", [])
                arena_hand_grpids = set(c.get("grpId") for c in frame.arena_hand_cards if c.get("grpId"))
                parsed_hand_grpids = set(c.get("grp_id") for c in parsed_hand if c.get("grp_id"))
                
                missing_hand = arena_hand_grpids - parsed_hand_grpids
                if missing_hand:
                    missing_names = [c.get("name") for c in frame.arena_hand_cards if c.get("grpId") in missing_hand]
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field="hand_missing_cards",
                        arena_value=list(missing_names),
                        parsed_value=[],
                        turn=frame.arena_turn,
                        severity="warning"  # Hand can be tricky with revealed cards
                    ))
            
            # CARD-LEVEL VALIDATION: Compare stack (spells being cast)
            if frame.arena_stack_cards:
                parsed_stack = zones.get("stack", [])
                arena_stack_grpids = set(c.get("grpId") for c in frame.arena_stack_cards if c.get("grpId"))
                parsed_stack_grpids = set(c.get("grp_id") for c in parsed_stack if c.get("grp_id"))
                
                missing_stack = arena_stack_grpids - parsed_stack_grpids
                if missing_stack:
                    missing_names = [c.get("name") for c in frame.arena_stack_cards if c.get("grpId") in missing_stack]
                    results.append(ValidationResult(
                        frame_number=frame.frame_number,
                        field="stack_missing_cards",
                        arena_value=list(missing_names),
                        parsed_value=[],
                        turn=frame.arena_turn,
                        severity="error"  # Missing stack items is serious
                    ))
        
        return results
    
    def generate_report(self, recording: MatchRecording) -> str:
        """Generate a human-readable validation report."""
        results = self.validate_recording(recording)
        
        lines = [
            "=" * 60,
            f"MATCH VALIDATION REPORT",
            f"Match ID: {recording.match_id}",
            f"Frames Analyzed: {len(recording.frames)}",
            "=" * 60,
            ""
        ]
        
        if not results:
            lines.append("[OK] No discrepancies found! Parsing matches Arena data.")
        else:
            # Group by severity
            errors = [r for r in results if r.severity == "error"]
            warnings = [r for r in results if r.severity == "warning"]
            infos = [r for r in results if r.severity == "info"]
            
            # Only count actual problems (not info)
            problem_count = len(errors) + len(warnings)
            
            if problem_count == 0:
                lines.append("[OK] No significant issues found!")
                lines.append(f"  (Timing variations: {len(infos)})")
            else:
                lines.append(f"Found {problem_count} issues:")
                lines.append(f"  - {len(errors)} errors (significant)")
                lines.append(f"  - {len(warnings)} warnings (off-by-one)")
                if infos:
                    lines.append(f"  - {len(infos)} timing variations (expected)")
                lines.append("")
                
                if errors:
                    lines.append("ERRORS:")
                    for r in errors:
                        lines.append(f"  {r}")
                    lines.append("")
                
                if warnings:
                    lines.append("WARNINGS (timing tolerance):")
                    for r in warnings[:20]:  # Limit output
                        lines.append(f"  {r}")
                    if len(warnings) > 20:
                        lines.append(f"  ... and {len(warnings) - 20} more warnings")
        
        lines.append("")
        lines.append("=" * 60)
        
        return "\n".join(lines)


# Global recorder instance
_current_recording: Optional[MatchRecording] = None


def start_recording(match_id: str) -> MatchRecording:
    """Start recording a new match."""
    global _current_recording
    _current_recording = MatchRecording(
        match_id=match_id,
        start_time=datetime.now()
    )
    logger.info(f"Started recording match: {match_id}")
    return _current_recording


def get_current_recording() -> Optional[MatchRecording]:
    """Get the current recording, if any."""
    return _current_recording


def stop_recording() -> Optional[MatchRecording]:
    """Stop recording and return the completed recording."""
    global _current_recording
    recording = _current_recording
    _current_recording = None
    return recording


def record_frame(raw_message: dict, parsed_snapshot: Optional[dict] = None) -> None:
    """Record a frame if recording is active.
    
    Call this after each game state update to capture the frame.
    Does nothing if no recording is in progress.
    
    Args:
        raw_message: The raw Arena JSON message
        parsed_snapshot: Our parsed game state snapshot
    """
    if _current_recording is not None:
        try:
            _current_recording.add_frame(raw_message, parsed_snapshot)
        except Exception as e:
            logger.warning(f"Failed to record frame: {e}")

