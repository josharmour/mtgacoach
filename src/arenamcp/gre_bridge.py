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
    "ActionsAvailableRequest": "actions_available",
    "SelectTargetsReq": "target_selection",
    "MulliganReq": "mulligan",
    "GroupReq": "group_selection",
    "GroupOptionReq": "modal_choice",
    "DeclareAttackersReq": "declare_attackers",
    "DeclareBlockersReq": "declare_blockers",
    "SearchReq": "search",
    "DistributionReq": "distribution",
    "NumericInputReq": "numeric_input",
    "PromptReq": "prompt",
    "PayCostsReq": "pay_costs",
    "AssignDamageReq": "assign_damage",
    "OrderCombatDamageReq": "order_combat_damage",
    "SelectNReq": "selection_generic",
    "ChooseStartingPlayerReq": "choose_starting_player",
    "SelectReplacementReq": "select_replacement",
    "SelectCountersReq": "select_counters",
    "OrderReq": "order_triggers",
    "OptionalActionMessage": "optional_action",
    "CastingTimeOptionsReq": "casting_time_options",
    "SelectNGroupReq": "select_n_group",
    "SelectFromGroupsReq": "select_from_groups",
    "SearchFromGroupsReq": "search_from_groups",
    "GatherReq": "gather",
    "RevealHandReq": "reveal_hand",
}

# Reverse mapping for human-readable pending_decision labels
_BRIDGE_REQUEST_TO_LABEL: dict[str, str] = {
    "ActionsAvailableRequest": "Action Required",
    "SelectTargetsReq": "Select Targets",
    "MulliganReq": "Mulligan",
    "GroupReq": "Group Selection",
    "GroupOptionReq": "Choose Mode",
    "DeclareAttackersReq": "Declare Attackers",
    "DeclareBlockersReq": "Declare Blockers",
    "SearchReq": "Search Library",
    "DistributionReq": "Distribute",
    "NumericInputReq": "Choose Number",
    "PromptReq": "Prompt",
    "PayCostsReq": "Pay Costs",
    "AssignDamageReq": "Assign Damage",
    "OrderCombatDamageReq": "Order Damage",
    "SelectNReq": "Select Cards",
    "ChooseStartingPlayerReq": "Play or Draw",
    "SelectReplacementReq": "Order Replacement",
    "SelectCountersReq": "Select Counters",
    "OrderReq": "Order Triggers",
    "OptionalActionMessage": "Optional Action",
    "CastingTimeOptionsReq": "Casting Option",
}


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

    _MAX_CONSECUTIVE_ERRORS = 3

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
                return None

        if not self._was_connected:
            self._was_connected = True
            logger.info("Bridge decision detection active")

        # Poll
        resp = self._bridge.get_pending_actions()
        if resp is None:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._MAX_CONSECUTIVE_ERRORS:
                self._fallback_mode = True
                logger.warning(
                    f"Bridge polling failed {self._consecutive_errors}x consecutively, "
                    "entering fallback mode"
                )
            return None

        self._consecutive_errors = 0
        self._last_poll_result = resp

        has_pending = resp.get("has_pending", False)
        request_type = resp.get("request_type") if has_pending else None
        actions = resp.get("actions", [])
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

        decision_type = _BRIDGE_REQUEST_TO_DECISION_TYPE.get(request_type or "")

        result = {
            "trigger": trigger_name,
            "request_type": request_type,
            "decision_type": decision_type,
            "has_pending": has_pending,
            "actions": actions,
            "can_pass": resp.get("can_pass", False),
            "can_cancel": resp.get("can_cancel", False),
            "allow_undo": resp.get("allow_undo", False),
        }

        if trigger_name == "decision_required":
            label = _BRIDGE_REQUEST_TO_LABEL.get(request_type or "", request_type or "Unknown")
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
        actions = poll.get("actions", [])

        snapshot["_bridge_request_type"] = request_type if has_pending else None
        snapshot["_bridge_actions"] = actions if actions else None
        snapshot["_bridge_can_pass"] = poll.get("can_pass", False)

        if not has_pending:
            return

        # Enrich pending_decision if log hasn't caught up yet
        if not snapshot.get("pending_decision") and request_type:
            label = _BRIDGE_REQUEST_TO_LABEL.get(request_type, request_type)
            snapshot["pending_decision"] = label
            logger.debug(f"Bridge set pending_decision: {label}")

        # Enrich decision_context type from bridge's authoritative request_type
        decision_type = _BRIDGE_REQUEST_TO_DECISION_TYPE.get(request_type or "")
        if decision_type:
            existing_ctx = snapshot.get("decision_context") or {}
            existing_type = existing_ctx.get("type")
            # Bridge request type is authoritative — update if missing or generic
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
