"""Action-execution routing for AutopilotEngine using direct GRE Named Pipe submissions."""

from __future__ import annotations

import logging
from typing import Any

from arenamcp.action_planner import ActionType, GameAction
from arenamcp.autopilot_models import ClickResult, ExecutionPath
from arenamcp.gre_bridge import _ACTIONS_AVAILABLE_BRIDGE_REQUESTS

logger = logging.getLogger(__name__)


class _ActionExecMixin:
    """Action execution mixin routing commands directly through the GRE bridge."""

    def _execute_action(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult:
        """Route an action to the GRE bridge submission handler."""
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
                    is_displaced_main_action = (
                        action.action_type in (ActionType.PLAY_LAND, ActionType.CAST_SPELL)
                        and bridge_has_other_request
                    )
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
                                    "step — passing priority to advance toward combat",
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
                        if self._gre_bridge.submit_targets([only_id]):
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

    def _build_blocker_id_map(self, game_state: dict[str, Any]) -> dict[str, int]:
        """Build a name -> instance_id map from the blocker decision context."""
        decision = game_state.get("decision_context") or {}
        if decision.get("type") != "declare_blockers":
            return {}

        names = decision.get("legal_blockers", [])
        ids = decision.get("legal_blocker_ids", [])
        if len(names) != len(ids) or not ids:
            return {}

        return dict(zip(names, ids, strict=False))

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

    @staticmethod
    def _parse_pay_cost_requirements(decision_context: dict[str, Any]) -> dict[str, int]:
        """Return normalized mana requirements for a Pay Costs decision."""
        import re

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
        import re

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
        import re

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

        selected_candidates: list[dict[str, Any]] = []

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
            selected_candidates.append(chosen)
            return chosen

        for color in ("W", "U", "B", "R", "G", "C"):
            for _ in range(requirements.get(color, 0)):
                if pick_candidate(color) is None:
                    return [candidate["card"] for candidate in selected_candidates]

        for _ in range(requirements.get("generic", 0) + requirements.get("Any", 0)):
            if pick_candidate() is None:
                break

        return [candidate["card"] for candidate in selected_candidates]
