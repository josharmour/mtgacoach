r"""Arena .rply replay file parser.

Parses MTGA's native replay format (.rply files) which contain
the full message exchange between client and server during a match.

Enable replay recording by creating an empty .autoplay file at:
Windows: %APPDATA%\LocalLow\Wizards Of The Coast\MTGA\ArenaAutoplayConfigs\.autoplay
Then hold Alt during matches to access debug panel with recording controls.
"""

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class ArenaReplay:
    """Parser for Arena .rply replay files."""
    
    def __init__(self, replay_path: Path):
        """Initialize replay parser.

        Args:
            replay_path: Path to .rply file
        """
        self.replay_path = Path(replay_path)
        self._messages: list[dict] = []
        self._loaded = False
        self._format: str = "unknown"
        self._metadata: dict = {}
        
    def load(self) -> bool:
        """Load and parse the replay file.

        Supports both formats:
        - NDJSON: Each line is a JSON message
        - Version2: #Version2 header, metadata line, then IN-/OUT- prefixed messages

        Returns:
            True if loaded successfully
        """
        if self._loaded:
            return True

        try:
            with open(self.replay_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if not lines:
                logger.error("Empty replay file")
                return False

            # Detect format from first line
            first_line = lines[0].strip()

            if first_line.startswith("#Version"):
                # Version2 format: #Version2 header, metadata, then IN-/OUT- messages
                self._format = "version2"
                self._metadata = json.loads(lines[1].strip()) if len(lines) > 1 else {}

                for line in lines[2:]:
                    line = line.strip()
                    if not line:
                        continue

                    # Parse IN- (server) and OUT- (client) messages
                    if line.startswith("IN-") or line.startswith("OUT-"):
                        colon_idx = line.find(':')
                        if colon_idx > 0:
                            direction = "in" if line.startswith("IN-") else "out"
                            try:
                                msg = json.loads(line[colon_idx+1:])
                                msg["_direction"] = direction  # Tag direction
                                self._messages.append(msg)
                            except json.JSONDecodeError:
                                continue

                logger.info(f"Loaded {len(self._messages)} messages from {self.replay_path.name} (Version2 format)")
            else:
                # NDJSON format: each line is a JSON message
                self._format = "ndjson"
                for line_num, line in enumerate(lines, 1):
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        msg = json.loads(line)
                        self._messages.append(msg)
                    except json.JSONDecodeError as e:
                        logger.warning(f"Skipping malformed JSON at line {line_num}: {e}")
                        continue

                logger.info(f"Loaded {len(self._messages)} messages from {self.replay_path.name} (NDJSON format)")

            self._loaded = True
            return True

        except Exception as e:
            logger.error(f"Failed to load replay file: {e}")
            return False
    
    def iter_messages(self) -> Iterator[dict]:
        """Iterate through all messages in replay order.
        
        Yields:
            Message dict from replay
        """
        if not self._loaded:
            self.load()
            
        for msg in self._messages:
            yield msg
    
    def get_message_count(self) -> int:
        """Get total number of messages in replay."""
        if not self._loaded:
            self.load()
        return len(self._messages)
    
    def filter_messages(self, msg_type: Optional[str] = None, 
                       exclude_hover: bool = True) -> list[dict]:
        """Filter messages by type.
        
        Args:
            msg_type: Only return messages of this type (e.g., "GREMessageType_GameStateMessage")
            exclude_hover: Exclude onHover UI messages (reduces noise)
            
        Returns:
            Filtered list of messages
        """
        if not self._loaded:
            self.load()
            
        filtered = []
        for msg in self._messages:
            # Exclude hover messages if requested
            if exclude_hover and "onHover" in json.dumps(msg):
                continue
                
            # Filter by type if specified
            if msg_type:
                if msg.get("type") == msg_type or msg.get("greToClientEvent", {}).get("type") == msg_type:
                    filtered.append(msg)
            else:
                filtered.append(msg)
                
        return filtered
    
    def get_game_state_messages(self) -> list[dict]:
        """Get all game state update messages.
        
        Returns:
            List of GameStateMessage objects
        """
        return self.filter_messages(msg_type="GREMessageType_GameStateMessage")
    
    def get_decision_messages(self) -> list[dict]:
        """Get all decision request messages (mulligan, targets, modes, etc.).
        
        Returns:
            List of decision request messages
        """
        decision_types = [
            "GREMessageType_MulliganReq",
            "GREMessageType_SelectTargetsReq",
            "GREMessageType_SelectNReq",
            "GREMessageType_GroupOptionReq",
            "GREMessageType_PromptReq",
        ]
        
        decisions = []
        for msg in self._messages:
            msg_type = msg.get("type") or msg.get("greToClientEvent", {}).get("type")
            if msg_type in decision_types:
                decisions.append(msg)
                
        return decisions
    
    def extract_metadata(self) -> dict[str, Any]:
        """Extract match metadata from replay.
        
        Returns:
            Dict with match info: players, decks, outcome, duration, etc.
        """
        if not self._loaded:
            self.load()
            
        metadata = {
            "file": str(self.replay_path),
            "message_count": len(self._messages),
            "timestamp": datetime.fromtimestamp(self.replay_path.stat().st_mtime),
        }
        
        # Try to extract match info from early messages
        for msg in self._messages[:50]:  # Check first 50 messages
            # Look for match info
            if "matchId" in msg:
                metadata["match_id"] = msg["matchId"]
            if "gameNumber" in msg:
                metadata["game_number"] = msg["gameNumber"]
                
            # Look for player info
            game_state = msg.get("gameStateMessage", {})
            if "players" in game_state:
                metadata["players"] = game_state["players"]
                break
                
        return metadata


def repair_replay_file(source_path: Path, output_path: Optional[Path] = None) -> Path:
    """Remove UI hover messages from replay file to fix playback errors.
    
    Arena's replay recorder sometimes has concurrency issues that cause
    "Timeout Exceeded" errors. Removing hover messages fixes most cases.
    
    Args:
        source_path: Original .rply file
        output_path: Output path (default: source_path with _repaired suffix)
        
    Returns:
        Path to repaired file
    """
    if output_path is None:
        output_path = source_path.with_stem(f"{source_path.stem}_repaired")
    
    logger.info(f"Repairing replay: {source_path.name} -> {output_path.name}")
    
    with open(source_path, 'r', encoding='utf-8') as infile, \
         open(output_path, 'w', encoding='utf-8') as outfile:
        
        removed = 0
        kept = 0
        
        for line in infile:
            # Skip lines containing onHover
            if '"onHover"' in line:
                removed += 1
                continue
                
            outfile.write(line)
            kept += 1
    
    logger.info(f"Repair complete: kept {kept} messages, removed {removed} hover messages")
    return output_path


def find_replay_files(replay_dir: Optional[Path] = None) -> list[Path]:
    """Find all Arena replay files in a directory.
    
    Args:
        replay_dir: Directory to search (default: Arena's Replays folder)
        
    Returns:
        List of .rply file paths sorted by modification time (newest first)
    """
    if replay_dir is None:
        # Default Arena replay location
        import os
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if appdata:
            replay_dir = Path(appdata).parent / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Replays"
        else:
            replay_dir = Path.cwd()
    
    replay_dir = Path(replay_dir)
    
    if not replay_dir.exists():
        logger.warning(f"Replay directory not found: {replay_dir}")
        return []
    
    # Find all .rply files
    replay_files = list(replay_dir.glob("*.rply"))
    
    # Sort by modification time (newest first)
    replay_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    
    logger.info(f"Found {len(replay_files)} replay files in {replay_dir}")
    return replay_files


if __name__ == "__main__":
    # Quick test
    import sys
    
    if len(sys.argv) > 1:
        replay_path = Path(sys.argv[1])
        replay = ArenaReplay(replay_path)
        
        if replay.load():
            print(f"\n=== Replay Info ===")
            metadata = replay.extract_metadata()
            for key, value in metadata.items():
                print(f"{key}: {value}")
            
            print(f"\n=== Decision Points ===")
            decisions = replay.get_decision_messages()
            for i, dec in enumerate(decisions, 1):
                msg_type = dec.get("type") or dec.get("greToClientEvent", {}).get("type")
                print(f"{i}. {msg_type}")
    else:
        print("Usage: python arena_replay.py <replay_file.rply>")
        print("\nSearching for replay files...")
        replays = find_replay_files()
        if replays:
            print(f"Found {len(replays)} replays:")
            for r in replays[:5]:
                print(f"  - {r.name}")
