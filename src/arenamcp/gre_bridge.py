"""GRE Bridge — named-pipe client for direct action submission via BepInEx plugin.

Connects to the MtgaCoachBridge BepInEx plugin's named pipe and submits
GRE actions directly to MTGA without mouse clicks.

The bridge is the primary execution backend for autopilot when available,
falling back to mouse/keyboard input when disconnected.
"""

from __future__ import annotations

import json
import logging
import struct
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Named pipe path (Windows named pipe)
PIPE_NAME = r"\\.\pipe\mtgacoach_gre"
PIPE_TIMEOUT_MS = 3000
COMMAND_TIMEOUT = 5.0


class GREBridgeError(Exception):
    """Error communicating with the GRE bridge plugin."""
    pass


class GREBridge:
    """Client for the MtgaCoachBridge BepInEx plugin.

    Communicates via Windows named pipe using newline-delimited JSON.
    Thread-safe for single-command-at-a-time use.
    """

    def __init__(self):
        self._pipe = None
        self._connected = False
        self._last_connect_attempt = 0.0
        self._reconnect_cooldown = 2.0  # seconds between reconnect attempts

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Attempt to connect to the GRE bridge pipe.

        Returns True if connected, False if the pipe is not available.
        Respects a cooldown between reconnect attempts.
        """
        if self._connected:
            return True

        now = time.monotonic()
        if now - self._last_connect_attempt < self._reconnect_cooldown:
            return False

        self._last_connect_attempt = now

        try:
            import ctypes
            import ctypes.wintypes

            GENERIC_READ = 0x80000000
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value

            kernel32 = ctypes.windll.kernel32

            handle = kernel32.CreateFileW(
                PIPE_NAME,
                GENERIC_READ | GENERIC_WRITE,
                0,  # no sharing
                None,  # default security
                OPEN_EXISTING,
                0,  # default attributes
                None,
            )

            if handle == INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                logger.debug(f"GRE bridge pipe not available (error {err})")
                return False

            # Wrap the raw handle in a Python file object
            import msvcrt
            import os
            fd = msvcrt.open_osfhandle(handle, 0)
            self._pipe_handle = handle
            self._pipe_fd = fd
            # Open for binary read/write
            self._pipe_file = os.fdopen(fd, "r+b", buffering=0)
            self._connected = True

            # Verify with ping
            try:
                resp = self._send_command({"action": "ping"})
                if resp.get("ok"):
                    logger.info(f"GRE bridge connected (plugin v{resp.get('version', '?')})")
                    return True
                else:
                    logger.warning(f"GRE bridge ping failed: {resp}")
                    self.disconnect()
                    return False
            except Exception as e:
                logger.warning(f"GRE bridge ping failed: {e}")
                self.disconnect()
                return False

        except ImportError:
            # Not on Windows — named pipes not available
            logger.debug("GRE bridge requires Windows (named pipes)")
            self._reconnect_cooldown = 60.0  # Don't spam on non-Windows
            return False
        except Exception as e:
            logger.debug(f"GRE bridge connect error: {e}")
            return False

    def disconnect(self):
        """Close the pipe connection."""
        self._connected = False
        try:
            if self._pipe_file:
                self._pipe_file.close()
        except Exception:
            pass
        self._pipe_file = None
        self._pipe_fd = None
        self._pipe_handle = None

    def _send_command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON command and read the JSON response.

        Raises GREBridgeError on communication failure.
        """
        if not self._connected or not self._pipe_file:
            raise GREBridgeError("Not connected")

        try:
            line = json.dumps(cmd, separators=(",", ":")) + "\n"
            self._pipe_file.write(line.encode("utf-8"))
            self._pipe_file.flush()

            # Read response line
            response_bytes = b""
            while True:
                chunk = self._pipe_file.read(1)
                if not chunk:
                    raise GREBridgeError("Pipe closed")
                if chunk == b"\n":
                    break
                response_bytes += chunk

            return json.loads(response_bytes.decode("utf-8"))

        except (BrokenPipeError, OSError, IOError) as e:
            self.disconnect()
            raise GREBridgeError(f"Pipe communication error: {e}")

    def _send_safe(self, cmd: dict[str, Any]) -> dict[str, Any]:
        """Send command with auto-reconnect on failure."""
        if not self._connected:
            if not self.connect():
                raise GREBridgeError("Not connected to GRE bridge")

        try:
            return self._send_command(cmd)
        except GREBridgeError:
            # One retry after reconnect
            if self.connect():
                return self._send_command(cmd)
            raise

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def ping(self) -> Optional[str]:
        """Ping the bridge. Returns plugin version or None."""
        try:
            resp = self._send_safe({"action": "ping"})
            if resp.get("ok"):
                return resp.get("version")
        except GREBridgeError:
            pass
        return None

    def get_pending_actions(self) -> Optional[dict[str, Any]]:
        """Get the current pending actions from the game.

        Returns a dict with:
        - has_pending: bool
        - request_type: str (e.g. "ActionsAvailable")
        - actions: list of action dicts (if ActionsAvailable)
        - can_pass: bool

        Returns None on error.
        """
        try:
            resp = self._send_safe({"action": "get_pending_actions"})
            if resp.get("ok"):
                return resp
        except GREBridgeError as e:
            logger.debug(f"get_pending_actions failed: {e}")
        return None

    def submit_action_by_index(
        self,
        action_index: int,
        auto_pass: bool = False,
    ) -> bool:
        """Submit an action by its index in the pending actions list.

        Args:
            action_index: Index into the actions array from get_pending_actions.
            auto_pass: Whether to auto-pass priority after this action.

        Returns:
            True if the action was submitted successfully.
        """
        try:
            resp = self._send_safe({
                "action": "submit_action",
                "action_index": action_index,
                "auto_pass": auto_pass,
            })
            if resp.get("ok"):
                logger.info(
                    f"GRE bridge submitted action [{action_index}]: "
                    f"{resp.get('submitted_type')} grpId={resp.get('submitted_grp_id')}"
                )
                return True
            else:
                logger.warning(f"GRE bridge submit failed: {resp.get('error')}")
                return False
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit error: {e}")
            return False

    def submit_action_by_match(
        self,
        action_type: str,
        grp_id: int = 0,
        instance_id: int = 0,
        ability_grp_id: int = 0,
        auto_pass: bool = False,
    ) -> bool:
        """Submit an action by matching its fields against pending actions.

        Fetches pending actions from the plugin, finds the best match,
        and submits by index.

        Args:
            action_type: GRE action type string (e.g. "Cast", "Play", "Pass").
            grp_id: Card group ID to match (0 = don't match).
            instance_id: Instance ID to match (0 = don't match).
            ability_grp_id: Ability group ID to match (0 = don't match).
            auto_pass: Whether to auto-pass priority.

        Returns:
            True if matched and submitted.
        """
        pending = self.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            logger.warning("No pending actions to match against")
            return False

        actions = pending.get("actions", [])
        if not actions:
            logger.warning("Pending request has no actions")
            return False

        # Find best matching action
        best_idx = self._find_matching_action(
            actions, action_type, grp_id, instance_id, ability_grp_id
        )

        if best_idx is None:
            logger.warning(
                f"No match for {action_type} grpId={grp_id} instanceId={instance_id} "
                f"among {len(actions)} actions"
            )
            return False

        return self.submit_action_by_index(best_idx, auto_pass=auto_pass)

    def submit_pass(self) -> bool:
        """Submit a pass action.

        Returns True if pass was submitted.
        """
        try:
            resp = self._send_safe({"action": "submit_pass"})
            if resp.get("ok"):
                logger.info("GRE bridge submitted pass")
                return True
            else:
                logger.warning(f"GRE bridge pass failed: {resp.get('error')}")
                return False
        except GREBridgeError as e:
            logger.warning(f"GRE bridge pass error: {e}")
            return False

    # -------------------------------------------------------------------
    # Matching logic
    # -------------------------------------------------------------------

    @staticmethod
    def _find_matching_action(
        actions: list[dict],
        action_type: str,
        grp_id: int,
        instance_id: int,
        ability_grp_id: int,
    ) -> Optional[int]:
        """Find the best matching action index.

        Scoring:
        - action_type match is required
        - Each additional field match adds 1 to score
        - Returns index of highest-scoring action, or None
        """
        # Normalize action_type: accept both "Cast" and "ActionType_Cast"
        at_normalized = action_type
        if not at_normalized.startswith("ActionType_"):
            # Also accept the short form from protobuf enum
            pass  # The plugin returns enum names like "Cast", "Play", "Pass"

        best_idx = None
        best_score = -1

        for idx, act in enumerate(actions):
            act_type = act.get("actionType", "")

            # Match action type (handle both "Cast" and "ActionType_Cast")
            if act_type != at_normalized and f"ActionType_{act_type}" != at_normalized and act_type != f"ActionType_{at_normalized}":
                # Try removing prefix
                act_short = act_type.replace("ActionType_", "")
                at_short = at_normalized.replace("ActionType_", "")
                if act_short != at_short:
                    continue

            score = 0

            if grp_id and act.get("grpId") == grp_id:
                score += 1
            if instance_id and act.get("instanceId") == instance_id:
                score += 1
            if ability_grp_id and act.get("abilityGrpId") == ability_grp_id:
                score += 1

            if score > best_score:
                best_score = score
                best_idx = idx

        return best_idx


# Module-level singleton for convenience
_bridge: Optional[GREBridge] = None


def get_bridge() -> GREBridge:
    """Get or create the module-level GRE bridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = GREBridge()
    return _bridge
