
import json
import os
import re
import logging

logger = logging.getLogger(__name__)

# Default MTGA log path on Windows
# Use LOCALAPPDATA approach which is more reliable than APPDATA/../LocalLow
_local_appdata = os.environ.get("LOCALAPPDATA", "")
if _local_appdata:
    # LOCALAPPDATA is C:\Users\<user>\AppData\Local, we need LocalLow sibling
    PLAYER_LOG_PATH = os.path.join(
        os.path.dirname(_local_appdata),
        "LocalLow",
        "Wizards Of The Coast",
        "MTGA",
        "Player.log"
    )
else:
    PLAYER_LOG_PATH = os.path.expandvars(r"%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log")

def get_local_player_id():
    """Scans Player.log for the AuthenticateResponse to find the local user ID.
    
    Order of precedence:
    1. MTGA_PLAYER_ID environment variable
    2. settings.json "player_id" value
    3. Scanned value from Player.log
    """
    # 1. Environment Variable
    env_id = os.environ.get("MTGA_PLAYER_ID")
    if env_id:
        logger.info(f"Using Local Player ID from Environment: {env_id}")
        return env_id

    # 2. Settings.json (requires importing get_settings lazily to avoid circular imports if any)
    try:
        from arenamcp.settings import get_settings
        settings = get_settings()
        setting_id = settings.get("player_id")
        if setting_id:
            logger.info(f"Using Local Player ID from Settings: {setting_id}")
            return setting_id
    except Exception as e:
        logger.warning(f"Failed to check settings for player_id: {e}")

    # 3. Log Scan
    if not os.path.exists(PLAYER_LOG_PATH):
        logger.error(f"Player.log not found at {PLAYER_LOG_PATH}")
        return None

    try:
        # Regex to capture clientId from AuthenticateResponse structure
        # Matches: "authenticateResponse": { "clientId": "X", ... }
        # Handles potential whitespace and newlines
        pattern = re.compile(r'"authenticateResponse"\s*:\s*\{[^}]*"clientId"\s*:\s*"([^"]+)"')
        
        with open(PLAYER_LOG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
            match = pattern.search(content)
            if match:
                client_id = match.group(1)
                logger.info(f"Found Local Player ID via Regex: {client_id}")
                return client_id
                
            # Fallback for alternative formatting
            # sometimes it appears as "clientId": "X", "screenName": "Y" inside authenticateResponse
            # Assuming uniqueness of clientId in that block
            
    except Exception as e:
        logger.error(f"Error scanning for local player ID: {e}")
        
    return None

def get_seat_mapping():
    """Scans Player.log backwards for the last MatchGameRoomStateChangedEvent."""
    if not os.path.exists(PLAYER_LOG_PATH):
        return None

    try:
        chunk_size = 100000 # 100KB chunks
        with open(PLAYER_LOG_PATH, 'rb') as f:
            f.seek(0, 2)
            pos = f.tell()
            
            # Read backwards in chunks
            # Limit search to last ~5MB to avoid infinite hangs on huge logs
            bytes_read = 0
            max_bytes = 5 * 1024 * 1024 
            
            buffer = b""
            
            while pos > 0 and bytes_read < max_bytes:
                to_read = min(chunk_size, pos)
                pos -= to_read
                f.seek(pos)
                chunk = f.read(to_read)
                buffer = chunk + buffer
                bytes_read += to_read
                
                # Check for event
                # We do this by decoding and splitting lines. 
                # Ideally we find the *last* occurrence in the file.
                text = buffer.decode('utf-8', errors='ignore')
                if "MatchGameRoomStateChangedEvent" in text:
                    # We found it. Now split lines and find the LAST one in this block
                    lines = text.splitlines()
                    for line in reversed(lines):
                        if "MatchGameRoomStateChangedEvent" in line:
                            # Parse it
                            try:
                                start = line.find('{')
                                if start != -1:
                                    data = json.loads(line[start:])
                                    payload = data.get("matchGameRoomStateChangedEvent", {}).get("gameRoomInfo", {}).get("gameRoomConfig", {})
                                    
                                    # Payload might be a nested JSON string sometimes? 
                                    # Usually it's direct object in the log wrapper.
                                    
                                    players = payload.get("reservedPlayers", [])
                                    if players:
                                        mapping = {}
                                        for p in players:
                                            uid = p.get("userId")
                                            seat = p.get("systemSeatId")
                                            if uid and seat:
                                                mapping[uid] = seat
                                        
                                        logger.info(f"Found Seat Mapping: {mapping}")
                                        return mapping
                            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                                logger.debug("Failed to parse seat mapping line: %s", exc)
                                continue
            
    except Exception as e:
        logger.error(f"Error scanning for seat mapping: {e}")
        
    return None

def detect_local_seat():
    """Determines the local player's seat ID from the log."""
    local_id = get_local_player_id()
    if not local_id:
        logger.warning("Could not find local player ID")
        return None
        
    seat_map = get_seat_mapping()
    if not seat_map:
        logger.warning("Could not find match seat mapping")
        return None
        
    seat = seat_map.get(local_id)
    if seat:
        logger.info(f"Detected Local Seat: {seat}")
        return seat
    
    return None

if __name__ == "__main__":
    # Test run
    logging.basicConfig(level=logging.INFO)
    print(f"Detected Seat: {detect_local_seat()}")
