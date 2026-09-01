"""Action legality verification and action mapping mixin for ActionPlanner."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from arenamcp.action_planner import ActionType, GameAction, _strip_attacker_annotations

logger = logging.getLogger(__name__)


class _ActionLegalityMixin:
    """Methods for validating action legality against current GRE state and mapping legal actions."""

    @staticmethod
    def _humanize_legal_action(legal: str) -> str:
        """Turn a legal-action string into a short speakable sentence."""
        s = (legal or "").strip()
        if not s:
            return ""
        low = s.lower()
        if low.startswith("play land:"):
            card = s.split(":", 1)[1].strip()
            return f"Play {card}."
        if low.startswith("cast "):
            # strip trailing tags like "[OK]"
            name = s[5:].strip()
            name = re.sub(r"\s*\[[^\]]*\]\s*$", "", name)
            return f"Cast {name}."
        if "no block" in low or "no attack" in low or low in ("done", "decline"):
            # "Done (no blocks)" / "Declare no blockers" etc.
            return "Don't block." if "block" in low else "Pass."
        if low.startswith("attack with:") or low.startswith("declare attackers:"):
            return f"Attack with {_strip_attacker_annotations(s.split(':', 1)[1]).strip()}."
        if low.startswith("block with:"):
            return f"Block with {_strip_attacker_annotations(s.split(':', 1)[1]).strip()}."
        if low.startswith("activate "):
            return f"Activate {s[9:].strip()}."
        if "done" in low and "confirm" in low:
            return "Confirm."
        if low.startswith("pass"):
            return "Pass."
        return s

    @staticmethod
    def _match_legal_action_in_text(response: str, legal_actions: list[str]) -> str | None:
        text = (response or "").lower()
        if not text:
            return None
        # Prefer longer legal actions to avoid matching generic fragments first.
        for legal in sorted(legal_actions, key=len, reverse=True):
            if legal.lower() in text:
                return legal
        return None

    @staticmethod
    def _pick_preferred_legal_action(legal_actions: list[str]) -> str | None:
        """Pick a deterministic fallback action when model output is invalid."""
        if not legal_actions:
            return None

        # When [OK] tagging is active (bridge confirmed autotap solutions),
        # a Cast line WITHOUT the tag means MTGA lists the action but cannot
        # auto-pay it — submitting it starts a cast workflow that dies at
        # payment, gets cancelled, and re-planned forever (live livelock
        # 2026-06-09: Momentum Breaker / Ruthless Negotiation cast-cancel
        # loop locked the user out of the UI). Prefer passing over that.
        ok_tagging_active = any("[OK]" in a for a in legal_actions)

        def score(action: str) -> int:
            a = action.lower().strip()
            if a.startswith("play land:"):
                return 100
            if a.startswith("cast "):
                # Below Pass. This heuristic only runs when the planner's
                # output was garbage — blind-casting then picks targets
                # blindly too, and a wedged/rolled-back cast gets re-picked
                # every priority window (Patriar's Humiliation spiral,
                # 2026-07-01, burned the user's match timer). A missed cast
                # costs one window; a blind cast can cost the match.
                return 25 if (not ok_tagging_active or "[ok]" in a) else 15
            if a.startswith("declare attackers:") or a.startswith("attack with:"):
                # Below Pass for the same reason as casts: a blind attack-all
                # when the planner failed can throw the board away.
                return 24
            if a.startswith("activate "):
                # Below Pass: a blind activation opens a targeting window that
                # then also gets answered blindly — live 2026-07-02, the 403'd
                # planner auto-picked "Activate Ability: Mutagen" and the
                # blind target fallback buffed the OPPONENT's creature (#387).
                return 23
            if a.startswith("select target:"):
                return 60
            if "choose: play" in a or "choose: draw" in a:
                return 55
            if "done" in a or "auto-pay" in a:
                return 40
            if "pass" in a or "wait" in a:
                return 30
            return 10

        return max(legal_actions, key=score)

    @staticmethod
    def _strip_decoration(name: str) -> str:
        """Strip P/T suffix and trailing tags from a card name.

        The rules engine annotates legal actions with display-only suffixes
        like "Veteran Survivor (4/3)" or "Foo [NO TARGETS]". The bridge
        submitter looks up cards by their plain battlefield name, so the
        decoration must come off before we hand the action to the bridge.
        """
        if not name:
            return ""
        # Remove a trailing "(P/T)" — power/toughness can be digits or '*'.
        name = re.sub(r"\s*\([\dxX*+-]+/[\dxX*+-]+\)\s*$", "", name).strip()
        # Remove any number of trailing "[...]" tags ([OK], [NO TARGETS], etc.).
        prev = None
        while prev != name:
            prev = name
            name = re.sub(r"\s*\[[^\]]*\]\s*$", "", name).strip()
        return name

    def _legal_action_to_action(self, legal_action: str) -> GameAction | None:
        """Convert a rules-engine legal action string into a GameAction."""
        act = self._normalize_action_text(legal_action)
        lower = act.lower()

        if lower.startswith("play land:"):
            return GameAction(
                action_type=ActionType.PLAY_LAND,
                card_name=self._strip_decoration(act.split(":", 1)[1]),
            )
        if lower.startswith("cast "):
            return GameAction(action_type=ActionType.CAST_SPELL, card_name=self._strip_decoration(act[5:]))
        if lower.startswith("activate ability:"):
            # gamestate emits "Activate Ability: <card>" — without this branch
            # the generic "activate " prefix below yields card_name
            # "Ability: <card>", which never matches the planner's card name
            # and got legal activations dropped as illegal (live 2026-06-09).
            return GameAction(
                action_type=ActionType.ACTIVATE_ABILITY,
                card_name=self._strip_decoration(act.split(":", 1)[1]),
            )
        if lower.startswith("activate "):
            return GameAction(
                action_type=ActionType.ACTIVATE_ABILITY, card_name=self._strip_decoration(act[9:])
            )
        if lower.startswith("declare attackers:"):
            names = [self._strip_decoration(n) for n in self._split_creature_list(act.split(":", 1)[1])]
            names = [n for n in names if n]
            return GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=names)
        if lower.startswith("attack with:"):
            names = [self._strip_decoration(n) for n in self._split_creature_list(act.split(":", 1)[1])]
            names = [n for n in names if n]
            return GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=names)
        if lower.startswith("block with:"):
            name = self._strip_decoration(act.split(":", 1)[1])
            return GameAction(
                action_type=ActionType.DECLARE_BLOCKERS,
                blocker_assignments={name: ""} if name else {},
            )
        if lower.startswith("select target:"):
            return GameAction(
                action_type=ActionType.SELECT_TARGET,
                target_names=[self._strip_decoration(act.split(":", 1)[1])],
            )
        if lower.startswith("x = "):
            # Casting-time X chooser entry ("X = 3") — P3-1.
            try:
                return GameAction(
                    action_type=ActionType.NUMERIC_INPUT,
                    numeric_value=int(lower[4:].strip()),
                )
            except ValueError:
                pass
        if lower == "cast normally":
            # CastingTimeOptions default option — a modal pick, not a cast
            # of a card named "normally" (the fallback's pseudo-cast never
            # matched any GRE action; P3-2, live 2026-07-05 22:53).
            return GameAction(action_type=ActionType.MODAL_CHOICE, modal_index=0)
        if lower.startswith("action: playmdfc"):
            # MDFC land face (#39): playable land side of a modal
            # double-faced card in hand. The name isn't in the menu line;
            # the matcher resolves via the raw PlayMDFC action.
            return GameAction(action_type=ActionType.PLAY_LAND, mdfc=True)
        if lower.startswith("pay costs for") or "auto-pay" in lower:
            return GameAction(action_type=ActionType.PAY_COSTS)
        if "choose: play" in lower:
            return GameAction(action_type=ActionType.CHOOSE_STARTING_PLAYER, play_or_draw="play")
        if "choose: draw" in lower:
            return GameAction(action_type=ActionType.CHOOSE_STARTING_PLAYER, play_or_draw="draw")
        if lower.startswith("accept") or lower in ("allow", "yes"):
            return GameAction(action_type=ActionType.CLICK_BUTTON, card_name="accept")
        if lower.startswith("decline") or lower in ("cancel", "no"):
            return GameAction(action_type=ActionType.CLICK_BUTTON, card_name="decline")
        if "done" in lower:
            return GameAction(action_type=ActionType.CLICK_BUTTON, card_name="done")
        if "resolve" in lower:
            return GameAction(action_type=ActionType.RESOLVE)
        if "pass" in lower or "wait" in lower:
            return GameAction(action_type=ActionType.PASS_PRIORITY)

        return None

    # Action types that correspond to decision-specific GRE requests
    # (SelectN, SearchRequest, DistributionReq, NumericInputReq, etc.).
    # For these, the `legal_actions` list is often stale (it's from the
    # prior ActionsAvailable window) because MTGA doesn't re-send an
    # ActionsAvailable while the decision is pending. The bridge request
    # type or decision_context.type is the authoritative signal.
    _DECISION_ACTION_TYPES = frozenset(
        {
            ActionType.SELECT_N,
            ActionType.SELECT_TARGET,
            ActionType.SEARCH_LIBRARY,
            ActionType.DISTRIBUTE,
            ActionType.NUMERIC_INPUT,
            ActionType.MODAL_CHOICE,
            ActionType.CHOOSE_STARTING_PLAYER,
            ActionType.ASSIGN_DAMAGE,
            ActionType.ORDER_COMBAT_DAMAGE,
            ActionType.ORDER_BLOCKERS,
            ActionType.ORDER_TRIGGERS,
            ActionType.PAY_COSTS,
            ActionType.SELECT_REPLACEMENT,
            ActionType.SELECT_COUNTERS,
            ActionType.CASTING_OPTIONS,
            ActionType.MULLIGAN_KEEP,
            ActionType.MULLIGAN_MULL,
        }
    )

    # Bridge request type → action type(s) that should be trusted for it
    _BRIDGE_REQUEST_ACCEPTS: dict[str, set[ActionType]] = {
        "SelectN": {
            ActionType.SELECT_N,
            ActionType.SELECT_TARGET,
            ActionType.SELECT_REPLACEMENT,
            ActionType.SELECT_COUNTERS,
        },
        "SelectTargets": {ActionType.SELECT_TARGET, ActionType.SELECT_N},
        "SelectReplacement": {ActionType.SELECT_REPLACEMENT, ActionType.SELECT_N, ActionType.CLICK_BUTTON},
        "SelectReplacementRequest": {
            ActionType.SELECT_REPLACEMENT,
            ActionType.SELECT_N,
            ActionType.CLICK_BUTTON,
        },
        "Search": {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchRequest": {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchFromGroups": {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchFromGroupsRequest": {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "Distribution": {ActionType.DISTRIBUTE},
        "DistributionReq": {ActionType.DISTRIBUTE},
        "DistributionRequest": {ActionType.DISTRIBUTE},
        "NumericInput": {ActionType.NUMERIC_INPUT},
        "NumericInputReq": {ActionType.NUMERIC_INPUT},
        "PayCosts": {ActionType.PAY_COSTS},
        "PayCostsReq": {ActionType.PAY_COSTS},
        "ChooseStartingPlayer": {ActionType.CHOOSE_STARTING_PLAYER},
        "Mulligan": {ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL},
        "CastingTimeOption": {ActionType.CASTING_OPTIONS, ActionType.MODAL_CHOICE, ActionType.NUMERIC_INPUT},
        "CastingTimeOptions": {ActionType.CASTING_OPTIONS, ActionType.MODAL_CHOICE},
        "Group": {
            ActionType.ORDER_TRIGGERS,
            ActionType.ORDER_BLOCKERS,
            ActionType.SELECT_N,
            ActionType.SELECT_TARGET,
        },
        "GroupReq": {
            ActionType.ORDER_TRIGGERS,
            ActionType.ORDER_BLOCKERS,
            ActionType.SELECT_N,
            ActionType.SELECT_TARGET,
        },
        "GroupRequest": {
            ActionType.ORDER_TRIGGERS,
            ActionType.ORDER_BLOCKERS,
            ActionType.SELECT_N,
            ActionType.SELECT_TARGET,
        },
        "Order": {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.ORDER_COMBAT_DAMAGE},
        "OrderRequest": {
            ActionType.ORDER_TRIGGERS,
            ActionType.ORDER_BLOCKERS,
            ActionType.ORDER_COMBAT_DAMAGE,
        },
        "SelectFromGroups": {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.SELECT_N},
        "SelectFromGroupsRequest": {
            ActionType.ORDER_TRIGGERS,
            ActionType.ORDER_BLOCKERS,
            ActionType.SELECT_N,
        },
        "SelectNGroup": {ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "SelectNGroupRequest": {ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "AssignDamage": {ActionType.ASSIGN_DAMAGE, ActionType.DISTRIBUTE},
        "AssignDamageRequest": {ActionType.ASSIGN_DAMAGE, ActionType.DISTRIBUTE},
        "SelectCounters": {ActionType.SELECT_COUNTERS, ActionType.SELECT_N},
        "SelectCountersRequest": {ActionType.SELECT_COUNTERS, ActionType.SELECT_N},
        "Gather": {ActionType.DISTRIBUTE, ActionType.SELECT_N},
        "GatherRequest": {ActionType.DISTRIBUTE, ActionType.SELECT_N},
        "AutoTapActions": {ActionType.PAY_COSTS, ActionType.MODAL_CHOICE},
        "AutoTapActionsRequest": {ActionType.PAY_COSTS, ActionType.MODAL_CHOICE},
        "DeclareAttackers": {ActionType.DECLARE_ATTACKERS},
        "DeclareBlockers": {ActionType.DECLARE_BLOCKERS},
        "OptionalAction": {ActionType.CLICK_BUTTON},
        "OptionalActionMessage": {ActionType.CLICK_BUTTON},
        "OptionalActionMessageRequest": {ActionType.CLICK_BUTTON},
        "Intermission": {ActionType.CLICK_BUTTON},
        "IntermissionRequest": {ActionType.CLICK_BUTTON},
        "StringInput": {ActionType.MODAL_CHOICE, ActionType.SELECT_N},
        "StringInputRequest": {ActionType.MODAL_CHOICE, ActionType.SELECT_N},
    }

    def _is_action_legal(
        self,
        action: GameAction,
        legal_actions: list[str],
        decision_context: dict[str, Any] | None = None,
        bridge_request: str | None = None,
    ) -> bool:
        """Require planner output to map to a current legal action.

        Composition wrapper: dispatches to per-family handlers. Each handler
        answers a focused question, so the cyclomatic surface stays inside
        the handler that needs it. See `_is_legal_decision_passthrough` /
        `_is_legal_combat_declaration` / `_is_legal_default`.
        """
        if self._is_legal_decision_passthrough(action, decision_context, bridge_request):
            return True

        if not legal_actions:
            return True

        if action.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS):
            return self._is_legal_combat_declaration(action, legal_actions)

        return self._is_legal_default(action, legal_actions)

    def _is_legal_decision_passthrough(
        self,
        action: GameAction,
        decision_context: dict[str, Any] | None,
        bridge_request: str | None,
    ) -> bool:
        """Trust planner output for decision-family actions when the bridge or
        decision context confirms a matching decision is open.

        For decision-specific actions (SelectN, Search, etc.) the bridge
        request type is the authoritative signal — the legal_actions list
        can be stale from the prior ActionsAvailable window.
        """
        if action.action_type not in self._DECISION_ACTION_TYPES:
            return False

        if bridge_request:
            accepts = self._BRIDGE_REQUEST_ACCEPTS.get(bridge_request)
            if accepts:
                if action.action_type in accepts:
                    return True
                # Known bridge request that does NOT accept this action:
                # authoritative deny. Falling through to decision_context
                # here approved a stale select_target against a live
                # DeclareAttackers window (decision_context still said
                # target_selection) — autopilot then burned the whole
                # attack step re-submitting it (live 2026-07-02, Nesting
                # Grounds / Michelangelo never attacked).
                return False

        if not decision_context:
            return False

        ctx_type = str(decision_context.get("type") or "").lower()
        # e.g. "selection_generic", "search_library", "distribute"
        action_hint = action.action_type.value.lower()
        if action_hint in ctx_type or ctx_type in action_hint:
            return True
        # "target_selection" vs "select_target": neither string contains the
        # other, so the substring check above misses the single most common
        # decision pairing — the planner's valid target pick got dropped as
        # illegal and the targeting window stalled (live 2026-06-09,
        # Nurturing Presence).
        if ctx_type == "target_selection" and action.action_type in (
            ActionType.SELECT_TARGET,
            ActionType.SELECT_N,
        ):
            return True
        return bool(
            ctx_type == "selection_generic"
            and action.action_type
            in (
                ActionType.SELECT_N,
                ActionType.SELECT_TARGET,
                ActionType.SELECT_REPLACEMENT,
                ActionType.SEARCH_LIBRARY,
            )
        )

    def _is_legal_combat_declaration(self, action: GameAction, legal_actions: list[str]) -> bool:
        """Validate a DECLARE_ATTACKERS / DECLARE_BLOCKERS plan.

        Combat declarations come in as one "Attack with: X" / "Block with: X"
        string per creature, while the planner emits a full set in a single
        GameAction. We compare the planner's set against the union of legal
        creature names rather than exact-matching on any single entry.
        """
        legal_names, in_combat_context = self._collect_combat_legal_names(action.action_type, legal_actions)
        if not in_combat_context:
            return False

        if action.action_type == ActionType.DECLARE_ATTACKERS:
            plan_names = {n.strip().lower() for n in action.attacker_names if n and n.strip()}
        else:
            plan_names = {k.strip().lower() for k in action.blocker_assignments if k and k.strip()}

        for name in plan_names:
            base = re.sub(r"\s*#\d+\s*$", "", name).strip()
            if name not in legal_names and base not in legal_names:
                return False
        return True

    def _collect_combat_legal_names(
        self, action_type: ActionType, legal_actions: list[str]
    ) -> tuple[set[str], bool]:
        """Walk legal_actions and pull out the set of legal attacker/blocker names.

        Returns (legal_names, in_combat_context). in_combat_context is True
        iff at least one combat-related legal line was seen — so a missing
        flag distinguishes "wrong window entirely" from "right window, but
        the planner picked an off-list creature".
        """
        legal_names: set[str] = set()
        in_combat_context = False

        # For the "Declare Attackers: A, B, C" summary line we can't safely
        # comma-split because card names themselves may contain commas
        # (e.g. "Lluwen, Imperfect Naturalist"). Prefer the per-creature
        # "Attack with: X" lines when they exist.
        has_individual_attack_lines = any(
            la.lower().strip().startswith("attack with:") for la in legal_actions
        )

        for legal_action in legal_actions:
            low = legal_action.lower().strip()
            if action_type == ActionType.DECLARE_ATTACKERS:
                if low.startswith("attack with:"):
                    tail = legal_action.split(":", 1)[1]
                    tail = _strip_attacker_annotations(tail)
                    clean = self._normalize_action_text(tail).strip().lower()
                    if clean:
                        legal_names.add(clean)
                        base = re.sub(r"\s*#\d+\s*$", "", clean).strip()
                        if base:
                            legal_names.add(base)
                    in_combat_context = True
                elif low.startswith("declare attackers:"):
                    in_combat_context = True
                    if not has_individual_attack_lines:
                        tail = legal_action.split(":", 1)[1]
                        for name in tail.split(","):
                            name = _strip_attacker_annotations(name)
                            clean = self._normalize_action_text(name).strip().lower()
                            if clean:
                                legal_names.add(clean)
                                base = re.sub(r"\s*#\d+\s*$", "", clean).strip()
                                if base:
                                    legal_names.add(base)
                elif "confirm attackers" in low:
                    in_combat_context = True
            else:
                if low.startswith("block with:"):
                    tail = legal_action.split(":", 1)[1]
                    tail = _strip_attacker_annotations(tail)
                    clean = self._normalize_action_text(tail).strip().lower()
                    if clean:
                        legal_names.add(clean)
                        base = re.sub(r"\s*#\d+\s*$", "", clean).strip()
                        if base:
                            legal_names.add(base)
                    in_combat_context = True
                elif "confirm blockers" in low:
                    in_combat_context = True

        return legal_names, in_combat_context

    def _is_legal_default(self, action: GameAction, legal_actions: list[str]) -> bool:
        """Match a non-combat planner action against legal_actions.

        Iterates legal_actions and tries to round-trip each one through
        `_legal_action_to_action`, then compares the right field for the
        action's family (card_name for cast/play/activate, target_names[0]
        for select-target, play_or_draw for choose-starting-player, otherwise
        action-type match alone).
        """
        normalized_card_name = self._normalize_action_text(action.card_name).lower()

        for legal_action in legal_actions:
            legal = self._legal_action_to_action(legal_action)
            if not legal or legal.action_type != action.action_type:
                continue

            if action.action_type in (
                ActionType.CAST_SPELL,
                ActionType.PLAY_LAND,
                ActionType.ACTIVATE_ABILITY,
            ):
                if legal.card_name.strip().lower() == normalized_card_name:
                    return True
                continue

            if action.action_type == ActionType.SELECT_TARGET:
                if legal.target_names and action.target_names:
                    if legal.target_names[0].strip().lower() == action.target_names[0].strip().lower():
                        return True
                continue

            if action.action_type == ActionType.CHOOSE_STARTING_PLAYER:
                if (legal.play_or_draw or "").lower() == (action.play_or_draw or "").lower():
                    return True
                continue

            return True

        return False

    def _parse_action(self, data: dict[str, Any]) -> GameAction | None:
        """Parse a single action dict into a GameAction."""
        try:
            action_type_str = data.get("action_type", "")
            try:
                action_type = ActionType(action_type_str)
            except ValueError:
                logger.warning(f"Unknown action type: {action_type_str}")
                return None

            return GameAction(
                action_type=action_type,
                card_name=data.get("card_name", ""),
                target_names=data.get("target_names", []),
                attacker_names=data.get("attacker_names", []),
                blocker_assignments=data.get("blocker_assignments", {}),
                modal_index=data.get("modal_index", 0),
                select_card_names=data.get("select_card_names", []),
                scry_position=data.get("scry_position", ""),
                numeric_value=data.get("numeric_value", 0),
                distribution=data.get("distribution", {}),
                play_or_draw=data.get("play_or_draw", ""),
                reasoning=data.get("reasoning", ""),
                confidence=data.get("confidence", 1.0),
            )
        except Exception as e:
            logger.error(f"Failed to parse action: {e}, data={data}")
            return None
