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
import re
import select
import socket
import struct
import threading
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
UNMAPPED_INTERACTION_TYPE = "unmapped_interaction"
_GENERIC_SELECTION_TYPES = {"group_selection", "selection_generic", "select_n"}
_GENERIC_SELECTION_LABELS = {"Group Selection", "Order Cards", "Select Cards"}


class GREBridgeError(Exception):
    """Error communicating with the GRE bridge plugin."""
    pass


def _infer_specific_decision_type(
    existing_ctx: dict[str, Any],
    request_payload: Any,
    request_type: Optional[str],
    request_class: Optional[str],
) -> Optional[str]:
    """Infer a concrete decision type from generic bridge selection payloads."""
    values: list[str] = []
    for key in (
        "prompt",
        "promptText",
        "message",
        "messageText",
        "help",
        "helpText",
        "context",
        "contextRaw",
        "label",
        "text",
    ):
        value = existing_ctx.get(key)
        if value:
            values.append(str(value))

    if request_payload:
        try:
            values.append(json.dumps(request_payload, ensure_ascii=False, default=str))
        except Exception:
            values.append(str(request_payload))

    if request_type:
        values.append(str(request_type))
    if request_class:
        values.append(str(request_class))

    haystack = " ".join(values).lower()
    if not haystack:
        return None

    if re.search(r"\bsurveil\b", haystack):
        return "surveil"
    if re.search(r"\bscry\b", haystack):
        return "scry"
    if re.search(r"\bdiscard\b", haystack):
        return "discard"
    if re.search(r"\bmill\b", haystack):
        return "mill"
    if re.search(r"\bexplore\b", haystack):
        return "explore"
    if re.search(r"\bsacrifice\b", haystack):
        return "sacrifice"
    if re.search(r"\bexile\b", haystack):
        return "exile"
    if re.search(r"\bdestroy\b", haystack):
        return "destroy"
    if re.search(r"\breturn\b", haystack):
        return "return"
    return None


def _label_for_decision_type(decision_type: str, count: Any = None) -> Optional[str]:
    if decision_type == "scry":
        suffix = f" {count}" if count not in (None, "", 1) else ""
        return f"Scry{suffix}"
    if decision_type == "surveil":
        suffix = f" {count}" if count not in (None, "", 1) else ""
        return f"Surveil{suffix}"
    if decision_type == "discard":
        return "Discard"
    if decision_type == "mill":
        return "Mill"
    if decision_type == "explore":
        return "Explore"
    return None


class GREBridge:
    """Client for the MtgaCoachBridge BepInEx plugin.

    Communicates via TCP loopback socket using newline-delimited JSON.
    Thread-safe for single-command-at-a-time use.
    """

    def __init__(self):
        self._connected = False
        # Keepalive thread — proactively reconnects after disconnect and
        # pings periodically so we detect silently-broken sockets quickly.
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop = threading.Event()
        self._last_connect_attempt = 0.0
        self._reconnect_cooldown = 0.5  # seconds between reconnect attempts
        # Last successful ping; if it's been longer than `_ping_max_age`,
        # the keepalive will issue a ping to confirm the socket is still alive.
        self._last_ping_at = 0.0
        self._ping_max_age = 5.0
        self._server_socket = None  # The listening TCP socket
        self._client_socket = None  # The accepted client TCP socket
        self._pipe_file = None      # Wrapped file object for writing/reading
        self._pipe_lock = threading.Lock()  # Serialize socket I/O across threads

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Create a TCP server socket and wait for the BepInEx plugin to connect."""
        if self._connected:
            return True

        now = time.monotonic()
        if now - self._last_connect_attempt < self._reconnect_cooldown:
            return False

        self._last_connect_attempt = now

        try:
            # Phase 1: Create and bind the server socket if not exists
            if self._server_socket is None:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", 44222))
                s.listen(1)
                s.setblocking(False)
                self._server_socket = s
                logger.info("GRE bridge TCP server listening on 127.0.0.1:44222")

            # Phase 2: Check for incoming client connection (non-blocking)
            r, _, _ = select.select([self._server_socket], [], [], 0.0)
            if not r:
                return False

            client_sock, addr = self._server_socket.accept()
            client_sock.setblocking(True)
            client_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            self._client_socket = client_sock
            self._pipe_file = client_sock.makefile("rwb", buffering=0)
            self._connected = True

            logger.info(f"GRE bridge: plugin connected from {addr}")

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

        except Exception as e:
            logger.info(f"GRE bridge connect error: {e}")
            return False

    def disconnect(self):
        """Close the socket connection and client socket."""
        self._connected = False
        try:
            if self._pipe_file:
                self._pipe_file.close()
        except Exception:
            pass
        try:
            if self._client_socket:
                self._client_socket.close()
        except Exception:
            pass
        self._pipe_file = None
        self._client_socket = None

    # -------------------------------------------------------------------
    # Keepalive: background thread that holds the pipe open and quickly
    # reconnects after MTGA scene transitions or idle periods. Without
    # this, the plugin client can silently disconnect (e.g. during match
    # end → home) and Python only notices on the next command, leaving
    # autopilot stranded in "bridge disconnected" mode during the gap.
    # -------------------------------------------------------------------

    def start_keepalive(self, interval: float = 1.0) -> None:
        """Start a background thread that keeps the pipe connection alive.

        - If not connected, attempts to reconnect every `interval` seconds.
        - If connected, pings the plugin every `_ping_max_age` seconds to
          verify the pipe is still healthy.
        - If a ping fails, disconnects so the next tick recreates the pipe.

        Safe to call multiple times; starts at most one thread.
        """
        if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
            return
        self._keepalive_stop.clear()
        t = threading.Thread(
            target=self._keepalive_loop,
            args=(interval,),
            daemon=True,
            name="gre-bridge-keepalive",
        )
        self._keepalive_thread = t
        t.start()
        logger.info(f"GRE bridge keepalive started (interval={interval}s)")

    def stop_keepalive(self) -> None:
        self._keepalive_stop.set()

    def _keepalive_loop(self, interval: float) -> None:
        while not self._keepalive_stop.is_set():
            try:
                if not self._connected:
                    # Cheap reconnect attempt (respects _reconnect_cooldown)
                    self.connect()
                else:
                    age = time.monotonic() - self._last_ping_at
                    if age >= self._ping_max_age:
                        try:
                            resp = self._send_command({"action": "ping"})
                            if resp.get("ok"):
                                self._last_ping_at = time.monotonic()
                            else:
                                logger.info("GRE bridge keepalive: ping returned not-ok, disconnecting")
                                self.disconnect()
                        except Exception as e:
                            logger.info(f"GRE bridge keepalive: ping failed ({e}), disconnecting")
                            self.disconnect()
            except Exception as e:
                logger.debug(f"GRE bridge keepalive tick error: {e}")
            # Use Event.wait so we can be interrupted
            self._keepalive_stop.wait(interval)

    # Default per-command read timeout (seconds). Without this, a hung
    # plugin (Unity main thread busy mid-target-selection, scene transition,
    # etc.) would block the read forever while holding _pipe_lock — which
    # cascades into autopilot lock contention and a frozen UI. See bug
    # report 2026-05-01 (select_target lockup on Optimistic Scavenger).
    _DEFAULT_READ_TIMEOUT_S: float = 5.0

    def _send_command(
        self,
        cmd: dict[str, Any],
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send a JSON command and read the JSON response.

        Thread-safe: serializes pipe I/O so the bridge poller and
        autopilot don't interleave commands/responses.

        The read is bounded by ``timeout`` (default 5s). If the plugin
        doesn't respond in time, we close the pipe to unblock the read,
        disconnect, and raise — preventing the caller from holding the
        autopilot/UI lock indefinitely.

        Raises GREBridgeError on communication failure or read timeout.
        """
        if timeout is None:
            timeout = self._DEFAULT_READ_TIMEOUT_S

        with self._pipe_lock:
            if not self._connected or not self._pipe_file:
                raise GREBridgeError("Not connected")

            try:
                line = json.dumps(cmd, separators=(",", ":")) + "\n"
                self._pipe_file.write(line.encode("utf-8"))
                self._pipe_file.flush()
            except (BrokenPipeError, OSError, IOError) as e:
                self.disconnect()
                raise GREBridgeError(f"Pipe write error: {e}")

            # Read on a worker thread so we can enforce a timeout.
            # Closing self._pipe_file (via disconnect()) unblocks the read.
            result: list[bytes] = []
            exc: list[BaseException] = []

            def _reader() -> None:
                try:
                    response_bytes = b""
                    pipe_file = self._pipe_file
                    while True:
                        if pipe_file is None:
                            raise GREBridgeError("Pipe closed during read")
                        chunk = pipe_file.read(4096)
                        if not chunk:
                            raise GREBridgeError("Pipe closed")
                        response_bytes += chunk
                        nl_idx = response_bytes.find(b"\n")
                        if nl_idx >= 0:
                            response_bytes = response_bytes[:nl_idx]
                            break
                    result.append(response_bytes)
                except BaseException as e:  # noqa: BLE001 — propagate verbatim
                    exc.append(e)

            reader_thread = threading.Thread(
                target=_reader,
                name="gre-bridge-read",
                daemon=True,
            )
            reader_thread.start()
            reader_thread.join(timeout)

            if reader_thread.is_alive():
                # Plugin didn't respond in time. Force-disconnect so the
                # blocked read in the worker unblocks (file close raises
                # in the worker, it exits naturally as a daemon).
                action_name = cmd.get("action", "?")
                logger.warning(
                    "GRE bridge read timeout after %.1fs (action=%s) — disconnecting",
                    timeout,
                    action_name,
                )
                self.disconnect()
                raise GREBridgeError(
                    f"Pipe read timeout ({timeout:.1f}s) for action={action_name}"
                )

            if exc:
                err = exc[0]
                if isinstance(err, (BrokenPipeError, OSError, IOError)):
                    self.disconnect()
                    raise GREBridgeError(f"Pipe communication error: {err}")
                if isinstance(err, GREBridgeError):
                    self.disconnect()
                    raise err
                self.disconnect()
                raise GREBridgeError(f"Pipe read error: {err}")

            if not result:
                self.disconnect()
                raise GREBridgeError("Pipe read produced no result")

            try:
                # Strip UTF-8 BOM if present (C# StreamWriter may emit one)
                text = result[0].decode("utf-8-sig")
                parsed = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                raise GREBridgeError(f"Pipe response parse error: {e}")

            # Any successful round-trip counts as liveness evidence —
            # no need for a keepalive ping right after normal traffic.
            self._last_ping_at = time.monotonic()
            return parsed

    def _send_safe(
        self,
        cmd: dict[str, Any],
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """Send command with auto-reconnect on failure."""
        if not self._connected:
            if not self.connect():
                raise GREBridgeError("Not connected to GRE bridge")

        try:
            return self._send_command(cmd, timeout=timeout)
        except GREBridgeError:
            # One retry after reconnect
            if self.connect():
                return self._send_command(cmd, timeout=timeout)
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

    def return_to_home(self) -> bool:
        """Leave the post-match result screen and return to the Home screen.

        Invokes MatchEndScene.LeaveMatch() in the client (same as clicking the
        "Leave Match" button), so the harness can loop matches without clicks.
        Returns True if the call was accepted.
        """
        try:
            resp = self._send_safe({"action": "return_to_home"}, timeout=10.0)
            if resp.get("ok"):
                logger.info("GRE bridge returned to Home (LeaveMatch)")
                return True
            logger.warning(f"GRE bridge return_to_home failed: {resp.get('error')}")
            return False
        except GREBridgeError as e:
            logger.warning(f"GRE bridge return_to_home error: {e}")
            return False

    def submit_blockers(
        self,
        assignments: list[dict[str, Any]],
    ) -> bool:
        """Submit blocker assignments via the GRE bridge.

        Args:
            assignments: List of dicts, each with:
                - blockerInstanceId: int — the blocking creature's instance ID
                - attackerInstanceIds: list[int] — which attacker(s) it blocks

                Pass an empty list to submit "no blocks".

        Returns True if submitted successfully.
        """
        try:
            resp = self._send_safe({
                "action": "submit_blockers",
                "assignments": assignments,
            })
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted {len(assignments)} blocker assignments")
                return True
            else:
                logger.warning(f"GRE bridge submit_blockers failed: {resp.get('error')}")
                return False
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_blockers error: {e}")
            return False

    def submit_attackers(
        self,
        attackers: list[dict[str, Any]],
    ) -> bool:
        """Submit attacker declarations via the GRE bridge.

        Args:
            attackers: List of dicts, each with:
                - attackerInstanceId: int — the attacking creature's instance ID
                - damageRecipient: dict with type, seatId, instanceId

                Pass an empty list to submit "no attacks".

        Returns True if submitted successfully.
        """
        try:
            resp = self._send_safe({
                "action": "submit_attackers",
                "attackers": attackers,
            })
            logger.info(f"GRE bridge submit_attackers response: {resp}")
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted {len(attackers)} attackers")
                return True
            else:
                logger.warning(f"GRE bridge submit_attackers failed: {resp.get('error')}")
                return False
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_attackers error: {e}")
            return False

    def submit_attackers_raw(
        self,
        attackers: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        """Submit attacker declarations and return full response dict.

        Unlike submit_attackers(), returns the full response so callers can
        check needs_finalize for the two-step UpdateAttacker/SubmitAttackers flow.
        """
        try:
            resp = self._send_safe({
                "action": "submit_attackers",
                "attackers": attackers,
            })
            logger.info(f"GRE bridge submit_attackers_raw response: {resp}")
            return resp
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_attackers_raw error: {e}")
            return None

    def submit_mulligan(self, keep: bool) -> bool:
        """Submit mulligan decision (keep or mulligan)."""
        try:
            resp = self._send_safe({"action": "submit_mulligan", "keep": keep})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted mulligan: {'keep' if keep else 'mulligan'}")
                return True
            logger.warning(f"GRE bridge submit_mulligan failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_mulligan error: {e}")
        return False

    def submit_choose_starting_player(self, seat_id: int) -> bool:
        """Submit choose starting player (play/draw)."""
        try:
            resp = self._send_safe({"action": "submit_choose_starting_player", "seat_id": seat_id})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted choose starting player: seat {seat_id}")
                return True
            logger.warning(f"GRE bridge submit_choose failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_choose error: {e}")
        return False

    def submit_selection(self, ids: list[int]) -> bool:
        """Submit selection for SelectN or Search requests."""
        try:
            resp = self._send_safe({"action": "submit_selection", "ids": ids})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted selection: {len(ids)} ids")
                return True
            logger.warning(f"GRE bridge submit_selection failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_selection error: {e}")
        return False

    def submit_group(self, groups: list[dict[str, Any]]) -> bool:
        """Submit group ordering (scry top/bottom, etc.)."""
        try:
            resp = self._send_safe({"action": "submit_group", "groups": groups})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted {len(groups)} groups")
                return True
            logger.warning(f"GRE bridge submit_group failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_group error: {e}")
        return False

    def submit_optional(self, accept: bool) -> bool:
        """Submit optional action response (accept/decline)."""
        try:
            resp = self._send_safe({"action": "submit_optional", "accept": accept})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted optional: {'accept' if accept else 'decline'}")
                return True
            logger.warning(f"GRE bridge submit_optional failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_optional error: {e}")
        return False

    def submit_numeric(self, value: int) -> bool:
        """Submit numeric input (X cost, etc.)."""
        try:
            resp = self._send_safe({"action": "submit_numeric", "value": value})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted numeric: {value}")
                return True
            logger.warning(f"GRE bridge submit_numeric failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_numeric error: {e}")
        return False

    def submit_targets(self, target_instance_id: int) -> bool:
        """Submit target selection by instance ID."""
        try:
            resp = self._send_safe({
                "action": "submit_targets",
                "target_instance_id": target_instance_id,
            })
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted target: instance_id={target_instance_id}")
                return True
            logger.warning(f"GRE bridge submit_targets failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_targets error: {e}")
        return False

    # -------------------------------------------------------------------
    # Coverage expansion: full BaseUserRequest family handlers
    # -------------------------------------------------------------------

    def submit_assign_damage(self, assigners: list[dict[str, Any]]) -> bool:
        """Submit combat damage assignments.

        assigners: [{"instanceId": <attacker>,
                     "assignments": [{"instanceId": <receiver>, "damage": <int>}, ...]}, ...]
        """
        try:
            resp = self._send_safe({"action": "submit_assign_damage", "assigners": assigners})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted assign_damage: {resp.get('assigner_count')} assigners")
                return True
            logger.warning(f"GRE bridge submit_assign_damage failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_assign_damage error: {e}")
        return False

    def submit_distribution(self, distributions: dict[int, int]) -> bool:
        """Submit a distribution decision (e.g. divide N counters among targets).

        distributions: {target_instance_id: amount, ...}
        """
        try:
            payload = {str(k): int(v) for k, v in distributions.items()}
            resp = self._send_safe({"action": "submit_distribution", "distributions": payload})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted distribution: {resp.get('target_count')} targets")
                return True
            logger.warning(f"GRE bridge submit_distribution failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_distribution error: {e}")
        return False

    def submit_order(self, ids: Optional[list[int]] = None) -> bool:
        """Submit a stack/library ordering decision.

        ids: ordered list of instance IDs. None = submit current order as-is.
        """
        try:
            payload: dict[str, Any] = {"action": "submit_order"}
            if ids is not None:
                payload["ids"] = list(ids)
            resp = self._send_safe(payload)
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted order: {resp.get('count')} ids")
                return True
            logger.warning(f"GRE bridge submit_order failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_order error: {e}")
        return False

    def submit_select_replacement(
        self,
        index: int = 0,
        decline: bool = False,
    ) -> bool:
        """Submit replacement-effect choice (or decline if optional)."""
        try:
            payload: dict[str, Any] = {"action": "submit_select_replacement"}
            if decline:
                payload["decline"] = True
            else:
                payload["index"] = int(index)
            resp = self._send_safe(payload)
            if resp.get("ok"):
                if resp.get("declined"):
                    logger.info("GRE bridge submitted select_replacement: declined")
                else:
                    logger.info(f"GRE bridge submitted select_replacement: index {resp.get('index')}")
                return True
            logger.warning(f"GRE bridge submit_select_replacement failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_select_replacement error: {e}")
        return False

    def submit_select_counters(self, pairs: list[dict[str, Any]]) -> bool:
        """Submit counter selection.

        pairs: [{"counterType": "<name>", "amount": <int>, "instanceId": <int?>}, ...]
        """
        try:
            resp = self._send_safe({"action": "submit_select_counters", "pairs": pairs})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted select_counters: {resp.get('pair_count')} pairs")
                return True
            logger.warning(f"GRE bridge submit_select_counters failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_select_counters error: {e}")
        return False

    def submit_string_input(self, value: str) -> bool:
        """Submit a string input (e.g. naming a card)."""
        try:
            resp = self._send_safe({"action": "submit_string_input", "value": str(value)})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted string_input: '{value}'")
                return True
            logger.warning(f"GRE bridge submit_string_input failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_string_input error: {e}")
        return False

    def submit_intermission(self, option: str) -> bool:
        """Submit intermission decision (NextGameReq, ConcedeReq, etc.).

        option: ClientMessageType enum name as string.
        """
        try:
            resp = self._send_safe({"action": "submit_intermission", "option": option})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted intermission: {option}")
                return True
            logger.warning(f"GRE bridge submit_intermission failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_intermission error: {e}")
        return False

    def submit_gather(self, gatherings: list[dict[str, int]]) -> bool:
        """Submit gather request (per-target instance/amount pairs).

        gatherings: [{"instanceId": <int>, "amount": <int>}, ...]
        """
        try:
            resp = self._send_safe({"action": "submit_gather", "gatherings": gatherings})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted gather: {resp.get('count')} entries")
                return True
            logger.warning(f"GRE bridge submit_gather failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_gather error: {e}")
        return False

    def submit_auto_tap(self, solution_index: int = 0) -> bool:
        """Submit an auto-tap solution by index (default = first)."""
        try:
            resp = self._send_safe({
                "action": "submit_auto_tap",
                "solution_index": int(solution_index),
            })
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted auto_tap: index {solution_index}")
                return True
            logger.warning(f"GRE bridge submit_auto_tap failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_auto_tap error: {e}")
        return False

    def submit_select_from_groups(self, groups: list[dict[str, Any]]) -> bool:
        """Submit select-from-groups request.

        groups: [{"ids": [<int>, ...], "groupId": <int?>}, ...]
        """
        try:
            resp = self._send_safe({"action": "submit_select_from_groups", "groups": groups})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted select_from_groups: {resp.get('group_count')} groups")
                return True
            logger.warning(f"GRE bridge submit_select_from_groups failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_select_from_groups error: {e}")
        return False

    def submit_select_n_group(
        self,
        ids: Optional[list[int]] = None,
        single_id: Optional[int] = None,
    ) -> bool:
        """Submit a select-N-group request. Provide `ids` (list) or `single_id`."""
        try:
            payload: dict[str, Any] = {"action": "submit_select_n_group"}
            if ids is not None:
                payload["ids"] = list(ids)
            elif single_id is not None:
                payload["id"] = int(single_id)
            else:
                logger.warning("submit_select_n_group: must provide ids or single_id")
                return False
            resp = self._send_safe(payload)
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted select_n_group: {resp}")
                return True
            logger.warning(f"GRE bridge submit_select_n_group failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_select_n_group error: {e}")
        return False

    def submit_search_from_groups(
        self,
        zone: Optional[int] = None,
        groups: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        """Submit search-from-groups (pick a zone, or pick groups within one)."""
        try:
            payload: dict[str, Any] = {"action": "submit_search_from_groups"}
            if zone is not None:
                payload["zone"] = int(zone)
            elif groups is not None:
                payload["groups"] = groups
            else:
                logger.warning("submit_search_from_groups: must provide zone or groups")
                return False
            resp = self._send_safe(payload)
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted search_from_groups: {resp}")
                return True
            logger.warning(f"GRE bridge submit_search_from_groups failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_search_from_groups error: {e}")
        return False

    def submit_casting_mana_type(self, colors: list[str]) -> bool:
        """Submit casting-time mana-type selection (one ManaColor name per inner request)."""
        try:
            resp = self._send_safe({"action": "submit_casting_mana_type", "colors": list(colors)})
            if resp.get("ok"):
                logger.info(f"GRE bridge submitted casting_mana_type: {colors}")
                return True
            logger.warning(f"GRE bridge submit_casting_mana_type failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge submit_casting_mana_type error: {e}")
        return False

    def auto_respond(self) -> bool:
        """Send AutoRespond on whatever request is currently pending.

        This is MTGA's built-in "do the default" response — works for ANY
        request type. Use as a universal fallback when we can't handle a
        specific request type (pay costs, X values, casting options, etc.).
        """
        try:
            resp = self._send_safe({"action": "auto_respond"})
            if resp.get("ok"):
                logger.info(f"GRE bridge auto_respond: {resp.get('request_class', '?')}")
                return True
            logger.warning(f"GRE bridge auto_respond failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge auto_respond error: {e}")
        return False

    def cancel_action(self) -> bool:
        """Cancel the current pending action (undo the cast/activation).

        Only works when the request has CanCancel=true (e.g. PayCostsRequest
        with AllowCancel_Abort). Falls back to AutoRespond if cancel not allowed.
        """
        try:
            resp = self._send_safe({"action": "cancel_action"})
            if resp.get("ok"):
                cancelled = resp.get("cancelled", False)
                logger.info(f"GRE bridge cancel_action: cancelled={cancelled}, {resp.get('request_class', '?')}")
                return True
            logger.warning(f"GRE bridge cancel_action failed: {resp.get('error')}")
        except GREBridgeError as e:
            logger.warning(f"GRE bridge cancel_action error: {e}")
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

    def get_draft_state(self) -> Optional[dict[str, Any]]:
        """Get draft state directly from DraftContentController via MTGA bridge.

        Returns draft mode, pack info, pick info, and picked cards.
        Returns None if not connected or draft not active.
        """
        try:
            resp = self._send_safe({"action": "get_draft_state"})
            if resp.get("ok"):
                return resp
            else:
                logger.debug(f"get_draft_state: {resp.get('error')}")
                return None
        except GREBridgeError as e:
            logger.debug(f"get_draft_state error: {e}")
            return None

    def get_card_positions(self) -> Optional[dict[str, Any]]:
        """Get on-screen rectangles for every visible card in the current match.

        Queries the BepInEx plugin which walks Unity's DuelScene_CDC objects,
        projects each card's collider bounds through MainCamera.WorldToScreenPoint,
        and returns a per-card screen rectangle (top-left origin, matching
        Windows/PySide convention).

        Returns a dict like:
            {
                "ok": True,
                "screen_w": 1920,
                "screen_h": 1080,
                "count": 22,
                "cards": [
                    {
                        "instance_id": 12345,
                        "grp_id": 87654,
                        "zone": "Hand",
                        "x": 820, "y": 920, "w": 120, "h": 168,
                        "nx": 0.427, "ny": 0.852, "nw": 0.063, "nh": 0.156
                    },
                    ...
                ]
            }

        Returns None if the bridge is not connected or a match is not active.
        """
        try:
            resp = self._send_safe({"action": "get_card_positions"})
            if resp.get("ok"):
                return resp
            logger.debug(f"get_card_positions: {resp.get('error')}")
            return None
        except GREBridgeError as e:
            logger.debug(f"get_card_positions error: {e}")
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
    "OrderTriggers": "order_triggers",
    "OrderTriggersReq": "order_triggers",
    "OrderTriggersRequest": "order_triggers",
    "OptionalActionMessage": "optional_action",
    "OptionalActionMessageRequest": "optional_action",
    "OptionalActionMessageReq": "optional_action",
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
    "OrderTriggers": "Order Triggers",
    "OrderTriggersReq": "Order Triggers",
    "OrderTriggersRequest": "Order Triggers",
    "OptionalActionMessage": "Optional Action",
    "OptionalActionMessageRequest": "Optional Action",
    "OptionalActionMessageReq": "Optional Action",
    "CastingTimeOptions": "Casting Option",
    "CastingTimeOptionsReq": "Casting Option",
    "CastingTimeOptionRequest": "Casting Option",
    "SelectNGroup": "Select From Group",
    "SelectNGroupReq": "Select From Group",
    "SelectNGroupRequest": "Select From Group",
    "SelectFromGroups": "Select From Groups",
    "SelectFromGroupsReq": "Select From Groups",
    "SelectFromGroupsRequest": "Select From Groups",
    "SearchFromGroups": "Search From Groups",
    "SearchFromGroupsReq": "Search From Groups",
    "SearchFromGroupsRequest": "Search From Groups",
    "Gather": "Gather",
    "GatherReq": "Gather",
    "GatherRequest": "Gather",
    "RevealHand": "Reveal Hand",
    "RevealHandReq": "Reveal Hand",
    "RevealHandRequest": "Reveal Hand",
}

_ACTIONS_AVAILABLE_BRIDGE_REQUESTS = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}

_INTERMISSION_BRIDGE_REQUESTS = {
    "Intermission",
    "IntermissionReq",
    "IntermissionRequest",
}

_NON_ACTIONABLE_BRIDGE_REQUESTS = _INTERMISSION_BRIDGE_REQUESTS

# Tracks the last logged (from, to) pair for pending_decision overrides
# so the poll loop (~4 Hz) only emits one INFO line per transition.
_LAST_OVERRIDE_LOG: Optional[tuple[str, str]] = None


def _get_bridge_decision_type(
    request_type: Optional[str],
    request_class: Optional[str] = None,
) -> Optional[str]:
    mapped = (
        _BRIDGE_REQUEST_TO_DECISION_TYPE.get(request_type or "")
        or _BRIDGE_REQUEST_TO_DECISION_TYPE.get(request_class or "")
    )
    if mapped:
        return mapped
    if request_type or request_class:
        return UNMAPPED_INTERACTION_TYPE
    return None


def _get_bridge_request_label(
    request_type: Optional[str],
    request_class: Optional[str] = None,
) -> str:
    if _get_bridge_decision_type(request_type, request_class) == UNMAPPED_INTERACTION_TYPE:
        return "Manual Required"
    return (
        _BRIDGE_REQUEST_TO_LABEL.get(request_type or "")
        or _BRIDGE_REQUEST_TO_LABEL.get(request_class or "")
        or request_type
        or request_class
        or "Unknown"
    )


def enrich_snapshot_from_pending_response(
    snapshot: dict[str, Any],
    poll: Optional[dict[str, Any]],
    *,
    bridge_connected: Optional[bool] = None,
) -> None:
    """Overlay a raw get_pending_actions() response onto a snapshot dict.

    Composition wrapper. Each phase is its own helper so the cyclomatic
    complexity stays inside the function that actually needs it:

      1. _normalize_poll              — sanitize intermission/non-actionable
      2. _stamp_bridge_fields         — write _bridge_* fields onto snapshot
      3. _clear_snapshot_for_no_pending — fast-path when nothing's pending
      4. _apply_pending_decision_label — dedupe-aware label override
      5. _merge_decision_context_from_bridge — overlay bridge ctx + req tags
      6. _resolve_decision_context_type    — stale-vs-fresh type pick
      7. _refine_generic_selection_type    — promote generic → specific
    """
    if bridge_connected is not None:
        snapshot["_bridge_connected"] = bridge_connected

    if poll is None:
        return

    normalized = _normalize_poll(poll)
    is_intermission = normalized["is_intermission"]

    # Surface intermission as a durable signal so the coach loop can
    # detect end-of-match even after the request fields are zeroed out.
    snapshot["_bridge_in_intermission"] = is_intermission
    if is_intermission:
        snapshot["match_ended"] = True

    _stamp_bridge_fields(snapshot, poll, normalized)

    if not normalized["has_pending"]:
        _clear_snapshot_for_no_pending(snapshot, bridge_connected)
        return

    request_type = normalized["request_type"]
    request_class = normalized["request_class"]
    request_payload = normalized["request_payload"]
    bridge_decision_context = normalized["bridge_decision_context"]

    _apply_pending_decision_label(snapshot, request_type, request_class)

    decision_type = _get_bridge_decision_type(request_type, request_class)
    plugin_provided_type = bool(
        bridge_decision_context and bridge_decision_context.get("type")
    )
    existing_ctx = _merge_decision_context_from_bridge(
        snapshot, bridge_decision_context, request_type, request_class
    )
    existing_ctx = _resolve_decision_context_type(
        existing_ctx, decision_type, plugin_provided_type
    )
    existing_ctx = _refine_generic_selection_type(
        snapshot,
        existing_ctx,
        request_payload,
        request_type,
        request_class,
        decision_type,
    )

    if existing_ctx:
        snapshot["decision_context"] = existing_ctx


def _normalize_poll(poll: dict[str, Any]) -> dict[str, Any]:
    """Pull the request fields out of a poll, blanking out non-actionable ones.

    Returns a dict with normalized has_pending / request_type / request_class
    / actions / request_payload / bridge_decision_context plus an
    is_intermission flag (which stays True even after the actionable bits
    are zeroed, so the caller can mark match_ended).
    """
    has_pending = poll.get("has_pending", False)
    request_type = poll.get("request_type")
    request_class = poll.get("request_class")
    actions = poll.get("actions", [])
    request_payload = poll.get("request_payload")
    bridge_decision_context = poll.get("decision_context") or {}

    is_intermission = (
        (request_type or "") in _INTERMISSION_BRIDGE_REQUESTS
        or (request_class or "") in _INTERMISSION_BRIDGE_REQUESTS
    )
    if has_pending and _is_non_actionable_bridge_request(request_type, request_class):
        has_pending = False
        request_type = None
        request_class = None
        actions = []
        request_payload = None
        bridge_decision_context = {}

    return {
        "has_pending": has_pending,
        "request_type": request_type,
        "request_class": request_class,
        "actions": actions,
        "request_payload": request_payload,
        "bridge_decision_context": bridge_decision_context,
        "is_intermission": is_intermission,
    }


def _stamp_bridge_fields(
    snapshot: dict[str, Any],
    poll: dict[str, Any],
    normalized: dict[str, Any],
) -> None:
    """Write the `_bridge_*` overlay fields onto the snapshot."""
    has_pending = normalized["has_pending"]
    snapshot["_bridge_request_type"] = (
        normalized["request_type"] if has_pending else None
    )
    snapshot["_bridge_request_class"] = (
        normalized["request_class"] if has_pending else None
    )
    actions = normalized["actions"]
    snapshot["_bridge_actions"] = actions if actions else None
    snapshot["_bridge_can_pass"] = poll.get("can_pass", False)
    snapshot["_bridge_can_cancel"] = poll.get("can_cancel", False)
    snapshot["_bridge_allow_undo"] = poll.get("allow_undo", False)
    request_payload = normalized["request_payload"]
    snapshot["_bridge_request_payload"] = (
        request_payload if has_pending and request_payload else None
    )


def _clear_snapshot_for_no_pending(
    snapshot: dict[str, Any], bridge_connected: Optional[bool]
) -> None:
    """No bridge decision pending — clear stale decision/legal-action hints.

    Only does the clear when the bridge is *connected*. If we're disconnected,
    leave the log-parsed legal_actions in place so the coach can still work.
    """
    if not bridge_connected:
        return
    snapshot["pending_decision"] = None
    snapshot["decision_context"] = None
    if "legal_actions" in snapshot:
        snapshot["legal_actions"] = []
    if "legal_actions_raw" in snapshot:
        snapshot["legal_actions_raw"] = []


def _apply_pending_decision_label(
    snapshot: dict[str, Any],
    request_type: Optional[str],
    request_class: Optional[str],
) -> None:
    """Set or override `pending_decision` from the bridge request label.

    Bridge polls at ~4 Hz and each fresh snapshot carries the raw
    log-parsed pending_decision (often "Priority") even after we've
    overridden it once — without dedupe this produced hundreds of
    identical override-log lines per priority window.
    """
    label = _get_bridge_request_label(request_type, request_class)
    existing = snapshot.get("pending_decision")
    if not existing:
        snapshot["pending_decision"] = label
        logger.debug(f"Bridge set pending_decision: {label}")
        return
    if existing == label:
        return

    global _LAST_OVERRIDE_LOG
    key = (existing, label)
    if _LAST_OVERRIDE_LOG != key:
        logger.info(
            f"Bridge overriding stale pending_decision: "
            f"{existing!r} → {label!r}"
        )
        _LAST_OVERRIDE_LOG = key
    else:
        logger.debug(
            f"Bridge overriding stale pending_decision: "
            f"{existing!r} → {label!r} (repeat)"
        )
    snapshot["pending_decision"] = label


def _merge_decision_context_from_bridge(
    snapshot: dict[str, Any],
    bridge_decision_context: dict[str, Any],
    request_type: Optional[str],
    request_class: Optional[str],
) -> dict[str, Any]:
    """Overlay bridge-provided decision_context onto the snapshot's, then
    backfill requestType / requestClass tags. Returns the merged dict.
    """
    existing_ctx = snapshot.get("decision_context") or {}
    if bridge_decision_context:
        snapshot["decision_context"] = {
            **existing_ctx,
            **bridge_decision_context,
            "_bridge_source": True,
        }
        existing_ctx = snapshot["decision_context"]

    if request_type and "requestType" not in existing_ctx:
        existing_ctx = {**existing_ctx, "requestType": request_type}
    if request_class and "requestClass" not in existing_ctx:
        existing_ctx = {**existing_ctx, "requestClass": request_class}
    return existing_ctx


def _resolve_decision_context_type(
    existing_ctx: dict[str, Any],
    decision_type: Optional[str],
    plugin_provided_type: bool,
) -> dict[str, Any]:
    """Pick the freshest `type` value for decision_context.

    Stale snapshots can carry a previous window's type (e.g.
    "actions_available") after the bridge has moved on to a Search /
    SelectTargets / PayCosts request. When the plugin didn't stamp a type
    this poll, trust the bridge-mapped one.
    """
    if not decision_type:
        return existing_ctx
    existing_type = existing_ctx.get("type")
    stale_disagrees = bool(
        existing_type
        and existing_type != decision_type
        and not plugin_provided_type
    )
    needs_update = (
        not existing_type
        or existing_type in {"unknown_req", UNMAPPED_INTERACTION_TYPE}
        or stale_disagrees
    )
    if not needs_update:
        return existing_ctx
    logger.debug(
        f"Bridge enriched decision_context: {existing_type} → {decision_type}"
    )
    return {**existing_ctx, "type": decision_type, "_bridge_source": True}


def _refine_generic_selection_type(
    snapshot: dict[str, Any],
    existing_ctx: dict[str, Any],
    request_payload: Any,
    request_type: Optional[str],
    request_class: Optional[str],
    decision_type: Optional[str],
) -> dict[str, Any]:
    """Promote a generic "selection" type into a more specific one when the
    request payload has enough hints (count, ZoneToSearch, etc.).

    Also rewrites the human-readable pending_decision label when the
    current one is also generic.
    """
    current_type = str(existing_ctx.get("type") or "")
    if (
        current_type not in _GENERIC_SELECTION_TYPES
        and decision_type not in _GENERIC_SELECTION_TYPES
    ):
        return existing_ctx

    inferred_type = _infer_specific_decision_type(
        existing_ctx, request_payload, request_type, request_class
    )
    if not inferred_type:
        return existing_ctx

    if snapshot.get("pending_decision") in _GENERIC_SELECTION_LABELS:
        better_label = _label_for_decision_type(
            inferred_type, existing_ctx.get("count")
        )
        if better_label:
            snapshot["pending_decision"] = better_label
    logger.debug(
        "Bridge inferred specific decision type: %s → %s",
        current_type or decision_type,
        inferred_type,
    )
    return {**existing_ctx, "type": inferred_type, "_bridge_source": True}


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
        enrich_snapshot_from_pending_response(
            snapshot,
            poll,
            bridge_connected=self.connected,
        )

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
    """Get or create the module-level GRE bridge singleton.

    Also starts the keepalive thread so the pipe stays connected across
    MTGA scene transitions and idle periods without requiring callers to
    remember to reconnect.
    """
    global _bridge
    if _bridge is None:
        _bridge = GREBridge()
        try:
            _bridge.start_keepalive()
        except Exception as e:
            logger.debug(f"Could not start bridge keepalive: {e}")
    return _bridge


def get_poller() -> BridgeDecisionPoller:
    """Get or create the module-level bridge decision poller singleton."""
    global _poller
    if _poller is None:
        _poller = BridgeDecisionPoller(get_bridge())
    return _poller


class BotBattlePipeServer:
    """A synchronous TCP server for bot battles.

    Accepts connections from the BepInEx client (BridgeStrategy) on
    127.0.0.1:44223, reads a decision request JSON line,
    passes it to a decision handler callback, and writes the response JSON line.
    """

    def __init__(self, port: int = 44223):
        self.port = port
        self.running = False
        self._server_socket = None

    def start(self, handler_callback):
        """Starts the server loop, blocking until stop() is called or connection fails.

        handler_callback is a function: callback(request_dict) -> response_dict
        """
        self.running = True
        logger.info(f"Starting BotBattle TCP Server on 127.0.0.1:{self.port}")

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", self.port))
            s.listen(5)
            self._server_socket = s
        except Exception as e:
            logger.error(f"Failed to start BotBattle TCP server on port {self.port}: {e}")
            self.running = False
            return

        while self.running:
            try:
                # Use select with 1s timeout to check self.running regularly
                r, _, _ = select.select([self._server_socket], [], [], 1.0)
                if not r:
                    continue
                client_sock, addr = self._server_socket.accept()
            except Exception as e:
                if self.running:
                    logger.error(f"BotBattle server accept failed: {e}")
                time.sleep(1.0)
                continue

            client_sock.setblocking(True)
            pipe_file = client_sock.makefile("rwb", buffering=0)

            try:
                line = pipe_file.readline()
                if line:
                    req_json = json.loads(line.decode("utf-8").strip())
                    resp_dict = handler_callback(req_json)
                    resp_line = json.dumps(resp_dict) + "\n"
                    pipe_file.write(resp_line.encode("utf-8"))
                    pipe_file.flush()
            except Exception as e:
                logger.warning(f"Error handling bot battle request: {e}")
            finally:
                try:
                    pipe_file.close()
                except Exception:
                    pass
                try:
                    client_sock.close()
                except Exception:
                    pass

        self._server_socket = None

    def stop(self):
        self.running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except Exception:
                pass

