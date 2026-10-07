"""Autopilot modes (AFK, Land-Drop-Only) mixin.

Extracted from autopilot.py: methods are unchanged and mixed back into AutopilotEngine.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from arenamcp.action_planner import ActionType, GameAction

logger = logging.getLogger(__name__)


class _AutopilotModesMixin:
    """AFK mode, land-drop mode, and optional cost handling."""

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
        if self._ward_payment_source(game_state) is not None:
            # Paying an opposing ward keeps the spell we aimed at it; its
            # card text (even a harmful ETB) says nothing about this cost.
            logger.info("Optional cost is a ward payment; paying it")
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

    @staticmethod
    def _ward_payment_source(game_state: dict[str, Any]) -> dict[str, Any] | None:
        """The opposing warded permanent whose ward trigger is asking us to pay, if any."""
        from arenamcp.ward import ward_trigger_source

        context = game_state.get("decision_context") or {}
        payload = game_state.get("_bridge_request_payload") or {}
        try:
            source_id = int(
                context.get("source_id") or context.get("sourceId") or payload.get("sourceId") or 0
            )
        except (TypeError, ValueError):
            source_id = 0
        return ward_trigger_source(game_state, source_id)

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
