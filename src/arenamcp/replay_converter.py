"""Convert Arena replay files to ArenaMCP match recording format.

This allows us to use Arena's native .rply recordings as test data
for advisor validation and tuning.
"""

import json
import logging
from pathlib import Path
from typing import Optional
from datetime import datetime

from arenamcp.arena_replay import ArenaReplay
from arenamcp.match_validator import MatchRecording, MatchFrame
from arenamcp.gamestate import GameState, create_game_state_handler

logger = logging.getLogger(__name__)


class ReplayConverter:
    """Converts Arena .rply files to ArenaMCP MatchRecording format."""
    
    def __init__(self, card_db=None):
        """Initialize converter.
        
        Args:
            card_db: Optional CardDatabase instance for enriching data
        """
        self.card_db = card_db
        
    def convert(self, arena_replay_path: Path, 
                output_path: Optional[Path] = None) -> Optional[MatchRecording]:
        """Convert Arena replay to MatchRecording.
        
        Args:
            arena_replay_path: Path to Arena .rply file
            output_path: Optional path to save converted recording
            
        Returns:
            MatchRecording object, or None on error
        """
        logger.info(f"Converting Arena replay: {arena_replay_path.name}")
        
        # Load Arena replay
        arena_replay = ArenaReplay(arena_replay_path)
        if not arena_replay.load():
            logger.error("Failed to load Arena replay")
            return None
        
        # Create game state tracker
        game_state = GameState()
        handler = create_game_state_handler(game_state)

        # Create match recording
        recording = MatchRecording(
            match_id=f"arena_{arena_replay_path.stem}",
            start_time=datetime.fromtimestamp(arena_replay_path.stat().st_mtime)
        )
        
        # Process messages and build frames
        frame_number = 0
        for msg in arena_replay.iter_messages():
            # Skip hover messages (noise)
            msg_str = json.dumps(msg)
            if '"onHover"' in msg_str:
                continue
            
            # Extract message type
            msg_type = msg.get("type") or msg.get("greToClientEvent", {}).get("type")
            if not msg_type:
                continue
            
            # Update game state
            try:
                handler(msg)
            except Exception as e:
                logger.debug(f"GameState processing error: {e}")
                # Continue anyway - we want partial data
            
            # Create frame for important messages
            if self._is_important_message(msg_type):
                # Get game state snapshot
                snapshot = game_state.get_snapshot() if game_state else {}

                frame = MatchFrame(
                    timestamp=datetime.now(),  # Arena replays don't include timestamps
                    frame_number=frame_number,
                    raw_message=msg,
                    message_type=msg_type,
                    parsed_snapshot=snapshot,
                )

                # Store trigger info in raw_message for later access
                trigger = self._detect_trigger(msg_type, msg)
                if trigger:
                    frame.raw_message["_trigger"] = trigger

                recording.frames.append(frame)
                frame_number += 1
        
        recording.end_time = datetime.now()
        
        # Save if output path provided
        if output_path:
            recording.save(output_path)
            logger.info(f"Saved converted recording to {output_path}")
        
        logger.info(f"Converted {len(recording.frames)} frames from Arena replay")
        return recording
    
    def _is_important_message(self, msg_type: str) -> bool:
        """Check if message type should be recorded as a frame.
        
        Args:
            msg_type: GRE message type
            
        Returns:
            True if message should create a frame
        """
        important_types = {
            # Game state updates
            "GREMessageType_GameStateMessage",
            
            # Decision requests
            "GREMessageType_MulliganReq",
            "GREMessageType_SelectTargetsReq",
            "GREMessageType_SelectNReq",
            "GREMessageType_GroupOptionReq",
            "GREMessageType_PromptReq",
            
            # Turn/phase changes
            "GREMessageType_ActionsAvailableReq",
            
            # Combat
            "GREMessageType_DeclareAttackersReq",
            "GREMessageType_DeclareBlockersReq",
        }
        
        return msg_type in important_types
    
    def _detect_trigger(self, msg_type: str, msg: dict) -> Optional[str]:
        """Detect what advisor trigger this message would create.
        
        Args:
            msg_type: GRE message type
            msg: Full message dict
            
        Returns:
            Trigger name (e.g., "decision_required", "combat_attackers")
        """
        # Map message types to advisor triggers
        trigger_map = {
            "GREMessageType_MulliganReq": "decision_required",
            "GREMessageType_SelectTargetsReq": "decision_required",
            "GREMessageType_SelectNReq": "decision_required",
            "GREMessageType_GroupOptionReq": "decision_required",
            "GREMessageType_PromptReq": "decision_required",
            "GREMessageType_DeclareAttackersReq": "combat_attackers",
            "GREMessageType_DeclareBlockersReq": "combat_blockers",
        }
        
        return trigger_map.get(msg_type)


def convert_replay_file(replay_path: Path, output_dir: Optional[Path] = None) -> Optional[Path]:
    """Convert an Arena replay file to ArenaMCP format.
    
    Args:
        replay_path: Path to .rply file
        output_dir: Optional output directory (default: same as input)
        
    Returns:
        Path to converted .json file, or None on error
    """
    if output_dir is None:
        output_dir = replay_path.parent
    
    output_path = output_dir / f"{replay_path.stem}_converted.json"
    
    converter = ReplayConverter()
    recording = converter.convert(replay_path, output_path)
    
    return output_path if recording else None


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        replay_path = Path(sys.argv[1])
        converted_path = convert_replay_file(replay_path)
        
        if converted_path:
            print(f"✓ Converted to: {converted_path}")
        else:
            print("✗ Conversion failed")
    else:
        print("Usage: python replay_converter.py <replay_file.rply>")
