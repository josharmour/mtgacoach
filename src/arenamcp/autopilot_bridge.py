"""GRE-bridge submission helpers for AutopilotEngine, extracted from autopilot.py.

Pure move: methods are unchanged and mixed back into AutopilotEngine."""

import logging
import re
import time
from typing import Any

from arenamcp.action_planner import ActionPlan, ActionType, GameAction
from arenamcp.autopilot_models import ExecutionPath
from arenamcp.autopilot_targets import (
    _match_target_in_battlefield,
    _normalize_planner_card_name,
)
from arenamcp.input_controller import ClickResult
from arenamcp.mana import mana_cost_to_cmc

logger = logging.getLogger(__name__)


class _BridgeSubmitMixin:
    def _try_gre_bridge(
        self,
        action: GameAction,
        game_state: dict[str, Any],
    ) -> ClickResult | None:
        """Try to execute an action via the GRE bridge (direct submission).

        Returns a ClickResult if the bridge handled it, or None to fall
        through to mouse-click execution.
        """
        if game_state.get("game_engine_busy"):
            return None

        if not self._gre_bridge.connect():
            return None

        method = action.action_type.value
        if method in self._gre_bridge_failed_methods:
            return None

        # DECLARE ATTACKERS and DECLARE BLOCKERS bypass the bridge-idle check
        # because their dedicated methods query get_pending_actions() directly
        # and will fail gracefully if no request is actually pending.
        # The bridge-idle check uses game_state metadata that may not be
        # populated at the moment this method is called, causing false skips.
        if action.action_type == ActionType.DECLARE_ATTACKERS:
            return self._try_bridge_declare_attackers(action)

        if action.action_type == ActionType.DECLARE_BLOCKERS:
            return self._try_gre_bridge_blockers(action)

        if game_state.get("_bridge_connected") and not (
            game_state.get("_bridge_request_type")
            or game_state.get("_bridge_request_class")
            or game_state.get("_bridge_has_pending")
        ):
            # Snapshot may be stale — re-poll the bridge before bailing.
            # During chained interaction windows (e.g. Eerie/ETB triggers
            # firing right after a target submission) the snapshot can briefly
            # show no pending while the live bridge already has the next
            # request queued.
            try:
                live = self._gre_bridge.get_pending_actions() or {}
            except Exception:
                live = {}
            if not live.get("has_pending"):
                logger.info(
                    "GRE bridge execution skipped: bridge is connected but reports no pending window (live re-poll confirmed)"
                )
                return None
            logger.info(
                "GRE bridge stale-snapshot recovery: live poll shows pending "
                f"{live.get('request_class') or live.get('request_type')!r}; proceeding"
            )

        gre_ref = getattr(action, "gre_action_ref", None)

        # CLICK_BUTTON on an OptionalActionMessageRequest must go through
        # submit_optional, NOT submit_pass — the latter is rejected by MTGA
        # ("Cannot pass on current interaction"). Issue #161 was filed because
        # this branch lumped CLICK_BUTTON with pass/resolve and surfaced
        # bridge_submit_failed when the LLM tried to accept an ETB trigger.
        if action.action_type in (
            ActionType.CLICK_BUTTON,
            ActionType.ACTIVATE_ABILITY,
            ActionType.CAST_SPELL,
        ):
            bridge_request_class = (
                game_state.get("_bridge_request_class") or game_state.get("_bridge_request_type") or ""
            )
            decision_type = (game_state.get("decision_context") or {}).get("type") or ""
            is_optional_window = "Optional" in str(bridge_request_class) or decision_type == "optional_action"
            # activate_ability / cast_spell against an OptionalActionMessage
            # window ("Use Alseid's ability?") is the planner answering that
            # exact yes/no — accept it. Without this mapping the action falls
            # through to the action matcher, which finds no Cast/Activate
            # entries on an Optional request and pauses as bridge_submit_failed
            # (#252). Only CLICK_BUTTON may decline; wanting to activate/cast
            # IS the accept.
            if is_optional_window and action.action_type in (
                ActionType.ACTIVATE_ABILITY,
                ActionType.CAST_SPELL,
            ):
                if self._gre_bridge.submit_optional(True):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"{action.action_type.value} ({action.card_name or '?'}): "
                        "optional window — submit_optional(accept=True)",
                    )
                    return ClickResult(True, 0, 0, action.card_name or "accept", "GRE bridge")
                logger.info(
                    "GRE bridge submit_optional failed for "
                    f"{action.action_type.value}; surfacing manual-required to caller"
                )
                self._gre_bridge_failed_methods.add(method)
                return None
            if action.action_type == ActionType.CLICK_BUTTON and is_optional_window:
                button_name = (action.card_name or "").lower().strip()
                # The LLM occasionally leaves card_name empty when the prompt
                # is a yes/no benefit (e.g. "Search your library?"). Default
                # to accept when the name doesn't explicitly decline — the
                # earlier auto-decline path already handled the "no
                # meaningful actions" case, so reaching here means the LLM
                # actively chose a side.
                if button_name in ("decline", "cancel", "no", "skip"):
                    accept = False
                else:
                    accept = True
                if self._gre_bridge.submit_optional(accept):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"click_button: submit_optional(accept={accept}) via GRE bridge",
                    )
                    return ClickResult(
                        True, 0, 0, button_name or ("accept" if accept else "decline"), "GRE bridge"
                    )
                logger.info("GRE bridge submit_optional failed; surfacing manual-required to caller")
                self._gre_bridge_failed_methods.add(method)
                return None

        # NUMERIC_INPUT with a chosen value (X spells) — P3-1.
        #
        # submit_x is the right command, not submit_numeric (issue #390): an
        # X cost arrives as a CastingTimeOption_NumericInputRequest *child* of
        # a CastingTimeOptionRequest, and the plugin's HandleSubmitNumeric only
        # matches a standalone NumericInputRequest — so the parent-type
        # mismatch failed and every X cast fell through to MANUAL REQUIRED.
        # HandleSubmitX walks ChildRequests for the numeric child and also
        # accepts a standalone NumericInputRequest, making it a superset;
        # submit_numeric stays as a fallback for plugin builds predating the
        # submit_x command.
        if action.action_type == ActionType.NUMERIC_INPUT and action.numeric_value:
            value = int(action.numeric_value)
            if self._gre_bridge.submit_x(value) or self._gre_bridge.submit_numeric(value):
                self._log_execution_path(ExecutionPath.GRE_AWARE, f"numeric_input: X={value} via GRE bridge")
                return ClickResult(True, 0, 0, f"X={value}", "GRE bridge")
            logger.info("GRE bridge submit_x/submit_numeric failed; surfacing manual-required")
            self._gre_bridge_failed_methods.add(method)
            return None

        # PASS / RESOLVE / generic CLICK_BUTTON — use bridge submit_pass
        if action.action_type in (ActionType.PASS_PRIORITY, ActionType.RESOLVE, ActionType.CLICK_BUTTON):
            # "Done (confirm attackers/blockers)" is not a pass — submit_pass
            # is always illegal on combat declaration requests ("Cannot pass
            # on current interaction", 7x MANUAL REQUIRED spam 2026-07-05).
            # Route it to an empty combat declaration instead.
            request_type = (
                game_state.get("_bridge_request_type") or game_state.get("_bridge_request_class") or ""
            )
            if action.action_type == ActionType.CLICK_BUTTON and request_type:
                if "DeclareAttacker" in request_type:
                    # "Done (confirm attackers)" — MTGA may have auto-selected
                    # attackers. Submitting empty clears those selections and
                    # can silently fizzle the attack step. Check the combat
                    # solver first for beneficial attackers, matching the
                    # preflight pattern in autopilot.py:2295-2331.
                    # Cluster: issues #398-#402 (5x bridge_submit_failed).
                    if game_state.get("_bridge_connected"):
                        solver_names = self._solver_attack_names(game_state)
                        if solver_names:
                            logger.info(
                                "click_button(done) on DeclareAttacker with "
                                f"solver-picked attackers: {solver_names}; "
                                "routing through declare_attackers instead of empty submit"
                            )
                            dec_action = GameAction(
                                action_type=ActionType.DECLARE_ATTACKERS,
                                attacker_names=solver_names,
                                card_name=solver_names[0] if solver_names else "done",
                                reasoning="click_button(done) → solver-picked attackers",
                            )
                            return self._try_bridge_declare_attackers(dec_action)
                    if self._gre_bridge.submit_attackers([]):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            "click_button(done): empty attacker declaration via GRE bridge",
                        )
                        return ClickResult(True, 0, 0, "no attacks", "GRE bridge")
                    self._gre_bridge_failed_methods.add(method)
                    return None
                if "DeclareBlocker" in request_type:
                    if self._gre_bridge.submit_blockers([]):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            "click_button(done): empty blocker declaration via GRE bridge",
                        )
                        return ClickResult(True, 0, 0, "no blocks", "GRE bridge")
                    self._gre_bridge_failed_methods.add(method)
                    return None
            if self._gre_bridge.submit_pass():
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE, f"{action.action_type.value}: submitted via GRE bridge (pass)"
                )
                return ClickResult(True, 0, 0, "pass", "GRE bridge")
            logger.info("GRE bridge pass failed; surfacing manual-required to caller")
            self._gre_bridge_failed_methods.add(method)
            return None

        # MULLIGAN — submit keep/mulligan via bridge
        if action.action_type in (ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL):
            keep = action.action_type == ActionType.MULLIGAN_KEEP
            if self._gre_bridge.submit_mulligan(keep):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE, f"mulligan: {'keep' if keep else 'mulligan'} via GRE bridge"
                )
                return ClickResult(True, 0, 0, "mulligan", "GRE bridge")
            logger.info("GRE bridge mulligan failed, surfacing manual-required to caller")
            self._gre_bridge_failed_methods.add(method)
            return None

        # CHOOSE STARTING PLAYER — submit play/draw via bridge
        if action.action_type == ActionType.CHOOSE_STARTING_PLAYER:
            local_seat = None
            opp_seat = None
            for p in game_state.get("players", []):
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
                else:
                    opp_seat = p.get("seat_id")
            # play_or_draw field from LLM: "play" means we go first (our seat)
            seat = local_seat if getattr(action, "play_or_draw", "play") == "play" else opp_seat
            if seat and self._gre_bridge.submit_choose_starting_player(seat):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE, f"choose_starting_player: seat {seat} via GRE bridge"
                )
                return ClickResult(True, 0, 0, "choose_starting_player", "GRE bridge")
            logger.info("GRE bridge choose_starting_player failed; surfacing manual-required to caller")
            self._gre_bridge_failed_methods.add(method)
            return None

        # SELECT TARGET — submit via bridge if target instance IDs are resolvable
        if action.action_type == ActionType.SELECT_TARGET:
            return self._try_gre_bridge_select_target(action)

        # ASSIGN_DAMAGE — submit combat damage assignments via bridge.
        if action.action_type == ActionType.ASSIGN_DAMAGE:
            result = self._try_gre_bridge_assign_damage(action, game_state)
            if result is not None:
                return result

        # DISTRIBUTE — submit a distribution decision via bridge.
        if action.action_type == ActionType.DISTRIBUTE:
            result = self._try_gre_bridge_distribute(action, game_state)
            if result is not None:
                return result

        # ORDER_TRIGGERS / ORDER_BLOCKERS — submit ordering via bridge.
        # Routed by inspecting the bridge request class: OrderRequest →
        # submit_order; SelectFromGroupsRequest / GroupRequest → submit_group.
        if action.action_type in (
            ActionType.ORDER_TRIGGERS,
            ActionType.ORDER_BLOCKERS,
            ActionType.ORDER_COMBAT_DAMAGE,
        ):
            result = self._try_gre_bridge_order(action, game_state)
            if result is not None:
                return result

        # SELECT_REPLACEMENT — choose a replacement effect (or decline if optional).
        if action.action_type == ActionType.SELECT_REPLACEMENT:
            result = self._try_gre_bridge_select_replacement(action, game_state)
            if result is not None:
                return result

        # SELECT_N / SEARCH_LIBRARY / SELECT_COUNTERS — submit via bridge.
        # The legacy mouse path (kept only when bridge-only mode is off) clicks
        # by list index and
        # frequently misses the actual option positions (causing stuck loops
        # on things like Lluwen's ETB search).
        if action.action_type in (
            ActionType.SELECT_N,
            ActionType.SEARCH_LIBRARY,
            ActionType.SELECT_COUNTERS,
        ):
            # Scry / surveil / similar library-top ordering is a GroupRequest,
            # not a SelectN. Route it through submit_group so the client sends
            # the proper GroupResp with top/bottom zones populated.
            bridge_req_type = (
                game_state.get("_bridge_request_type") or game_state.get("_bridge_request_class") or ""
            )
            if "Group" in str(bridge_req_type):
                result = self._try_gre_bridge_scry(action, game_state)
                if result is not None:
                    return result

            result = self._try_gre_bridge_select_n(action, game_state)
            if result is not None:
                return result

        # MODAL CHOICE / CASTING OPTIONS — submit via bridge by matching
        # CastingTimeOption entries (actionType="CastingTimeOption").
        # The generic type-match path can't handle these because the bridge
        # uses "CastingTimeOption" not "ActionType_Cast" etc.
        if action.action_type in (ActionType.MODAL_CHOICE, ActionType.CASTING_OPTIONS):
            result = self._try_bridge_casting_time_option(action)
            if result:
                return result

        # For actions with a GRE ref, match by identity fields
        if gre_ref is not None:
            action_type = gre_ref.action_type if hasattr(gre_ref, "action_type") else ""
            grp_id = gre_ref.grp_id if hasattr(gre_ref, "grp_id") else 0
            instance_id = gre_ref.instance_id if hasattr(gre_ref, "instance_id") else 0
            ability_grp_id = gre_ref.ability_grp_id if hasattr(gre_ref, "ability_grp_id") else 0

            if self._gre_bridge.submit_action_by_match(
                action_type=action_type,
                grp_id=grp_id,
                instance_id=instance_id,
                ability_grp_id=ability_grp_id,
                auto_pass=self._config.auto_pass_priority,
            ):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"{action.action_type.value}: '{action.card_name}' submitted via GRE bridge",
                )
                return ClickResult(True, 0, 0, action.card_name or str(action), "GRE bridge")

            logger.info(
                f"GRE bridge match failed for {action.action_type.value}; surfacing manual-required to caller"
            )
            self._gre_bridge_failed_methods.add(method)
            return None

        # No GRE ref but bridge is connected — try matching by game action type
        from arenamcp.gre_action_matcher import ACTION_TYPE_MAP

        gre_type = ACTION_TYPE_MAP.get(action.action_type)
        if gre_type:
            # Use preloaded bridge actions if available (from bridge trigger detection),
            # otherwise query the bridge fresh
            bridge_actions = None
            if self._bridge_preloaded_actions:
                bridge_actions = self._bridge_preloaded_actions
            else:
                pending = self._gre_bridge.get_pending_actions()
                if pending and pending.get("has_pending") and pending.get("actions"):
                    bridge_actions = pending["actions"]
            if bridge_actions:
                # Find matching action by GRE type AND card identity.
                # Without card name verification, the first type match wins —
                # which submits the wrong card when multiple casts are legal
                # (e.g. submitting Michelangelo instead of Emerald Medallion).
                best_idx = None
                # Strip any leading "Ability: " / "Cast: " etc. label the LLM
                # may have left on action.card_name (the legal_actions strings
                # use those labels and models occasionally copy them through).
                wanted_name = _normalize_planner_card_name(action.card_name or "")
                for idx, ba in enumerate(bridge_actions):
                    ba_type = ba.get("actionType", "")
                    # Normalize comparison
                    if not (
                        ba_type == gre_type
                        or f"ActionType_{ba_type}" == gre_type
                        or ba_type == gre_type.replace("ActionType_", "")
                    ):
                        continue
                    # Verify card identity via grpId → card name lookup
                    if wanted_name:
                        ba_grp_id = ba.get("grpId", 0)
                        if ba_grp_id:
                            try:
                                from arenamcp import server

                                card_info = server.get_card_info(ba_grp_id)
                                ba_name = card_info.get("name", "")
                            except Exception:
                                ba_name = ""
                            if ba_name:
                                w = wanted_name.lower()
                                c = ba_name.lower()
                                # Allow substring-in-either-direction so split
                                # cards / faces (e.g. "Lightning Bolt //
                                # Shock") and shorthand still match.
                                if not (w == c or w in c or c in w):
                                    continue  # Wrong card — skip
                    best_idx = idx
                    break

                # Sole-candidate fallback: the planner's card_name didn't
                # exact-match any bridge action (truncation, split-card face,
                # grpId→name lookup miss), but if exactly ONE bridge action has
                # the wanted GRE type it is unambiguous — submit it rather than
                # dropping the play, which would let _pause_for_manual auto-pass
                # away a castable creature/land (the Spellbook Vendor /
                # Veteran Survivor skip bug). Mirrors the PLAY/CAST sole-candidate
                # branches in gre_action_matcher.match_action_to_gre.
                if best_idx is None:

                    def _type_eq(t: str) -> bool:
                        return (
                            t == gre_type
                            or f"ActionType_{t}" == gre_type
                            or t == gre_type.replace("ActionType_", "")
                        )

                    type_matches = [
                        idx for idx, ba in enumerate(bridge_actions) if _type_eq(ba.get("actionType", ""))
                    ]
                    if len(type_matches) == 1:
                        best_idx = type_matches[0]
                        logger.info(
                            f"GRE bridge sole-candidate: '{action.card_name}' "
                            f"didn't name-match, submitting the only {gre_type} "
                            f"action (idx={best_idx})"
                        )

                if best_idx is not None:
                    if self._gre_bridge.submit_action_by_index(
                        best_idx, auto_pass=self._config.auto_pass_priority
                    ):
                        self._log_execution_path(
                            ExecutionPath.GRE_AWARE,
                            f"{action.action_type.value}: '{action.card_name}' submitted via GRE bridge (type+name match)",
                        )
                        return ClickResult(True, 0, 0, action.card_name or str(action), "GRE bridge")
                    else:
                        logger.warning(
                            f"GRE bridge type+name match found idx={best_idx} for "
                            f"'{action.card_name}' but submit_action_by_index failed"
                        )
                elif action.card_name:
                    logger.warning(
                        f"GRE bridge type match: no action matched card name "
                        f"'{action.card_name}' among {len(bridge_actions)} bridge actions"
                    )

        return None

    def _try_bridge_casting_time_option(self, action: GameAction) -> ClickResult | None:
        """Submit a casting-time option (modal choice, done, kicker, etc.) via GRE bridge.

        Bridge actions for CastingTimeOptionRequest have actionType="CastingTimeOption"
        with a choiceKind field ("modal", "done", "choose_or_cost", etc.) and an
        optionIndex for modals. We match the LLM's modal_index to the bridge's
        optionIndex to pick the right entry.
        """
        bridge_actions = None
        if self._bridge_preloaded_actions:
            bridge_actions = self._bridge_preloaded_actions
        else:
            pending = self._gre_bridge.get_pending_actions()
            if pending and pending.get("has_pending") and pending.get("actions"):
                bridge_actions = pending["actions"]

        if not bridge_actions:
            return None

        # Filter to CastingTimeOption entries
        casting_entries = [
            (idx, ba) for idx, ba in enumerate(bridge_actions) if ba.get("actionType") == "CastingTimeOption"
        ]

        if not casting_entries:
            return None

        # For modal_choice: match by optionIndex (from LLM's modal_index field)
        modal_index = getattr(action, "modal_index", 0) or 0

        # For casting_options (the "done/confirm" step), just pick the first
        # non-modal entry (usually "done")
        if action.action_type == ActionType.CASTING_OPTIONS:
            # Prefer "done" entries, then fall back to first entry
            for idx, ba in casting_entries:
                if ba.get("choiceKind") == "done" and self._gre_bridge.submit_action_by_index(
                    idx, auto_pass=self._config.auto_pass_priority
                ):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"casting_options: '{action.card_name}' done via GRE bridge",
                    )
                    return ClickResult(True, 0, 0, action.card_name or "casting_option", "GRE bridge")
            # No "done" entry — fall through to mouse
            return None

        # modal_choice: find the entry with matching optionIndex
        for idx, ba in casting_entries:
            if ba.get("choiceKind") == "modal" and ba.get("optionIndex", -1) == modal_index:
                if self._gre_bridge.submit_action_by_index(idx, auto_pass=self._config.auto_pass_priority):
                    self._log_execution_path(
                        ExecutionPath.GRE_AWARE,
                        f"modal_choice: '{action.card_name}' option {modal_index} via GRE bridge",
                    )
                    return ClickResult(True, 0, 0, action.card_name or "modal", "GRE bridge")
                else:
                    logger.warning(
                        f"GRE bridge modal submit failed for '{action.card_name}' option {modal_index}"
                    )
                    return None

        # optionIndex not found — log and fall through to mouse
        available = [(ba.get("choiceKind"), ba.get("optionIndex")) for _, ba in casting_entries]
        logger.warning(
            f"GRE bridge modal: no entry with optionIndex={modal_index} "
            f"among {len(casting_entries)} entries: {available}"
        )
        return None

    def _solver_attack_names(self, game_state: dict[str, Any]) -> list[str]:
        """Deterministic attack pick for auto-confirmed DeclareAttackers.

        Used when a DeclareAttackers window is about to be auto-confirmed
        with no planner attack intent. Returns attacker names only when the
        combat solver's best plan is strictly better than not attacking
        (the solver scores the empty plan too); otherwise [] keeps the
        empty-confirm behavior.
        """
        try:
            from arenamcp.combat_solver import optimal_attacks
        except Exception:
            return []

        ctx = game_state.get("decision_context") or {}
        legal_names = {str(n) for n in (ctx.get("legal_attackers") or []) if n}
        if not legal_names:
            return []

        local_seat = next(
            (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
            None,
        )
        if local_seat is None:
            return []

        def _is_creature(c: dict) -> bool:
            tl = (c.get("type_line") or "").lower()
            return "creature" in tl or "CardType_Creature" in (c.get("card_types") or [])

        yours: list[dict] = []
        theirs: list[dict] = []
        for c in game_state.get("battlefield", []) or []:
            if not _is_creature(c):
                continue
            if c.get("controller_seat_id") == local_seat:
                yours.append(c)
            elif c.get("controller_seat_id") is not None:
                theirs.append(c)

        candidates = [c for c in yours if (c.get("name") or "") in legal_names]
        if not candidates:
            return []

        your_life, opp_life = 20, 20
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                your_life = p.get("life_total", 20)
            else:
                opp_life = p.get("life_total", 20)

        opp_blockers = [c for c in theirs if not c.get("is_tapped")]
        remaining_blockers = [c for c in yours if c not in candidates and not c.get("is_tapped")]
        try:
            plan = optimal_attacks(
                candidates,
                opp_blockers,
                opp_life,
                your_life,
                theirs,
                remaining_blockers,
            )
        except Exception as e:
            logger.debug(f"combat solver attack fallback failed: {e}")
            return []
        if plan is None or not plan.attacker_names:
            return []
        # Conservative gate: only override the empty confirm when the swing
        # actually accomplishes something (damage through or a favorable
        # material trade). A zero-value attack isn't worth the crackback
        # risk the solver might have underestimated.
        if plan.damage_through <= 0 and plan.blockers_killed_material <= plan.attackers_lost_material:
            return []
        logger.info(f"Combat solver attack fallback: {plan.explanation} (score={plan.score:.1f})")
        return [n for n in plan.attacker_names if n in legal_names]

    def _try_bridge_declare_attackers(self, action: GameAction) -> ClickResult | None:
        """Submit attacker declarations via GRE bridge (two-step NPE handler pattern).

        Step 1: UpdateAttacker — sets SelectedDamageRecipient on each attacker
        Step 2: SubmitAttackers — finalizes the declaration

        Returns ClickResult if bridge handled it, None to fall through to clicks.
        """
        if not self._gre_bridge.connect():
            return None

        # Verify the bridge has a DeclareAttackers request pending
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class", "")
        if "DeclareAttacker" not in req_class:
            logger.info(f"Bridge declare_attackers: pending is {req_class}, not DeclareAttacker")
            return None

        # Build name→instanceId map from decision context
        game_state = self._get_game_state()
        attacker_id_map = self._build_attacker_id_map(game_state)
        battlefield = game_state.get("battlefield", [])
        local_seat = next(
            (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
            None,
        )

        # Resolve attacker names to instance IDs
        attacker_entries = []
        for name in action.attacker_names:
            iid = attacker_id_map.get(name)
            if iid is None:
                iid = self._find_instance_id(name, battlefield, local_seat)
            if iid is not None:
                attacker_entries.append({"attackerInstanceId": iid})
            else:
                logger.warning(f"Bridge declare_attackers: can't resolve '{name}' to instance ID")

        if not attacker_entries:
            # Empty attacker list = "attack with nobody / Done (confirm attackers)".
            # The plugin treats an empty list as a direct SubmitAttackers() finalize.
            # This is correct when the user has no legal attackers (summoning-sick
            # or no creatures) or when auto-confirm fires after the LLM didn't
            # pick any attackers (action.attacker_names was [] from auto-confirm).
            if action.attacker_names:
                logger.warning(
                    "Bridge declare_attackers: requested attackers "
                    f"{action.attacker_names} could not be resolved, surfacing "
                    "manual-required to caller"
                )
                return None
            logger.info("Bridge declare_attackers: confirming with no attackers (Done)")
            resp = self._gre_bridge.submit_attackers_raw([])
            if not resp or not resp.get("ok"):
                logger.warning(f"Bridge declare_attackers (no-attackers confirm) failed: {resp}")
                return None
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                "declare_attackers: confirmed no attackers via GRE bridge",
            )
            return ClickResult(True, 0, 0, "attackers", "GRE bridge")

        # Step 1: UpdateAttacker (declare attackers with damage recipients)
        resp = self._gre_bridge.submit_attackers_raw(attacker_entries)
        if not resp or not resp.get("ok"):
            logger.warning(f"Bridge declare_attackers step 1 failed: {resp}")
            return None

        if resp.get("needs_finalize"):
            # Step 2: Wait for GRE to process, then finalize with SubmitAttackers
            time.sleep(0.4)
            resp2 = self._gre_bridge.submit_attackers_raw([])
            if not resp2 or not resp2.get("ok"):
                logger.warning(f"Bridge declare_attackers step 2 (finalize) failed: {resp2}")
                # Step 1 succeeded, so attackers are declared even if finalize fails
                # The game may auto-advance or we can retry
            else:
                logger.info("Bridge declare_attackers: finalized successfully")

        names_str = ", ".join(action.attacker_names)
        self._log_execution_path(ExecutionPath.GRE_AWARE, f"declare_attackers: [{names_str}] via GRE bridge")
        return ClickResult(True, 0, 0, "attackers", "GRE bridge")

    def _try_gre_bridge_blockers(self, action: GameAction) -> ClickResult | None:
        """Submit blocker assignments via the GRE bridge.

        Resolves blocker/attacker instance IDs from the bridge's own
        DeclareBlockersRequest payload (`blockers` array with
        `blockerInstanceId` and `attackerInstanceIds`). MTGA renumbers
        instances on zone transitions (e.g. token/clone re-IDs), so the
        gamestate snapshot can hold stale IDs by the time we submit —
        which made the plugin's match against `AllBlockers` silently
        fall through to a no-op `SubmitBlockersReq` and stuck the
        autopilot in a Declare-Blockers loop.
        """
        # Verify the bridge actually has a DeclareBlockers request pending
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            logger.info("GRE bridge blockers: no pending interaction, falling back")
            return None
        req_class = pending.get("request_class", "")
        if "DeclareBlockers" not in req_class:
            logger.info(f"GRE bridge blockers: pending is {req_class}, not DeclareBlockers, falling back")
            return None

        bridge_blockers = pending.get("blockers") or []
        if not bridge_blockers or not getattr(action, "blocker_assignments", None):
            logger.info("GRE bridge blockers: submitting empty blockers (no blockers to assign)")
            if self._gre_bridge.submit_blockers([]):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    "declare_blockers: [No Blocks] submitted via GRE bridge",
                )
                return ClickResult(True, 0, 0, "declare_blockers", "GRE bridge")
            return None

        game_state = self._get_game_state()
        battlefield = game_state.get("battlefield", [])

        def _name_of(iid: int) -> str:
            for c in battlefield:
                try:
                    if int(c.get("instance_id") or 0) == iid:
                        return (c.get("name") or "").lower()
                except (TypeError, ValueError):
                    continue
            return ""

        bridge_by_name: dict[str, dict] = {}
        bridge_id_list: list[int] = []
        for b in bridge_blockers:
            try:
                biid = int(b.get("blockerInstanceId") or 0)
            except (TypeError, ValueError):
                continue
            if not biid:
                continue
            bridge_id_list.append(biid)
            n = _name_of(biid)
            if n:
                bridge_by_name[n] = b

        assignments = []
        for blocker_name, attacker_name in action.blocker_assignments.items():
            bn = (blocker_name or "").lower()
            b_entry = bridge_by_name.get(bn)
            if not b_entry:
                for k, v in bridge_by_name.items():
                    if bn and (bn in k or k in bn):
                        b_entry = v
                        break

            if not b_entry:
                logger.warning(
                    f"GRE bridge blockers: can't find blocker {blocker_name!r} "
                    f"among bridge entries (names: {list(bridge_by_name)}, "
                    f"ids: {bridge_id_list}), surfacing manual-required"
                )
                return None

            try:
                blocker_id = int(b_entry["blockerInstanceId"])
            except (TypeError, ValueError, KeyError):
                logger.warning(f"GRE bridge blockers: bad blockerInstanceId in {b_entry}")
                return None

            an = (attacker_name or "").lower()
            attacker_id: int | None = None
            legal_attackers = b_entry.get("attackerInstanceIds") or []
            for aid in legal_attackers:
                try:
                    aid_i = int(aid)
                except (TypeError, ValueError):
                    continue
                cand_name = _name_of(aid_i)
                if cand_name == an or (an and (an in cand_name or cand_name in an)):
                    attacker_id = aid_i
                    break

            if attacker_id is None and len(legal_attackers) == 1:
                try:
                    attacker_id = int(legal_attackers[0])
                    logger.info(
                        f"GRE bridge blockers: attacker name lookup failed for "
                        f"{attacker_name!r}; using sole legal attacker {attacker_id}"
                    )
                except (TypeError, ValueError):
                    attacker_id = None

            if attacker_id is None:
                logger.warning(
                    f"GRE bridge blockers: can't resolve attacker {attacker_name!r} "
                    f"for blocker {blocker_name!r} (legal attacker ids: "
                    f"{legal_attackers}), surfacing manual-required"
                )
                return None

            assignments.append(
                {
                    "blockerInstanceId": blocker_id,
                    "attackerInstanceIds": [attacker_id],
                }
            )

        if self._gre_bridge.submit_blockers(assignments):
            # Blockers are a two-step server round-trip, like attackers: the
            # DeclareBlockersResp update makes the GRE re-issue a fresh
            # DeclareBlockersRequest carrying the selection, and the
            # SubmitBlockersReq confirm must go against THAT request. The
            # plugin fires its confirm immediately against the stale request,
            # which the GRE ignores (live 2026-06-11: blocker shown selected
            # in-game, "1 Blocker" confirm never fired, autopilot escaped to
            # manual-required). Finalize here against the refreshed request —
            # empty assignments make the plugin call SubmitBlockers() on it,
            # confirming the pending selection.
            time.sleep(0.8)
            try:
                still = self._gre_bridge.get_pending_actions()
                if (
                    still
                    and still.get("has_pending")
                    and "DeclareBlockers" in str(still.get("request_class", ""))
                ):
                    if self._gre_bridge.submit_blockers([]):
                        logger.info("Bridge declare_blockers: finalized on refreshed request")
                    else:
                        logger.warning("Bridge declare_blockers: finalize step failed")
            except Exception as e:
                logger.debug(f"Bridge declare_blockers finalize check failed: {e}")
            desc = ", ".join(f"{b}->{a}" for b, a in action.blocker_assignments.items())
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"declare_blockers: {desc} submitted via GRE bridge (bridge-authoritative ids)",
            )
            return ClickResult(True, 0, 0, "declare_blockers", "GRE bridge")

        logger.info("GRE bridge submit_blockers failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("declare_blockers")
        return None

    def _try_gre_bridge_attackers(self, action: GameAction) -> ClickResult | None:
        """Submit attacker declarations via the GRE bridge.

        Maps card names in action.attacker_names to instance IDs and
        targets the opponent's face by default.
        """
        # Verify the bridge actually has a DeclareAttacker request pending
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            logger.info("GRE bridge attackers: no pending interaction, falling back")
            return None
        req_class = pending.get("request_class", "")
        if "DeclareAttacker" not in req_class:
            logger.info(f"GRE bridge attackers: pending is {req_class}, not DeclareAttacker, falling back")
            return None

        game_state = self._get_game_state()
        attacker_id_map = self._build_attacker_id_map(game_state)
        battlefield = game_state.get("battlefield", [])
        local_seat = None
        opp_seat = None
        for p in game_state.get("players", []):
            if p.get("is_local"):
                local_seat = p.get("seat_id")
            else:
                opp_seat = p.get("seat_id")

        attacker_list = []
        for name in action.attacker_names:
            instance_id = attacker_id_map.get(name)
            if instance_id is None:
                instance_id = self._find_instance_id(name, battlefield, local_seat)
            if instance_id is None:
                logger.warning(
                    f"GRE bridge attackers: can't resolve ID for '{name}', "
                    "surfacing manual-required to caller"
                )
                return None

            attacker_list.append(
                {
                    "attackerInstanceId": instance_id,
                    "damageRecipient": {
                        "type": "DamageRecType_Player",
                        "playerSystemSeatId": opp_seat or 0,
                    },
                }
            )

        if self._gre_bridge.submit_attackers(attacker_list):
            names = ", ".join(action.attacker_names)
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"declare_attackers: {names} submitted via GRE bridge"
            )
            return ClickResult(True, 0, 0, "declare_attackers", "GRE bridge")

        logger.info("GRE bridge submit_attackers failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("declare_attackers")
        return None

    def _try_gre_bridge_select_target(self, action: GameAction) -> ClickResult | None:
        """Submit target selection via bridge.

        Uses submit_targets (SelectTargetsRequest) or submit_selection
        (SelectNRequest) depending on the pending request type.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        req_class = pending.get("request_class", "")
        game_state = self._get_game_state()
        battlefield = game_state.get("battlefield", [])

        # Restrict resolution to the bridge's legal-target set when present,
        # so a fuzzy match can't accidentally pick a creature that isn't a
        # valid choice for this particular spell/ability.
        bridge_candidate_ids: set[int] = set()
        for cand in pending.get("target_candidates") or []:
            try:
                iid = int(cand.get("targetInstanceId") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                bridge_candidate_ids.add(iid)

        def _eligible(card: dict[str, Any]) -> bool:
            if not bridge_candidate_ids:
                return True
            try:
                return int(card.get("instance_id") or 0) in bridge_candidate_ids
            except (TypeError, ValueError):
                return False

        # Resolve target name to instance ID — exact first, then fuzzy.
        target_names = action.target_names or ([action.card_name] if action.card_name else [])
        target_id = None
        matched_name = None
        target_id, matched_name = _match_target_in_battlefield(target_names, battlefield, _eligible)

        # Bridge-only fallback: opponent permanents may not appear in the local
        # battlefield zone, but the bridge ships their grpId in target_candidates.
        # Build synthetic candidate cards from the bridge list and re-match.
        if target_id is None and pending.get("target_candidates"):
            try:
                from arenamcp import server as _server
            except ImportError:
                _server = None
            synthetic: list[dict[str, Any]] = []
            seen_iids: set[int] = set()
            for cand in pending.get("target_candidates") or []:
                try:
                    iid = int(cand.get("targetInstanceId") or 0)
                    grp = int(cand.get("grpId") or 0)
                except (TypeError, ValueError):
                    continue
                if not iid or iid in seen_iids:
                    continue
                seen_iids.add(iid)
                name = ""
                if grp and _server is not None:
                    try:
                        info = _server.enrich_with_oracle_text(grp)
                        name = str(info.get("name") or "")
                    except Exception:
                        name = ""
                synthetic.append({"instance_id": iid, "name": name, "grp_id": grp})
            if synthetic:
                target_id, matched_name = _match_target_in_battlefield(
                    target_names, synthetic, lambda _c: True
                )

        # Last resort: if the bridge reports exactly one legal candidate,
        # use it even when the name lookup failed. Catches common cases
        # like "Target creature you control" with only one creature.
        if target_id is None and len(bridge_candidate_ids) == 1:
            target_id = next(iter(bridge_candidate_ids))
            matched_name = f"<single-candidate id={target_id}>"
            logger.info(
                f"GRE bridge select_target: name lookup failed for {target_names}; "
                f"using sole bridge candidate {target_id}"
            )

        if target_id is None:
            # Log candidate list so bug reports show what was available.
            cand_summary = []
            seen_log_iids: set[int] = set()
            for card in battlefield:
                if _eligible(card):
                    try:
                        iid_log = int(card.get("instance_id") or 0)
                    except (TypeError, ValueError):
                        iid_log = 0
                    if iid_log:
                        seen_log_iids.add(iid_log)
                    cand_summary.append(f"{card.get('name')!r}#{card.get('instance_id')}")
            # Also surface bridge-only candidates so bug reports show opponent
            # permanents that aren't in the local battlefield zone.
            try:
                from arenamcp import server as _server_log
            except ImportError:
                _server_log = None
            for cand in pending.get("target_candidates") or []:
                try:
                    iid_b = int(cand.get("targetInstanceId") or 0)
                    grp_b = int(cand.get("grpId") or 0)
                except (TypeError, ValueError):
                    continue
                if not iid_b or iid_b in seen_log_iids:
                    continue
                name_b = ""
                if grp_b and _server_log is not None:
                    try:
                        name_b = str(_server_log.enrich_with_oracle_text(grp_b).get("name") or "")
                    except Exception:
                        name_b = ""
                cand_summary.append(f"{name_b!r}#{iid_b}(bridge)")
            logger.info(
                f"GRE bridge select_target: can't resolve ID for {target_names} "
                f"(candidates: [{', '.join(cand_summary) or 'none'}]), falling back"
            )
            return None

        # Use the right bridge method based on request type
        success = False
        if "SelectTargets" in req_class:
            # Multi-slot coverage: an Aura may need a second target (e.g.
            # exile an opponent's permanent) the name lookup didn't resolve.
            # Build the per-slot decision and cover every required slot so
            # we don't submit one id and wedge on the unfilled slot.
            target_ids = [target_id]
            try:
                from arenamcp.decisions import (
                    build_pending_decision,
                    expand_target_selection,
                )

                decision = build_pending_decision(pending)
                if decision is not None and len(decision.slots) > 1:
                    covered = expand_target_selection(decision, [f"tgt:{target_id}"])
                    if covered:
                        target_ids = covered
            except Exception as e:
                logger.debug(f"select_target multi-slot expand failed: {e}")
            success = self._gre_bridge.submit_targets(target_ids)
        else:
            success = self._gre_bridge.submit_selection([target_id])

        if success:
            display = matched_name or ", ".join(target_names)
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"select_target: {display} (id={target_id}) via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_target", "GRE bridge")

        logger.info("GRE bridge select_target failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("select_target")
        return None

    def _try_gre_bridge_assign_damage(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult | None:
        """Submit combat damage assignments via the GRE bridge.

        The planner's GameAction carries `distribution` as a name → damage
        map per attacker, but for AssignDamage we expect a richer structure:
        the LLM ideally emits `target_names` listing the receivers in
        order with `distribution` keyed by receiver name. When only one
        attacker has damage to assign, we accept the simple form and
        treat `distribution` as receiver_name → damage. Otherwise we
        fall back to the bridge's existing assigner template.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        if "AssignDamage" not in str(req_class):
            return None

        battlefield = game_state.get("battlefield", []) or []

        bridge_assigners = pending.get("assigners") or []
        if not bridge_assigners:
            # Without bridge-side assigner shape we can't safely synthesize a
            # full AssignDamage submission. Surface manual-required.
            logger.info(
                "GRE bridge assign_damage: bridge did not surface assigners; surfacing manual-required"
            )
            return None

        assigners: list[dict[str, Any]] = []
        dist_map = {k.lower(): v for k, v in (action.distribution or {}).items()}
        for assigner in bridge_assigners:
            try:
                attacker_id = int(assigner.get("instanceId") or 0)
                total = int(assigner.get("totalDamage") or 0)
            except (TypeError, ValueError):
                continue
            if not attacker_id or total <= 0:
                continue
            assignments_in = assigner.get("assignments") or []
            built: list[dict[str, int]] = []
            remaining = total
            # If the LLM gave us a per-receiver distribution, use that;
            # otherwise dump everything onto the first legal receiver
            # (typically the defending player or the only blocker).
            if dist_map:
                for entry in assignments_in:
                    try:
                        receiver_id = int(entry.get("instanceId") or 0)
                    except (TypeError, ValueError):
                        continue
                    rname = ""
                    for c in battlefield:
                        try:
                            if int(c.get("instance_id") or 0) == receiver_id:
                                rname = str(c.get("name") or "").lower()
                                break
                        except (TypeError, ValueError):
                            continue
                    dmg = int(dist_map.get(rname, 0))
                    if dmg <= 0:
                        continue
                    built.append({"instanceId": receiver_id, "damage": min(dmg, remaining)})
                    remaining -= dmg
                    if remaining <= 0:
                        break
            if not built:
                # Default: dump all damage on the first listed assignment
                # (bridge-supplied default order matches MTGA's blocker ordering).
                if assignments_in:
                    try:
                        first_id = int(assignments_in[0].get("instanceId") or 0)
                    except (TypeError, ValueError):
                        first_id = 0
                    if first_id:
                        built.append({"instanceId": first_id, "damage": total})
                        remaining = 0
            # Spill any leftover damage to the last assignment slot.
            if remaining > 0 and built:
                built[-1]["damage"] = int(built[-1]["damage"]) + remaining
            assigners.append({"instanceId": attacker_id, "assignments": built})

        if not assigners:
            logger.info("GRE bridge assign_damage: no assignments built; surfacing manual-required")
            return None

        if self._gre_bridge.submit_assign_damage(assigners):
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"assign_damage: {len(assigners)} assigners via GRE bridge"
            )
            return ClickResult(True, 0, 0, "assign_damage", "GRE bridge")
        self._gre_bridge_failed_methods.add("assign_damage")
        return None

    def _try_gre_bridge_distribute(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult | None:
        """Submit a Distribution decision via GRE bridge."""
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        if "Distribution" not in str(req_class):
            return None

        battlefield = game_state.get("battlefield", []) or []

        # The LLM expresses distribution as name → amount. Resolve names
        # to instance_ids before sending. If we can't resolve everything
        # we surface manual-required so vision/manual can take over.
        distributions: dict[int, int] = {}
        for name, amount in (action.distribution or {}).items():
            try:
                amount = int(amount)
            except (TypeError, ValueError):
                continue
            if amount <= 0:
                continue
            iid, _ = _match_target_in_battlefield([name], battlefield, lambda _c: True)
            if not iid:
                logger.info(f"GRE bridge distribute: can't resolve {name!r}; surfacing manual-required")
                return None
            distributions[int(iid)] = amount

        if not distributions:
            return None

        if self._gre_bridge.submit_distribution(distributions):
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"distribute: {len(distributions)} targets via GRE bridge"
            )
            return ClickResult(True, 0, 0, "distribute", "GRE bridge")
        self._gre_bridge_failed_methods.add("distribute")
        return None

    def _try_gre_bridge_order(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult | None:
        """Submit ordering (triggers / blockers / combat damage) via GRE bridge.

        Bridge mapping:
          OrderRequest         → submit_order
          SelectFromGroups     → submit_select_from_groups
          GroupRequest         → submit_group (existing)
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        req_class_str = str(req_class)

        # OrderRequest: submit current order (or LLM-provided ordering if
        # ever reified). Most stack-trigger ordering is "default order is
        # fine"; sending the bridge's current Ids list confirms it.
        if "Order" in req_class_str and "Group" not in req_class_str:
            if self._gre_bridge.submit_order():
                self._log_execution_path(ExecutionPath.GRE_AWARE, "order: default ordering via GRE bridge")
                return ClickResult(True, 0, 0, "order", "GRE bridge")
            self._gre_bridge_failed_methods.add("order")
            return None

        # SelectFromGroupsRequest: submit a single empty group to accept
        # the bridge's current default. The vast majority of in-game
        # SelectFromGroups prompts (e.g. assignment of triggers to stack
        # spots) are "confirm the default" interactions.
        if "SelectFromGroups" in req_class_str:
            if self._gre_bridge.submit_select_from_groups([]):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE, "order: select_from_groups default via GRE bridge"
                )
                return ClickResult(True, 0, 0, "order", "GRE bridge")
            self._gre_bridge_failed_methods.add("order")
            return None

        return None

    def _try_gre_bridge_select_replacement(
        self, action: GameAction, game_state: dict[str, Any]
    ) -> ClickResult | None:
        """Submit a SelectReplacement choice via GRE bridge.

        Uses action.modal_index as the replacement index when set,
        otherwise picks index 0. Honors a 'decline' card_name / no-op
        modal as a decline when the request is optional.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        req_class = pending.get("request_class") or pending.get("request_type") or ""
        if "SelectReplacement" not in str(req_class):
            return None

        button_name = (action.card_name or "").lower().strip()
        if button_name in ("decline", "cancel", "no", "skip"):
            if self._gre_bridge.submit_select_replacement(decline=True):
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE, "select_replacement: declined via GRE bridge"
                )
                return ClickResult(True, 0, 0, "select_replacement", "GRE bridge")
            self._gre_bridge_failed_methods.add("select_replacement")
            return None

        idx = int(getattr(action, "modal_index", 0) or 0)
        if self._gre_bridge.submit_select_replacement(index=idx):
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"select_replacement: index {idx} via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_replacement", "GRE bridge")
        self._gre_bridge_failed_methods.add("select_replacement")
        return None

    def _try_gre_bridge_select_n(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult | None:
        """Submit SelectN / Search / SelectCounters via the GRE bridge.

        The LLM gives us `select_card_names`; we resolve those names to
        grp_ids by scanning the game state's known zones (library peek,
        hand, battlefield, graveyard, exile, stack). If we can't find a
        specific grp_id match, we submit an empty list which triggers
        `SubmitArbitrary()` on the plugin side — the game will pick
        automatically rather than leave the selection hanging.

        This path exists specifically to avoid the mouse-click fallback
        (`_exec_select_n`) which clicks by list index and often misses
        the actual option positions, causing the autopilot to loop.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        req_type = str(pending.get("request_type") or "")
        req_class = str(pending.get("request_class") or "")

        # Only handle SelectN and Search variants; other request types
        # need dedicated paths.
        is_select_n = "SelectN" in req_class or "Search" in req_class or req_type in ("SelectN", "Search")
        if not is_select_n:
            return None

        desired_names = [
            (n or "").lower().strip() for n in (action.select_card_names or []) if n and (n or "").strip()
        ]
        if not desired_names and action.card_name:
            desired_names = [action.card_name.lower().strip()]

        # Decide whether the request takes instance IDs or grp IDs. For
        # most library-reveal style selections (Lluwen, Scry, Surveil,
        # mill-then-pick) the IdType is InstanceId — two copies of the
        # same card have different instance IDs. Submitting grp_ids in
        # that case silently no-ops and the game keeps asking.
        #
        # Prefer the explicit fields from the bridge response when they
        # exist (newer plugin builds — see HandleGetPendingActions for
        # SelectNRequest). Fall back to decision_context for older
        # bridges that only emitted the reflected request_payload.
        decision_context = game_state.get("decision_context") or {}
        explicit_ids = pending.get("select_n_ids")
        if isinstance(explicit_ids, list):
            try:
                option_ids = [int(x) for x in explicit_ids]
            except (TypeError, ValueError):
                option_ids = []
        else:
            option_ids = decision_context.get("option_ids") or []
            try:
                option_ids = [int(x) for x in option_ids]
            except (TypeError, ValueError):
                option_ids = []

        # Explicit flag wins; fall back to id_type string parsing + the
        # battlefield-membership heuristic when the bridge didn't tag it.
        explicit_is_instance = pending.get("select_n_is_instance_id")
        if isinstance(explicit_is_instance, bool):
            wants_instance_ids = explicit_is_instance
        else:
            id_type = str(pending.get("select_n_id_type") or decision_context.get("id_type") or "").strip()
            wants_instance_ids = (
                "InstanceId" in id_type
                or "instance" in id_type.lower()
                or (
                    len(option_ids) > 0
                    and len(option_ids) <= 20
                    and all(
                        any(
                            int(c.get("instance_id") or 0) == oid
                            for c in (
                                game_state.get("battlefield", [])
                                + game_state.get("library_top_revealed", [])
                                + game_state.get("hand", [])
                                + game_state.get("graveyard", [])
                                + game_state.get("stack", [])
                                + game_state.get("exile", [])
                            )
                            if isinstance(c, dict)
                        )
                        for oid in option_ids[:5]
                    )
                )
            )

        # Selection size: prefer explicit min/max from the bridge so the
        # match loop doesn't over- or under-collect when decision_context
        # is empty (e.g. fresh CastingTime sub-decision).
        try:
            select_min = int(pending.get("select_n_min") or 0)
        except (TypeError, ValueError):
            select_min = 0
        try:
            select_max = int(pending.get("select_n_max") or 0)
        except (TypeError, ValueError):
            select_max = 0
        if select_max > 0:
            target_count = select_max
        else:
            try:
                target_count = max(1, int(decision_context.get("count") or 1))
            except (TypeError, ValueError):
                target_count = 1

        matched_ids: list[int] = []
        zone_keys = (
            "library",
            "library_top_revealed",
            "hand",
            "battlefield",
            "battlefield_player",
            "battlefield_opponent",
            "graveyard",
            "graveyard_player",
            "graveyard_opponent",
            "exile",
            "stack",
        )

        if wants_instance_ids:
            # Resolve desired_names against visible cards and submit their
            # instance_ids — restricted to option_ids when available.
            option_id_set = set(option_ids)
            for zone_key in zone_keys:
                zone = game_state.get(zone_key)
                if not isinstance(zone, list):
                    continue
                for card in zone:
                    if not isinstance(card, dict):
                        continue
                    name = str(card.get("name") or "").lower().strip()
                    iid = int(card.get("instance_id") or 0)
                    if not (name and iid):
                        continue
                    if option_id_set and iid not in option_id_set:
                        continue
                    for want in desired_names:
                        if want and (want == name or want in name or name in want):
                            if iid not in matched_ids:
                                matched_ids.append(iid)
                            break
                    if len(matched_ids) >= target_count:
                        break
                if len(matched_ids) >= target_count:
                    break

        if not matched_ids:
            # Collect candidate cards by grp_id from every visible zone
            # (falls back to this path when IdType is grp-based or we
            # couldn't resolve by instance).
            for zone_key in zone_keys:
                zone = game_state.get(zone_key)
                if not isinstance(zone, list):
                    continue
                for card in zone:
                    if not isinstance(card, dict):
                        continue
                    name = str(card.get("name") or "").lower().strip()
                    grp = card.get("grp_id") or 0
                    if not (name and grp):
                        continue
                    for want in desired_names:
                        if want and (want == name or want in name or name in want):
                            if int(grp) not in matched_ids:
                                matched_ids.append(int(grp))
                            break

            # Fallback: some SelectN targets are library-top reveals (Lluwen ETB,
            # Cultivate, etc.) that don't appear in hand/battlefield/graveyard.
            # Resolve by card name lookup against the card DB so we can still
            # submit the right grp_id.
            if not matched_ids and desired_names:
                try:
                    from arenamcp.card_db import get_card_database

                    card_db = get_card_database()
                    for want in desired_names:
                        card = card_db.get_card_by_name(want)
                        if card and getattr(card, "arena_id", 0):
                            grp = int(card.arena_id)
                            if grp not in matched_ids:
                                matched_ids.append(grp)
                    if matched_ids:
                        logger.info(f"select_n: resolved {desired_names} via card DB -> {matched_ids}")
                except Exception as e:
                    logger.debug(f"select_n card DB lookup failed: {e}")

        # Mandatory-selection fallback: if we couldn't resolve specific cards
        # but the request REQUIRES at least select_min picks (e.g. end-of-turn
        # discard down to 7), an empty/arbitrary submit is rejected and loops
        # forever. Pick select_min candidate ids so the decision completes.
        if not matched_ids and select_min > 0 and option_ids:
            matched_ids = [int(x) for x in list(option_ids)[:select_min]]
            logger.info(
                f"select_n: mandatory min={select_min}, no pick resolved — "
                f"defaulting to first {len(matched_ids)} candidate(s) {matched_ids}"
            )

        # Submit — empty list → SubmitArbitrary (safe fallback when we can't
        # resolve a specific option)
        success = self._gre_bridge.submit_selection(matched_ids)
        if success:
            id_kind = "instance_ids" if wants_instance_ids else "grp_ids"
            method = f"{len(matched_ids)} {id_kind}" if matched_ids else "arbitrary"
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"select_n: {method} (req={req_class or req_type}) via GRE bridge"
            )
            return ClickResult(True, 0, 0, "select_n", "GRE bridge")

        logger.info("GRE bridge select_n failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("select_n")
        return None

    def _try_gre_bridge_scry(self, action: GameAction, game_state: dict[str, Any]) -> ClickResult | None:
        """Submit a scry / surveil-style GroupRequest via the bridge.

        The client represents scry as a `GroupRequest` over the cards being
        scryed, expecting two Groups in the response (top and bottom of the
        library). The LLM gives us:
          - `select_card_names`: the card(s) to keep on top
          - `scry_position`: "top" or "bottom" — applied to all revealed
             cards when no specific names are given

        Cards not named in `select_card_names` go to the other group.
        """
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        req_class = str(pending.get("request_class") or "")
        if "GroupRequest" not in req_class and "Group" not in str(pending.get("request_type") or ""):
            return None

        # Extract the revealed instance IDs the request is asking us to order.
        request_payload = pending.get("request_payload") or {}
        instance_ids_raw = (
            request_payload.get("instanceIds")
            or (pending.get("decision_context") or {}).get("instanceIds")
            or []
        )
        instance_ids: list[int] = []
        for v in instance_ids_raw:
            try:
                instance_ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if not instance_ids:
            logger.info("scry: GroupRequest has no instanceIds; cannot split top/bottom")
            return None

        # Map the LLM's chosen names to instance IDs via the stack / library
        # peek in game state.
        desired_names = [
            (n or "").lower().strip() for n in (action.select_card_names or []) if n and (n or "").strip()
        ]
        name_to_iid: dict[str, list[int]] = {}
        for zone_key in ("library_top_revealed", "stack", "scry_cards", "revealed"):
            zone = game_state.get(zone_key)
            if not isinstance(zone, list):
                continue
            for card in zone:
                if not isinstance(card, dict):
                    continue
                name = str(card.get("name") or "").lower().strip()
                iid = card.get("instance_id") or card.get("instanceId") or 0
                if name and iid:
                    name_to_iid.setdefault(name, []).append(int(iid))

        top_ids: list[int] = []
        if desired_names:
            for want in desired_names:
                for name, iids in name_to_iid.items():
                    if want and (want == name or want in name or name in want):
                        for iid in iids:
                            if iid in instance_ids and iid not in top_ids:
                                top_ids.append(iid)

        # If no specific names resolved, fall back to scry_position intent.
        pos = (action.scry_position or "").lower()
        if not top_ids and not desired_names and pos == "top":
            top_ids = list(instance_ids)
            # pos == "bottom" or empty: leave top empty (all go bottom)

        bottom_ids = [iid for iid in instance_ids if iid not in top_ids]

        groups = [
            {"ids": top_ids, "zone": "Library", "sub_zone": "Top"},
            {"ids": bottom_ids, "zone": "Library", "sub_zone": "Bottom"},
        ]
        success = self._gre_bridge.submit_group(groups)
        if success:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE, f"scry: top={len(top_ids)} bottom={len(bottom_ids)} via GRE bridge"
            )
            return ClickResult(True, 0, 0, "scry", "GRE bridge")

        logger.info("GRE bridge scry failed, surfacing manual-required to caller")
        self._gre_bridge_failed_methods.add("scry")
        return None

    # ------------------------------------------------------------------
    # Safe-default net for non-passable interactive GRE requests
    # ------------------------------------------------------------------
    #
    # Many interactive requests (Group bottoming, SelectN, Search,
    # NumericInput, SelectTargets, ...) do NOT accept a pass. When the
    # planner/fallback can only produce pass/resolve for one of these, the
    # plugin rejects the pass, the "blocked action repeated" guard fires, and
    # the autopilot dead-loops on the opening interaction (observed live with
    # the London-mulligan bottoming GroupRequest). These helpers submit a
    # *legal* typed default via the bridge so the GRE always advances.

    @staticmethod
    def _plan_cannot_legally_submit(plan: ActionPlan | None) -> bool:
        """True when the plan can't produce a real (non-pass) submission."""
        if not plan or not plan.actions:
            return True
        passive = {ActionType.PASS_PRIORITY, ActionType.RESOLVE}
        return all(a.action_type in passive for a in plan.actions)

    def _is_non_passable_interactive(self, game_state: dict[str, Any]) -> bool:
        """True when the pending bridge request is interactive and rejects pass.

        Excludes the ActionsAvailable priority window (the normal
        play-land/cast/attack/pass path) and the dedicated Mulligan keep/mull
        request, both of which have their own correct handling.
        """
        btype = str(game_state.get("_bridge_request_type") or "")
        bclass = str(game_state.get("_bridge_request_class") or "")
        if not btype and not bclass:
            return False
        if "ActionsAvailable" in btype or "ActionsAvailable" in bclass:
            return False
        # The Mulligan keep/mull decision (request type "Mulligan") is its own
        # critical path. Note the post-keep London bottoming step is a separate
        # GroupRequest (type "Group", context "LondonMulligan"), which IS
        # handled by the net below.
        if btype in ("Mulligan", "MulliganReq", "MulliganRequest") or bclass == "MulliganRequest":
            return False
        return not game_state.get("_bridge_can_pass")

    def _try_interactive_safe_default(self, game_state: dict[str, Any], trigger: str) -> bool | None:
        """Submit a typed safe default for a non-passable interactive request.

        Returns True if a legal default was submitted, False if every attempt
        (including MTGA's own AutoRespond) failed, or None if the request
        isn't actionable here (dry-run, bridge offline, or nothing pending).
        """
        if self._config.dry_run:
            return None
        if not (self._gre_bridge.connected or self._gre_bridge.connect()):
            return None
        pending = self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None

        dec_type = self._decision_type(game_state)
        btype = str(game_state.get("_bridge_request_type") or pending.get("request_type") or "")
        bclass = str(game_state.get("_bridge_request_class") or pending.get("request_class") or "")
        label = btype or bclass or dec_type or "interactive"

        def _ok(detail: str) -> bool:
            self._log_execution_path(ExecutionPath.GRE_AWARE, f"{label}: safe-default submission ({detail})")
            self._record_autopilot_decision(
                game_state,
                trigger,
                action_type="safe_default",
                summary=f"{label}: {detail}",
            )
            return True

        # Group: London-mulligan bottoming / scry-surveil / ordering default.
        if "Group" in btype or "Group" in bclass or dec_type == "group_selection":
            res = self._try_gre_bridge_group_default(game_state, pending)
            if res is not None and res.success:
                return _ok("group default")

        # SelectN / Search: submit the resolvable selection, else SubmitArbitrary.
        if (
            dec_type in ("select_n", "selection_generic", "search")
            or "SelectN" in btype
            or "SelectN" in bclass
            or "Search" in btype
            or "Search" in bclass
        ):
            res = self._try_gre_bridge_select_n(GameAction(action_type=ActionType.SELECT_N), game_state)
            if res is not None and res.success:
                return _ok("select_n/search min-or-arbitrary")
            if self._gre_bridge.submit_selection([]):
                return _ok("empty selection (SubmitArbitrary)")

        # NumericInput: min (or first suggested) legal value.
        if dec_type == "numeric_input" or "Numeric" in btype or "Numeric" in bclass:
            value = self._safe_default_numeric(pending)
            if self._gre_bridge.submit_numeric(value):
                return _ok(f"numeric={value}")

        # SelectTargets: first legal candidate.
        if dec_type == "target_selection" or "SelectTargets" in btype or "SelectTargets" in bclass:
            tid = self._first_target_candidate(pending)
            if tid is not None and self._gre_bridge.submit_targets(tid):
                return _ok(f"first target {tid}")

        # SelectReplacement: first replacement.
        if dec_type == "select_replacement" or "SelectReplacement" in btype or "SelectReplacement" in bclass:
            if self._gre_bridge.submit_select_replacement(index=0):
                return _ok("replacement index 0")

        # Ordering / SelectFromGroups: accept the given default order.
        if dec_type in ("order_triggers", "order_combat_damage", "select_from_groups"):
            if self._gre_bridge.submit_order():
                return _ok("default order")
            if self._gre_bridge.submit_select_from_groups([]):
                return _ok("select_from_groups default")

        # Universal fallback: MTGA's own "do the default" for this request.
        if self._gre_bridge.auto_respond():
            return _ok("auto_respond")
        return False

    @staticmethod
    def _safe_default_numeric(pending: dict[str, Any]) -> int:
        """Pick a safe legal value for a NumericInputRequest (suggested|min)."""
        disallowed = set()
        for v in pending.get("numeric_disallowed") or []:
            try:
                disallowed.add(int(v))
            except (TypeError, ValueError):
                continue
        for v in pending.get("numeric_suggested") or []:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if iv not in disallowed:
                return iv
        try:
            lo = int(pending.get("numeric_min") or 0)
        except (TypeError, ValueError):
            lo = 0
        try:
            hi = int(pending.get("numeric_max") or lo)
        except (TypeError, ValueError):
            hi = lo
        v = lo
        while v in disallowed and v < hi:
            v += 1
        return v

    @staticmethod
    def _first_target_candidate(pending: dict[str, Any]) -> int | None:
        """Return the first legal target instance_id, if any."""
        for c in pending.get("target_candidates") or []:
            if not isinstance(c, dict):
                continue
            try:
                iid = int(c.get("targetInstanceId") or c.get("instance_id") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                return iid
        return None

    def _try_gre_bridge_group_default(
        self,
        game_state: dict[str, Any],
        pending: dict[str, Any] | None = None,
    ) -> ClickResult | None:
        """Submit a safe-default GroupRequest response via the bridge.

        Two cases, matching MTGA's own LondonWorkflow / ScryWorkflow shapes:
          - London mulligan bottoming: put the worst N cards on the bottom of
            the library, keep the rest in hand. N = GroupSpecs[bottom].LowerBound
            (the slot the client requires us to fill).
          - Any other ordering Group (scry / surveil / trigger ordering): accept
            the cards in the order/zones already presented (nothing to bottom).

        Returns a ClickResult (success flag set), or None if not a GroupRequest.
        """
        pending = pending or self._gre_bridge.get_pending_actions()
        if not pending or not pending.get("has_pending"):
            return None
        btype = str(pending.get("request_type") or "")
        bclass = str(pending.get("request_class") or "")
        if "Group" not in btype and "Group" not in bclass:
            return None

        payload = pending.get("request_payload") or {}
        raw_ids = (
            pending.get("group_instance_ids")
            or payload.get("instanceIds")
            or (game_state.get("decision_context") or {}).get("instanceIds")
            or []
        )
        instance_ids: list[int] = []
        for v in raw_ids:
            try:
                instance_ids.append(int(v))
            except (TypeError, ValueError):
                continue

        specs = pending.get("group_specs") or payload.get("groupSpecs") or []
        context = str(pending.get("group_context") or payload.get("context") or "")

        def _spec_bound(spec: dict[str, Any]) -> int:
            for key in ("lowerBound", "upperBound", "lower_bound", "upper_bound"):
                try:
                    b = int(spec.get(key) or 0)
                except (TypeError, ValueError):
                    b = 0
                if b > 0:
                    return b
            return 0

        def _is_bottom_spec(spec: dict[str, Any]) -> bool:
            zone = str(spec.get("zoneType") or spec.get("zone") or "")
            sub = str(spec.get("subZoneType") or spec.get("subZone") or "")
            return "Bottom" in sub or "Library" in zone

        # Determine how many cards must go to the bottom. Prefer the bottom
        # spec's bound (LondonWorkflow reads GroupSpecs[1].LowerBound); fall
        # back to hand_size - 7 for a London mulligan when specs are opaque.
        bottom_count = 0
        for spec in specs:
            if isinstance(spec, dict) and _is_bottom_spec(spec):
                bottom_count += _spec_bound(spec)
        if bottom_count <= 0 and "LondonMulligan" in context:
            bottom_count = max(0, len(instance_ids) - 7)
        bottom_count = max(0, min(bottom_count, len(instance_ids)))

        if not instance_ids or bottom_count <= 0:
            # Nothing to bottom: accept the default order. Put every card in the
            # first (top/keep) group, mirroring the request's first spec zone.
            top_zone, top_sub = "Hand", "Top"
            if specs and isinstance(specs[0], dict):
                top_zone = str(specs[0].get("zoneType") or specs[0].get("zone") or top_zone)
                top_sub = str(specs[0].get("subZoneType") or specs[0].get("subZone") or top_sub)
            groups = [{"ids": list(instance_ids), "zone": top_zone, "sub_zone": top_sub}]
            for spec in specs[1:]:
                z = str(spec.get("zoneType") or spec.get("zone") or "") if isinstance(spec, dict) else ""
                s = (
                    str(spec.get("subZoneType") or spec.get("subZone") or "")
                    if isinstance(spec, dict)
                    else ""
                )
                groups.append({"ids": [], "zone": z or None, "sub_zone": s or None})
            ok = self._gre_bridge.submit_group(groups)
            if ok:
                self._log_execution_path(
                    ExecutionPath.GRE_AWARE,
                    f"group: default order ({len(instance_ids)} cards, ctx={context or '?'}) via GRE bridge",
                )
                return ClickResult(True, 0, 0, "group", "GRE bridge")
            self._gre_bridge_failed_methods.add("group")
            return ClickResult(False, 0, 0, "group", "GRE bridge")

        # Bottom the worst N cards; keep the rest in hand. Response shape mirrors
        # MTGA's LondonWorkflow: [Hand/Top keep group, Library/Bottom group].
        worst_first = self._rank_cards_for_bottoming(instance_ids, game_state)
        bottom_ids = worst_first[:bottom_count]
        keep_ids = [iid for iid in instance_ids if iid not in bottom_ids]
        groups = [
            {"ids": keep_ids, "zone": "Hand", "sub_zone": "Top"},
            {"ids": bottom_ids, "zone": "Library", "sub_zone": "Bottom"},
        ]
        ok = self._gre_bridge.submit_group(groups)
        if ok:
            self._log_execution_path(
                ExecutionPath.GRE_AWARE,
                f"group: bottom {len(bottom_ids)} keep {len(keep_ids)} (ctx={context or '?'}) via GRE bridge",
            )
            return ClickResult(True, 0, 0, "group", "GRE bridge")
        self._gre_bridge_failed_methods.add("group")
        return ClickResult(False, 0, 0, "group", "GRE bridge")

    def _rank_cards_for_bottoming(self, instance_ids: list[int], game_state: dict[str, Any]) -> list[int]:
        """Order instance_ids worst-keep first (best candidates to bottom).

        Heuristic: bottom excess lands first (keep ~4), then highest-cmc
        spells, keeping a low land+cheap-spell base. Cards with no resolvable
        info fall to the end of their bucket — at minimum we still return a
        valid permutation so a default selection is always available.
        """
        info = {iid: self._lookup_card_for_bottoming(iid, game_state) for iid in instance_ids}
        lands = [iid for iid in instance_ids if info[iid] and info[iid]["is_land"]]
        nonlands = [iid for iid in instance_ids if iid not in lands]
        keep_lands = min(len(lands), 4)
        excess_lands = lands[keep_lands:]
        kept_lands = lands[:keep_lands]
        nonlands_sorted = sorted(
            nonlands,
            key=lambda i: -(info[i]["cmc"] if info[i] else 0),
        )
        # Worst first: extra lands, then most expensive spells, then the cheap
        # spells + the lands we'd rather keep (least likely to be bottomed).
        return excess_lands + nonlands_sorted + kept_lands

    def _lookup_card_for_bottoming(
        self, instance_id: int, game_state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Resolve an instance_id to {name, is_land, cmc} from visible zones."""
        for zone_key in ("hand", "library_top_revealed", "stack"):
            for c in game_state.get(zone_key) or []:
                if not isinstance(c, dict):
                    continue
                try:
                    iid = int(c.get("instance_id") or 0)
                except (TypeError, ValueError):
                    continue
                if iid != instance_id:
                    continue
                type_line = str(c.get("type_line") or "")
                mana_cost = str(c.get("mana_cost") or "")
                return {
                    "name": str(c.get("name") or ""),
                    "is_land": "land" in type_line.lower(),
                    "cmc": self._parse_mana_value(mana_cost),
                }
        return None

    @staticmethod
    def _parse_mana_value(mana_cost: str) -> int:
        """Convert a mana-cost string like '{2}{G}{G}' to a CMC integer."""
        return mana_cost_to_cmc(mana_cost)
