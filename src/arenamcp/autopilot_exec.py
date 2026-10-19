"""Action-execution routing for AutopilotEngine using direct GRE Named Pipe submissions."""

from __future__ import annotations

import logging
from typing import Any

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot_models import ClickResult, ExecutionPath
from arenamcp.gre_bridge import _ACTIONS_AVAILABLE_BRIDGE_REQUESTS

logger = logging.getLogger(__name__)

# Answers only one request family takes (its bridge handler submits nothing
# else): against any other pending request the plan is stale, not a bridge gap.
_REQUEST_SCOPED_ACTIONS: dict[ActionType, tuple[str, ...]] = {
    ActionType.ASSIGN_DAMAGE: ("AssignDamage",),
    ActionType.DISTRIBUTE: ("Distribution",),
}


class _ActionExecMixin:
    """Action execution mixin routing commands directly through the GRE bridge."""

    def _execute_action(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Route an action to the GRE bridge submission handler."""
        if self._abort_event.is_set():
            return ClickResult(False, 0, 0, action.action_type.value, "aborted")
        # Match just ended — the bridge is in Intermission and no action is legal.
        if game_state.get("_bridge_in_intermission") or game_state.get("match_ended"):
            logger.info(f"Autopilot: skipping {action.action_type.value} — bridge in intermission")
            return ClickResult(True, 0, 0, action.action_type.value, "intermission_noop")

        # Try GRE bridge first (direct action submission, zero pixel/coordinate dependency)
        if not self._config.dry_run:
            gre_result = self._try_gre_bridge(action, game_state)
            if (
                gre_result is None
                and not getattr(self._gre_bridge, "connected", False)
                and self._wait_for_bridge_reconnect()
            ):
                gre_result = self._try_gre_bridge(action, game_state)
            if self._abort_event.is_set():
                return ClickResult(False, 0, 0, action.action_type.value, "aborted")
            if gre_result is not None:
                return gre_result

        bridge_connected = self._gre_bridge is not None and getattr(self._gre_bridge, "connected", False)
        if self._config.bridge_only_when_connected and not self._config.dry_run:
            if bridge_connected:
                gs_for_classify = game_state
                try:
                    live = self._gre_bridge.get_pending_actions() or {}
                except Exception:
                    live = {}
                if live:
                    gs_for_classify = dict(game_state)
                    if live.get("has_pending"):
                        gs_for_classify["_bridge_request_type"] = live.get("request_type") or ""
                        gs_for_classify["_bridge_request_class"] = live.get("request_class") or ""
                        if "actions" in live:
                            gs_for_classify["_bridge_actions"] = live.get("actions") or []
                        if "can_pass" in live:
                            gs_for_classify["_bridge_can_pass"] = live.get("can_pass")
                    else:
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: window closed before "
                            "submission (live poll) — no-op",
                        )
                        return ClickResult(
                            True,
                            0,
                            0,
                            action.action_type.value,
                            "GRE bridge (no-op, window closed)",
                        )

                if self._is_planner_action_stale_vs_bridge(action, gs_for_classify):
                    bridge_type = str(gs_for_classify.get("_bridge_request_type") or "")
                    bridge_class = str(gs_for_classify.get("_bridge_request_class") or "")
                    bridge_is_actions_available = (
                        bridge_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                        or bridge_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                    )
                    bridge_has_other_request = bool(
                        (bridge_type or bridge_class) and not bridge_is_actions_available
                    )

                    is_combat_stale = action.action_type in (
                        ActionType.DECLARE_ATTACKERS,
                        ActionType.DECLARE_BLOCKERS,
                    )
                    # A play the window no longer offers (e.g. the land was
                    # already played from another trigger) is re-planned, not
                    # escalated: 2026-09-24 the MANUAL REQUIRED here auto-passed
                    # priority and threw away the rest of main phase 1.
                    is_displaced_main_action = action.action_type in (
                        ActionType.PLAY_LAND,
                        ActionType.CAST_SPELL,
                    ) or (action.action_type == ActionType.ACTIVATE_ABILITY and bridge_has_other_request)
                    is_displaced_select = action.action_type in (
                        ActionType.SELECT_N,
                        ActionType.SEARCH_LIBRARY,
                        ActionType.SELECT_COUNTERS,
                        ActionType.SELECT_TARGET,
                    )
                    is_displaced_pass = (
                        action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE)
                        and bridge_has_other_request
                    )
                    is_displaced_numeric = action.action_type == ActionType.NUMERIC_INPUT
                    is_displaced_casting = action.action_type == ActionType.CASTING_OPTIONS
                    is_displaced_answer = action.action_type in _REQUEST_SCOPED_ACTIONS

                    if (
                        is_combat_stale
                        and bridge_is_actions_available
                        and bool(gs_for_classify.get("_bridge_can_pass"))
                        and self._gre_bridge is not None
                    ):
                        try:
                            if self._progress_bridge(game_state, poll=live or None).submit_pass():
                                self._log_execution_path(
                                    ExecutionPath.GRE_AWARE,
                                    f"{action.action_type.value}: not in combat "
                                    "step — passing priority to advance toward combat",
                                )
                                return ClickResult(
                                    True,
                                    0,
                                    0,
                                    "pass_priority",
                                    "GRE bridge (advance-to-combat)",
                                    submitted_action=GameAction(action_type=ActionType.PASS_PRIORITY),
                                )
                        except Exception as e:
                            logger.debug(f"advance-to-combat pass failed: {e}")

                    if (
                        is_combat_stale
                        or is_displaced_main_action
                        or is_displaced_select
                        or is_displaced_pass
                        or is_displaced_numeric
                        or is_displaced_casting
                        or is_displaced_answer
                    ):
                        if is_combat_stale:
                            reason = "bridge not in combat step yet"
                        elif is_displaced_main_action:
                            reason = f"bridge moved to {bridge_type or bridge_class}"
                        elif is_displaced_pass:
                            reason = f"window is now {bridge_type or bridge_class} — pass not applicable"
                        elif is_displaced_numeric:
                            reason = (
                                f"window is now {bridge_type or bridge_class or 'nothing'} — no number asked"
                            )
                        elif is_displaced_casting:
                            reason = f"window is now {bridge_type or bridge_class} — casting options resolved"
                        elif is_displaced_answer:
                            reason = (
                                f"window is now {bridge_type or bridge_class or 'nothing'} — "
                                f"no {_REQUEST_SCOPED_ACTIONS[action.action_type][0]} request to answer"
                            )
                        else:
                            reason = (
                                f"bridge has no SelectN/Search pending "
                                f"(now: {bridge_type or bridge_class or 'nothing'})"
                            )
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: {reason} — skipping (will re-plan)",
                        )
                        return ClickResult(True, 0, 0, action.action_type.value, "GRE bridge (stale-skip)")

                    msg = (
                        f"Game advanced past {action.action_type.value} "
                        f"({action.card_name or '?'}) — bridge no longer offers this action. Take it manually if still needed."
                    )
                    return self._manual_required_bridge_result(
                        action,
                        game_state,
                        "planner_action_stale",
                        msg,
                    )

                if action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE):
                    try:
                        live = self._gre_bridge.get_pending_actions() or {}
                    except Exception:
                        live = {}
                    live_type = str(live.get("request_type") or "")
                    live_class = str(live.get("request_class") or "")
                    if not live.get("has_pending"):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: window already closed (live poll) — no-op",
                        )
                        return ClickResult(
                            True,
                            0,
                            0,
                            action.action_type.value,
                            "GRE bridge (no-op, window closed)",
                        )
                    if not (
                        live_type in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                        or live_class in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
                    ):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: window is now "
                            f"{live_class or live_type} — skipping (will re-plan)",
                        )
                        return ClickResult(
                            True,
                            0,
                            0,
                            action.action_type.value,
                            "GRE bridge (stale-skip)",
                        )

                bridge_pending_anything = bool(
                    game_state.get("_bridge_request_type")
                    or game_state.get("_bridge_request_class")
                    or game_state.get("_bridge_has_pending")
                )
                if (
                    action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE)
                    and not bridge_pending_anything
                ):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"{action.action_type.value}: no pending bridge request — benign no-op",
                    )
                    return ClickResult(
                        True,
                        0,
                        0,
                        action.action_type.value,
                        "GRE bridge (no-op, no window)",
                    )

                if action.action_type == ActionType.SELECT_TARGET and not action.card_name:
                    pending = game_state.get("_bridge_pending") or {}
                    candidates = pending.get("candidate_ids") or []
                    if len(candidates) == 1:
                        only_id = int(candidates[0])
                        if self._progress_bridge(game_state, poll=live or None).submit_targets([only_id]):
                            self._log_execution_path(
                                ExecutionPath.GRE_AWARE,
                                f"select_target (no name): auto-picked sole candidate {only_id}",
                            )
                            return ClickResult(True, 0, 0, "select_target", "GRE bridge")

                msg = (
                    f"Bridge couldn't handle {action.action_type.value} "
                    f"({action.card_name or '?'}) — take this action manually."
                )
                return self._manual_required_bridge_result(
                    action,
                    game_state,
                    "bridge_submit_failed",
                    msg,
                )

            msg = (
                f"GRE bridge is unavailable for {action.action_type.value} "
                f"({action.card_name or '?'}) — take this action manually."
            )
            return self._manual_required_bridge_result(
                action,
                game_state,
                "bridge_unavailable",
                msg,
            )

        return ClickResult(False, 0, 0, action.action_type.value, "Bridge not connected")

    def _find_instance_id(
        self, card_name: str, battlefield: list[dict[str, Any]], owner_seat: int
    ) -> int | None:
        """Find the instance_id of a card on the battlefield by name and owner."""
        import re

        match = re.match(r"^(.*?)(?:\s+#(\d+))?$", card_name.strip())
        base_name = (match.group(1) if match else card_name).strip().lower()
        ordinal = int(match.group(2)) if match and match.group(2) else 1

        matches = [
            card
            for card in battlefield
            if card.get("owner_seat_id") == owner_seat and card.get("name", "").strip().lower() == base_name
        ]
        if not matches:
            return None

        matches.sort(key=lambda card: int(card.get("instance_id", 0) or 0))
        index = max(0, min(ordinal - 1, len(matches) - 1))
        return matches[index].get("instance_id")
