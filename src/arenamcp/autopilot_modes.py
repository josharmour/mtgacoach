"""Autopilot modes (AFK, Land-Drop-Only) mixin.

Extracted from autopilot.py: methods are unchanged and mixed back into AutopilotEngine.
"""

from __future__ import annotations

import logging
import re
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
        if not game_state.get("_bridge_can_cancel"):
            return None
        payment = self._ward_payment(game_state)
        if payment is not None:
            # A ward tax, not an optional cost: unpaid, the spell or ability
            # we aimed at that permanent is countered (match a9cd5a2e T10: our
            # Burst Lightning's {2} read as an optional cost of Burst itself,
            # declined, countered). Its card text says nothing about this cost.
            # Several wards are paid one trigger at a time: when the rest can't
            # be paid too, the spell is countered anyway, so keep the mana (T8:
            # {2} paid into a second {2}, Impractical Joke countered regardless).
            from arenamcp.ward import ward_payable, ward_payment_mana

            mana = ward_payment_mana(game_state)
            if ward_payable(payment.ward, game_state, mana) is False:
                return f"unpayable ward tax: {payment.describe()}, and only {mana} mana is available"
            logger.info("Optional cost is a ward tax (%s); paying it", payment.describe())
            return None
        if self._cost_on_our_spell(game_state):
            return None
        if self._paying_for_own_play(game_state):
            return None
        name, oracle = self._resolve_decision_source(game_state)
        harmful = self._source_spell_is_harmful_to_target(game_state, None, None)
        # Ledger #306: a beneficial trigger can still spend mana reserved for
        # protection. Free optional effects do not need this payment review.
        optional_mana = bool(re.search(r"\bmay pay\s+\{(?:o)?[0-9WUBRGCX]", oracle, re.IGNORECASE))
        if not harmful and not optional_mana:
            return None
        cost_kind = "harmful optional cost" if harmful else "optional mana cost"
        verdict: bool | None = None
        try:
            verdict = self._planner.plan_pay_or_decline(name, oracle, game_state)
        except Exception as e:
            logger.debug(f"pay/decline LLM check failed: {e}")
        if verdict is True:
            return None
        if verdict is False:
            return f"{cost_kind} ({name or 'unknown'}): LLM chose decline"
        return f"{cost_kind} ({name or 'unknown'}): LLM unavailable, declining conservatively"

    def _paying_for_own_play(self, game_state: dict[str, Any]) -> bool:
        """The cost belongs to the cast/activation the autopilot submitted moments ago.

        Typed-path plays (every priority window since Phase E) count too, when
        the request names that play (or nothing): an opponent's trigger asking
        right after our cast is not our cost.
        """
        context = game_state.get("decision_context") or {}
        source = context.get("source_card") or ""
        if isinstance(source, dict):
            source = source.get("name") or ""
        source_id = context.get("source_id") or context.get("sourceId")
        if not source and source_id:
            card = next(
                (c for c in game_state.get("stack") or [] if str(c.get("instance_id")) == str(source_id)),
                {},
            )
            source = (card.get("source_card") or {}).get("name") or card.get("name") or ""
            if not source:
                # An explicitly identified but unresolved source is not proof
                # that this cost belongs to the previous play.
                return False
        source = str(source).strip().lower()
        if self._last_cast_submitted and (
            time.monotonic() - self._last_cast_submitted_ts <= self._OPTIONAL_COST_OWN_ACTION_WINDOW_S
        ):
            turn, name = self._last_cast_submitted
            current_turn = (game_state.get("turn") or {}).get("turn_number")
            if (current_turn is None or turn == current_turn) and (
                source in ("", "unknown") or str(name).lower() in source
            ):
                return True
        recent = getattr(self, "_recent_own_play_name", None)
        name = str(recent(game_state) if callable(recent) else "").strip().lower()
        return bool(name) and (source in ("", "unknown") or name in source)

    @staticmethod
    def _cost_on_our_spell(game_state: dict[str, Any]) -> bool:
        """The cost is charged to OUR spell on the stack: unpaid, that spell is countered.

        2026-10-19 12:21:04 and 12:23:49 (match 5d5d32f8, ledger #93): their
        Spell Pierce ("counter target noncreature spell unless its controller
        pays {2}") asked {2} for our Burst Lightning; the PayCostsReq names our
        Burst as the cost's object. It went to the harmful-optional-cost gate,
        whose model call read it as the kicker and declined; the lethal Burst
        (opponent at 2) was countered. The own-play window only covers 10 s after
        our cast. A cost on our own spell card (its cast cost, a ward tax, a
        Spell Pierce / Mana Leak tax) is always worth paying when Arena asks:
        an unaffordable one has no Auto Pay and is cancelled by the payment step.
        """
        context = game_state.get("decision_context") or {}
        payload = game_state.get("_bridge_request_payload") or {}
        try:
            source_id = int(
                context.get("source_id") or context.get("sourceId") or payload.get("sourceId") or 0
            )
        except (TypeError, ValueError):
            return False
        if not source_id:
            return False
        local = game_state.get("local_seat_id") or next(
            (
                p.get("seat_id")
                for p in game_state.get("players") or []
                if isinstance(p, dict) and p.get("is_local")
            ),
            None,
        )
        for item in game_state.get("stack") or []:
            if not isinstance(item, dict) or int(item.get("instance_id") or 0) != source_id:
                continue
            if str(item.get("object_kind") or "").upper() == "ABILITY":
                return False
            controller = item.get("controller_seat_id") or item.get("owner_seat_id")
            if local is None or controller != local:
                return False
            logger.info(
                "PayCosts is charged to our own %s on the stack (%s); paying it",
                item.get("name") or "spell",
                context.get("mana_cost") or context.get("mana") or "cost",
            )
            return True
        return False

    @staticmethod
    def _ward_payment(game_state: dict[str, Any]) -> Any:
        """The opposing ward triggers asking us to pay now (``ward.WardPayment``), if any."""
        from arenamcp.ward import pending_ward_payment

        context = game_state.get("decision_context") or {}
        payload = game_state.get("_bridge_request_payload") or {}
        try:
            source_id = int(
                context.get("source_id") or context.get("sourceId") or payload.get("sourceId") or 0
            )
        except (TypeError, ValueError):
            source_id = 0
        return pending_ward_payment(game_state, source_id)

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
