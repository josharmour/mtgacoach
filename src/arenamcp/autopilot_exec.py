"""Action-execution helpers for AutopilotEngine, extracted from autopilot.py.

Pure move: methods are unchanged and mixed back into AutopilotEngine."""

import logging
import re
import time
from typing import Any

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot_models import ExecutionPath
from arenamcp.gre_bridge import (
    _ACTIONS_AVAILABLE_BRIDGE_REQUESTS,
)
from arenamcp.input_controller import ClickResult
from arenamcp.screen_mapper import ScreenCoord

logger = logging.getLogger(__name__)


class _ActionExecMixin:
    def _execute_action(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Route an action to the appropriate execution handler.

        Tries the GRE bridge first for direct submission (no mouse clicks).
        Falls back to screen-mapped mouse/keyboard input if the bridge is
        unavailable or fails.

        Args:
            action: The GameAction to execute.
            game_state: Current game state for context.

        Returns:
            ClickResult from the execution.
        """
        # Match just ended — the bridge is in Intermission and no action
        # (including pass_priority) is legal. Queued actions from the last
        # priority window would otherwise fail against IntermissionRequest
        # and produce duplicate `bridge_only_suppressed` fallback bug
        # reports (see issues #124-127). Treat it as a silent no-op.
        if game_state.get("_bridge_in_intermission") or game_state.get("match_ended"):
            logger.info(f"Autopilot: skipping {action.action_type.value} — bridge in intermission")
            return ClickResult(True, 0, 0, action.action_type.value, "intermission_noop")

        # Try GRE bridge first (direct action submission, no mouse needed)
        if not self._config.dry_run:
            gre_result = self._try_gre_bridge(action, game_state)
            if (
                gre_result is None
                and not getattr(self._gre_bridge, "connected", False)
                and self._wait_for_bridge_reconnect()
            ):
                # Bridge came back mid-window — retry the submission instead
                # of cascading into MANUAL REQUIRED (live failure 2026-06-07:
                # every action in a match died "Bridge offline" because the
                # executor never gave the plugin's reconnect loop a chance).
                gre_result = self._try_gre_bridge(action, game_state)
            if gre_result is not None:
                return gre_result

        bridge_connected = self._gre_bridge is not None and getattr(self._gre_bridge, "connected", False)
        if self._config.bridge_only_when_connected and not self._config.dry_run:
            if bridge_connected:
                # Distinguish "planner picked an action the bridge has already
                # moved past" (e.g. user already played a land this turn, but
                # the planner saw stale legal_actions) from a real bridge
                # failure. We still surface MANUAL REQUIRED in both cases —
                # the user needs to take over — but the stale-state path
                # is self-inflicted and shouldn't auto-file a bug report
                # (see issues #136 #137 #139 #140 — the cluster of
                # `bridge_submit_failed` for play_land where the bridge
                # simply has no Play action because lands_played != 0).
                # Classify against the LIVE window, not the planning
                # snapshot. During window churn the snapshot routinely
                # lags the bridge by one request — declare_attackers vs a
                # SelectTargets that just opened (#243), click_button vs a
                # Search that became OptionalAction (#242 #235 #232),
                # select_n vs SelectTargets (#266 #234). Snapshot-based
                # classification calls those "real failures" and pauses.
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
                        # Window closed entirely: nothing for anyone to act
                        # on. Silent no-op — the next window replans.
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
                    # Silent-skip cases — the bridge will surface the right
                    # request shortly and the next plan cycle will pick
                    # correctly. Pausing for manual input here is wrong:
                    # the user can't act on a step that hasn't started yet
                    # (combat) or one that's been displaced by an
                    # in-resolution decision window (SelectN/Search/etc).
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
                    is_displaced_main_action = (
                        action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL)
                        and bridge_has_other_request
                    )
                    is_displaced_select = action.action_type in (
                        ActionType.SELECT_N,
                        ActionType.SEARCH_LIBRARY,
                        ActionType.SELECT_COUNTERS,
                        # SELECT_TARGET was missing here — a stale
                        # select_target vs a live DeclareAttackers window
                        # fell through to MANUAL REQUIRED and burned the
                        # user's whole attack step (live 2026-07-02:
                        # Nesting Grounds counter-move re-planned after it
                        # had already resolved; no attack was declared).
                        ActionType.SELECT_TARGET,
                    )
                    is_displaced_pass = (
                        action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE)
                        and bridge_has_other_request
                    )

                    # Combat livelock fix: when we want to declare attackers/
                    # blockers but the bridge is still at a precombat
                    # ActionsAvailableRequest, a no-op skip livelocks — the next
                    # plan re-issues the same combat action and we never advance.
                    # Pass priority to move the game into the combat step, where
                    # the bridge presents the DeclareAttacker/Blocker request and
                    # we can actually attack/block.
                    if (
                        is_combat_stale
                        and bridge_is_actions_available
                        and bool(gs_for_classify.get("_bridge_can_pass"))
                        and self._gre_bridge is not None
                    ):
                        try:
                            if self._gre_bridge.submit_pass():
                                self._log_execution_path(
                                    ExecutionPath.GRE_AWARE,
                                    f"{action.action_type.value}: not in combat "
                                    "step — passing priority to advance toward "
                                    "combat",
                                )
                                return ClickResult(
                                    True,
                                    0,
                                    0,
                                    "pass_priority",
                                    "GRE bridge (advance-to-combat)",
                                )
                        except Exception as e:
                            logger.debug(f"advance-to-combat pass failed: {e}")

                    if (
                        is_combat_stale
                        or is_displaced_main_action
                        or is_displaced_select
                        or is_displaced_pass
                    ):
                        if is_combat_stale:
                            reason = "bridge not in combat step yet"
                        elif is_displaced_main_action:
                            reason = f"bridge moved to {bridge_type or bridge_class}"
                        elif is_displaced_pass:
                            reason = f"window is now {bridge_type or bridge_class} — pass not applicable"
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

                    # Real Shape 1: bridge IS ActionsAvailable but doesn't
                    # offer the specific Play/Cast we wanted. The user
                    # genuinely needs to take over (e.g. they already played
                    # their land for the turn).
                    msg = (
                        f"Game advanced past {action.action_type.value} "
                        f"({action.card_name or '?'}) — bridge no longer "
                        "offers this action. Take it manually if still needed."
                    )
                    return self._manual_required_bridge_result(
                        action,
                        game_state,
                        "planner_action_stale",
                        msg,
                    )

                # Pass/resolve failures are overwhelmingly races: the window
                # we planned against closed or was replaced while the LLM was
                # thinking. Classify against a LIVE poll, not the planning
                # snapshot — the snapshot routinely still says
                # ActionsAvailable when the bridge has already moved on
                # (observed live 2026-06-10: repeated "failure N/5:
                # pass_priority" during opponent-turn window churn).
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
                    # Live window IS ActionsAvailable and pass still failed —
                    # fall through to the genuine manual-required path.

                # Pattern A: pass_priority + nothing pending = no-op success.
                # MTGA already cleared the priority window we wanted to pass on
                # (race between plan execution and decision clearing). Logging
                # a "manual required" here is misleading — there's literally
                # nothing for the user to do. Treat as benign success.
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
                        f"{action.action_type.value}: bridge has nothing pending — no-op",
                    )
                    return ClickResult(True, 0, 0, action.action_type.value, "GRE bridge (no-op)")

                # Pattern B: select_target with no card_name. If the bridge
                # has SelectTargets pending and exactly one legal candidate,
                # submit it as a safety net (the planner failed to specify
                # which target, but there's only one valid choice). Otherwise
                # fall through to manual required — multi-candidate selection
                # without a name is a real planner gap that needs a human.
                if action.action_type == ActionType.SELECT_TARGET and not (action.card_name or "").strip():
                    bridge_class = str(game_state.get("_bridge_request_class") or "")
                    bridge_type = str(game_state.get("_bridge_request_type") or "")
                    if "SelectTargets" in bridge_class or "SelectTargets" in bridge_type:
                        only_id = self._pick_single_target_candidate(game_state)
                        if only_id is not None and self._gre_bridge.submit_targets(only_id):
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

        # Legacy mouse fallback path retained only when bridge-only mode is off.
        gre_ref = getattr(action, "gre_action_ref", None)
        if gre_ref is not None:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"{action.action_type.value}: {action.card_name or action} (gre_ref={gre_ref}, bridge unavailable)",
            )

        handlers = {
            ActionType.PASS_PRIORITY: self._exec_pass_priority,
            ActionType.RESOLVE: self._exec_resolve,
            ActionType.CLICK_BUTTON: lambda: self._exec_click_button(action),
            ActionType.PLAY_LAND: lambda: self._exec_play_card(action, game_state),
            ActionType.CAST_SPELL: lambda: self._exec_play_card(action, game_state),
            ActionType.ACTIVATE_ABILITY: lambda: self._exec_activate_ability(action, game_state),
            ActionType.DECLARE_ATTACKERS: lambda: self._exec_declare_attackers(action, game_state),
            ActionType.DECLARE_BLOCKERS: lambda: self._exec_declare_blockers(action, game_state),
            ActionType.SELECT_TARGET: lambda: self._exec_select_target(action, game_state),
            ActionType.SELECT_N: lambda: self._exec_select_n(action, game_state),
            ActionType.MODAL_CHOICE: lambda: self._exec_modal_choice(action, game_state),
            ActionType.MULLIGAN_KEEP: lambda: self._exec_mulligan(keep=True),
            ActionType.MULLIGAN_MULL: lambda: self._exec_mulligan(keep=False),
            ActionType.DRAFT_PICK: lambda: self._exec_draft_pick(action, game_state),
            ActionType.ORDER_BLOCKERS: lambda: self._exec_order_blockers(action, game_state),
            # New decision types — most resolve via Done/pass after LLM selection
            ActionType.ASSIGN_DAMAGE: lambda: self._exec_done_action("assign_damage"),
            ActionType.ORDER_COMBAT_DAMAGE: lambda: self._exec_done_action("order_combat_damage"),
            ActionType.PAY_COSTS: lambda: self._exec_pay_costs(action, game_state),
            ActionType.SEARCH_LIBRARY: lambda: self._exec_select_n(action, game_state),
            ActionType.DISTRIBUTE: lambda: self._exec_done_action("distribute"),
            ActionType.NUMERIC_INPUT: lambda: self._exec_done_action("numeric_input"),
            ActionType.CHOOSE_STARTING_PLAYER: lambda: self._exec_choose_play_draw(action),
            ActionType.SELECT_REPLACEMENT: lambda: self._exec_done_action("select_replacement"),
            ActionType.SELECT_COUNTERS: lambda: self._exec_select_n(action, game_state),
            ActionType.CASTING_OPTIONS: lambda: self._exec_modal_choice(action, game_state),
            ActionType.ORDER_TRIGGERS: lambda: self._exec_done_action("order_triggers"),
        }

        handler = handlers.get(action.action_type)
        if handler:
            result = handler()
            if result.success:
                return result
            # Click handler failed — only allow auto_respond for safe cases.
            logger.warning(
                f"Action handler failed for {action.action_type.value}: {result.error}. "
                "Evaluating safe fallback."
            )
        else:
            result = ClickResult(False, 0, 0, str(action), f"No handler for {action.action_type}")

        if (
            not self._config.dry_run
            and self._should_allow_auto_respond(game_state, action)
            and (self._gre_bridge.connected or self._gre_bridge.connect())
        ) and self._gre_bridge.auto_respond():
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"auto_respond fallback: {action.action_type.value} '{action.card_name}'",
            )
            # Log diagnostic for future fix
            game_state_summary = {
                "action_type": action.action_type.value,
                "card_name": action.card_name,
                "target_names": action.target_names,
                "attacker_names": action.attacker_names,
                "blocker_assignments": action.blocker_assignments,
                "pending_decision": game_state.get("pending_decision"),
                "bridge_request": game_state.get("_bridge_request_type"),
                "bridge_class": game_state.get("_bridge_request_class"),
                "legal_actions": game_state.get("legal_actions", [])[:5],
            }
            logger.warning(
                f"AUTO_RESPOND_FALLBACK: {game_state_summary} — "
                "this action type needs a proper bridge handler"
            )
            return ClickResult(True, 0, 0, action.card_name or str(action), "auto_respond fallback")

        if self._is_critical_decision_state(game_state, action):
            self._pause_for_manual(f"No safe automatic fallback for {action.action_type.value}", game_state)
            return ClickResult(False, 0, 0, action.card_name or str(action), "manual required")

        return result

    def _click_fixed(self, name: str) -> ClickResult:
        """Click a fixed-position button by name."""
        coord = self._mapper.get_button_coord(name)
        if not coord:
            return ClickResult(False, 0, 0, name, f"Unknown button: {name}")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, coord.description, window_rect)

    def _exec_pass_priority(self) -> ClickResult:
        """Click the pass/resolve button."""
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, "pass_priority: fixed button")
        return self._click_fixed("pass")

    def _exec_resolve(self) -> ClickResult:
        """Click the resolve button."""
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, "resolve: fixed button")
        return self._click_fixed("resolve")

    def _exec_click_button(self, action: GameAction) -> ClickResult:
        """Click a named button."""
        button_name = action.card_name.lower().replace(" ", "_")
        # Optional-action dialogs (e.g. commander-to-command-zone prompt) are
        # answered via the GRE bridge's submit_optional, not by clicking at a
        # fixed coordinate — the dialog buttons have no deterministic location.
        if button_name in ("accept", "allow", "yes", "decline", "cancel", "no"):
            if self._gre_bridge.connected or self._gre_bridge.connect():
                accept = button_name in ("accept", "allow", "yes")
                if self._gre_bridge.submit_optional(accept):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"optional: submit_optional(accept={accept}) via GRE bridge",
                    )
                    return ClickResult(True, 0, 0, button_name, "GRE bridge")
            logger.warning(
                "optional %s could not be submitted via GRE bridge — no click fallback",
                button_name,
            )
            return ClickResult(False, 0, 0, button_name, "submit_optional failed")
        self._log_execution_path(
            ExecutionPath.DETERMINISTIC_GEOMETRY, f"click_button: {button_name} (fixed coords)"
        )
        # Fallback for common MTGA action buttons that might be named differently by the LLM
        if button_name in ("next", "attack", "all_attack", "done", "no_attacks", "no_blocks"):
            return self._click_fixed("pass")  # They all share the same spot
        return self._click_fixed(button_name)

    def _exec_play_card(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Play a card from hand (land or spell).

        Lands are dragged from hand to the battlefield land row (y ≈ 0.75)
        because MTGA requires a drag gesture to play them. Spells are
        clicked normally (MTGA auto-casts on click).

        Coordinate resolution priority:
        1. Deterministic arc-based hand geometry
        2. Vision fallback (only if deterministic fails and vision is enabled)
        """
        hand = game_state.get("hand", [])
        hand_names = [c.get("name", "???") for c in hand]
        logger.info(
            f"_exec_play_card: looking for '{action.card_name}' in hand ({len(hand)} cards): {hand_names}"
        )
        coord = self._mapper.get_card_in_hand_coord(action.card_name, hand, game_state)

        if coord:
            self._log_execution_path(
                ExecutionPath.DETERMINISTIC_GEOMETRY,
                f"play_card: '{action.card_name}' found via arc-based hand lookup",
            )
        else:
            # Vision fallback — only if deterministic fails
            if self._config.enable_vision_fallback and not (
                self._config.prefer_deterministic and getattr(action, "gre_action_ref", None) is not None
            ):
                logger.info(f"Trying vision fallback for '{action.card_name}'")
                coord = self._get_vision_coord(action.card_name, zone="hand")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK, f"play_card: '{action.card_name}' found via vision"
                    )

            if not coord:
                return ClickResult(
                    False, 0, 0, action.card_name, "Card not found in hand (Heuristic & Vision failed)"
                )

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, action.card_name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)

        # Lands and Spells: drag from hand to battlefield center
        if action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL):
            target = ScreenCoord(0.50, 0.50, f"Battlefield: {action.card_name}")
            to_x, to_y = target.to_absolute(window_rect)
            return self._controller.drag_card_from_hand(
                abs_x, abs_y, to_x, to_y, action.card_name, window_rect
            )

        # Abilities/Other: click to cast
        return self._controller.click_card_in_hand(abs_x, abs_y, action.card_name, window_rect)

    def _exec_activate_ability(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Click a permanent on the battlefield to activate its ability.

        Coordinate resolution priority:
        1. Deterministic heuristic (permanent grid position)
        2. Vision fallback (only if deterministic fails and vision is enabled)
        """
        battlefield = game_state.get("battlefield", [])
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")

        if not local_seat:
            return ClickResult(False, 0, 0, action.card_name, "Local seat not found")

        # Try instance_id lookup if available from GRE context
        instance_id = self._find_instance_id(action.card_name, battlefield, local_seat)
        coord = self._mapper.get_permanent_coord(
            action.card_name, instance_id, battlefield, local_seat, local_seat
        )

        if coord:
            self._log_execution_path(
                ExecutionPath.DETERMINISTIC_GEOMETRY,
                f"activate_ability: '{action.card_name}' found via heuristic lookup",
            )
        else:
            # Vision fallback — only if deterministic fails
            if self._config.enable_vision_fallback and not (
                self._config.prefer_deterministic and getattr(action, "gre_action_ref", None) is not None
            ):
                logger.info(f"Trying vision fallback for board permanent '{action.card_name}'")
                coord = self._get_vision_coord(action.card_name, zone="battlefield_yours")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"activate_ability: '{action.card_name}' found via vision",
                    )

            if not coord:
                return ClickResult(
                    False,
                    0,
                    0,
                    action.card_name,
                    "Permanent not found on battlefield (Heuristic & Vision failed)",
                )

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, action.card_name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, f"Activate: {action.card_name}", window_rect)

    def _exec_declare_attackers(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Click each attacking creature, then click Done.

        When instance_ids are available from the decision context, uses them
        for more reliable coordinate lookup.
        """
        # Log GRE action reference if present
        gre_ref = getattr(action, "gre_action_ref", None)
        if gre_ref is not None:
            logger.info(f"declare_attackers: GRE action ref type={type(gre_ref).__name__}, value={gre_ref}")

        battlefield = game_state.get("battlefield", [])
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")

        if not local_seat:
            return ClickResult(False, 0, 0, "attackers", "Local seat not found")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "attackers", "MTGA window not found")

        # Build name -> instance_id mapping from decision context if available
        attacker_id_map = self._build_attacker_id_map(game_state)

        last_result = ClickResult(True, 0, 0, "attackers")

        for attacker_name in action.attacker_names:
            # Prefer instance_id lookup from decision context
            instance_id = attacker_id_map.get(attacker_name)
            if instance_id is None:
                # Fallback: search battlefield for matching name
                instance_id = self._find_instance_id(attacker_name, battlefield, local_seat)

            coord = self._mapper.get_permanent_coord(
                attacker_name, instance_id, battlefield, local_seat, local_seat
            )
            if coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_attackers: '{attacker_name}' (instance_id={instance_id})",
                )
                abs_x, abs_y = coord.to_absolute(window_rect)
                result = self._controller.click(abs_x, abs_y, f"Attack: {attacker_name}", window_rect)
                if not result.success:
                    logger.warning(f"Failed to click attacker {attacker_name}")
                last_result = result
            else:
                # Vision fallback for attackers
                if self._config.enable_vision_fallback:
                    coord = self._get_vision_coord(attacker_name, zone="battlefield_yours")
                    if coord:
                        self._log_execution_path(
                            ExecutionPath.VISION_FALLBACK,
                            f"declare_attackers: '{attacker_name}' found via vision",
                        )
                        abs_x, abs_y = coord.to_absolute(window_rect)
                        result = self._controller.click(abs_x, abs_y, f"Attack: {attacker_name}", window_rect)
                        last_result = result
                    else:
                        logger.warning(f"Failed to find attacker {attacker_name} (heuristic & vision)")
                else:
                    logger.warning(
                        f"Failed to find attacker {attacker_name} (heuristic only, vision disabled)"
                    )
            self._controller.wait(self._config.action_delay, "between attacker clicks")

        # Click Done
        self._controller.wait(0.3, "before Done")
        done_result = self._click_fixed("done")
        return done_result if done_result.success else last_result

    def _exec_declare_blockers(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Click blocker, then click attacker it should block, then Done.

        When instance_ids are available from the decision context, uses them
        for more reliable coordinate lookup.
        """
        # Log GRE action reference if present
        gre_ref = getattr(action, "gre_action_ref", None)
        if gre_ref is not None:
            logger.info(f"declare_blockers: GRE action ref type={type(gre_ref).__name__}, value={gre_ref}")

        battlefield = game_state.get("battlefield", [])
        local_seat = None
        opp_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
            else:
                opp_seat = p.get("seat_id")

        if not local_seat or not opp_seat:
            return ClickResult(False, 0, 0, "blockers", "Seat info not found")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "blockers", "MTGA window not found")

        # Build name -> instance_id mapping from decision context if available
        blocker_id_map = self._build_blocker_id_map(game_state)

        last_result = ClickResult(True, 0, 0, "blockers")

        for blocker_name, attacker_name in action.blocker_assignments.items():
            # Click the blocker (our creature) — prefer instance_id lookup
            blocker_instance_id = blocker_id_map.get(blocker_name)
            if blocker_instance_id is None:
                blocker_instance_id = self._find_instance_id(blocker_name, battlefield, local_seat)

            blocker_coord = self._mapper.get_permanent_coord(
                blocker_name, blocker_instance_id, battlefield, local_seat, local_seat
            )
            blocker_found = False
            if blocker_coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_blockers: blocker '{blocker_name}' (instance_id={blocker_instance_id})",
                )
                bx, by = blocker_coord.to_absolute(window_rect)
                self._controller.click(bx, by, f"Blocker: {blocker_name}", window_rect)
                self._controller.wait(0.2, "blocker selected")
                blocker_found = True
            elif self._config.enable_vision_fallback:
                coord = self._get_vision_coord(blocker_name, zone="battlefield_yours")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"declare_blockers: blocker '{blocker_name}' found via vision",
                    )
                    bx, by = coord.to_absolute(window_rect)
                    self._controller.click(bx, by, f"Blocker: {blocker_name}", window_rect)
                    self._controller.wait(0.2, "blocker selected")
                    blocker_found = True

            if not blocker_found:
                logger.warning(
                    f"Could not locate blocker '{blocker_name}' "
                    f"(instance_id={blocker_instance_id}) — aborting block assignment"
                )
                return ClickResult(False, 0, 0, "blockers", f"Blocker '{blocker_name}' not found")

            # Click the attacker (opponent's creature) — use instance_id if available
            attacker_instance_id = self._find_instance_id(attacker_name, battlefield, opp_seat)
            attacker_coord = self._mapper.get_permanent_coord(
                attacker_name, attacker_instance_id, battlefield, opp_seat, local_seat
            )
            attacker_found = False
            if attacker_coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"declare_blockers: attacker '{attacker_name}' (instance_id={attacker_instance_id})",
                )
                ax, ay = attacker_coord.to_absolute(window_rect)
                result = self._controller.click(
                    ax, ay, f"Block {attacker_name} with {blocker_name}", window_rect
                )
                last_result = result
                attacker_found = True
            elif self._config.enable_vision_fallback:
                coord = self._get_vision_coord(attacker_name, zone="battlefield_opponent")
                if coord:
                    self._log_execution_path(
                        ExecutionPath.VISION_FALLBACK,
                        f"declare_blockers: attacker '{attacker_name}' found via vision",
                    )
                    ax, ay = coord.to_absolute(window_rect)
                    result = self._controller.click(
                        ax, ay, f"Block {attacker_name} with {blocker_name}", window_rect
                    )
                    last_result = result
                    attacker_found = True

            if not attacker_found:
                logger.warning(
                    f"Could not locate attacker '{attacker_name}' "
                    f"(instance_id={attacker_instance_id}) — aborting block assignment"
                )
                return ClickResult(False, 0, 0, "blockers", f"Attacker '{attacker_name}' not found")
            self._controller.wait(self._config.action_delay, "between block assignments")

        # Click Done
        self._controller.wait(0.3, "before Done")
        done_result = self._click_fixed("done")
        return done_result if done_result.success else last_result

    def _exec_select_target(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Click on a target permanent or player.

        Coordinate resolution priority:
        1. Instance_id-based deterministic lookup (if available from GRE context)
        2. Name-based deterministic heuristic lookup
        3. Vision fallback (if both above fail)
        """
        if not action.target_names:
            return ClickResult(False, 0, 0, "target", "No target specified")

        # Log GRE action reference if present
        gre_ref = getattr(action, "gre_action_ref", None)
        if gre_ref is not None:
            logger.info(f"select_target: GRE action ref type={type(gre_ref).__name__}, value={gre_ref}")

        target_name = action.target_names[0]
        battlefield = game_state.get("battlefield", [])

        # Try to find target on battlefield
        local_seat = None
        opp_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
            else:
                opp_seat = p.get("seat_id")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, target_name, "MTGA window not found")

        # Search both sides of the battlefield, using instance_id when available
        for owner in self._get_target_owner_order(game_state, local_seat, opp_seat):
            if owner is None:
                continue
            instance_id = self._find_instance_id(target_name, battlefield, owner)
            coord = self._mapper.get_permanent_coord(target_name, instance_id, battlefield, owner, local_seat)
            if coord:
                self._log_execution_path(
                    ExecutionPath.DETERMINISTIC_GEOMETRY,
                    f"select_target: '{target_name}' (owner={owner}, instance_id={instance_id})",
                )
                abs_x, abs_y = coord.to_absolute(window_rect)
                return self._controller.click(abs_x, abs_y, f"Target: {target_name}", window_rect)

        # Vision fallback for targets
        if self._config.enable_vision_fallback:
            coord = self._get_vision_coord(target_name, zone="battlefield")
            if coord:
                self._log_execution_path(
                    ExecutionPath.VISION_FALLBACK, f"select_target: '{target_name}' found via vision"
                )
                abs_x, abs_y = coord.to_absolute(window_rect)
                return self._controller.click(abs_x, abs_y, f"Target: {target_name}", window_rect)

        return ClickResult(False, 0, 0, target_name, "Target not found on battlefield")

    def _exec_select_n(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Handle scry or multi-select UI (select N cards)."""
        # Scry: top or bottom
        if action.scry_position:
            button = "scry_top" if action.scry_position == "top" else "scry_bottom"
            return self._click_fixed(button)

        # Multi-select: click each card then Done
        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "select_n", "MTGA window not found")

        last_result = ClickResult(True, 0, 0, "select_n")

        for i, card_name in enumerate(action.select_card_names):
            coord = self._mapper.get_option_coord(i, len(action.select_card_names), "select")
            if coord:
                abs_x, abs_y = coord.to_absolute(window_rect)
                result = self._controller.click(abs_x, abs_y, f"Select: {card_name}", window_rect)
                last_result = result
                self._controller.wait(0.2, "between selections")

        # Click Done
        self._controller.wait(0.3, "before Done")
        done_result = self._click_fixed("done")
        return done_result if done_result.success else last_result

    def _exec_modal_choice(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Click a modal choice option."""
        # Determine total options from decision context
        decision = game_state.get("decision_context", {})
        total_options = decision.get("total_options", 2)

        coord = self._mapper.get_option_coord(action.modal_index, total_options, "modal")
        if not coord:
            return ClickResult(False, 0, 0, "modal", "Cannot determine option position")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, "modal", "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, f"Modal option {action.modal_index}", window_rect)

    def _exec_mulligan(self, keep: bool) -> ClickResult:
        """Click Keep or Mulligan button."""
        choice = "keep" if keep else "mulligan"
        self._log_execution_path(ExecutionPath.DETERMINISTIC_GEOMETRY, f"mulligan: {choice} (fixed coords)")
        return self._click_fixed(choice)

    def _exec_draft_pick(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Double-click a draft card to pick it."""
        # Try positional first, then vision fallback
        # For draft, we need pack info
        pack = game_state.get("draft_pack", {})
        cards = pack.get("cards", [])
        pack_size = len(cards)

        # Find card index
        card_idx = None
        for i, card in enumerate(cards):
            if card.get("name", "").lower() == action.card_name.lower():
                card_idx = i
                break

        if card_idx is None:
            return ClickResult(False, 0, 0, action.card_name, "Card not found in draft pack")

        coord = self._mapper.get_draft_card_coord(card_idx, pack_size)
        if not coord:
            return ClickResult(False, 0, 0, action.card_name, "Cannot calculate draft position")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, action.card_name, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.double_click(abs_x, abs_y, f"Draft pick: {action.card_name}", window_rect)

    def _exec_order_blockers(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Order blockers by dragging (rarely needed)."""
        # Blocker ordering uses drag to reorder. For now, just click Done
        # since MTGA defaults to a reasonable order.
        logger.info("Blocker ordering: using default order (click Done)")
        return self._click_fixed("done")

    def _exec_done_action(self, decision_name: str) -> ClickResult:
        """Generic handler for decisions that just need a Done click after MTGA auto-selects."""
        self._log_execution_path(
            ExecutionPath.DETERMINISTIC_GEOMETRY, f"done_action: {decision_name} (fixed coords)"
        )
        logger.info(f"{decision_name}: accepting default / clicking Done")
        result = self._click_fixed("done")
        if not result.success:
            # Fallback: try spacebar
            self._controller.press_key("space", f"{decision_name}: spacebar fallback")
            return ClickResult(True, 0, 0, decision_name, "spacebar fallback")
        return result

    @staticmethod
    def _parse_pay_cost_requirements(decision_context: dict[str, Any]) -> dict[str, int]:
        """Return normalized mana requirements for a Pay Costs decision."""
        requirements = {
            "generic": 0,
            "W": 0,
            "U": 0,
            "B": 0,
            "R": 0,
            "G": 0,
            "C": 0,
            "Any": 0,
        }

        raw = decision_context.get("mana_requirements")
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in requirements:
                    try:
                        requirements[key] = int(value)
                    except (TypeError, ValueError):
                        continue
            if any(requirements.values()):
                return requirements

        mana_cost = str(decision_context.get("mana_cost", "") or "")
        if not mana_cost:
            return requirements

        token_map = {
            "manacolor_white": "W",
            "manacolor_blue": "U",
            "manacolor_black": "B",
            "manacolor_red": "R",
            "manacolor_green": "G",
            "manacolor_colorless": "C",
            "manacolor_any": "Any",
            "manacolor_generic": "generic",
            "generic": "generic",
            "w": "W",
            "u": "U",
            "b": "B",
            "r": "R",
            "g": "G",
            "c": "C",
            "any": "Any",
        }
        for count_str, token in re.findall(r"(\d+)x([^,]+)", mana_cost):
            mapped = token_map.get(token.strip().lower())
            if not mapped:
                continue
            requirements[mapped] += int(count_str)

        return requirements

    @staticmethod
    def _infer_mana_source_colors(card: dict[str, Any]) -> set[str]:
        """Infer which colors a permanent can produce when tapped for mana."""
        colors: set[str] = set()
        color_map = {
            "1": "W",
            "2": "U",
            "3": "B",
            "4": "R",
            "5": "G",
            "6": "C",
            "manacolor_white": "W",
            "manacolor_blue": "U",
            "manacolor_black": "B",
            "manacolor_red": "R",
            "manacolor_green": "G",
            "manacolor_colorless": "C",
            "manacolor_any": "Any",
            "w": "W",
            "u": "U",
            "b": "B",
            "r": "R",
            "g": "G",
            "c": "C",
            "any": "Any",
        }

        for raw in card.get("color_production", []) or []:
            mapped = color_map.get(str(raw).strip().lower())
            if mapped:
                colors.add(mapped)

        name = str(card.get("name", "") or "")
        type_line = str(card.get("type_line", "") or "").lower()
        oracle = str(card.get("oracle_text", "") or "")
        oracle_lower = oracle.lower()

        if "plains" in name.lower() or "plains" in type_line:
            colors.add("W")
        if "island" in name.lower() or "island" in type_line:
            colors.add("U")
        if "swamp" in name.lower() or "swamp" in type_line:
            colors.add("B")
        if "mountain" in name.lower() or "mountain" in type_line:
            colors.add("R")
        if "forest" in name.lower() or "forest" in type_line:
            colors.add("G")
        if re.search(r"\{o?W\}", oracle):
            colors.add("W")
        if re.search(r"\{o?U\}", oracle):
            colors.add("U")
        if re.search(r"\{o?B\}", oracle):
            colors.add("B")
        if re.search(r"\{o?R\}", oracle):
            colors.add("R")
        if re.search(r"\{o?G\}", oracle):
            colors.add("G")
        if re.search(r"\{o?C\}", oracle):
            colors.add("C")
        if "any color" in oracle_lower:
            colors.add("Any")

        return colors

    @staticmethod
    def _select_pay_cost_sources(
        game_state: dict[str, Any],
        decision_context: dict[str, Any],
        local_seat: int,
    ) -> list[dict[str, Any]]:
        """Choose mana sources to tap for a Pay Costs decision."""
        battlefield = game_state.get("battlefield", [])
        by_instance = {
            card.get("instance_id"): card for card in battlefield if card.get("instance_id") is not None
        }

        autotap = decision_context.get("autotap_solution") or {}
        lands_to_tap = autotap.get("lands_to_tap") if isinstance(autotap, dict) else None
        if isinstance(lands_to_tap, list) and lands_to_tap:
            selected = []
            for tap in lands_to_tap:
                instance_id = tap.get("instanceId") if isinstance(tap, dict) else None
                card = by_instance.get(instance_id)
                if card and card.get("controller_seat_id") == local_seat and not card.get("is_tapped"):
                    selected.append(card)
            if selected:
                return selected

        requirements = _ActionExecMixin._parse_pay_cost_requirements(decision_context)
        if not any(requirements.values()):
            return []

        turn_num = game_state.get("turn", {}).get("turn_number", 0)
        candidates: list[dict[str, Any]] = []
        for card in battlefield:
            if card.get("controller_seat_id") != local_seat or card.get("is_tapped"):
                continue

            type_line = str(card.get("type_line", "") or "").lower()
            oracle = str(card.get("oracle_text", "") or "")
            is_land = "land" in type_line
            is_creature = "creature" in type_line
            has_mana_ability = bool(re.search(r"\{(?:o)?t\}.*add\s+(\{|one |two |three )", oracle, re.I))
            entered = card.get("turn_entered_battlefield", -1)
            has_haste = "haste" in oracle.lower()
            is_sick = is_creature and entered == turn_num and not has_haste

            if not (is_land or (has_mana_ability and not is_sick)):
                continue

            colors = _ActionExecMixin._infer_mana_source_colors(card)
            flexibility = len([color for color in colors if color != "Any"]) or 99
            candidates.append(
                {
                    "card": card,
                    "colors": colors,
                    "flexibility": flexibility,
                }
            )

        selected: list[dict[str, Any]] = []

        def pick_candidate(color: str | None = None) -> dict[str, Any] | None:
            pool = candidates
            if color is not None:
                pool = [
                    candidate
                    for candidate in candidates
                    if color in candidate["colors"] or "Any" in candidate["colors"]
                ]
            if not pool:
                return None
            if color is None:
                pool = sorted(
                    pool,
                    key=lambda candidate: (
                        candidate["flexibility"],
                        candidate["card"].get("name", ""),
                        candidate["card"].get("instance_id", 0),
                    ),
                )
            else:
                pool = sorted(
                    pool,
                    key=lambda candidate: (
                        0 if color in candidate["colors"] and "Any" not in candidate["colors"] else 1,
                        candidate["flexibility"],
                        candidate["card"].get("name", ""),
                        candidate["card"].get("instance_id", 0),
                    ),
                )
            chosen = pool[0]
            candidates.remove(chosen)
            selected.append(chosen)
            return chosen

        for color in ("W", "U", "B", "R", "G", "C"):
            for _ in range(requirements.get(color, 0)):
                if pick_candidate(color) is None:
                    return [candidate["card"] for candidate in selected]

        for _ in range(requirements.get("generic", 0) + requirements.get("Any", 0)):
            if pick_candidate() is None:
                break

        return [candidate["card"] for candidate in selected]

    def _click_battlefield_card(
        self,
        card: dict[str, Any],
        battlefield: list[dict[str, Any]],
        local_seat: int,
        description: str,
    ) -> ClickResult:
        """Click a permanent on the battlefield by instance ID when possible."""
        card_name = str(card.get("name", "") or description)
        instance_id = card.get("instance_id")
        owner_seat = card.get("owner_seat_id", local_seat)
        coord = self._mapper.get_permanent_coord(
            card_name,
            instance_id,
            battlefield,
            owner_seat,
            local_seat,
        )

        if coord is None and self._config.enable_vision_fallback:
            coord = self._get_vision_coord(card_name, zone="battlefield_yours")

        if coord is None:
            return ClickResult(False, 0, 0, description, f"Permanent not found: {card_name}")

        window_rect = self._mapper.window_rect
        if not window_rect:
            window_rect = self._mapper.refresh_window()
        if not window_rect:
            return ClickResult(False, 0, 0, description, "MTGA window not found")

        abs_x, abs_y = coord.to_absolute(window_rect)
        return self._controller.click(abs_x, abs_y, description, window_rect)

    def _exec_pay_costs(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Resolve Pay Costs by tapping mana sources instead of blind Done clicks."""
        decision_context = game_state.get("decision_context") or {}
        if decision_context.get("type") != "pay_costs":
            logger.info("pay_costs: no pay-costs context, falling back to Done")
            return self._exec_done_action("pay_costs")

        local_seat = None
        for player in game_state.get("players", []):
            if player.get("is_local"):
                local_seat = player.get("seat_id")
                break
        if local_seat is None:
            return ClickResult(False, 0, 0, "pay_costs", "Local seat not found")

        battlefield = game_state.get("battlefield", [])
        sources = self._select_pay_cost_sources(game_state, decision_context, local_seat)
        if not sources:
            if decision_context.get("has_autotap"):
                logger.info("pay_costs: no explicit tap targets, confirming autotap/default")
            else:
                logger.warning("pay_costs: no mana sources resolved, falling back to Done")
            return self._exec_done_action("pay_costs")

        descriptions = [str(source.get("name", source.get("instance_id", "?"))) for source in sources]
        self._log_execution_path(
            ExecutionPath.DETERMINISTIC_GEOMETRY,
            f"pay_costs: tapping {', '.join(descriptions)}",
        )
        logger.info("pay_costs: tapping mana sources %s", descriptions)

        last_result: ClickResult | None = None
        for source in sources:
            source_name = str(source.get("name", source.get("instance_id", "?")))
            result = self._click_battlefield_card(
                source,
                battlefield,
                local_seat,
                f"Mana source: {source_name}",
            )
            if not result.success:
                return result
            last_result = result
            time.sleep(0.08)

        if last_result is None:
            return ClickResult(False, 0, 0, "pay_costs", "No mana sources tapped")

        return last_result

    def _exec_choose_play_draw(self, action: GameAction) -> ClickResult:
        """Handle choose starting player (play or draw)."""
        choice = action.play_or_draw.lower() if action.play_or_draw else "play"
        logger.info(f"Choosing to {choice}")
        # In MTGA, "Play" is the first option button, "Draw" is second
        # Both typically resolve via the pass/done area or modal options
        if choice == "draw":
            # Try clicking the second option
            coord = self._mapper.get_option_coord(1, 2, "modal")
            if coord:
                window_rect = self._mapper.window_rect
                if not window_rect:
                    window_rect = self._mapper.refresh_window()
                if window_rect:
                    abs_x, abs_y = coord.to_absolute(window_rect)
                    return self._controller.click(abs_x, abs_y, "Choose: Draw", window_rect)
        # Default: "Play" = first option
        coord = self._mapper.get_option_coord(0, 2, "modal")
        if coord:
            window_rect = self._mapper.window_rect
            if not window_rect:
                window_rect = self._mapper.refresh_window()
            if window_rect:
                abs_x, abs_y = coord.to_absolute(window_rect)
                return self._controller.click(abs_x, abs_y, "Choose: Play", window_rect)
        # Last fallback
        return self._click_fixed("pass")

    @staticmethod
    def _get_target_owner_order(
        game_state: dict[str, Any],
        local_seat: int | None,
        opp_seat: int | None,
    ) -> list[int]:
        """Prefer the correct battlefield side for target selection."""
        decision = game_state.get("decision_context") or {}
        source_oracle = str(
            decision.get("source_oracle_text") or decision.get("source_card_oracle_text") or ""
        )
        if source_oracle:
            try:
                from arenamcp.rules_engine import RulesEngine

                req = RulesEngine._infer_target_requirements(source_oracle)
                if req.get("must_control") == "you":
                    return [seat for seat in (local_seat, opp_seat) if seat is not None]
                if req.get("must_control") == "opponent":
                    return [seat for seat in (opp_seat, local_seat) if seat is not None]
            except Exception as exc:
                logger.debug(f"target owner preference inference failed: {exc}")

        return [seat for seat in (opp_seat, local_seat) if seat is not None]

    def _find_instance_id(
        self, card_name: str, battlefield: list[dict[str, Any]], owner_seat: int
    ) -> int | None:
        """Find the instance_id of a card on the battlefield by name and owner.

        Searches battlefield entries for a card matching the given name and
        owner seat, returning its instance_id for more reliable coordinate
        lookup.

        Args:
            card_name: Card name to search for.
            battlefield: List of battlefield card dicts.
            owner_seat: Owner seat_id to filter by.

        Returns:
            instance_id if found, None otherwise.
        """
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

    def _build_attacker_id_map(self, game_state: dict[str, Any]) -> dict[str, int]:
        """Build a name -> instance_id map from the attacker decision context.

        Uses legal_attacker_ids from the decision context (if available) paired
        with legal_attackers names to create a reliable mapping.

        Returns:
            Dict mapping card name -> instance_id.
        """
        decision = game_state.get("decision_context") or {}
        if decision.get("type") != "declare_attackers":
            return {}

        names = decision.get("legal_attackers", [])
        ids = decision.get("legal_attacker_ids", [])
        if len(names) != len(ids) or not ids:
            return {}

        return dict(zip(names, ids, strict=False))

    def _build_blocker_id_map(self, game_state: dict[str, Any]) -> dict[str, int]:
        """Build a name -> instance_id map from the blocker decision context.

        Uses legal_blocker_ids from the decision context (if available) paired
        with legal_blockers names to create a reliable mapping.

        Returns:
            Dict mapping card name -> instance_id.
        """
        decision = game_state.get("decision_context") or {}
        if decision.get("type") != "declare_blockers":
            return {}

        names = decision.get("legal_blockers", [])
        ids = decision.get("legal_blocker_ids", [])
        if len(names) != len(ids) or not ids:
            return {}

        return dict(zip(names, ids, strict=False))
