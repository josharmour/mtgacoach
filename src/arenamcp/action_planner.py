"""Structured LLM Action Planning for Autopilot Mode.

Converts game state + trigger into structured JSON action commands
via a separate LLM call with a constrained schema prompt.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


_ACTIONS_AVAILABLE_BRIDGE_REQUESTS = {
    "ActionsAvailable",
    "ActionsAvailableReq",
    "ActionsAvailableRequest",
}


class ActionType(Enum):
    """Types of actions the autopilot can execute in MTGA."""
    PLAY_LAND = "play_land"
    CAST_SPELL = "cast_spell"
    DECLARE_ATTACKERS = "declare_attackers"
    DECLARE_BLOCKERS = "declare_blockers"
    SELECT_TARGET = "select_target"
    SELECT_N = "select_n"
    MODAL_CHOICE = "modal_choice"
    MULLIGAN_KEEP = "mulligan_keep"
    MULLIGAN_MULL = "mulligan_mull"
    PASS_PRIORITY = "pass_priority"
    RESOLVE = "resolve"
    DRAFT_PICK = "draft_pick"
    CLICK_BUTTON = "click_button"
    ACTIVATE_ABILITY = "activate_ability"
    ORDER_BLOCKERS = "order_blockers"
    # New decision types from GRE protocol
    ASSIGN_DAMAGE = "assign_damage"
    ORDER_COMBAT_DAMAGE = "order_combat_damage"
    PAY_COSTS = "pay_costs"
    SEARCH_LIBRARY = "search_library"
    DISTRIBUTE = "distribute"
    NUMERIC_INPUT = "numeric_input"
    CHOOSE_STARTING_PLAYER = "choose_starting_player"
    SELECT_REPLACEMENT = "select_replacement"
    SELECT_COUNTERS = "select_counters"
    CASTING_OPTIONS = "casting_options"
    ORDER_TRIGGERS = "order_triggers"


@dataclass
class GameAction:
    """A single structured action to execute in MTGA."""
    action_type: ActionType
    card_name: str = ""
    target_names: list[str] = field(default_factory=list)
    attacker_names: list[str] = field(default_factory=list)
    blocker_assignments: dict[str, str] = field(default_factory=dict)
    modal_index: int = 0
    select_card_names: list[str] = field(default_factory=list)
    scry_position: str = ""  # "top" or "bottom"
    numeric_value: int = 0  # For numeric_input (X spells, pay life)
    distribution: dict[str, int] = field(default_factory=dict)  # target_name -> amount
    play_or_draw: str = ""  # "play" or "draw"
    reasoning: str = ""
    confidence: float = 1.0
    gre_action_ref: Optional[Any] = None  # GREActionRef from gre_action_matcher

    def __str__(self) -> str:
        parts = [self.action_type.value]
        if self.card_name:
            parts.append(self.card_name)
        if self.target_names:
            parts.append(f"-> {', '.join(self.target_names)}")
        if self.attacker_names:
            parts.append(f"attackers: {', '.join(self.attacker_names)}")
        if self.blocker_assignments:
            assigns = [f"{b}->{a}" for b, a in self.blocker_assignments.items()]
            parts.append(f"blocks: {', '.join(assigns)}")
        if self.scry_position:
            parts.append(f"scry {self.scry_position}")
        return " | ".join(parts)


@dataclass
class ActionPlan:
    """A complete plan of actions to execute."""
    actions: list[GameAction] = field(default_factory=list)
    overall_strategy: str = ""
    voice_advice: str = ""
    trigger: str = ""
    turn_number: int = 0

    def __str__(self) -> str:
        lines = [f"Plan ({self.trigger}, turn {self.turn_number}): {self.overall_strategy}"]
        for i, action in enumerate(self.actions, 1):
            lines.append(f"  {i}. {action}")
        return "\n".join(lines)


# JSON schema embedded in the system prompt for constrained output
ACTION_SCHEMA = """{
  "actions": [{
    "action_type": "play_land|cast_spell|declare_attackers|declare_blockers|select_target|select_n|modal_choice|mulligan_keep|mulligan_mull|pass_priority|resolve|draft_pick|click_button|activate_ability|order_blockers|assign_damage|order_combat_damage|pay_costs|search_library|distribute|numeric_input|choose_starting_player|select_replacement|select_counters|casting_options|order_triggers",
    "card_name": "string (card name, empty if not applicable)",
    "target_names": ["string (target card/player names)"],
    "attacker_names": ["string (creature names to attack with)"],
    "blocker_assignments": {"blocker_name": "attacker_name"},
    "modal_index": 0,
    "select_card_names": ["string (cards to select for scry/discard/etc)"],
    "scry_position": "top|bottom (only for scry decisions)",
    "numeric_value": 0,
    "distribution": {"target_name": 0},
    "play_or_draw": "play|draw",
    "reasoning": "string (brief explanation)"
  }],
  "overall_strategy": "string (1-sentence strategy summary)",
  "voice_advice": "string (1-2 sentence spoken coaching advice for the player, concise and actionable)"
}"""


AUTOPILOT_SYSTEM_PROMPT = """You are an expert MTG Arena autopilot. Given the current game state and trigger,
output a JSON action plan that the autopilot will execute by clicking in the MTGA client.

CRITICAL RULES:
- ONLY suggest actions that appear in the "Legal:" line. Never hallucinate actions.
- If a pending decision is active, resolve that decision instead of proposing a new cast/play from hand.
- ONE ACTION PER PLAN: You must suggest only ONE card to play or ONE button to click per JSON response.
- EXCEPTION: For "declare_attackers" or "declare_blockers", provide the full attacker/blocker set in one action. Do NOT add a separate "done" click action (the executor handles confirmation).
- DO NOT sequence plays (e.g. do not suggest "play land" AND "cast spell"). Suggest the land, wait for the next trigger, then suggest the spell.
- Creatures tagged [SS] have SUMMONING SICKNESS — they CANNOT attack.
- Tokens are prefixed with * (e.g. "*Soldier"). Counters shown as [3P1P] = 3 +1/+1 counters.
- Output ONLY valid JSON matching the schema below. No markdown, no commentary outside JSON.
- Be decisive. Pick the best line of play.

DECISION-SPECIFIC RULES:
- choose_starting_player: Set play_or_draw to "play" (aggro) or "draw" (control).
- numeric_input: Set numeric_value (e.g. X for X spells). Check min/max in context.
- distribute: Set distribution dict mapping target names to amounts. Total must match.
- assign_damage: Order targets by priority (kill most important first).
- search_library: Use select_card_names for what to find. Consider mana curve and game plan.
- select_counters: Use select_card_names for which counters to select.

JSON SCHEMA:
""" + ACTION_SCHEMA


class ActionPlanner:
    """Converts game state + trigger into structured JSON action commands via LLM."""

    # Ring buffer size for recent planning diagnostics (kept for debug reports)
    _DIAG_BUFFER_SIZE = 10

    def __init__(self, backend: Any, timeout: float = 5.0):
        """Initialize the action planner.

        Args:
            backend: An LLMBackend instance (same interface as CoachEngine uses).
            timeout: Maximum seconds to wait for LLM response.
        """
        self._backend = backend
        self._timeout = timeout
        # Recent planning diagnostics ring buffer for debug reports
        self._recent_diagnostics: list[dict[str, Any]] = []
        # Turn-consistency memo: cache the last plan we produced for a turn
        # so subsequent priority windows in the same turn see it and are
        # nudged to stay committed to the same strategy instead of re-reasoning
        # from scratch. Cleared on turn change.
        self._turn_memo_turn: int = -1
        self._turn_memo: Optional[ActionPlan] = None
        # Executed actions this turn (by string repr); used to tell the LLM
        # "you already did X" in subsequent priority windows.
        self._turn_executed: list[str] = []

    def plan_actions(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: Optional[list[str]] = None,
        decision_context: Optional[dict[str, Any]] = None,
        legal_actions_raw: Optional[list[dict]] = None,
    ) -> ActionPlan:
        """Plan actions for the current game state.

        Args:
            game_state: Full game state dict from get_game_state().
            trigger: The trigger that caused this planning (e.g. "new_turn").
            legal_actions: Optional pre-computed legal actions list.
            decision_context: Optional decision context from game state.
            legal_actions_raw: Optional raw GRE action dicts for GRE matching.

        Returns:
            ActionPlan with structured actions to execute.
        """
        start = time.perf_counter()
        effective_legal_actions = self._filter_legal_actions_for_planning(
            game_state, legal_actions or []
        )

        # Clear turn memo on turn change, and record what was executed in the
        # previous window so the next prompt sees it.
        current_turn = game_state.get("turn", {}).get("turn_number", 0)
        if current_turn != self._turn_memo_turn:
            if self._turn_memo:
                logger.debug(
                    f"Turn changed ({self._turn_memo_turn} -> {current_turn}), "
                    "clearing planner memo"
                )
            self._turn_memo = None
            self._turn_memo_turn = current_turn
            self._turn_executed = []
        # Mark the last-memoized action as executed if we're in a new priority
        # window of the same turn (the action we proposed last time either
        # fired or became irrelevant — either way, don't re-propose it).
        elif self._turn_memo and self._turn_memo.actions:
            last = self._turn_memo.actions[0]
            last_repr = f"{last.action_type.value}({last.card_name})" if last.card_name else last.action_type.value
            if last_repr not in self._turn_executed:
                self._turn_executed.append(last_repr)

        diag: dict[str, Any] = {
            "timestamp": time.time(),
            "trigger": trigger,
            "turn": current_turn,
            "legal_actions": legal_actions,
            "effective_legal_actions": effective_legal_actions,
            "decision_context_type": (decision_context or {}).get("type"),
            "bridge_request": game_state.get("_bridge_request_type"),
        }

        # Build the prompt
        system_prompt = AUTOPILOT_SYSTEM_PROMPT
        user_message = self._build_action_prompt(
            game_state, trigger, effective_legal_actions, decision_context
        )
        diag["prompt_len"] = len(user_message)

        # Call LLM with enforced timeout.
        # Use temperature=0 for deterministic planning — avoids different
        # actions being proposed across priority windows in the same turn.
        # Backends that don't accept the kwarg (older local backends) fall
        # back to their default temperature.
        import concurrent.futures

        def _complete() -> str:
            try:
                return self._backend.complete(
                    system_prompt, user_message, 400, temperature=0.0
                )
            except TypeError:
                return self._backend.complete(system_prompt, user_message)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_complete)
                response = future.result(timeout=self._timeout)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Action planning took {elapsed:.0f}ms")
        except concurrent.futures.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(f"Action planning timed out after {elapsed:.0f}ms (limit {self._timeout}s)")
            diag["failure"] = "timeout"
            diag["elapsed_ms"] = elapsed
            self._record_diagnostic(diag)
            return ActionPlan(trigger=trigger)
        except Exception as e:
            logger.error(f"Action planning LLM call failed: {e}")
            diag["failure"] = f"llm_error: {e}"
            self._record_diagnostic(diag)
            return ActionPlan(trigger=trigger)

        diag["elapsed_ms"] = (time.perf_counter() - start) * 1000
        diag["response_len"] = len(response) if response else 0
        diag["response_preview"] = (response or "")[:300]

        # Parse response — pass bridge request + decision context so we can
        # accept decision-type actions (select_n, search_library, etc.) even
        # when legal_actions is stale.
        plan = self._parse_response(
            response,
            effective_legal_actions,
            decision_context=decision_context,
            bridge_request=game_state.get("_bridge_request_type"),
        )
        plan.trigger = trigger
        plan.turn_number = game_state.get("turn", {}).get("turn_number", 0)

        if not plan.actions:
            logger.warning(
                f"Planner JSON parse returned 0 actions for trigger={trigger}, "
                f"legal_actions={effective_legal_actions}, response={response[:200] if response else 'None'!r}"
            )
            fallback = self._fallback_plan(response, effective_legal_actions)
            fallback.trigger = trigger
            fallback.turn_number = plan.turn_number
            if fallback.actions:
                logger.info(f"Planner fallback recovered: {fallback.overall_strategy}")
                plan = fallback
            else:
                diag["failure"] = "empty_plan"
                logger.warning(
                    f"Planner fallback also failed: trigger={trigger}, "
                    f"{len(effective_legal_actions)} legal actions"
                )

        # Attach GRE action refs if raw actions are available. If the bridge says
        # the current request has no actions (e.g. PayCostsReq), do not fall back
        # to stale ActionsAvailable actions from the previous window.
        raw = self._resolve_raw_actions_for_matching(game_state, legal_actions_raw)
        if raw and plan.actions:
            self._attach_gre_refs(plan, raw, game_state)

        diag["planned_actions"] = len(plan.actions)
        diag["strategy"] = plan.overall_strategy
        self._record_diagnostic(diag)

        # Cache the plan as the turn memo so subsequent priority windows see
        # it and stay consistent. Skip caching for mulligan and pass-only
        # plans (no commitment to preserve).
        if plan.actions and plan.actions[0].action_type.value not in ("pass_priority", "mulligan_keep", "mulligan_mull"):
            self._turn_memo = plan
            self._turn_memo_turn = current_turn

        logger.info(f"Planned {len(plan.actions)} actions: {plan.overall_strategy}")
        return plan

    @staticmethod
    def _resolve_raw_actions_for_matching(
        game_state: dict[str, Any],
        legal_actions_raw: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        """Choose the freshest raw GRE actions for ref attachment."""
        if legal_actions_raw is not None:
            return legal_actions_raw

        bridge_request = game_state.get("_bridge_request_type")
        bridge_request_class = game_state.get("_bridge_request_class")
        bridge_actions = game_state.get("_bridge_actions")

        if (
            bridge_request
            and bridge_request not in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
            and bridge_request_class not in _ACTIONS_AVAILABLE_BRIDGE_REQUESTS
        ):
            return bridge_actions or []

        return bridge_actions or game_state.get("legal_actions_raw") or []

    def _attach_gre_refs(
        self,
        plan: ActionPlan,
        raw_actions: list[dict],
        game_state: dict[str, Any],
    ) -> None:
        """Attempt to match each action in the plan to a raw GRE action."""
        try:
            from arenamcp.gre_action_matcher import match_action_to_gre
        except ImportError:
            logger.debug("gre_action_matcher not available, skipping GRE ref attachment")
            return

        # Build game_objects lookup: instance_id -> object dict
        game_objects: dict[int, dict] = {}
        zones = game_state.get("zones", {})
        for zone_key in ("battlefield", "my_hand", "stack", "graveyard", "exile", "command"):
            for obj in zones.get(zone_key, []):
                if isinstance(obj, dict):
                    iid = obj.get("instance_id", 0)
                    if iid:
                        game_objects[iid] = obj

        # Build a scryfall lookup helper
        def scryfall_lookup(grp_id: int) -> Optional[str]:
            try:
                from arenamcp import server
                info = server.get_card_info(grp_id)
                return info.get("name")
            except Exception as e:
                logger.debug(f"Scryfall lookup failed for grp_id={grp_id}: {e}")
                return None

        for action in plan.actions:
            ref = match_action_to_gre(action, raw_actions, game_objects, scryfall_lookup)
            if ref:
                action.gre_action_ref = ref
                logger.debug(f"Attached GRE ref to {action.action_type.value}: {ref.to_dict()}")
            else:
                logger.debug(f"No GRE ref found for {action.action_type.value} ({action.card_name})")

    @staticmethod
    def _normalize_action_text(text: str) -> str:
        return re.sub(r"\s*\[[^\]]+\]\s*$", "", (text or "").strip())

    def _filter_legal_actions_for_planning(
        self,
        game_state: dict[str, Any],
        legal_actions: list[str],
    ) -> list[str]:
        """Remove actions the planner must not choose."""
        if not legal_actions:
            return []

        filtered: list[str] = []
        mana_pool = None
        rules_engine_cls = None

        for legal_action in legal_actions:
            lower = legal_action.lower()
            if lower.startswith("cast "):
                if "[ok]" not in lower:
                    continue

                if mana_pool is None:
                    try:
                        from arenamcp.rules_engine import RulesEngine

                        rules_engine_cls = RulesEngine
                        local_seat = next(
                            (
                                p.get("seat_id")
                                for p in game_state.get("players", [])
                                if p.get("is_local")
                            ),
                            None,
                        )
                        if local_seat is not None:
                            mana_pool = RulesEngine._get_mana_pool(game_state, local_seat)
                        else:
                            mana_pool = {}
                    except Exception:
                        mana_pool = {}

                if mana_pool and rules_engine_cls is not None:
                    card_name = self._normalize_action_text(legal_action).replace("Cast ", "").strip()
                    card_cost = ""
                    for card in game_state.get("hand", []):
                        if card.get("name", "").lower() == card_name.lower():
                            card_cost = card.get("mana_cost", "")
                            break
                    if card_cost and not rules_engine_cls._can_afford(card_cost, mana_pool):
                        logger.info(
                            "Filtering unaffordable spell: %s (cost=%s)",
                            card_name,
                            card_cost,
                        )
                        continue

            filtered.append(legal_action)

        return filtered or legal_actions

    def _build_action_prompt(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: Optional[list[str]] = None,
        decision_context: Optional[dict[str, Any]] = None,
    ) -> str:
        """Build the user message with formatted game context.

        Reuses the compact format from CoachEngine._format_game_context().
        """
        # Import and use CoachEngine's formatter for consistency
        try:
            from arenamcp.coach import (
                CoachEngine,
                _format_bounded_json_for_prompt,
                _format_raw_gre_events_for_prompt,
            )
            # Create a temporary instance just for formatting
            formatter = CoachEngine.__new__(CoachEngine)
            context = formatter._format_game_context(game_state)
        except Exception as e:
            logger.warning(f"Failed to use CoachEngine formatter: {e}")
            context = self._fallback_format(game_state)
            _format_bounded_json_for_prompt = None
            _format_raw_gre_events_for_prompt = None

        # Build trigger description
        trigger_descriptions = {
            "new_turn": "Your turn started (Main Phase 1). Plan your plays.",
            "opponent_turn": "Opponent's turn. Plan responses if you have instants.",
            "combat_attackers": "Declare attackers phase. Choose which creatures attack.",
            "combat_blockers": "Opponent is attacking. Assign blockers.",
            "priority_gained": "You have priority. Respond or pass.",
            "spell_resolved": "A spell resolved. What's next?",
            "decision_required": "A game decision is pending. Make your choice.",
            "mulligan": "Mulligan decision. Keep or mulligan?",
            "land_played": "Land played. What's the next play?",
            # New triggers for expanded decision types
            "assign_damage": "Assign combat damage to blockers/attackers. Order by priority.",
            "order_combat_damage": "Order combat damage assignment. Prioritize lethal.",
            "pay_costs": "Pay costs for a spell or ability. Choose mana sources wisely.",
            "search_library": "Search your library. Pick the best card for the situation.",
            "distribute": "Distribute damage/counters among targets.",
            "numeric_input": "Choose a number (X spell, pay life, etc.).",
            "choose_starting_player": "Won the die roll. Choose to play or draw.",
            "select_replacement": "Multiple replacement effects. Choose which applies first.",
            "select_counters": "Select counters to add or remove.",
            "casting_options": "Choose alternative casting cost (Foretell, Flashback, etc.).",
            "order_triggers": "Order triggered abilities on the stack.",
        }
        trigger_desc = trigger_descriptions.get(trigger, f"Trigger: {trigger}")

        parts = [
            f"TRIGGER: {trigger_desc}",
            "",
            context,
        ]

        # Turn-consistency context: if we already planned something this turn,
        # show the LLM what we promised and what's been executed, so it stays
        # committed to the same strategy instead of re-reasoning from scratch
        # (avoids the "play Forest → then cast Giant instead of Ogre" flip).
        current_turn = game_state.get("turn", {}).get("turn_number", 0)
        if self._turn_memo and self._turn_memo_turn == current_turn:
            consistency_lines = [
                "\nTURN CONSISTENCY CONTEXT:",
                f"- Earlier this turn you planned: {self._turn_memo.overall_strategy}",
            ]
            if self._turn_memo.voice_advice:
                consistency_lines.append(f"- You told the player: \"{self._turn_memo.voice_advice}\"")
            if self._turn_executed:
                consistency_lines.append(
                    f"- Already executed this turn: {', '.join(self._turn_executed)}"
                )
            consistency_lines.append(
                "- STAY COMMITTED to the strategy above unless the board has "
                "materially changed (opponent response, unexpected trigger, "
                "lethal threat). Do NOT flip-flop to a different plan just "
                "because you could."
            )
            parts.append("\n".join(consistency_lines))

        if legal_actions:
            parts.append(f"\nLegal: {', '.join(legal_actions)}")

        if decision_context:
            parts.append(f"\nDecision: {json.dumps(decision_context, indent=2)}")

        # Bridge request type helps the LLM understand the exact GRE request
        bridge_req = game_state.get("_bridge_request_type")
        if bridge_req:
            parts.append(f"\nGRE Request Type: {bridge_req}")
        bridge_request_class = game_state.get("_bridge_request_class")
        if bridge_request_class and bridge_request_class != bridge_req:
            parts.append(f"\nGRE Request Class: {bridge_request_class}")
        bridge_request_payload = game_state.get("_bridge_request_payload")
        if bridge_request_payload:
            if _format_bounded_json_for_prompt is not None:
                payload_text = _format_bounded_json_for_prompt(bridge_request_payload)
            else:
                payload_text = json.dumps(bridge_request_payload, separators=(",", ":"))
            parts.append(f"\nGRE Request Payload: {payload_text}")
        raw_gre_events = game_state.get("raw_gre_events") or []
        if raw_gre_events:
            if _format_raw_gre_events_for_prompt is not None:
                raw_events_text = _format_raw_gre_events_for_prompt(raw_gre_events)
            else:
                raw_events_text = json.dumps(raw_gre_events[-3:], separators=(",", ":"))
            parts.append(f"\nRecent GRE: {raw_events_text}")

        parts.append("\nRespond with ONLY a JSON action plan matching the schema.")

        return "\n".join(parts)

    def _fallback_format(self, game_state: dict[str, Any]) -> str:
        """Fallback game state formatter if CoachEngine is unavailable."""
        parts = []

        # Turn info
        turn = game_state.get("turn", {})
        parts.append(
            f"Turn {turn.get('turn_number', '?')} | "
            f"Phase: {turn.get('phase', '?')} | "
            f"Step: {turn.get('step', '')} | "
            f"Active: Seat {turn.get('active_player', '?')}"
        )

        # Players with damage tracking
        damage_taken = game_state.get("damage_taken", {})
        for p in game_state.get("players", []):
            marker = "(YOU)" if p.get("is_local") else "(OPP)"
            seat = p.get("seat_id", "?")
            life = p.get("life", p.get("life_total", "?"))
            dmg = damage_taken.get(str(seat), damage_taken.get(seat, 0))
            dmg_str = f" (taken {dmg} dmg)" if dmg else ""
            parts.append(f"Seat {seat} {marker}: Life={life}{dmg_str}")

        # Hand
        hand = game_state.get("hand", [])
        if hand:
            card_names = [c.get("name", "?") for c in hand]
            parts.append(f"Hand: {', '.join(card_names)}")

        # Battlefield with token/counter annotations
        battlefield = game_state.get("battlefield", [])
        if battlefield:
            bf_names = []
            for c in battlefield:
                name = c.get("name", "?")
                kind = c.get("object_kind", "")
                if kind == "TOKEN":
                    name = f"*{name}"
                counters = c.get("counters", {})
                if counters:
                    cparts = [f"{v}{k.replace('CounterType_','')[:4]}" for k, v in counters.items()]
                    name += f" [{','.join(cparts)}]"
                bf_names.append(name)
            parts.append(f"Battlefield: {', '.join(bf_names)}")

        # Recent events
        recent = game_state.get("recent_events", [])
        if recent:
            event_strs = []
            for evt in recent[-5:]:
                etype = evt.get("type", "")
                if etype == "damage_dealt":
                    event_strs.append(f"{evt.get('source','?')} dealt {evt.get('amount',0)} damage")
                elif etype == "zone_transfer":
                    event_strs.append(f"{evt.get('card','?')} moved zones")
                elif etype == "counter_added":
                    event_strs.append(f"+{evt.get('amount',1)} counter on {evt.get('card','?')}")
                elif etype == "token_created":
                    event_strs.append(f"Token created: {evt.get('card','?')}")
                elif etype == "card_revealed":
                    event_strs.append(f"Revealed: {evt.get('card','?')}")
                elif etype == "controller_changed":
                    event_strs.append(f"{evt.get('card','?')} changed controller")
            if event_strs:
                parts.append(f"Recent: {'; '.join(event_strs)}")

        return "\n".join(parts)

    def _parse_response(
        self,
        response: str,
        legal_actions: list[str],
        decision_context: Optional[dict[str, Any]] = None,
        bridge_request: Optional[str] = None,
    ) -> ActionPlan:
        """Parse LLM response into an ActionPlan.

        Handles markdown fences, trailing commas, missing fields, and
        other common LLM output quirks.
        """
        plan = ActionPlan()

        # Extract JSON from markdown fences if present
        json_str = response.strip()
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", json_str, re.DOTALL)
        if fence_match:
            json_str = fence_match.group(1).strip()

        # Remove trailing commas before } or ]
        json_str = re.sub(r",\s*([\]}])", r"\1", json_str)

        # Try to parse
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse action plan JSON: {e}")
            logger.debug(f"Raw response: {response[:500]}")
            return plan

        # Extract overall strategy and voice advice
        plan.overall_strategy = data.get("overall_strategy", "")
        plan.voice_advice = data.get("voice_advice", "")

        # Parse actions
        for action_data in data.get("actions", []):
            action = self._parse_action(action_data)
            if action and self._is_action_legal(
                action, legal_actions, decision_context, bridge_request
            ):
                plan.actions.append(action)
            elif action:
                logger.warning(
                    "Dropping illegal planner action: %s (%s) bridge_request=%r "
                    "decision=%r not in %s",
                    action.action_type.value,
                    action.card_name,
                    bridge_request,
                    (decision_context or {}).get("type"),
                    legal_actions,
                )

        return plan

    def _record_diagnostic(self, diag: dict[str, Any]) -> None:
        """Append a planning diagnostic entry to the ring buffer."""
        self._recent_diagnostics.append(diag)
        if len(self._recent_diagnostics) > self._DIAG_BUFFER_SIZE:
            self._recent_diagnostics.pop(0)

    def get_recent_diagnostics(self) -> list[dict[str, Any]]:
        """Return recent planning diagnostics for debug reports."""
        return list(self._recent_diagnostics)

    def _fallback_plan(self, response: str, legal_actions: list[str]) -> ActionPlan:
        """Fallback parser for non-JSON backend output.

        Works across backends that may return plain text / markdown advice.
        """
        plan = ActionPlan()
        if not legal_actions:
            logger.debug("Planner fallback: no legal actions available")
            return plan

        selected = self._match_legal_action_in_text(response, legal_actions)
        if not selected:
            logger.debug("Planner fallback: no text match in response, trying heuristic")
            selected = self._pick_preferred_legal_action(legal_actions)
        if not selected:
            logger.debug(f"Planner fallback: heuristic also failed, legal={legal_actions}")
            return plan

        action = self._legal_action_to_action(selected)
        if not action:
            logger.debug(f"Planner fallback: could not convert legal action {selected!r}")
            return plan

        plan.actions = [action]
        # Produce human-readable strategy AND voice advice so the TTS/overlay
        # have natural output instead of the debug "Fallback from legal action: X"
        # string. We also tag the strategy with [auto-pick] so bug reports can
        # still distinguish fallback cases, but the user-facing advice is clean.
        plan.overall_strategy = f"[auto-pick] {selected}"
        plan.voice_advice = self._humanize_legal_action(selected)
        return plan

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
        if low.startswith("attack with:"):
            name = s.split(":", 1)[1].strip()
            return f"Attack with {name}."
        if low.startswith("declare attackers:"):
            return f"Attack with {s.split(':', 1)[1].strip()}."
        if low.startswith("block with:"):
            return f"Block with {s.split(':', 1)[1].strip()}."
        if low.startswith("activate "):
            return f"Activate {s[9:].strip()}."
        if "done" in low and "confirm" in low:
            return "Confirm."
        if low.startswith("pass"):
            return "Pass."
        return s

    @staticmethod
    def _match_legal_action_in_text(response: str, legal_actions: list[str]) -> Optional[str]:
        text = (response or "").lower()
        if not text:
            return None
        # Prefer longer legal actions to avoid matching generic fragments first.
        for legal in sorted(legal_actions, key=len, reverse=True):
            if legal.lower() in text:
                return legal
        return None

    @staticmethod
    def _pick_preferred_legal_action(legal_actions: list[str]) -> Optional[str]:
        """Pick a deterministic fallback action when model output is invalid."""
        if not legal_actions:
            return None

        def score(action: str) -> int:
            a = action.lower().strip()
            if a.startswith("play land:"):
                return 100
            if a.startswith("cast "):
                return 90
            if a.startswith("declare attackers:") or a.startswith("attack with:"):
                return 80
            if a.startswith("activate "):
                return 70
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

    def _legal_action_to_action(self, legal_action: str) -> Optional[GameAction]:
        """Convert a rules-engine legal action string into a GameAction."""
        act = self._normalize_action_text(legal_action)
        lower = act.lower()

        if lower.startswith("play land:"):
            return GameAction(
                action_type=ActionType.PLAY_LAND,
                card_name=act.split(":", 1)[1].strip(),
            )
        if lower.startswith("cast "):
            return GameAction(action_type=ActionType.CAST_SPELL, card_name=act[5:].strip())
        if lower.startswith("activate "):
            return GameAction(action_type=ActionType.ACTIVATE_ABILITY, card_name=act[9:].strip())
        if lower.startswith("declare attackers:"):
            names = [n.strip() for n in act.split(":", 1)[1].split(",") if n.strip()]
            return GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=names)
        if lower.startswith("attack with:"):
            names = [n.strip() for n in act.split(":", 1)[1].split(",") if n.strip()]
            return GameAction(action_type=ActionType.DECLARE_ATTACKERS, attacker_names=names)
        if lower.startswith("block with:"):
            name = act.split(":", 1)[1].strip()
            return GameAction(
                action_type=ActionType.DECLARE_BLOCKERS,
                blocker_assignments={name: ""} if name else {},
            )
        if lower.startswith("select target:"):
            return GameAction(
                action_type=ActionType.SELECT_TARGET,
                target_names=[act.split(":", 1)[1].strip()],
            )
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
    _DECISION_ACTION_TYPES = frozenset({
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
    })

    # Bridge request type → action type(s) that should be trusted for it
    _BRIDGE_REQUEST_ACCEPTS: dict[str, set[ActionType]] = {
        "SelectN":                 {ActionType.SELECT_N, ActionType.SELECT_TARGET, ActionType.SELECT_REPLACEMENT, ActionType.SELECT_COUNTERS},
        "SelectTargets":           {ActionType.SELECT_TARGET, ActionType.SELECT_N},
        "SelectReplacement":       {ActionType.SELECT_REPLACEMENT, ActionType.SELECT_N},
        "Search":                  {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchRequest":           {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "Distribution":            {ActionType.DISTRIBUTE},
        "DistributionReq":         {ActionType.DISTRIBUTE},
        "NumericInput":            {ActionType.NUMERIC_INPUT},
        "NumericInputReq":         {ActionType.NUMERIC_INPUT},
        "PayCosts":                {ActionType.PAY_COSTS},
        "PayCostsReq":             {ActionType.PAY_COSTS},
        "ChooseStartingPlayer":    {ActionType.CHOOSE_STARTING_PLAYER},
        "Mulligan":                {ActionType.MULLIGAN_KEEP, ActionType.MULLIGAN_MULL},
        "CastingTimeOption":       {ActionType.CASTING_OPTIONS, ActionType.MODAL_CHOICE, ActionType.NUMERIC_INPUT},
        "CastingTimeOptions":      {ActionType.CASTING_OPTIONS, ActionType.MODAL_CHOICE},
        "Group":                   {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "GroupReq":                {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "DeclareAttackers":        {ActionType.DECLARE_ATTACKERS},
        "DeclareBlockers":         {ActionType.DECLARE_BLOCKERS},
    }

    def _is_action_legal(
        self,
        action: GameAction,
        legal_actions: list[str],
        decision_context: Optional[dict[str, Any]] = None,
        bridge_request: Optional[str] = None,
    ) -> bool:
        """Require planner output to map to a current legal action.

        For decision-specific actions (SelectN, Search, etc.) the bridge
        request type is the authoritative signal — the legal_actions list
        can be stale from the prior ActionsAvailable window.
        """
        # If this is a decision-type action and the bridge tells us a
        # matching decision is pending, trust the planner's output.
        if action.action_type in self._DECISION_ACTION_TYPES and bridge_request:
            accepts = self._BRIDGE_REQUEST_ACCEPTS.get(bridge_request)
            if accepts and action.action_type in accepts:
                return True
        # Similarly, if decision_context.type matches one of the decision
        # family types, trust the planner.
        if action.action_type in self._DECISION_ACTION_TYPES and decision_context:
            ctx_type = str(decision_context.get("type") or "").lower()
            # e.g. "selection_generic", "search_library", "distribute"
            action_hint = action.action_type.value.lower()
            if action_hint in ctx_type or ctx_type in action_hint:
                return True
            if ctx_type == "selection_generic" and action.action_type in (
                ActionType.SELECT_N, ActionType.SELECT_TARGET,
                ActionType.SELECT_REPLACEMENT, ActionType.SEARCH_LIBRARY,
            ):
                return True

        if not legal_actions:
            return True

        # Combat declarations come in as one "Attack with: X" / "Block with: X"
        # string per creature, while the planner emits a full set in a single
        # GameAction. Validate the plan against the union of legal creature
        # names, not by exact-match on a single legal entry.
        if action.action_type in (ActionType.DECLARE_ATTACKERS, ActionType.DECLARE_BLOCKERS):
            legal_names: set[str] = set()
            in_combat_context = False
            # For the "Declare Attackers: A, B, C" summary line we can't
            # safely comma-split because card names themselves may contain
            # commas (e.g. "Lluwen, Imperfect Naturalist"). We prefer the
            # individual "Attack with: X" lines, which are one-name-per-line.
            has_individual_attack_lines = any(
                la.lower().strip().startswith("attack with:") for la in legal_actions
            )
            def _strip_attacker_annotations(tail: str) -> str:
                # Legal lines may carry a "(P/T)" suffix plus warning tags
                # like "[0 POWER ...]"; drop anything after the name+#N.
                cleaned = tail
                for marker in (" (", " ["):
                    cut = cleaned.find(marker)
                    if cut >= 0:
                        cleaned = cleaned[:cut]
                return cleaned

            for legal_action in legal_actions:
                low = legal_action.lower().strip()
                if action.action_type == ActionType.DECLARE_ATTACKERS:
                    if low.startswith("attack with:"):
                        # Exactly one creature per line; keep the full tail
                        # (including any commas in the name).
                        tail = legal_action.split(":", 1)[1]
                        tail = _strip_attacker_annotations(tail)
                        clean = self._normalize_action_text(tail).strip().lower()
                        if clean:
                            legal_names.add(clean)
                        in_combat_context = True
                    elif low.startswith("declare attackers:"):
                        in_combat_context = True
                        if not has_individual_attack_lines:
                            # Fallback comma-split only if no per-creature lines
                            tail = legal_action.split(":", 1)[1]
                            for name in tail.split(","):
                                name = _strip_attacker_annotations(name)
                                clean = self._normalize_action_text(name).strip().lower()
                                if clean:
                                    legal_names.add(clean)
                    elif "confirm attackers" in low:
                        in_combat_context = True
                else:
                    if low.startswith("block with:"):
                        tail = legal_action.split(":", 1)[1]
                        tail = _strip_attacker_annotations(tail)
                        clean = self._normalize_action_text(tail).strip().lower()
                        if clean:
                            legal_names.add(clean)
                        in_combat_context = True
                    elif "confirm blockers" in low:
                        in_combat_context = True

            if not in_combat_context:
                return False

            if action.action_type == ActionType.DECLARE_ATTACKERS:
                plan_names = {n.strip().lower() for n in action.attacker_names if n and n.strip()}
            else:
                plan_names = {
                    k.strip().lower() for k in action.blocker_assignments.keys() if k and k.strip()
                }
            return plan_names.issubset(legal_names)

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

    def _parse_action(self, data: dict[str, Any]) -> Optional[GameAction]:
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
