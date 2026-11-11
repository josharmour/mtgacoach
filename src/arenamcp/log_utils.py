import json
import logging
import os
import re

logger = logging.getLogger(__name__)


def _resolve_player_log_path() -> str:
    """MTGA_LOG_PATH env override first, then the watcher's cross-platform
    default (Windows LOCALAPPDATA, WSL /mnt/c, Linux Steam/Proton incl.
    Flatpak, macOS ~/Library/Logs). The old Windows-only fallback here
    logged literal '%USERPROFILE%\\...' errors on every Linux session
    (#237)."""
    env_path = os.environ.get("MTGA_LOG_PATH", "").strip()
    if env_path:
        return env_path
    try:
        from arenamcp.watcher import DEFAULT_LOG_PATH

        return DEFAULT_LOG_PATH
    except Exception:
        import sys

        if sys.platform == "darwin":
            return os.path.join(
                os.path.expanduser("~"),
                "Library",
                "Logs",
                "Wizards Of The Coast",
                "MTGA",
                "Player.log",
            )
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            return os.path.join(
                os.path.dirname(local_appdata),
                "LocalLow",
                "Wizards Of The Coast",
                "MTGA",
                "Player.log",
            )
        return os.path.expandvars(r"%USERPROFILE%\AppData\LocalLow\Wizards Of The Coast\MTGA\Player.log")


PLAYER_LOG_PATH = _resolve_player_log_path()

# Account identity bound to each match at the moment its room event arrives,
# so a record written later attributes to the account that played the match
# rather than whoever happens to be logged in when the record is finally
# written (delayed flush, replay, or a switch before recording). Bounded:
# matches are recorded shortly after they end, so keeping the most recent
# bindings covers delayed flushes and replays without growing forever.
_MATCH_ACCOUNT_BINDINGS: dict[str, str] = {}
_MATCH_ACCOUNT_BINDING_LIMIT = 64


def bind_match_account(match_id: str, account_id: str) -> None:
    """Remember which account was logged in when this match started."""
    if not match_id or not account_id:
        return
    if (
        len(_MATCH_ACCOUNT_BINDINGS) >= _MATCH_ACCOUNT_BINDING_LIMIT
        and match_id not in _MATCH_ACCOUNT_BINDINGS
    ):
        _MATCH_ACCOUNT_BINDINGS.pop(next(iter(_MATCH_ACCOUNT_BINDINGS)))
    _MATCH_ACCOUNT_BINDINGS[str(match_id)] = str(account_id)


def account_bound_to_match(match_id: str) -> str | None:
    """Return the account bound to this match when it started, if any."""
    if not match_id:
        return None
    return _MATCH_ACCOUNT_BINDINGS.get(str(match_id))


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

        with open(PLAYER_LOG_PATH, encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Player.log accumulates across sessions and account switches, so the
        # FIRST authenticateResponse in the file can belong to an account that
        # logged out long ago. The local player is whoever logged in most
        # recently: prefer the clientId that follows the last
        # "[Accounts - Login] Logged in successfully" marker, and fall back to
        # the last authenticateResponse anywhere in the file.
        login_marker = "[Accounts - Login] Logged in successfully"
        marker_idx = content.rfind(login_marker)
        if marker_idx != -1:
            match = pattern.search(content, marker_idx)
            if match:
                client_id = match.group(1)
                logger.info(f"Found Local Player ID via Regex (most recent login): {client_id}")
                return client_id

        last_match = None
        for match in pattern.finditer(content):
            last_match = match
        if last_match:
            client_id = last_match.group(1)
            logger.info(f"Found Local Player ID via Regex (last authenticateResponse): {client_id}")
            return client_id

    except Exception as e:
        logger.error(f"Error scanning for local player ID: {e}")

    return None


def get_seat_mapping():
    """Scans Player.log backwards for the last MatchGameRoomStateChangedEvent."""
    if not os.path.exists(PLAYER_LOG_PATH):
        return None

    try:
        chunk_size = 100000  # 100KB chunks
        with open(PLAYER_LOG_PATH, "rb") as f:
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
                text = buffer.decode("utf-8", errors="ignore")
                if "MatchGameRoomStateChangedEvent" in text:
                    # We found it. Now split lines and find the LAST one in this block
                    lines = text.splitlines()
                    for line in reversed(lines):
                        if "MatchGameRoomStateChangedEvent" in line:
                            # Parse it
                            try:
                                start = line.find("{")
                                if start != -1:
                                    data = json.loads(line[start:])
                                    payload = (
                                        data.get("matchGameRoomStateChangedEvent", {})
                                        .get("gameRoomInfo", {})
                                        .get("gameRoomConfig", {})
                                    )

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
