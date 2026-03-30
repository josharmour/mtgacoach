"""GRE Bridge — named-pipe client for direct action submission via BepInEx plugin.

Connects to the MtgaCoachBridge BepInEx plugin's named pipe and submits
GRE actions directly to MTGA without mouse clicks.

The bridge is the primary execution backend for autopilot when available,
falling back to mouse/keyboard input when disconnected.

Also provides BridgeDecisionPoller for proactive decision detection —
polling the bridge to know immediately when a game decision is pending
and what the valid options are, replacing reactive log-diff detection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Named pipe path (Windows named pipe)
# Python is the SERVER, BepInEx plugin is the CLIENT.
# This avoids MTGA internals grabbing the pipe and scene transitions
# killing the pipe server inside the game process.
PIPE_NAME = r"\\.\pipe\mtgacoach_bridge_v2"
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
        self._server_pipe_handle = None  # Raw HANDLE for the server pipe (created once)
        self._pipe_created = False  # Whether the server pipe exists

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Create a named pipe SERVER and wait for the BepInEx plugin to connect.

        Python owns the pipe (server). The BepInEx plugin connects as client.
        This avoids MTGA-internal issues (mystery clients, scene transitions,
        MonoBehaviour lifecycle) since the pipe lives in the Python process.

        Two-phase: first call creates the pipe, subsequent calls check if a
        client connected. Non-blocking — safe to call from the polling loop.
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

            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CreateNamedPipeW.restype = ctypes.wintypes.HANDLE
            kernel32.ConnectNamedPipe.restype = ctypes.wintypes.BOOL
            kernel32.CloseHandle.restype = ctypes.wintypes.BOOL

            PIPE_ACCESS_DUPLEX = 0x00000003
            FILE_FLAG_OVERLAPPED = 0x40000000
            PIPE_TYPE_BYTE = 0x00000000
            PIPE_READMODE_BYTE = 0x00000000
            PIPE_NOWAIT = 0x00000001
            INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
            BUFFER_SIZE = 4096
            ERROR_PIPE_CONNECTED = 535
            ERROR_PIPE_LISTENING = 536
            ERROR_NO_DATA = 232

            # Phase 1: Create the pipe if it doesn't exist yet
            if not self._pipe_created:
                handle = kernel32.CreateNamedPipeW(
                    PIPE_NAME,
                    PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
                    PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_NOWAIT,
                    1,           # max instances
                    BUFFER_SIZE,
                    BUFFER_SIZE,
                    0,           # default timeout
                    None,        # default security
                )

                if handle == INVALID_HANDLE_VALUE:
                    err = ctypes.get_last_error()
                    logger.debug(f"GRE bridge CreateNamedPipe failed (error {err})")
                    return False

                self._server_pipe_handle = handle
                self._pipe_created = True
                logger.info(f"GRE bridge pipe server created: {PIPE_NAME}")

                # Initiate non-blocking ConnectNamedPipe
                kernel32.ConnectNamedPipe(handle, None)
                # With PIPE_NOWAIT, this returns immediately.
                # Check error: LISTENING means waiting, CONNECTED means ready.

            # Phase 2: Check if a client connected
            handle = self._server_pipe_handle
            result = kernel32.ConnectNamedPipe(handle, None)
            err = ctypes.get_last_error()

            if not result:
                if err == ERROR_PIPE_CONNECTED:
                    pass  # Client already connected — success!
                elif err == ERROR_NO_DATA:
                    pass  # Client connected (NOWAIT mode)
                elif err == ERROR_PIPE_LISTENING:
                    # Still waiting for client — not connected yet
                    return False
                else:
                    logger.debug(f"GRE bridge ConnectNamedPipe check: error {err}")
                    return False

            # Client connected! Wrap handle in a file object.
            # Switch pipe back to blocking mode for normal I/O
            PIPE_WAIT = 0x00000000
            mode = ctypes.wintypes.DWORD(PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT)
            kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None)

            import msvcrt
            import os
            fd = msvcrt.open_osfhandle(handle, 0)
            self._pipe_handle = handle
            self._pipe_fd = fd
            self._pipe_file = os.fdopen(fd, "r+b", buffering=0)
            self._connected = True
            self._pipe_created = False  # Reset so next disconnect creates fresh

            logger.info("GRE bridge: plugin connected to our pipe server")

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
            logger.debug("GRE bridge requires Windows (named pipes)")
            self._reconnect_cooldown = 60.0
            return False
        except Exception as e:
            logger.info(f"GRE bridge connect error: {e}")
            return False

    def disconnect(self):
        """Close the pipe connection and server handle."""
        self._connected = False
        self._pipe_created = False
        try:
            if self._pipe_file:
                self._pipe_file.close()
        except Exception:
            pass
        # If server pipe was created but no file wrapper, close raw handle
        if self._server_pipe_handle and not self._pipe_file:
            try:
                import ctypes
                ctypes.WinDLL('kernel32').CloseHandle(self._server_pipe_handle)
            except Exception:
                pass
        self._pipe_file = None
        self._pipe_fd = None
        self._pipe_handle = None
        self._server_pipe_handle = None

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

            # Strip UTF-8 BOM if present (C# StreamWriter may emit one)
            text = response_bytes.decode("utf-8-sig")
            return json.loads(text)

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
    # Phase 2: new game state commands
    # -------------------------------------------------------------------

    def get_game_state(self) -> Optional[dict[str, Any]]:
        """Get full game state directly from MTGA's MtgGameState.

        Returns the complete game state including zones, cards, players,
        turn info, combat, timers, and designations — bypassing log parsing.

        Returns None if not connected or game not active.
        """
        try:
            resp = self._send_safe({"action": "get_game_state"})
            if resp.get("ok"):
                return resp
            else:
                logger.debug(f"get_game_state: {resp.get('error')}")
                return None
        except GREBridgeError as e:
            logger.debug(f"get_game_state error: {e}")
            return None

    def get_timer_state(self) -> Optional[dict[str, Any]]:
        """Get timer/chess clock state from the game.

        Returns per-player timer info including time remaining,
        timer type, and running state.
        """
        try:
            resp = self._send_safe({"action": "get_timer_state"})
            if resp.get("ok"):
                return resp
            else:
                logger.debug(f"get_timer_state: {resp.get('error')}")
                return None
        except GREBridgeError as e:
            logger.debug(f"get_timer_state error: {e}")
            return None

    def get_match_info(self) -> Optional[dict[str, Any]]:
        """Get match metadata (game number, format, stage, etc.).

        Returns match-level info not available from individual GRE messages.
        """
        try:
            resp = self._send_safe({"action": "get_match_info"})
            if resp.get("ok"):
                return resp
            else:
                logger.debug(f"get_match_info: {resp.get('error')}")
                return None
        except GREBridgeError as e:
            logger.debug(f"get_match_info error: {e}")
            return None

    # -------------------------------------------------------------------
    # Phase 3: Replay recording commands
    # -------------------------------------------------------------------

    def enable_replay(self, replay_name: str = "mtgacoach") -> Optional[dict[str, Any]]:
        """Enable MTGA's built-in replay recording.

        Replays are saved as .rply files (line-delimited JSON with timestamps).
        """
        try:
            cmd = {"action": "enable_replay", "replay_name": replay_name}
            resp = self._send_safe(cmd)
            if resp.get("ok"):
                logger.info(f"Replay recording enabled: {resp.get('replay_folder')}")
                return resp
            else:
                logger.warning(f"enable_replay failed: {resp.get('error')}")
                return None
        except GREBridgeError as e:
            logger.debug(f"enable_replay error: {e}")
            return None

    def disable_replay(self) -> bool:
        """Disable replay recording."""
        try:
            resp = self._send_safe({"action": "disable_replay"})
            return resp.get("ok", False)
        except GREBridgeError:
            return False

    def get_replay_status(self) -> Optional[dict[str, Any]]:
        """Check if replay recording is active and get current status."""
        try:
            resp = self._send_safe({"action": "get_replay_status"})
            if resp.get("ok"):
                return resp
            return None
        except GREBridgeError:
            return None

    def list_replays(self) -> Optional[dict[str, Any]]:
        """List available replay files (most recent first, max 50)."""
        try:
            resp = self._send_safe({"action": "list_replays"})
            if resp.get("ok"):
                return resp
            return None
        except GREBridgeError:
            return None

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


# -------------------------------------------------------------------
# Bridge request type → decision_context type mapping
# -------------------------------------------------------------------

# Maps Plugin.cs request.Type.ToString() values to the decision_context
# "type" strings that coach.py _format_decision_lines() already handles.
_BRIDGE_REQUEST_TO_DECISION_TYPE: dict[str, str] = {
    "ActionsAvailable": "actions_available",
    "ActionsAvailableReq": "actions_available",
    "ActionsAvailableRequest": "actions_available",
    "SelectTargets": "target_selection",
    "SelectTargetsReq": "target_selection",
    "SelectTargetsRequest": "target_selection",
    "Mulligan": "mulligan",
    "MulliganReq": "mulligan",
    "MulliganRequest": "mulligan",
    "Group": "group_selection",
    "GroupReq": "group_selection",
    "GroupRequest": "group_selection",
    "GroupOption": "modal_choice",
    "GroupOptionReq": "modal_choice",
    "GroupOptionRequest": "modal_choice",
    "DeclareAttackers": "declare_attackers",
    "DeclareAttackersReq": "declare_attackers",
    "DeclareAttackersRequest": "declare_attackers",
    "DeclareBlockers": "declare_blockers",
    "DeclareBlockersReq": "declare_blockers",
    "DeclareBlockersRequest": "declare_blockers",
    "Search": "search",
    "SearchReq": "search",
    "SearchRequest": "search",
    "Distribution": "distribution",
    "DistributionReq": "distribution",
    "DistributionRequest": "distribution",
    "NumericInput": "numeric_input",
    "NumericInputReq": "numeric_input",
    "NumericInputRequest": "numeric_input",
    "Prompt": "prompt",
    "PromptReq": "prompt",
    "PromptRequest": "prompt",
    "PayCosts": "pay_costs",
    "PayCostsReq": "pay_costs",
    "PayCostsRequest": "pay_costs",
    "AssignDamage": "assign_damage",
    "AssignDamageReq": "assign_damage",
    "AssignDamageRequest": "assign_damage",
    "OrderCombatDamage": "order_combat_damage",
    "OrderCombatDamageReq": "order_combat_damage",
    "OrderCombatDamageRequest": "order_combat_damage",
    "SelectN": "selection_generic",
    "SelectNReq": "selection_generic",
    "SelectNRequest": "selection_generic",
    "ChooseStartingPlayer": "choose_starting_player",
    "ChooseStartingPlayerReq": "choose_starting_player",
    "ChooseStartingPlayerRequest": "choose_starting_player",
    "SelectReplacement": "select_replacement",
    "SelectReplacementReq": "select_replacement",
    "SelectReplacementRequest": "select_replacement",
    "SelectCounters": "select_counters",
    "SelectCountersReq": "select_counters",
    "SelectCountersRequest": "select_counters",
    "Order": "order_triggers",
    "OrderReq": "order_triggers",
    "OrderRequest": "order_triggers",
    "OptionalActionMessage": "optional_action",
    "CastingTimeOptions": "casting_time_options",
    "CastingTimeOptionsReq": "casting_time_options",
    "CastingTimeOptionRequest": "casting_time_options",
    "SelectNGroup": "select_n_group",
    "SelectNGroupReq": "select_n_group",
    "SelectNGroupRequest": "select_n_group",
    "SelectFromGroups": "select_from_groups",
    "SelectFromGroupsReq": "select_from_groups",
    "SelectFromGroupsRequest": "select_from_groups",
    "SearchFromGroups": "search_from_groups",
    "SearchFromGroupsReq": "search_from_groups",
    "SearchFromGroupsRequest": "search_from_groups",
    "Gather": "gather",
    "GatherReq": "gather",
    "GatherRequest": "gather",
    "RevealHand": "reveal_hand",
    "RevealHandReq": "reveal_hand",
    "RevealHandRequest": "reveal_hand",
}

# Reverse mapping for human-readable pending_decision labels
_BRIDGE_REQUEST_TO_LABEL: dict[str, str] = {
    "ActionsAvailable": "Action Required",
    "ActionsAvailableReq": "Action Required",
    "ActionsAvailableRequest": "Action Required",
    "SelectTargets": "Select Targets",
    "SelectTargetsReq": "Select Targets",
    "SelectTargetsRequest": "Select Targets",
    "Mulligan": "Mulligan",
    "MulliganReq": "Mulligan",
    "MulliganRequest": "Mulligan",
    "Group": "Group Selection",
    "GroupReq": "Group Selection",
    "GroupRequest": "Group Selection",
    "GroupOption": "Choose Mode",
    "GroupOptionReq": "Choose Mode",
    "GroupOptionRequest": "Choose Mode",
    "DeclareAttackers": "Declare Attackers",
    "DeclareAttackersReq": "Declare Attackers",
    "DeclareAttackersRequest": "Declare Attackers",
    "DeclareBlockers": "Declare Blockers",
    "DeclareBlockersReq": "Declare Blockers",
    "DeclareBlockersRequest": "Declare Blockers",
    "Search": "Search Library",
    "SearchReq": "Search Library",
    "SearchRequest": "Search Library",
    "Distribution": "Distribute",
    "DistributionReq": "Distribute",
    "DistributionRequest": "Distribute",
    "NumericInput": "Choose Number",
    "NumericInputReq": "Choose Number",
    "NumericInputRequest": "Choose Number",
    "Prompt": "Prompt",
    "PromptReq": "Prompt",
    "PromptRequest": "Prompt",
    "PayCosts": "Pay Costs",
    "PayCostsReq": "Pay Costs",
    "PayCostsRequest": "Pay Costs",
    "AssignDamage": "Assign Damage",
    "AssignDamageReq": "Assign Damage",
    "AssignDamageRequest": "Assign Damage",
    "OrderCombatDamage": "Order Damage",
    "OrderCombatDamageReq": "Order Damage",
    "OrderCombatDamageRequest": "Order Damage",
    "SelectN": "Select Cards",
    "SelectNReq": "Select Cards",
    "SelectNRequest": "Select Cards",
    "ChooseStartingPlayer": "Play or Draw",
    "ChooseStartingPlayerReq": "Play or Draw",
    "ChooseStartingPlayerRequest": "Play or Draw",
    "SelectReplacement": "Order Replacement",
    "SelectReplacementReq": "Order Replacement",
    "SelectReplacementRequest": "Order Replacement",
    "SelectCounters": "Select Counters",
    "SelectCountersReq": "Select Counters",
    "SelectCountersRequest": "Select Counters",
    "Order": "Order Triggers",
    "OrderReq": "Order Triggers",
    "OrderRequest": "Order Triggers",
    "OptionalActionMessage": "Optional Action",
    "CastingTimeOptions": "Casting Option",
    "CastingTimeOptionsReq": "Casting Option",
    "CastingTimeOptionRequest": "Casting Option",
}

_ACTIONS_AVAILABLE_BRIDGE_REQUESTS = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}

_NON_ACTIONABLE_BRIDGE_REQUESTS = {
    "Intermission",
    "IntermissionReq",
    "IntermissionRequest",
}


def _get_bridge_decision_type(
    request_type: Optional[str],
    request_class: Optional[str] = None,
) -> Optional[str]:
    return (
        _BRIDGE_REQUEST_TO_DECISION_TYPE.get(request_type or "")
        or _BRIDGE_REQUEST_TO_DECISION_TYPE.get(request_class or "")
    )


def _get_bridge_request_label(
    request_type: Optional[str],
    request_class: Optional[str] = None,
) -> str:
    return (
        _BRIDGE_REQUEST_TO_LABEL.get(request_type or "")
        or _BRIDGE_REQUEST_TO_LABEL.get(request_class or "")
        or request_type
        or request_class
        or "Unknown"
    )


def _is_non_actionable_bridge_request(
    request_type: Optional[str],
    request_class: Optional[str] = None,
) -> bool:
    return (
        (request_type or "") in _NON_ACTIONABLE_BRIDGE_REQUESTS
        or (request_class or "") in _NON_ACTIONABLE_BRIDGE_REQUESTS
    )


class BridgeDecisionPoller:
    """Polls GRE bridge for decision state changes.

    Detects when a new game decision is pending (or cleared) by polling
    get_pending_actions() and comparing to previous state. When connected,
    this replaces the reactive log-diff trigger detection for decisions.

    Usage in the coaching loop:
        poller = BridgeDecisionPoller(bridge)
        # Each iteration:
        trigger = poller.poll()
        if trigger:
            poller.enrich_snapshot(curr_state)
    """

    _MAX_CONSECUTIVE_ERRORS = 30  # High tolerance — commands may time out when Update() is dead

    def __init__(self, bridge: GREBridge):
        self._bridge = bridge
        self._last_request_type: Optional[str] = None
        self._last_action_sig: Optional[str] = None
        self._last_has_pending: bool = False
        self._last_poll_result: Optional[dict[str, Any]] = None
        self._consecutive_errors: int = 0
        self._fallback_mode: bool = False
        self._was_connected: bool = False

    @property
    def connected(self) -> bool:
        """Whether bridge polling is active (connected and not in fallback)."""
        return self._bridge.connected and not self._fallback_mode

    def poll(self) -> Optional[dict[str, Any]]:
        """Poll bridge for decision state changes.

        Returns a trigger dict when the decision state changes, None otherwise.

        Returns:
            None if no change, or a dict:
            {
                "trigger": "decision_required" | "decision_cleared",
                "request_type": str | None,
                "decision_type": str | None,  # mapped decision_context type
                "has_pending": bool,
                "actions": list[dict],  # full action data (ActionsAvailable only)
                "can_pass": bool,
                "can_cancel": bool,
            }
        """
        if self._fallback_mode:
            # Periodically try to recover (every ~10 polls from caller)
            if self._bridge.connected or self._bridge.connect():
                self._fallback_mode = False
                self._consecutive_errors = 0
                logger.info("Bridge decision polling recovered from fallback")
            else:
                return None

        # Ensure connection
        if not self._bridge.connected:
            if not self._bridge.connect():
                if self._was_connected:
                    self._was_connected = False
                    logger.info("Bridge disconnected, falling back to log-based detection")
                # NOT connected yet — don't count as error, just wait.
                # The bridge may not exist yet (MTGA still starting).
                return None

        if not self._was_connected:
            self._was_connected = True
            logger.info("Bridge decision detection active")

        # Poll
        resp = self._bridge.get_pending_actions()
        if resp is None:
            self._consecutive_errors += 1
            # Only enter fallback if we WERE connected and lost the connection.
            # Don't give up during initial connection attempts.
            if self._was_connected and self._consecutive_errors >= self._MAX_CONSECUTIVE_ERRORS:
                self._fallback_mode = True
                self._was_connected = False
                logger.info(
                    f"Bridge polling failed {self._consecutive_errors}x, "
                    "entering fallback (will recover automatically)"
                )
            return None

        self._consecutive_errors = 0
        self._last_poll_result = resp

        raw_has_pending = resp.get("has_pending", False)
        request_type = resp.get("request_type") if raw_has_pending else None
        request_class = resp.get("request_class") if raw_has_pending else None
        actions = resp.get("actions", [])
        ignored_request = _is_non_actionable_bridge_request(request_type, request_class)
        has_pending = raw_has_pending and not ignored_request
        if ignored_request:
            logger.debug(
                f"Bridge ignoring non-actionable request: {request_type or request_class}"
            )
            request_type = None
            request_class = None
            actions = []
        action_sig = self._compute_action_sig(actions) if actions else None

        # Detect state change
        changed = False
        trigger_name: Optional[str] = None

        if has_pending and not self._last_has_pending:
            # New decision appeared
            changed = True
            trigger_name = "decision_required"
        elif not has_pending and self._last_has_pending:
            # Decision was cleared
            changed = True
            trigger_name = "decision_cleared"
        elif has_pending and request_type != self._last_request_type:
            # Different request type (e.g., ActionsAvailable → SelectTargets)
            changed = True
            trigger_name = "decision_required"
        elif has_pending and action_sig != self._last_action_sig:
            # Same request type but different actions (new legal actions set)
            changed = True
            trigger_name = "decision_required"

        # Update tracked state
        self._last_has_pending = has_pending
        self._last_request_type = request_type
        self._last_action_sig = action_sig

        if not changed:
            return None

        decision_type = _get_bridge_decision_type(request_type, request_class)

        result = {
            "trigger": trigger_name,
            "request_type": request_type,
            "request_class": request_class,
            "decision_type": decision_type,
            "has_pending": has_pending,
            "actions": actions,
            "can_pass": resp.get("can_pass", False),
            "can_cancel": resp.get("can_cancel", False),
            "allow_undo": resp.get("allow_undo", False),
            "request_payload": resp.get("request_payload") if has_pending else None,
            "decision_context": resp.get("decision_context") if has_pending else None,
        }

        if trigger_name == "decision_required":
            label = _get_bridge_request_label(request_type, request_class)
            logger.info(
                f"Bridge detected decision: {label} "
                f"(type={request_type}, actions={len(actions)})"
            )
        else:
            logger.info(f"Bridge detected decision cleared (was: {self._last_request_type})")

        return result

    def enrich_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Overlay bridge decision data onto a game state snapshot dict.

        Adds bridge metadata fields and enriches decision/action data
        when bridge provides fresher information than log parsing.

        Modifies the snapshot dict in-place.
        """
        snapshot["_bridge_connected"] = self.connected

        poll = self._last_poll_result
        if poll is None:
            return

        has_pending = poll.get("has_pending", False)
        request_type = poll.get("request_type")
        request_class = poll.get("request_class")
        actions = poll.get("actions", [])
        request_payload = poll.get("request_payload")
        bridge_decision_context = poll.get("decision_context") or {}

        if has_pending and _is_non_actionable_bridge_request(request_type, request_class):
            has_pending = False
            request_type = None
            request_class = None
            actions = []
            request_payload = None
            bridge_decision_context = {}

        snapshot["_bridge_request_type"] = request_type if has_pending else None
        snapshot["_bridge_request_class"] = request_class if has_pending else None
        snapshot["_bridge_actions"] = actions if actions else None
        snapshot["_bridge_can_pass"] = poll.get("can_pass", False)
        snapshot["_bridge_can_cancel"] = poll.get("can_cancel", False)
        snapshot["_bridge_allow_undo"] = poll.get("allow_undo", False)
        snapshot["_bridge_request_payload"] = (
            request_payload if has_pending and request_payload else None
        )

        if not has_pending:
            return

        # Enrich pending_decision if log hasn't caught up yet
        if not snapshot.get("pending_decision") and request_type:
            label = _get_bridge_request_label(request_type, request_class)
            snapshot["pending_decision"] = label
            logger.debug(f"Bridge set pending_decision: {label}")

        # Enrich decision_context type from bridge's authoritative request_type
        decision_type = _get_bridge_decision_type(request_type, request_class)
        existing_ctx = snapshot.get("decision_context") or {}
        if bridge_decision_context:
            snapshot["decision_context"] = {
                **existing_ctx,
                **bridge_decision_context,
                "_bridge_source": True,
            }
            existing_ctx = snapshot["decision_context"]

        if decision_type:
            existing_type = existing_ctx.get("type")
            if not existing_type or existing_type == "unknown_req":
                snapshot["decision_context"] = {
                    **existing_ctx,
                    "type": decision_type,
                    "_bridge_source": True,
                }
                logger.debug(
                    f"Bridge enriched decision_context: {existing_type} → {decision_type}"
                )

        # Enrich legal_actions_raw with bridge action data when available
        # Bridge actions have the latest castability flags + autotap solutions
        if actions:
            snapshot["_bridge_actions"] = actions

    @staticmethod
    def _compute_action_sig(actions: list[dict[str, Any]]) -> str:
        """Compute a signature for an action list to detect changes."""
        # Use a lightweight hash of action types + grpIds + instanceIds
        parts = []
        for a in actions:
            parts.append(
                f"{a.get('actionType', '')}:{a.get('grpId', 0)}:"
                f"{a.get('instanceId', 0)}:{a.get('abilityGrpId', 0)}"
            )
        sig_str = "|".join(parts)
        return hashlib.md5(sig_str.encode()).hexdigest()[:12]

    def reset(self) -> None:
        """Reset tracked state (e.g., on match start/end)."""
        self._last_request_type = None
        self._last_action_sig = None
        self._last_has_pending = False
        self._last_poll_result = None


# Module-level singleton for convenience
_bridge: Optional[GREBridge] = None
_poller: Optional[BridgeDecisionPoller] = None


def get_bridge() -> GREBridge:
    """Get or create the module-level GRE bridge singleton."""
    global _bridge
    if _bridge is None:
        _bridge = GREBridge()
    return _bridge


def get_poller() -> BridgeDecisionPoller:
    """Get or create the module-level bridge decision poller singleton."""
    global _poller
    if _poller is None:
        _poller = BridgeDecisionPoller(get_bridge())
    return _poller
