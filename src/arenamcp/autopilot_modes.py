"""Autopilot modes (AFK, Land-Drop-Only, Deterministic Fallback) mixin.

Extracted from autopilot.py: methods are unchanged and mixed back into AutopilotEngine.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from arenamcp.action_planner import ActionPlan, ActionType, GameAction

logger = logging.getLogger(__name__)


class _AutopilotModesMixin:
    """AFK mode, land-drop mode, deterministic fallback, and optional cost handling."""

    _OPTIONAL_COST_OWN_ACTION_WINDOW_S = 10.0

    @property
    def afk_mode(self) -> bool:
        """Whether AFK mode is currently active."""
        return self._config.afk_mode

    def toggle_afk(self) -> bool:
        """Toggle AFK mode on/off."""
        self._config.afk_mode = not self._config.afk_mode
        state_str = "ON" if self._config.afk_mode else "OFF"
        self._notify("AUTOPILOT", f"AFK mode {state_str}")
        logger.info(f"AFK mode toggled {state_str}")
        return self._config.afk_mode

    @property
    def land_drop_mode(self) -> bool:
        """Whether Land-Drop-Only mode is currently active."""
        return self._config.land_drop_mode

    def toggle_land_drop(self) -> bool:
        """Toggle Land-Drop-Only mode on/off."""
        self._config.land_drop_mode = not self._config.land_drop_mode
        state_str = "ON" if self._config.land_drop_mode else "OFF"
        self._notify("AUTOPILOT", f"Land-Drop mode {state_str}")
        logger.info(f"Land-Drop mode toggled {state_str}")
        return self._config.land_drop_mode

    def _should_decline_optional_cost(self, game_state: dict[str, Any]) -> str | None:
        """Reason to decline this PayCosts window instead of blind auto-pay."""
        if self._last_cast_submitted and (
            time.monotonic() - self._last_cast_submitted_ts <= self._OPTIONAL_COST_OWN_ACTION_WINDOW_S
        ):
            return None
        if not game_state.get("_bridge_can_cancel"):
            return None
        if not self._source_spell_is_harmful_to_target(game_state, None, None):
            return None
        name, oracle = self._resolve_decision_source(game_state)
        verdict: bool | None = None
        try:
            verdict = self._planner.plan_pay_or_decline(name, oracle, game_state)
        except Exception as e:
            logger.debug(f"pay/decline LLM check failed: {e}")
        if verdict is True:
            return None
        if verdict is False:
            return f"harmful optional cost ({name or 'unknown'}): LLM chose decline"
        return f"harmful optional cost ({name or 'unknown'}): LLM unavailable, declining conservatively"

    def _deterministic_fallback(
        self,
        game_state: dict[str, Any],
        trigger: str,
    ) -> ActionPlan:
        """Generate a deterministic fallback plan when LLM is unavailable."""
        legal_actions = self._get_legal_actions(game_state)
        legal_actions = self._filter_rolled_back_casts(legal_actions, game_state)
        plan = ActionPlan(
            trigger=trigger,
            turn_number=game_state.get("turn", {}).get("turn_number", 0),
            raw_response="deterministic fallback",
        )

        turn = game_state.get("turn", {})
        is_my_turn = turn.get("active_player") == "local" or turn.get("decision_player") == "local"
        phase = turn.get("phase", "").lower()
        step = turn.get("step", "").lower()

        can_play_land = "main" in phase or "precombat" in step or "postcombat" in step
        if is_my_turn and can_play_land:
            land_actions = [a for a in legal_actions if a.startswith("Play ") or a.startswith("Cast ")]
            lands_in_hand = [
                c
                for c in game_state.get("hand", [])
                if "Land" in c.get("card_types", []) or "Land" in c.get("type_line", "")
            ]
            if lands_in_hand and land_actions:
                for la in land_actions:
                    action = self._planner._legal_action_to_action(la)
                    if action:
                        action.reasoning = "deterministic fallback: play land"
                        plan.actions = [action]
                        plan.overall_strategy = f"Fallback: {la}"
                        logger.info(f"Deterministic fallback: {la}")
                        return plan

        if legal_actions:
            priority_order = [
                lambda a: a.startswith("Play "),
                lambda a: a.startswith("Cast ") and "Creature" in a,
                lambda a: a.startswith("Cast "),
                lambda a: a.startswith("Activate "),
            ]
            selected = None
            for pred in priority_order:
                matching = [a for a in legal_actions if pred(a)]
                if matching:
                    selected = matching[0]
                    break

            if selected:
                action = self._planner._legal_action_to_action(selected)
                if action:
                    action.reasoning = "deterministic fallback"
                    plan.actions = [action]
                    plan.overall_strategy = f"Fallback: {selected}"
                    logger.info(f"Deterministic fallback: {selected}")
                    return plan

        plan.actions = [
            GameAction(
                action_type=ActionType.PASS_PRIORITY,
                reasoning="deterministic fallback: last resort pass",
            )
        ]
        plan.overall_strategy = "Fallback: pass priority (last resort)"
        logger.info("Deterministic fallback: pass priority")
        return plan

    def _handle_afk(self, game_state: dict[str, Any], trigger: str) -> bool:
        """Handle a trigger in AFK mode — auto-pass without LLM."""
        pending = game_state.get("pending_decision")
        decision_context = game_state.get("decision_context") or {}
        dec_type = decision_context.get("type", "")

        if pending:
            pending_lower = pending.lower() if isinstance(pending, str) else ""

            if "mulligan" in pending_lower:
                logger.info("AFK: keeping hand (mulligan)")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.MULLIGAN_KEEP,
                        reasoning="AFK safe default: keep hand",
                    ),
                    game_state,
                )

            if "scry" in pending_lower:
                logger.info("AFK: scry to bottom")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.SELECT_N,
                        scry_position="bottom",
                        reasoning="AFK safe default: put scry cards on bottom",
                    ),
                    game_state,
                )

            if dec_type == "declare_attackers":
                logger.info("AFK: skipping attackers")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.DECLARE_ATTACKERS,
                        attacker_names=[],
                        reasoning="AFK safe default: no attacks",
                    ),
                    game_state,
                )

            if dec_type == "declare_blockers":
                logger.info("AFK: skipping blockers")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.DECLARE_BLOCKERS,
                        blocker_assignments={},
                        reasoning="AFK safe default: no blocks",
                    ),
                    game_state,
                )

            if dec_type == "choose_starting_player":
                logger.info("AFK: choosing to play")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CHOOSE_STARTING_PLAYER,
                        play_or_draw="play",
                        reasoning="AFK safe default: choose play",
                    ),
                    game_state,
                )

            if dec_type in (
                "assign_damage",
                "order_combat_damage",
                "pay_costs",
                "search",
                "distribution",
                "numeric_input",
                "select_replacement",
                "casting_time_options",
                "select_counters",
                "order_triggers",
                "select_n_group",
                "select_from_groups",
                "search_from_groups",
                "gather",
            ):
                logger.info(f"AFK: auto-accepting decision '{dec_type}'")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"AFK default confirmation for {dec_type}",
                    ),
                    game_state,
                )

            if pending_lower and "mulligan" not in pending_lower and "scry" not in pending_lower:
                logger.warning(f"AFK: unknown decision '{pending}' - trying bridge confirmation")
                return self._run_bridge_action(
                    GameAction(
                        action_type=ActionType.CLICK_BUTTON,
                        card_name="done",
                        reasoning=f"AFK unknown decision fallback for {pending}",
                    ),
                    game_state,
                )

        logger.info(f"AFK: passing ({trigger})")
        return self._run_bridge_action(
            GameAction(
                action_type=ActionType.PASS_PRIORITY,
                reasoning=f"AFK auto-pass for {trigger}",
            ),
            game_state,
        )

    def _handle_land_drop(self, game_state: dict[str, Any], trigger: str) -> bool:
        """Handle a trigger in land-drop-only mode."""
        turn = game_state.get("turn", {})
        phase = turn.get("phase", "")
        local_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("system_seat_id")
                break
        active_seat = turn.get("active_player_seat")
        is_active = local_seat is not None and active_seat is not None and local_seat == active_seat

        turn_num = turn.get("turn_number", 0)
        already_played = self._land_drop_last_turn == turn_num and turn_num > 0
        is_main = "Main" in phase

        if is_active and is_main and not already_played:
            hand = game_state.get("hand", [])
            land = None
            for card in hand:
                types = card.get("card_types", [])
                type_line = card.get("type_line", "")
                if "Land" in types or "Land" in type_line:
                    land = card
                    break

            if land:
                name = land.get("name", "Land")
                grp_id = land.get("grp_id", 0)
                logger.info(f"Land-drop mode: playing {name} (grpId={grp_id})")

                result = self._gre_bridge.submit_action(
                    action_type="PlayLand",
                    grp_id=grp_id,
                )
                if result.get("ok"):
                    self._land_drop_last_turn = turn_num
                    self._notify("AUTOPILOT", f"Played {name}")
                    return True
                else:
                    logger.warning(f"Land-drop mode: bridge failed to play {name}: {result.get('error')}")

        logger.info(f"Land-drop mode: passing priority ({trigger})")
        return self._run_bridge_action(
            GameAction(
                action_type=ActionType.PASS_PRIORITY,
                reasoning=f"Land-drop auto-pass for {trigger}",
            ),
            game_state,
        )
