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
  "overall_strategy": "string (1-sentence strategy summary)"
}"""


AUTOPILOT_SYSTEM_PROMPT = """You are an expert MTG Arena autopilot. Given the current game state and trigger,
output a JSON action plan that the autopilot will execute by clicking in the MTGA client.

CRITICAL RULES:
- ONLY suggest actions that appear in the "Legal:" line. Never hallucinate actions.
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

    def __init__(self, backend: Any, timeout: float = 5.0):
        """Initialize the action planner.

        Args:
            backend: An LLMBackend instance (same interface as CoachEngine uses).
            timeout: Maximum seconds to wait for LLM response.
        """
        self._backend = backend
        self._timeout = timeout

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

        # Build the prompt
        system_prompt = AUTOPILOT_SYSTEM_PROMPT
        user_message = self._build_action_prompt(
            game_state, trigger, legal_actions, decision_context
        )

        # Call LLM with enforced timeout
        import concurrent.futures
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._backend.complete, system_prompt, user_message)
                response = future.result(timeout=self._timeout)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Action planning took {elapsed:.0f}ms")
        except concurrent.futures.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(f"Action planning timed out after {elapsed:.0f}ms (limit {self._timeout}s)")
            return ActionPlan(trigger=trigger)
        except Exception as e:
            logger.error(f"Action planning LLM call failed: {e}")
            return ActionPlan(trigger=trigger)

        # Parse response
        plan = self._parse_response(response, legal_actions or [])
        plan.trigger = trigger
        plan.turn_number = game_state.get("turn", {}).get("turn_number", 0)

        if not plan.actions:
            fallback = self._fallback_plan(response, legal_actions or [])
            fallback.trigger = trigger
            fallback.turn_number = plan.turn_number
            if fallback.actions:
                plan = fallback

        # Attach GRE action refs if raw actions are available
        raw = legal_actions_raw or game_state.get("legal_actions_raw")
        if raw and plan.actions:
            self._attach_gre_refs(plan, raw, game_state)

        logger.info(f"Planned {len(plan.actions)} actions: {plan.overall_strategy}")
        return plan

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
            from arenamcp.coach import CoachEngine
            # Create a temporary instance just for formatting
            formatter = CoachEngine.__new__(CoachEngine)
            context = formatter._format_game_context(game_state)
        except Exception as e:
            logger.warning(f"Failed to use CoachEngine formatter: {e}")
            context = self._fallback_format(game_state)

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

        if legal_actions:
            parts.append(f"\nLegal: {', '.join(legal_actions)}")

        if decision_context:
            parts.append(f"\nDecision: {json.dumps(decision_context, indent=2)}")

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

    def _parse_response(self, response: str, legal_actions: list[str]) -> ActionPlan:
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

        # Extract overall strategy
        plan.overall_strategy = data.get("overall_strategy", "")

        # Parse actions
        for action_data in data.get("actions", []):
            action = self._parse_action(action_data)
            if action:
                plan.actions.append(action)

        return plan

    def _fallback_plan(self, response: str, legal_actions: list[str]) -> ActionPlan:
        """Fallback parser for non-JSON backend output.

        Works across backends that may return plain text / markdown advice.
        """
        plan = ActionPlan()
        if not legal_actions:
            return plan

        selected = self._match_legal_action_in_text(response, legal_actions)
        if not selected:
            selected = self._pick_preferred_legal_action(legal_actions)
        if not selected:
            return plan

        action = self._legal_action_to_action(selected)
        if not action:
            return plan

        plan.actions = [action]
        plan.overall_strategy = f"Fallback from legal action: {selected}"
        return plan

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
        act = (legal_action or "").strip()
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
        if lower.startswith("select target:"):
            return GameAction(
                action_type=ActionType.SELECT_TARGET,
                target_names=[act.split(":", 1)[1].strip()],
            )
        if "choose: play" in lower:
            return GameAction(action_type=ActionType.CHOOSE_STARTING_PLAYER, play_or_draw="play")
        if "choose: draw" in lower:
            return GameAction(action_type=ActionType.CHOOSE_STARTING_PLAYER, play_or_draw="draw")
        if "done" in lower:
            return GameAction(action_type=ActionType.CLICK_BUTTON, card_name="done")
        if "resolve" in lower:
            return GameAction(action_type=ActionType.RESOLVE)
        if "pass" in lower or "wait" in lower:
            return GameAction(action_type=ActionType.PASS_PRIORITY)

        return None

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
