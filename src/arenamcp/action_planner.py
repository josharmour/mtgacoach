"""Structured LLM Action Planning for Autopilot Mode.

Converts game state + trigger into structured JSON action commands
via a separate LLM call with a constrained schema prompt.
"""

import concurrent.futures
import inspect
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from arenamcp.backend_health import is_backend_error_text
from arenamcp.decisions import expand_target_selection
from arenamcp.match_context import STRATEGIC_POLICY, prepare_match_context, with_deck_reference
from arenamcp.play_safety import filter_play_options, find_source, shrink_note, unsafe_play_reason
from arenamcp.target_effects import (
    effect_mentions_harm,
    source_effect_text,
    target_effect_has_polarity,
    target_effect_is_harmful,
)
from arenamcp.ward import (
    targeting_mana,
    untapped_land_drop,
    ward_cast_note,
    ward_of,
    ward_payable,
    ward_trigger_source,
)

logger = logging.getLogger(__name__)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ability_rules_text(ability_grp_id: int) -> str:
    """Arena rules text of one ability id (e.g. a modal spell's chosen mode), or ""."""
    try:
        from arenamcp.card_db import get_card_database

        return str(get_card_database().get_ability_text(int(ability_grp_id)) or "")
    except Exception:
        return ""


# Sentinel returned by plan_decision_options when the safe move is to
# DECLINE the pending window (cancel / pause for manual) rather than pick
# any option. Live 2026-07-05: a harmful SelectTargets whose only legal
# candidates were the user's own permanents must not be auto-submitted.
DECLINE_DECISION = "__decline__"

# Internal sentinel for an "up to N" SelectTargets answered with no (more)
# targets. plan_decision_options returns it to callers as an empty list.
NO_TARGETS_DECISION = "__no_targets__"

# Blocks leaving this much life or less are replaced when the solver's blocks
# keep at least BLOCK_DANGER_MARGIN more (see _check_block_survival).
BLOCK_DANGER_LIFE = 5
BLOCK_DANGER_MARGIN = 5


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
    attacker_targets: dict[str, str] = field(default_factory=dict)
    blocker_assignments: dict[str, str] = field(default_factory=dict)
    modal_index: int = 0
    select_card_names: list[str] = field(default_factory=list)
    scry_position: str = ""  # "top" or "bottom"
    numeric_value: int = 0  # For numeric_input (X spells, pay life)
    distribution: dict[str, int] = field(default_factory=dict)  # target_name -> amount
    play_or_draw: str = ""  # "play" or "draw"
    reasoning: str = ""
    confidence: float = 1.0
    gre_action_ref: Any | None = None  # GREActionRef from gre_action_matcher
    # Land play via the MDFC back face ("Action: PlayMDFC" menu entries) —
    # the matcher must resolve it to the raw PlayMDFC action, not a plain
    # Play (#39, live 2026-07-06).
    mdfc: bool = False
    commander_return: bool = False
    # Bound at planning time, then revalidated against the live combat request.
    attacker_instance_ids: list[int] = field(default_factory=list)
    blocker_instance_assignments: dict[int, int] = field(default_factory=dict)

    def __str__(self) -> str:
        parts = [self.action_type.value]
        if self.card_name:
            parts.append(self.card_name)
        if self.target_names:
            parts.append(f"-> {', '.join(self.target_names)}")
        if self.attacker_names:
            parts.append(f"attackers: {', '.join(self.attacker_names)}")
        if self.attacker_targets:
            parts.append(
                "attack targets: "
                + ", ".join(f"{name} -> {target}" for name, target in self.attacker_targets.items())
            )
        if self.blocker_assignments:
            assigns = [f"{b}->{a}" for b, a in self.blocker_assignments.items()]
            parts.append(f"blocks: {', '.join(assigns)}")
        if self.scry_position:
            parts.append(f"scry {self.scry_position}")
        return " | ".join(parts)


# WP-0.4 fallback reason codes. A non-empty reason means a DEFAULT PATH, not the
# model, chose the action — so the resulting trajectory record must be excluded
# from positive training credit. Note "[pick-salvage]" is deliberately absent:
# that is the model's own pick index recovered from malformed JSON, so the
# decision is still the model's and the record is legitimate training data.
FALLBACK_AUTO_PICK = "planner_auto_pick"
FALLBACK_PREFLIGHT_LAND_DROP = "planner_preflight_land_drop"
FALLBACK_NO_ACTIONS = "planner_no_actions"
# The model server could not answer: the circuit breaker is open, the call
# timed out, or it failed with a transport/HTTP error or an error sentinel.
# An empty plan carrying this reason means "decide without the model now",
# not "nothing to do" (bug_20261006_185403: both looked identical).
FALLBACK_LLM_UNAVAILABLE = "llm_unavailable"


class LLMUnavailableError(RuntimeError):
    """The model was not asked (circuit open) or answered with an error sentinel."""


# Exception classes (matched by name to avoid importing the proxy/SDK here)
# that mean the model server failed rather than the answer being unusable.
_BACKEND_FAILURE_TYPES = frozenset(
    {
        "BackendError",
        "BackendUnavailable",
        "APIError",
        "APIConnectionError",
        "APITimeoutError",
        "APIStatusError",
    }
)


def _accepted_extras(complete: Any, extras: dict[str, Any]) -> dict[str, Any]:
    """The optional complete() keywords (call_class, first_token_timeout_s) this backend accepts.

    Dropping the ones it lacks keeps a missing keyword from reaching the
    TypeError fallbacks, which would also drop raise_on_error.
    """
    try:
        parameters = inspect.signature(complete).parameters
    except (TypeError, ValueError):
        return {}
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return dict(extras)
    return {key: value for key, value in extras.items() if key in parameters}


def llm_circuit_open(backend: Any) -> bool:
    """True while the proxy's shared circuit breaker reports the model server down.

    Only an explicit ``False`` from ``backend.available()`` counts, so
    backends without a breaker (and test doubles) are treated as available.
    """
    probe = getattr(backend, "available", None)
    try:
        state = probe() if callable(probe) else probe
    except Exception as error:  # a broken health probe must never block a decision
        logger.debug("backend availability probe failed: %s", error)
        return False
    return state is False


def model_unreachable(plan: Any, backend: Any = None) -> bool:
    """``plan`` is empty because the model server can't be reached now, not merely slow.

    True for a FALLBACK_LLM_UNAVAILABLE plan while ``backend``'s circuit
    breaker is open, or when the call failed with the circuit open, the
    proxy's 'model server unavailable' skip, or a connection error. A timeout
    or any other error is not: a slow planner says nothing about the separate
    vision model (2026-10-06 18:53: 30 s planning timeouts with vision fine).
    """
    if getattr(plan, "fallback_reason", "") != FALLBACK_LLM_UNAVAILABLE:
        return False
    if llm_circuit_open(backend):
        return True
    detail = str(getattr(plan, "fallback_detail", "") or "").lower()
    return any(mark in detail for mark in ("circuit open", "model server unavailable", "connect"))


def is_llm_unavailable_error(error: BaseException) -> bool:
    """A model-server failure (circuit open, timeout, transport, HTTP), not a bad answer."""
    if isinstance(
        error, (LLMUnavailableError, TimeoutError, ConnectionError, concurrent.futures.TimeoutError)
    ):
        return True
    return any(cls.__name__ in _BACKEND_FAILURE_TYPES for cls in type(error).__mro__)


# Strategy prefixes that used to be the ONLY fallback signal. Retained solely to
# classify plan objects built before ActionPlan.fallback_reason existed.
_LEGACY_FALLBACK_STRATEGY_TAGS = {
    "[auto-pick]": FALLBACK_AUTO_PICK,
    "[land-drop-first]": FALLBACK_PREFLIGHT_LAND_DROP,
}


def plan_fallback_reason(plan: Any) -> str:
    """Return why this plan is a fallback, or "" when the model decided it.

    Prefers the structured ``ActionPlan.fallback_reason`` and falls back to the
    legacy strategy-prefix sniff so older plan objects still classify correctly.
    """
    if plan is None or not getattr(plan, "actions", None):
        return FALLBACK_NO_ACTIONS
    reason = (getattr(plan, "fallback_reason", "") or "").strip()
    if reason:
        return reason
    strategy = (getattr(plan, "overall_strategy", "") or "").strip()
    for tag, legacy_reason in _LEGACY_FALLBACK_STRATEGY_TAGS.items():
        if strategy.startswith(tag):
            return legacy_reason
    return ""


@dataclass
class ActionPlan:
    """A complete plan of actions to execute."""

    actions: list[GameAction] = field(default_factory=list)
    overall_strategy: str = ""
    voice_advice: str = ""
    trigger: str = ""
    turn_number: int = 0
    # WP-0.4: why this plan did NOT come from the model ("" = the model decided).
    # The old signal was an "[auto-pick]" / "[land-drop-first]" prefix sniffed
    # out of overall_strategy, which fails silently: reword a strategy string
    # and fallbacks stop being identified. Bug reports and logs rely on this
    # structured tag to tell model decisions from deterministic ones.
    fallback_reason: str = ""
    # With FALLBACK_LLM_UNAVAILABLE: how the model call failed ("timeout",
    # "llm_unavailable: circuit open", "llm_error: <error>", ...), so callers can
    # tell a server that can't be reached from one that was only slow.
    fallback_detail: str = ""

    def spoken_actions(self) -> str:
        """Describe validated actions, never a separate model-generated recommendation."""
        from arenamcp.narration import combat_declaration, spoken_list, spoken_name

        lines = []
        for action in self.actions:
            kind = action.action_type
            if kind == ActionType.CAST_SPELL:
                line = f"Cast {action.card_name}"
            elif kind == ActionType.PLAY_LAND:
                line = f"Play {action.card_name}"
            elif kind == ActionType.ACTIVATE_ABILITY:
                line = f"Activate {action.card_name}"
            elif kind == ActionType.SELECT_TARGET:
                line = f"Target {', '.join(action.target_names)}"
            elif kind == ActionType.DECLARE_ATTACKERS:
                line = (
                    f"Attack with {spoken_list([spoken_name(name) for name in action.attacker_names])}"
                    if action.attacker_names
                    else "Don't attack"
                )
                if action.attacker_targets:
                    line = combat_declaration("Attack", action.attacker_targets)
                elif action.target_names:
                    line += " at " + spoken_list([spoken_name(name) for name in action.target_names])
            elif kind == ActionType.DECLARE_BLOCKERS:
                line = (
                    combat_declaration("Block", action.blocker_assignments)
                    if action.blocker_assignments
                    else "Don't block"
                )
            elif kind == ActionType.NUMERIC_INPUT:
                line = f"Choose {action.numeric_value}"
            elif kind == ActionType.PASS_PRIORITY:
                line = "Pass"
            elif kind == ActionType.SELECT_N:
                line = (
                    f"Select {', '.join(action.select_card_names)}"
                    if action.select_card_names
                    else "Confirm selection"
                )
            elif kind == ActionType.MULLIGAN_KEEP:
                line = "Keep this hand"
            elif kind == ActionType.MULLIGAN_MULL:
                line = "Mulligan"
            elif kind == ActionType.CLICK_BUTTON:
                if action.commander_return and action.card_name.lower() == "accept":
                    names = ", ".join(action.target_names) or "your commander"
                    line = f"Return {names} to the command zone"
                else:
                    line = f"Click {action.card_name or 'button'}"
            else:
                line = kind.value.replace("_", " ").capitalize()
            lines.append(f"{line}.")
        return " ".join(lines)

    @property
    def fallback(self) -> bool:
        """True when a default path, not the model, chose these actions."""
        return bool(self.fallback_reason)

    def __str__(self) -> str:
        lines = [f"Plan ({self.trigger}, turn {self.turn_number}): {self.overall_strategy}"]
        for i, action in enumerate(self.actions, 1):
            lines.append(f"  {i}. {action}")
        return "\n".join(lines)


@dataclass
class TurnPlanStep:
    """One user-visible play in a multi-step turn plan.

    Mana abilities, casting-time sub-decisions, and search prompts are
    intentionally NOT modeled here — those are mid-spell mechanical
    decisions, not plays. The status field is updated as steps execute.
    """

    action_type: str  # "play_land", "cast_spell", "activate_ability", "declare_attackers", etc.
    card_name: str = ""
    target_names: list[str] = field(default_factory=list)
    rationale: str = ""
    status: str = "pending"  # "pending" | "current" | "done" | "skipped"


@dataclass
class TurnPlan:
    """An ordered list of plays the autopilot intends to make this turn.

    Built once per turn (on the first non-trivial own-turn LLM call) and
    held until the turn changes or until divergence forces a replan. The
    UI displays this as a sticky panel and highlights progress as steps
    complete; per-priority-window single-action LLM calls still happen
    for execution. The plan is parallel context, not a replacement for
    the per-window planner.
    """

    turn_number: int
    steps: list[TurnPlanStep] = field(default_factory=list)
    current_idx: int = 0
    last_replanned_reason: str = ""

    def __post_init__(self) -> None:
        # First step starts as "current" so the UI has something to highlight
        # immediately, before any actions execute.
        if self.steps and 0 <= self.current_idx < len(self.steps):
            if self.steps[self.current_idx].status == "pending":
                self.steps[self.current_idx].status = "current"

    def remaining(self) -> list[TurnPlanStep]:
        return self.steps[self.current_idx :]

    def mark_current_done(self) -> None:
        if 0 <= self.current_idx < len(self.steps):
            self.steps[self.current_idx].status = "done"
            self.current_idx += 1
            if self.current_idx < len(self.steps):
                # Whichever step is now current gets the "current" marker so
                # the UI can highlight it. Don't touch already-skipped/done.
                if self.steps[self.current_idx].status == "pending":
                    self.steps[self.current_idx].status = "current"


# JSON schema embedded in the system prompt for constrained output
ACTION_SCHEMA = """{
  "actions": [{
    "pick": 0,
    "action_type": "play_land|cast_spell|declare_attackers|declare_blockers|select_target|select_n|modal_choice|mulligan_keep|mulligan_mull|pass_priority|resolve|draft_pick|click_button|activate_ability|order_blockers|assign_damage|order_combat_damage|pay_costs|search_library|distribute|numeric_input|choose_starting_player|select_replacement|select_counters|casting_options|order_triggers",
    "card_name": "string (card name, empty if not applicable)",
    "target_names": ["string (target card/player names)"],
    "attacker_names": ["string (creature names to attack with)"],
    "attacker_targets": {"attacker_name": "Opponent or exact legal planeswalker name [instance_id]"},
    "blocker_assignments": {"blocker_name": "attacker_name"},
    "modal_index": 0,
    "select_card_names": ["string (cards to select for scry/discard/etc)"],
    "scry_position": "top|bottom (only for scry decisions)",
    "numeric_value": 0,
    "distribution": {"target_name": 0},
    "play_or_draw": "play|draw",
    "reasoning": "string (brief explanation, max ~10 words)"
  }],
  "overall_strategy": "string (1-sentence strategy summary)",
  "voice_advice": "string (1-2 sentence spoken coaching advice for the player, concise and actionable)"
}
OMIT every field that does not apply. A menu pick needs ONLY "pick" (plus a
short "reasoning"); a structured action needs ONLY action_type + its own
fields. Never emit empty placeholder fields."""


def game_action_to_schema_json(action: Any) -> str:
    """Serialize a GameAction, dict, int (pick), or string into a JSON string conforming to ACTION_SCHEMA."""
    if isinstance(action, str):
        s = action.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict) and "actions" in parsed:
                    return s
                if isinstance(parsed, dict):
                    return json.dumps({"actions": [parsed]}, ensure_ascii=False)
            except Exception:
                pass
        return json.dumps({"actions": [{"reasoning": s}]}, ensure_ascii=False)

    if isinstance(action, int):
        return json.dumps({"actions": [{"pick": action}]}, ensure_ascii=False)

    if isinstance(action, dict):
        if "actions" in action:
            return json.dumps(action, ensure_ascii=False)
        return json.dumps({"actions": [action]}, ensure_ascii=False)

    if hasattr(action, "action_type"):
        action_type_val = (
            action.action_type.value if hasattr(action.action_type, "value") else str(action.action_type)
        )
        item: dict[str, Any] = {"action_type": action_type_val}
        if getattr(action, "card_name", ""):
            item["card_name"] = action.card_name
        if getattr(action, "target_names", None):
            item["target_names"] = action.target_names
        if getattr(action, "attacker_names", None):
            item["attacker_names"] = action.attacker_names
        if getattr(action, "attacker_targets", None):
            item["attacker_targets"] = action.attacker_targets
        if getattr(action, "blocker_assignments", None):
            item["blocker_assignments"] = action.blocker_assignments
        if getattr(action, "modal_index", 0):
            item["modal_index"] = action.modal_index
        if getattr(action, "select_card_names", None):
            item["select_card_names"] = action.select_card_names
        if getattr(action, "scry_position", ""):
            item["scry_position"] = action.scry_position
        if getattr(action, "numeric_value", 0):
            item["numeric_value"] = action.numeric_value
        if getattr(action, "distribution", None):
            item["distribution"] = action.distribution
        if getattr(action, "play_or_draw", ""):
            item["play_or_draw"] = action.play_or_draw
        if getattr(action, "reasoning", ""):
            item["reasoning"] = action.reasoning
        return json.dumps({"actions": [item]}, ensure_ascii=False)

    return json.dumps({"actions": [{"reasoning": str(action)}]}, ensure_ascii=False)


AUTOPILOT_SYSTEM_PROMPT = (
    """You are an MTG Arena autopilot. Given the game state and trigger, output a JSON action plan to execute.

RULES:
- PREFERRED OUTPUT: the "Legal:" menu is NUMBERED. For a simple play (cast,
  play land, activate, pass), answer with {"pick": <number>} — the number of
  the menu entry you choose. "pick" MUST be a bare integer (e.g. 3), never
  text. Respond in English only. Use structured action_type fields ONLY for
  combat declarations, targeting, distribution, and other decisions that
  need extra data.
- ONLY pick actions from the "Legal:" menu. Never invent actions. Never
  propose anything under EXCLUDED.
- MANA IS AUTO-PAID. The engine taps lands/rocks automatically when you cast
  or activate. NEVER try to tap a permanent for mana as your action — mana
  activations are not in your menu for exactly this reason.
- play_land ONLY when a "Play Land:" (or PlayMDFC) entry is in the menu.
  "#N" suffixes on battlefield names (e.g. "Forest #3") are display
  disambiguators for permanents already in play — never playable cards.
- TRUST [OK] tags. "[OK]" on a Cast or Activate Ability action means MTGA's mana solver already verified the cost is payable from your current mana — including hybrid, phyrexian, cost reductions, and affinity. Do NOT recompute the cost yourself or claim you "lack the mana": the Mana summary shows floating mana only, not what your untapped lands can produce. If an action is [OK] in the legal list, you CAN take it. Failing to play affordable [OK] actions wastes the turn. Every action in the legal list is offered by MTGA as currently legal — a missing [OK] on an Activate Ability does NOT mean it is unaffordable; tap/sacrifice activations cost no mana at all (e.g. cracking a fetch land — which also triggers landfall). The one exception is an explicit "[NEED:<cost>]" tag: that ability's mana cost was checked against your actual mana and you cannot pay it, so never recommend it.
- If a pending decision is shown, resolve that decision (not a new cast/play).
- ONE action per plan. Don't sequence (no "play land" + "cast spell").
- EXCEPTION: declare_attackers/declare_blockers carry the full set in one action — do NOT add a "done" click.
- ATTACK TARGETS: Supply attacker_names AND attacker_targets mapping each chosen creature to its legal recipient. When a planeswalker and the opponent are available, explicitly choose who each creature attacks; split attacks when useful. Compare killing the planeswalker (loyalty, abilities, future value) with lethal or pressure on the player. Damage to a planeswalker is NOT damage to the opponent. Prefer the recipient-aware combat search over the older player-only attack line when both appear. The search models visible combat approximately, not hidden tricks, triggered abilities, or future loyalty activations; never invent unknown loyalty. Say the chosen recipients in voice_advice; never leave that choice to a default UI target.
- ZERO-POWER ATTACKERS: A legal attacker need not be a useful attacker. Leave zero-power creatures untapped for mana or defense unless an actual attack trigger, required attack, pump plan, or damage-replacement effect gives attacking a concrete benefit. Flying alone does not make a zero-power attack useful.
- BLOCKERS: "Block with: X" names an eligible blocker, NOT a complete move. Supply action_type="declare_blockers" and blocker_assignments mapping each blocker to a named attacker. Never use a bare menu pick or an empty attacker name. Use an explicit empty mapping only when intentionally declaring no blocks.
- RESOURCE TRADES: For blocks, sacrifices or discards, compare the resulting board now AND after the earliest feasible recovery under the deck playbook. Price trigger payoffs, lost persistent resources, colored mana, tax and timing. A token label, legendary status, or generic material score cannot decide this comparison by itself.
- BLOCKING COST: Nonlethal damage is not free. Before declining blocks, compare remaining life and the next attack with sacrificing the least useful single blocker. Preserving an engine must enable a concrete recovery play, not just a vague ramp plan. Respect mana spending restrictions: mana usable only for creature abilities cannot help cast spells. A large hit can justify losing a support creature while retaining the stronger mana engine.
- ANIMATION: Making an artifact or land a creature does not untap it or make it enter again. A tapped source, including one tapped by the payment solution, cannot become an attacker just by animating it. Creatures that entered this turn need haste to attack; haste-on-entry triggers do not retroactively give older creatures haste. Require a concrete benefit before paying for temporary animation, and use current card types rather than leftover power/toughness to decide whether it is already a creature.
- EQUIPMENT: Reassess haste-granting equipment after its wearer taps or new creatures enter. Move it to an untapped summoning-sick creature when that enables a useful attack or tap ability now. A tapped wearer can still deserve shroud/hexproof protection; do not move equipment just because another creature is untapped. Equip only when Arena offers the activation; do not shuffle it endlessly or float mana without a concrete use.
- [SS] = summoning sick (can't attack). * prefix = token. [3P1P] = 3 +1/+1 counters. [UNBLOCKABLE] = can't be blocked right now (own text or a static grant); no blocker can stop its damage.
- If a TURN PLAN is shown, follow it. Stay committed to the locked turn plan unless a material change (opponent response, lethal threat, unexpected trigger) makes it obsolete.
- PROTECTIVE / LIFE-PAYMENT ABILITIES: Do NOT activate an ability that pays life (or other resources) for indestructible/hexproof/protection/a temporary buff unless there is a concrete threat to the creature right now — it is blocked by a creature that would kill it, it is targeted by removal/burn on the stack, or it must survive incoming damage this step. An unblocked attacker facing no removal needs no protection; activating "just in case" only loses life. When in doubt, pass.
- BOARD PRESSURE: When the opponent's board is wider than yours or grew by multiple creatures this turn, prioritize interaction (removal, profitable blocks, combat tricks) over advancing your own plan. At low life (below ~15), block with large or indestructible creatures — an indestructible blocker loses nothing by blocking.
- STACK: A "STACK (top resolves first):" block lists every spell and ability waiting to resolve, in resolution order — entry 1 resolves NEXT. Each entry shows its controller (YOU/OPP), its name, and "-> targets: ..." when it targets something. Read it before you act: if an OPP spell targets one of your permanents, decide whether to respond (counter it, or use the targeted permanent's ability in response so it isn't wasted) BEFORE it resolves — once it resolves the window is gone. Do not "respond" to your own spell that nothing is fighting over, and do not counter a spell that does not matter. When the stack is empty there is nothing to respond to.
- COMBAT SOLVER: "Computed optimal blocks:" and "Computed optimal attack:" are recommendations under approximate life/material/resource values, not proof of the best strategic move. Large searches may use a bounded heuristic. Prefer the recommendation, but account for combat tricks, removal, resource engines, your hand, and next-turn recovery; explain a concrete reason when deviating. Do not trade away multiple mana/draw/token engines merely to prevent nonlethal damage or kill one attacker: compare the damage with the resources and future blocks lost. Tokens with useful abilities are not disposable just because they are small. Survival takes precedence when taking the hit would be lethal. These lines never authorize a combat action absent from the Legal: menu; main-phase combat analysis is planning information only.
- VOICE CLARITY: when a play's payoff is on FUTURE turns (static effects,
  engines, ramp), voice_advice must say WHY NOW in a few words ("spare mana,
  nothing else to cast", "get the doubler down before next turn's counters") —
  a correct play that sounds random loses the user's trust.
- VOICE CLARITY: voice_advice must state ONE concrete action in plain spoken language and name the specific creatures involved. For blocks: either name the block ("Block their <attacker> with your <blocker>") or, if not blocking, say "Don't block — take <N> from <attacker>". For attacks, name who swings. Never give self-contradictory advice (e.g. saying both "let it trade" and "take the hit"), and never reference a creature that is not on the board. Your voice_advice must match your actual assignments; explain any deviation from the solver's recommendation.
- DESTRUCTIVE TARGETING (destroy, exile, deals damage, -N/-N): Ground each target in its current controller and instance ID. Prefer opposing threats. An intentional friendly target requires a concrete benefit (such as a death trigger or the friendly fighter in a fight effect); explicitly acknowledge that it is yours. No legal opponent targets does not justify destroying your own creature. Decline optional targeting when there is no useful target.
- TUTORS & SEARCH LIBRARY: When searching your library for an X-cost tutor (e.g. Green Sun's Zenith, Finale of Devastation, Chord of Calling), you may ONLY select a card whose mana value (CMC) is less than or equal to X. Never propose a card whose mana value exceeds X.
- LAND PLAY PRIORITY: On Precombat Main (Main1), if you have not played a land this turn and hold a land in hand, playing your land MUST be the FIRST step before casting spells that require that mana.
- Output ONLY JSON matching the schema. No prose, no markdown, no commentary.

ACTION_TYPE MAPPING (match Legal text → action_type):
- "Play Land: X"       → action_type=play_land,    card_name="X"
- "Cast X"             → action_type=cast_spell,   card_name="X"
- "Activate Ability: X"→ action_type=activate_ability, card_name="X"
- "Pass"               → action_type=pass_priority
NEVER use action_type=click_button for a card play — that's only for explicit UI buttons (Done, Skip, OK).

PER-DECISION FIELDS:
- choose_starting_player: play_or_draw = "play" or "draw".
- numeric_input: numeric_value within shown min/max.
- distribute: distribution dict; totals must match.
- assign_damage: order by priority (kill key targets first).
- search_library / select_counters: use select_card_names.
- casting_options (CastingTimeOptions window — "Cast normally" vs an
  alternative cost / mode while a spell is being cast): answer with
  {"pick": <menu number>} or action_type=modal_choice + modal_index.
  This is a mode pick, NOT numeric_input and NOT a new cast.

SCHEMA:
"""
    + ACTION_SCHEMA
)


# P0-8 (2026-07-05): plan_turn used to reuse AUTOPILOT_SYSTEM_PROMPT, whose
# "Output ONLY JSON matching the schema" hard-demanded the actions envelope
# while the user message asked for turn_plan — a model-dependent coin flip
# that yielded ZERO turn plans for match 1 (the 12B obeyed the system prompt
# all 3 times, perfectly valid JSON in the wrong shape).
TURN_PLAN_SYSTEM_PROMPT = """You are an MTG Arena turn planner. Given the game state, output the ordered list of user-visible plays for this whole turn.

RULES:
- TRUST [OK] tags: MTGA's mana solver already verified those costs are payable.
- Payable does not mean useful: budget X beyond the fixed cost and identify an eligible remaining tutor target before casting. Do not spend removal or sacrifice an ability source when its only targets are your own permanents.
- LAND PLAY PRIORITY: On Precombat Main (Main1), if you hold an unplayed land in hand, playing your land MUST be Step 1 of your turn plan before casting spells.
- Skip mana abilities, casting-time sub-decisions, and search prompts — list only user-visible plays (Play Land, Cast X, Activate X, Attack).
- Output ONLY a JSON object of this exact shape (no prose, no markdown):
{"turn_plan": {"steps": [{"action_type": "play_land|cast_spell|activate_ability|declare_attackers", "card_name": "string", "target_names": [], "rationale": "string (max 10 words)"}]}}
"""


def _strip_attacker_annotations(tail: str) -> str:
    """Drop trailing annotations from a "Attack with: X" / "Block with: X" tail.

    Legal lines may carry a "(P/T)" suffix plus warning tags like
    "[0 POWER ...]"; we keep only the card name + optional #N.
    """
    cleaned = tail
    for marker in (" (", " ["):
        cut = cleaned.find(marker)
        if cut >= 0:
            cleaned = cleaned[:cut]
    return cleaned


from arenamcp.action_legality import _ActionLegalityMixin


def linked_cast_note(game_state: dict[str, Any], meta: dict[str, Any], lookup: Any = None) -> str:
    """Rules for a cast whose card is not its source object (prepared spell, Adventure, back face).

    2026-10-05: "Cast Peer Review" (Prudent Fateseer's prepared spell) reached the
    model as a bare name; the spell is not in hand, so no zone listed its rules.
    """
    source = find_source(game_state, meta)
    cast_grp = meta.get("grpId")
    if not source or not cast_grp or not source.get("grp_id") or cast_grp == source.get("grp_id"):
        return ""
    if lookup is None:
        from arenamcp.match_context import _local_card

        def lookup(grp_id: int) -> dict[str, Any]:
            return _local_card(grp_id, int(time.monotonic() // 30))

    spell = lookup(int(cast_grp)) or {}
    if not spell.get("oracle_text"):
        return ""
    return " " + json.dumps(
        {
            "casts": spell.get("name"),
            "from": source.get("name"),
            "mana_cost": spell.get("mana_cost"),
            "type": spell.get("type_line"),
            "rules": " ".join(str(spell.get("oracle_text")).split())[:300],
        },
        ensure_ascii=False,
    )


POWER_ONLY_NOTE = (
    "  [-N/-0 lowers POWER only; toughness is unchanged, so it never kills. "
    "It only matters in this turn's combat]"
)


def power_only_note(text: Any) -> str:
    """Flag -N/-0 effects: 2026-10-05 the model cast Icy Reception's -5/-0 "to kill" a 2/3."""
    from arenamcp.play_safety import POWER_ONLY_DEBUFF

    return POWER_ONLY_NOTE if POWER_ONLY_DEBUFF.search(str(text or "")) else ""


class ActionPlanner(_ActionLegalityMixin):
    """Converts game state + trigger into structured JSON action commands via LLM."""

    # Ring buffer size for recent planning diagnostics (kept for debug reports)
    _DIAG_BUFFER_SIZE = 10

    def __init__(
        self,
        backend: Any,
        timeout: float = 5.0,
        land_drop_first: bool = True,
        deck_strategy_fn: Callable[[], str | None] | None = None,
        deck_playbook_fn: Callable[[], Any] | None = None,
    ):
        """Initialize the action planner.

        Args:
            backend: An LLMBackend instance (same interface as CoachEngine uses).
            timeout: Maximum seconds to wait for LLM response.
            land_drop_first: When True, deterministically play a land if one is
                legal and we've played 0 lands this turn — short-circuits the
                LLM. Set False for landfall-synergy decks.
            deck_strategy_fn: Read the current match's deck analysis, including
                results that arrive asynchronously after the planner is created.
        """
        self._backend = backend
        self._timeout = timeout
        self._land_drop_first = land_drop_first
        self._deck_strategy_fn = deck_strategy_fn
        self._deck_playbook_fn = deck_playbook_fn
        # Recent planning diagnostics ring buffer for debug reports
        self._recent_diagnostics: list[dict[str, Any]] = []
        # R3: the numbered menu shown in the most recent prompt; {"pick": N}
        # answers resolve against it (1-based).
        self._last_menu: list[str] = []
        # Turn-consistency memo: cache the last plan we produced for a turn
        # so subsequent priority windows in the same turn see it and are
        # nudged to stay committed to the same strategy instead of re-reasoning
        # from scratch. Cleared on turn change.
        self._turn_memo_turn: int = -1
        self._turn_memo: ActionPlan | None = None
        # Executed actions this turn (by string repr); used to tell the LLM
        # "you already did X" in subsequent priority windows.
        self._turn_executed: list[str] = []
        # Locked turn intent: the first non-trivial overall_strategy the LLM
        # produced this turn, captured and held for the rest of the turn so
        # subsequent priority windows reason as "continue the plan" instead
        # of re-deriving strategy from scratch (the flip-flop pattern).
        self._turn_intent: str | None = None
        # Active multi-step turn plan: the ordered list of user-visible plays
        # we intend to make this turn. Built once on the first non-trivial
        # own-turn LLM call (an additional `plan_turn` LLM call), then
        # advanced as actions execute and replaced wholesale on divergence.
        # Cleared on turn change.
        self._active_turn_plan: TurnPlan | None = None
        # Turn number we last attempted plan_turn on. Used to suppress
        # repeated plan_turn calls within the same turn after a failure
        # — without this, every priority window in a turn where plan_turn
        # fails would burn an extra LLM call.
        self._turn_plan_attempted_for_turn: int = -1
        # Persistent GAME PLAN block (strategic spine) injected into every
        # per-decision prompt. Owned externally by a GamePlanManager and set via
        # set_game_plan(); unlike _turn_intent it survives turn changes. "" when
        # no plan has been formed yet.
        self._game_plan: str = ""
        # Optional live renderer (GamePlanManager.strategy_block): the plan
        # plus board facts recomputed from each decision's own snapshot.
        self._game_plan_source: Callable[[dict], str] | None = None
        self._planned_recovery: tuple[str, int, int, int] | None = None
        # Why the last typed decision fell back to a deterministic pick:
        # "unavailable" (model server failed or circuit open), "bad_answer"
        # (it answered, but nothing usable), or "" (the model decided).
        self.last_llm_failure: str = ""

    def set_game_plan_source(self, source: Callable[[dict], str] | None) -> None:
        """Render the strategy block per decision from the live snapshot.

        ``source(state)`` returns ROLE + this turn's plan + board facts, then
        the game plan. Without a source the static plan text from
        :meth:`set_game_plan` follows freshly computed board facts.
        """
        self._game_plan_source = source

    def set_game_plan(self, plan_text: str | None) -> None:
        """Set the persistent strategic GAME PLAN block injected into prompts.

        Owned by a :class:`arenamcp.game_plan.GamePlanManager`; the autopilot
        refreshes it before planning. Persists across turns (it is NOT cleared on
        turn change, unlike the per-turn intent/memo).
        """
        self._game_plan = (plan_text or "").strip()

    def clear_game_plan(self) -> None:
        self._game_plan = ""
        self._game_plan_source = None

    def _deck_playbook(self):
        provider = getattr(self, "_deck_playbook_fn", None)
        return provider() if provider else None

    def _has_deck_rule(self, *kinds: str) -> bool:
        playbook = self._deck_playbook()
        return bool(
            playbook
            and any(
                "all" in rule["decisions"] or set(kinds).intersection(rule["decisions"])
                for rule in playbook.data["decision_rules"]
            )
        )

    def _committed_commander_return(self, state: dict, context: dict) -> bool:
        """Carry a priced recovery trade through its immediate zone choice."""
        intent = self._planned_recovery
        if intent is None:
            return False
        match, turn, gid, iid = intent
        if state.get("match_id") != match or (state.get("turn") or {}).get("turn_number") != turn:
            self._planned_recovery = None
            return False
        recipients = set(context.get("recipient_ids") or [])
        if iid in recipients:
            return True
        local = state.get("local_seat_id")
        return any(
            c.get("instance_id") in recipients and c.get("grp_id") == gid and c.get("owner_seat_id") == local
            for zone in ("graveyard", "exile")
            for c in state.get(zone, [])
        )

    def _grounded_plan_block(self, state: dict | None, *, with_lines: bool = True) -> str:
        """ROLE + this turn + clocks/lethal facts for ``state``, then the game plan.

        ``with_lines=False`` asks for the block without the search's LINES line
        (``GamePlanManager.strategy_block`` / ``grounded_facts_block``); a
        source that takes no ``with_lines`` keyword renders its block as is.
        """
        source = getattr(self, "_game_plan_source", None)
        if state is not None and callable(source):
            try:
                if not with_lines and _accepts_keyword(source, "with_lines"):
                    block = source(state, with_lines=False)
                else:
                    block = source(state)
                if isinstance(block, str) and block.strip():
                    return block.strip()
            except Exception as error:  # the strategic layer never blocks a decision
                logger.debug("game-plan source failed: %s", error)
        parts = []
        if state is not None:
            from arenamcp.game_plan import grounded_facts_block

            facts = grounded_facts_block(state, with_lines=with_lines)
            if facts:
                parts.append(facts)
        if getattr(self, "_game_plan", ""):
            parts.append(self._game_plan)
        return "\n".join(parts)

    def _strategy_context(self, state: dict | None = None, *, with_lines: bool = True) -> str:
        """Deck strategy or playbook, the grounded plan block, and the playbook's decision rules.

        ``with_lines=False``: the plan block leaves out the line search's LINES
        line (a typed decision shows it above its options).
        """
        parts = []
        playbook = self._deck_playbook()
        if playbook is None or state is None:
            provider = getattr(self, "_deck_strategy_fn", None)
            strategy = provider() if provider else None
            if strategy:
                parts.append(f"DECK STRATEGY:\n{strategy}")
        grounded = self._grounded_plan_block(state, with_lines=with_lines)
        if grounded:
            parts.append(grounded)
        if playbook is not None and state is not None:
            # The full Oracle reference already accompanies the live state.
            # Reuse just the plan and relevant rules instead of repeating the
            # entire playbook, every source quotation, and then these rules.
            parts.append(playbook.decision_context(state))
        return "\n\n".join(parts)

    # Decision calls give up on a server that has produced no first token by
    # then (bug_20261006_185403: saturated, no token in 30 s / 12 s). Proxies
    # without first-token support ignore it and keep the overall budget.
    _FIRST_TOKEN_TIMEOUT_S = 8.0
    # plan_actions prompts are the big ones (50-75k chars dominated the slow
    # first tokens): real plan calls that succeeded had their first token at
    # 11.3 s (total 13.1 s) and 11.7 s (12.5 s) on a busy shared server
    # (2026-10-04/06). Kept 2 s inside the planner budget.
    _PLAN_FIRST_TOKEN_TIMEOUT_S = 12.0

    def _plan_first_token_timeout(self) -> float:
        budget = float(self._timeout or 0.0)
        return min(self._PLAN_FIRST_TOKEN_TIMEOUT_S, max(self._FIRST_TOKEN_TIMEOUT_S, budget - 2.0))

    def _call_llm(
        self,
        system_prompt: str,
        user_message: str,
        max_tokens: int,
        *,
        timeout_s: float,
        call_class: str,
        first_token_timeout_s: float | None = None,
    ) -> str:
        """One planner model call: deterministic, raising on backend errors, labelled.

        Raises :class:`LLMUnavailableError` for an error-sentinel answer
        (legacy backends without raise_on_error), so no caller ever parses one.
        """
        complete = self._backend.complete
        extras: dict[str, Any] = {"call_class": call_class}
        if first_token_timeout_s is not None:
            extras["first_token_timeout_s"] = min(first_token_timeout_s, timeout_s)
        extras = _accepted_extras(complete, extras)
        try:
            response = complete(
                system_prompt,
                user_message,
                max_tokens,
                temperature=0.0,
                request_timeout_s=timeout_s,
                raise_on_error=True,
                **extras,
            )
        except TypeError:
            # Older backends: no raise_on_error / request budget / temperature.
            try:
                response = complete(system_prompt, user_message, max_tokens, temperature=0.0)
            except TypeError:
                response = complete(system_prompt, user_message)
        if response and is_backend_error_text(response):
            raise LLMUnavailableError(str(response)[:200])
        return response

    def _bounded_call(self, call: Callable[[], str]) -> str:
        """Run ``call`` with a hard wall-clock limit of ``self._timeout`` seconds.

        On timeout the worker is abandoned rather than joined: a with-block
        here used to wait for the worker, so a backend that ignored
        request_timeout_s held the coaching loop until the SDK gave up.
        """
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="planner-llm")
        try:
            return pool.submit(call).result(timeout=self._timeout)
        finally:
            pool.shutdown(wait=False)

    def _llm_unavailable_plan(
        self, trigger: str, turn: int, diag: dict[str, Any], start: float, failure: str
    ) -> ActionPlan:
        """An empty plan that tells the autopilot to decide without the model now."""
        diag["failure"] = failure
        diag["elapsed_ms"] = (time.perf_counter() - start) * 1000
        self._record_diagnostic(diag)
        return ActionPlan(
            trigger=trigger,
            turn_number=turn,
            fallback_reason=FALLBACK_LLM_UNAVAILABLE,
            fallback_detail=failure,
        )

    def plan_actions(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: list[str] | None = None,
        decision_context: dict[str, Any] | None = None,
        legal_actions_raw: list[dict] | None = None,
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
        effective_legal_actions = self._filter_legal_actions_for_planning(game_state, legal_actions or [])
        if legal_actions and (
            not effective_legal_actions
            or effective_legal_actions in (["No legal targets"], ["No useful X values"])
        ):
            return ActionPlan(trigger=trigger, fallback_reason=FALLBACK_NO_ACTIONS)
        dec_ctx = decision_context or game_state.get("decision_context") or {}
        dec_type = str(dec_ctx.get("type") or "").lower()
        if (
            dec_type == "optional_action"
            and dec_ctx.get("commander_return")
            and (
                self._committed_commander_return(game_state, dec_ctx)
                or not self._has_deck_rule("commander_zone")
            )
        ):
            plan = ActionPlan(
                actions=[
                    GameAction(
                        ActionType.CLICK_BUTTON,
                        card_name="accept",
                        target_names=list(dec_ctx.get("recipient_names") or []),
                        commander_return=True,
                        reasoning="Preserve access to the commander by returning it to the command zone.",
                    )
                ],
                overall_strategy="Return the commander to the command zone.",
                trigger=trigger,
                turn_number=game_state.get("turn", {}).get("turn_number", 0),
                fallback_reason="planner_commander_return",
            )
            plan.voice_advice = plan.spoken_actions()
            return plan
        if not effective_legal_actions and dec_type == "discard":
            option_cards = dec_ctx.get("option_cards") or []
            if not option_cards:
                option_cards = [c.get("name") for c in game_state.get("hand", []) if c.get("name")]
            if option_cards:
                effective_legal_actions = [f"Discard {c}" for c in option_cards]

        # Clear turn memo on turn change, and record what was executed in the
        # previous window so the next prompt sees it.
        current_turn = game_state.get("turn", {}).get("turn_number", 0)
        if current_turn != self._turn_memo_turn:
            if self._turn_memo:
                logger.debug(
                    f"Turn changed ({self._turn_memo_turn} -> {current_turn}), clearing planner memo"
                )
            self._turn_memo = None
            self._turn_memo_turn = current_turn
            self._turn_executed = []
            self._turn_intent = None
            # Drop the multi-step turn plan as well — a new turn earns a
            # fresh plan, computed lazily the next time we make an LLM
            # call from our own active turn.
            self._active_turn_plan = None
            # Reset the per-turn plan_turn attempt guard so the new turn
            # gets one attempt to build a fresh plan.
            self._turn_plan_attempted_for_turn = -1
        # P1-7: _turn_executed is now appended ONLY from the autopilot's
        # verified-execution callback (note_executed). The old
        # executed-by-assumption append here recorded guardrail-rejected
        # proposals as done — a land drop that never hit the battlefield
        # showed up as "Already executed this turn: play_land(Forest)"
        # (2026-07-05 22:47).

        diag: dict[str, Any] = {
            "timestamp": time.time(),
            "trigger": trigger,
            "turn": current_turn,
            "legal_actions": legal_actions,
            "effective_legal_actions": effective_legal_actions,
            "decision_context_type": (decision_context or {}).get("type"),
            "bridge_request": game_state.get("_bridge_request_type"),
        }

        # Land-drop preflight — if we have a legal Play Land and 0 lands
        # played this turn, short-circuit the LLM. Fixes the "drops land
        # after combat" pattern where the LLM picks declare_attackers /
        # cast_spell from a window that also offered Play Land.
        forced_land = self._should_force_land_drop(game_state, effective_legal_actions, decision_context)
        if forced_land:
            preflight_plan = self._build_preflight_plan(
                forced_land,
                trigger=trigger,
                turn_number=current_turn,
                tag="land-drop-first",
                fallback_reason=FALLBACK_PREFLIGHT_LAND_DROP,
            )
            if preflight_plan.actions:
                raw = self._resolve_raw_actions_for_matching(game_state, legal_actions_raw)
                if raw:
                    self._attach_gre_refs(preflight_plan, raw, game_state)
                diag["preflight"] = "land_drop_first"
                diag["elapsed_ms"] = (time.perf_counter() - start) * 1000
                diag["planned_actions"] = 1
                diag["strategy"] = preflight_plan.overall_strategy
                self._record_diagnostic(diag)
                self._turn_memo = preflight_plan
                self._turn_memo_turn = current_turn
                logger.info(f"Planner preflight: {preflight_plan.overall_strategy}")
                return preflight_plan

        # P2-6: a menu with no real choice needs no LLM. 7+ full calls on
        # 2026-07-05 fired on Wait-only / pass-only windows (incl. every
        # combat trigger while the opponent held priority) and every
        # response was discarded or auto-picked anyway.
        _trivial = {"pass", "action: activate_mana", "action: floatmana"}
        has_real_choice = any(
            a.strip().lower() not in _trivial and not a.strip().lower().startswith("wait (")
            for a in effective_legal_actions
        )
        if effective_legal_actions and not has_real_choice:
            plan = self._fallback_plan("", effective_legal_actions)
            if plan.actions:
                plan.trigger = trigger
                plan.turn_number = current_turn
                diag["preflight"] = "trivial_window_no_llm"
                diag["elapsed_ms"] = (time.perf_counter() - start) * 1000
                diag["planned_actions"] = len(plan.actions)
                self._record_diagnostic(diag)
                logger.info(
                    f"Planner short-circuit: trivial window ({effective_legal_actions}) — no LLM call"
                )
                return plan

        # Circuit open: the model server is known down. Waiting out the full
        # planning budget here stalled the coaching loop 30 s per window in
        # the 2026-10-06 18:53 outage; the autopilot's deterministic paths
        # (safe-default combat, board-math priority play) decide instead.
        if llm_circuit_open(self._backend):
            logger.warning("Action planning skipped: model server unavailable (circuit open)")
            return self._llm_unavailable_plan(
                trigger, current_turn, diag, start, "llm_unavailable: circuit open"
            )

        # R2: the turn plan rides along on the FIRST own-turn action call
        # instead of being a separate blocking LLM call. The old serial
        # game_plan → plan_turn → plan_actions chain took 17-23s on slow
        # backends and self-induced its own staleness discards (2026-07-05,
        # four calls / 23.0s / net effect zero at 22:46:17). The attempt
        # guard prevents re-requesting across priority windows after a
        # failure (parse error, timeout, etc.).
        want_turn_plan = (
            self._active_turn_plan is None
            and self._turn_plan_attempted_for_turn != current_turn
            and self._is_own_actions_available_window(game_state, decision_context)
        )
        if want_turn_plan:
            self._turn_plan_attempted_for_turn = current_turn

        # Build the prompt
        system_prompt = AUTOPILOT_SYSTEM_PROMPT + "\n" + STRATEGIC_POLICY
        user_message = self._build_action_prompt(
            game_state, trigger, effective_legal_actions, decision_context
        )
        if want_turn_plan:
            user_message += (
                "\n\nADDITIONALLY: this is the first decision of your turn. "
                'Include a top-level "turn_plan" key in the SAME JSON '
                "response with the full ordered list of user-visible plays "
                "for this turn (3-7 items; skip mana abilities and "
                "sub-decisions): "
                '{"turn_plan": {"steps": [{"action_type": "play_land", '
                '"card_name": "Forest", "rationale": "fix mana"}]}}. '
                "Your actions[0] must be the first step you can take right now."
            )
        diag["prompt_len"] = len(user_message)

        # Call LLM with enforced timeout.
        # Use temperature=0 for deterministic planning — avoids different
        # actions being proposed across priority windows in the same turn.
        # raise_on_error: never let the "Error getting advice: ..." sentinel
        # reach the JSON parser — during a backend outage the parse yields 0
        # actions and _fallback_plan would submit a real game action (blind
        # passes, 2026-07-05). _call_llm raises on a sentinel as well.
        def _complete(call_class: str = "decision.plan") -> str:
            return self._call_llm(
                system_prompt,
                user_message,
                4096,
                timeout_s=self._timeout,
                call_class=call_class,
                first_token_timeout_s=self._plan_first_token_timeout(),
            )

        try:
            response = self._bounded_call(_complete)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(f"Action planning took {elapsed:.0f}ms")
        except concurrent.futures.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error(f"Action planning timed out after {elapsed:.0f}ms (limit {self._timeout}s)")
            return self._llm_unavailable_plan(trigger, current_turn, diag, start, "timeout")
        except LLMUnavailableError as e:
            logger.error(f"Backend returned error sentinel; no plan: {str(e)[:160]}")
            return self._llm_unavailable_plan(
                trigger, current_turn, diag, start, f"llm_error_sentinel: {str(e)[:160]}"
            )
        except Exception as e:
            logger.error(f"Action planning LLM call failed: {e}")
            return self._llm_unavailable_plan(trigger, current_turn, diag, start, f"llm_error: {e}")

        diag["elapsed_ms"] = (time.perf_counter() - start) * 1000
        diag["response_len"] = len(response) if response else 0
        diag["response_preview"] = (response or "")[:300]

        # R2: extract the piggybacked turn plan from the same response.
        if want_turn_plan and response:
            try:
                tp = self._parse_turn_plan_response(response, current_turn)
                if tp and tp.steps:
                    self._active_turn_plan = tp
                    logger.info(
                        f"Turn plan locked (turn {current_turn}, merged call): "
                        + ", ".join(
                            (f"{s.action_type}:{s.card_name}" if s.card_name else s.action_type)
                            for s in tp.steps
                        )
                    )
            except Exception as e:
                logger.debug(f"merged turn-plan parse failed (non-fatal): {e}")

        # Parse response — pass bridge request + decision context so we can
        # accept decision-type actions (select_n, search_library, etc.) even
        # when legal_actions is stale.
        plan = self._parse_response(
            response,
            effective_legal_actions,
            decision_context=decision_context,
            bridge_request=game_state.get("_bridge_request_type"),
            game_state=game_state,
        )
        plan.trigger = trigger
        plan.turn_number = game_state.get("turn", {}).get("turn_number", 0)

        blocking_window = any(entry.lower().startswith("block with:") for entry in effective_legal_actions)
        repair_unavailable = False
        if not plan.actions and blocking_window and not llm_circuit_open(self._backend):
            user_message += (
                "\n\nYour previous response did not specify a valid complete block: "
                + str(response or "")[:1000]
                + '\nReturn one action_type="declare_blockers" with blocker_assignments '
                'as {"eligible blocker name": "attacking creature name"}. '
                'A bare "pick" cannot specify the attacker. Use {} only to deliberately decline all blocks.'
            )
            try:
                repair_response = self._bounded_call(lambda: _complete("decision.block_repair"))
                plan = self._parse_response(
                    repair_response,
                    effective_legal_actions,
                    decision_context=decision_context,
                    bridge_request=game_state.get("_bridge_request_type"),
                    game_state=game_state,
                )
                plan.trigger = trigger
                plan.turn_number = current_turn
                diag["block_repair_preview"] = (repair_response or "")[:300]
            except Exception as error:
                repair_unavailable = is_llm_unavailable_error(error)
                logger.warning("Block assignment repair failed: %s", error)

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
                if repair_unavailable:
                    plan.fallback_reason = FALLBACK_LLM_UNAVAILABLE
                logger.warning(
                    f"Planner fallback also failed: trigger={trigger}, "
                    f"{len(effective_legal_actions)} legal actions"
                )

        self._check_block_survival(
            plan, game_state, decision_context or game_state.get("decision_context") or {}
        )
        self._check_block_recovery(
            plan, game_state, decision_context or game_state.get("decision_context") or {}
        )
        self._check_losing_attacks(
            plan, game_state, decision_context or game_state.get("decision_context") or {}
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
        if plan.actions and plan.actions[0].action_type.value not in (
            "pass_priority",
            "mulligan_keep",
            "mulligan_mull",
        ):
            self._turn_memo = plan
            self._turn_memo_turn = current_turn

        # Capture the turn intent: the first non-trivial overall_strategy
        # produced this turn becomes the locked plan that subsequent windows
        # follow. Skip preflight/fallback tags and pure-pass plans — those
        # are reactive rather than strategic.
        self._maybe_capture_turn_intent(plan, game_state, current_turn)

        logger.info(f"Planned {len(plan.actions)} actions: {plan.overall_strategy}")
        return plan

    def _check_block_survival(self, plan: ActionPlan, state: dict, context: dict) -> None:
        """Never submit blocks that let lethal damage through when a legal block survives.

        2026-10-04 23:32: at 25 life against a 9/9 and an unblockable 18/19
        flyer, the planner declined to chump the 9/9 ("absorb 9 damage") and
        took 27. Damage counts every attacker, not only the blockable ones.

        Blocks that leave us at BLOCK_DANGER_LIFE or less are also replaced
        when the solver keeps BLOCK_DANGER_MARGIN more life: 2026-10-05 the
        planner kept a 6/5 Dragon back from a 13/11 and went 16 -> 3, then
        died to the next attack. That plan was a bare "done" click, which
        declines every block just like an empty declaration.
        """
        if len(plan.actions) != 1:
            return
        declined = context.get("type") == "declare_blockers" and plan.actions[0].action_type in (
            ActionType.CLICK_BUTTON,
            ActionType.PASS_PRIORITY,
        )
        if plan.actions[0].action_type != ActionType.DECLARE_BLOCKERS and not declined:
            return
        from arenamcp.combat_solver import _resolve_attacker, blocker_allowed_attackers_map, optimal_blocks

        local = next((p for p in state.get("players", []) if p.get("is_local")), None)
        life = (local or {}).get("life_total")
        if type(life) is not int or life <= 0:
            return
        local_seat = local.get("seat_id")
        cards = {c.get("instance_id"): c for c in state.get("battlefield", [])}
        attackers = [
            c
            for c in cards.values()
            if c.get("is_attacking") and (c.get("controller_seat_id") or c.get("owner_seat_id")) != local_seat
        ]
        legal_ids = {int(i) for i in context.get("legal_blocker_ids") or [] if str(i).isdigit()}
        blockers = [cards[i] for i in legal_ids if i in cards]
        if not attackers or not blockers:
            return
        if any(not isinstance(c.get("power"), int) for c in attackers):
            return
        action = plan.actions[0]
        planned = {} if declined else action.blocker_instance_assignments or {}
        through = sum(
            _resolve_attacker(
                atk, [cards[b] for b, a in planned.items() if a == atk["instance_id"] and b in cards]
            ).damage_through
            for atk in attackers
        )
        lethal = through >= life
        if not lethal and life - through > BLOCK_DANGER_LIFE:
            return
        allowed = blocker_allowed_attackers_map(context.get("raw_blockers") or [])
        survival = optimal_blocks(attackers, blockers, life, blocker_allowed_attackers=allowed or None)
        if survival is None or survival.damage_through >= life or not survival.assignments:
            return
        if not lethal and survival.damage_through > through - BLOCK_DANGER_MARGIN:
            return  # the solver's blocks do not keep meaningfully more life

        def label(iid):
            card = cards[iid]
            token = card.get("is_token") or "token" in str(card.get("object_kind", "")).lower()
            return f"{'*' if token else ''}{card.get('name', 'Creature')} [id:{iid}]"

        logger.warning(
            "%s block guard: planned blocks %s let %d damage through at %d life; using %s",
            "Lethal" if lethal else "Danger-zone",
            planned,
            through,
            life,
            survival.explanation,
        )
        if declined:
            action = plan.actions[0] = GameAction(action_type=ActionType.DECLARE_BLOCKERS)
        action.blocker_instance_assignments = dict(survival.assignments)
        action.blocker_assignments = {label(b): label(a) for b, a in survival.assignments.items()}
        outcome = "were lethal" if lethal else f"left us at {life - through}"
        action.reasoning = (
            f"Planned blocks {outcome} ({through} damage at {life} life); {survival.explanation}."
        )
        plan.fallback_reason = "planner_lethal_block" if lethal else "planner_danger_block"
        plan.overall_strategy = f"Survive combat: {survival.explanation}."
        blocks = "; ".join(f"{label(b)} against {label(a)}" for b, a in survival.assignments.items())
        risk = "not blocking was lethal" if lethal else f"not blocking left us at {life - through} life"
        plan.voice_advice = f"Blocking with {blocks}; {risk}."

    def _check_losing_attacks(self, plan: ActionPlan, state: dict, context: dict) -> None:
        """Hold back attackers that only feed an untapped blocker.

        bug_20261006_135027: the model sent a 1/1 Fblthp at a 1-loyalty Jace
        token past an untapped 3/2 Keeper of the Quiet Hour with no mana up
        ("Trade Fblthp into Jace"). Keeper blocked; Fblthp died; nothing else
        happened. The bridge's solver override skips attacks with explicit or
        planeswalker recipients, and the zero-power filter ignores 1-power
        creatures, so the plan went straight to submission.

        Runs only at a live DeclareAttackers decision, where the board is the
        one the declaration will meet; main-phase plans may still change it.
        See ``combat_strategy.losing_attackers`` for the conservative rules.
        """
        if str(context.get("type") or "").lower() != "declare_attackers":
            return
        from arenamcp.combat_identity import resolve_combatant
        from arenamcp.combat_strategy import losing_attackers

        eligible = [int(i) for i in context.get("legal_attacker_ids") or [] if str(i).isdigit()] or [
            int(entry.get("attackerInstanceId") or 0) for entry in context.get("raw_attackers") or []
        ]
        for action in plan.actions:
            if action.action_type != ActionType.DECLARE_ATTACKERS or not action.attacker_names:
                continue
            identities = list(action.attacker_instance_ids)
            if len(identities) != len(action.attacker_names):
                try:
                    identities = [
                        resolve_combatant(name, state, eligible, local_side=True)
                        for name in action.attacker_names
                    ]
                except (ValueError, TypeError, KeyError):
                    continue  # cannot tell which creatures attack: keep the plan
            losing = losing_attackers(state, identities)
            if not losing:
                continue
            kept = [
                (name, identity)
                for name, identity in zip(action.attacker_names, identities, strict=True)
                if identity not in losing
            ]
            dropped = [
                (name, identity)
                for name, identity in zip(action.attacker_names, identities, strict=True)
                if identity in losing
            ]
            for name, identity in dropped:
                logger.warning(
                    "Losing-attack guard: not attacking with %s [%d] (planned target: %s): %s",
                    name,
                    identity,
                    action.attacker_targets.get(name) or ", ".join(action.target_names) or "unspecified",
                    losing[identity],
                )
            action.attacker_names = [name for name, _ in kept]
            action.attacker_instance_ids = [identity for _, identity in kept]
            action.attacker_targets = {
                name: target
                for name, target in action.attacker_targets.items()
                if name in action.attacker_names
            }
            if not kept:
                action.target_names = []
            from arenamcp.narration import spoken_list, spoken_name

            held = spoken_list([spoken_name(name) for name, _ in dropped])
            outcome = "they would die to blocks" if len(dropped) > 1 else "it would die to a block"
            action.reasoning = f"Held back {held}: " + "; ".join(losing[i] for _, i in dropped) + "."
            plan.fallback_reason = "planner_losing_attack"
            plan.overall_strategy = f"Hold back {held}: {outcome} for nothing."
            plan.voice_advice = f"Not attacking with {held}: {outcome} for nothing." + (
                f" {plan.spoken_actions()}" if kept else ""
            )

    def _check_block_recovery(self, plan: ActionPlan, state: dict, context: dict) -> None:
        """Price supported recovery before committing a same-outcome trade."""
        if len(plan.actions) != 1 or plan.actions[0].action_type != ActionType.DECLARE_BLOCKERS:
            return
        from arenamcp.combat_recovery import improve_block_recovery

        action = plan.actions[0]
        book = self._deck_playbook()
        forecast_state = {**state, "deck_catalog": book.catalog} if book is not None else state
        improved = improve_block_recovery(forecast_state, context, action.blocker_instance_assignments)
        if improved is None:
            return
        assignments, explanation = improved
        cards = {c.get("instance_id"): c for c in state.get("battlefield", [])}

        def label(iid):
            card = cards[iid]
            token = card.get("is_token") or "token" in str(card.get("object_kind", "")).lower()
            return f"{'*' if token else ''}{card.get('name', 'Creature')} [id:{iid}]"

        logger.info(
            "Combat recovery comparison changed %s to %s: %s",
            action.blocker_instance_assignments,
            assignments,
            explanation,
        )
        action.blocker_instance_assignments = assignments
        action.blocker_assignments = {label(b): label(a) for b, a in assignments.items()}
        action.reasoning = explanation
        plan.fallback_reason = "planner_combat_recovery"
        plan.overall_strategy = (
            "Keep the same combat outcome and use affordable commander recovery to rebuild resources."
        )
        blocks = "; ".join(f"{label(b)} against {label(a)}" for b, a in assignments.items())
        plan.voice_advice = f"Blocking with {blocks} to rebuild through an affordable recast of my commander."
        local = next((p for p in state.get("players", []) if p.get("is_local")), {})
        recovering = set(assignments) & set(local.get("commander_ids") or [])
        if len(recovering) == 1 and state.get("match_id") and state.get("turn", {}).get("turn_number"):
            iid = next(iter(recovering))
            self._planned_recovery = (
                state["match_id"],
                state["turn"]["turn_number"],
                cards[iid].get("grp_id"),
                iid,
            )

    _NON_INTENT_PREFIXES: tuple[str, ...] = (
        "[land-drop-first]",
        "[auto-pick]",
    )

    def _maybe_capture_turn_intent(
        self,
        plan: ActionPlan,
        game_state: dict[str, Any],
        current_turn: int,
    ) -> None:
        """Lock the first strategic plan of the turn as the turn intent."""
        if self._turn_intent:
            return
        strategy = (plan.overall_strategy or "").strip()
        if not strategy:
            return
        # Skip deterministic/auto-pick markers — they're not strategy.
        if any(strategy.startswith(p) for p in self._NON_INTENT_PREFIXES):
            return
        if not plan.actions:
            return
        first_action = plan.actions[0].action_type.value
        if first_action in ("pass_priority", "mulligan_keep", "mulligan_mull"):
            return
        # Only lock intent on our own turn — we don't make turn-level plans
        # for opponent priority windows.
        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            for player in game_state.get("players", []):
                if player.get("is_local"):
                    local_seat = player.get("seat_id")
                    break
        active_player = (game_state.get("turn") or {}).get("active_player")
        if local_seat is None or active_player != local_seat:
            return
        self._turn_intent = strategy
        self._turn_memo_turn = current_turn
        logger.info(f"Turn {current_turn} intent locked: {strategy}")

    # ── Multi-step turn plan ─────────────────────────────────────────

    # Action types that count as "user-visible plays" worth showing in
    # the turn plan. Mana abilities, casting-time sub-decisions, search
    # prompts, and similar mid-spell mechanics are intentionally excluded.
    _TURN_PLAN_USER_VISIBLE_ACTIONS = frozenset(
        {
            "play_land",
            "cast_spell",
            "activate_ability",
            "declare_attackers",
            "declare_blockers",
        }
    )

    def _is_own_actions_available_window(
        self,
        game_state: dict[str, Any],
        decision_context: dict[str, Any] | None,
    ) -> bool:
        """Are we in a normal own-turn ActionsAvailable priority window?"""
        bridge_request = (game_state.get("_bridge_request_type") or "").strip()
        bridge_class = (game_state.get("_bridge_request_class") or "").strip()
        # Allow empty (test states) or ActionsAvailable-family.
        ok_requests = self._ACTIONS_AVAILABLE_PREFLIGHT_REQUESTS
        if (bridge_request and bridge_request not in ok_requests) or (
            bridge_class and bridge_class not in ok_requests
        ):
            return False
        dc_type = (decision_context or {}).get("type", "")
        if dc_type and dc_type != "actions_available":
            return False
        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            for player in game_state.get("players", []):
                if player.get("is_local"):
                    local_seat = player.get("seat_id")
                    break
        if local_seat is None:
            return False
        active_player = (game_state.get("turn") or {}).get("active_player")
        return active_player == local_seat

    def plan_turn(
        self,
        game_state: dict[str, Any],
        effective_legal_actions: list[str],
        decision_context: dict[str, Any] | None = None,
    ) -> TurnPlan | None:
        """One-shot LLM call that lays out the user-visible plays for the turn.

        Stores the result on `self._active_turn_plan`. Returns the plan or
        None if the call failed / produced no useful steps.
        """
        if llm_circuit_open(self._backend):
            logger.info("plan_turn skipped: model server unavailable (circuit open)")
            return None
        game_state = prepare_match_context(game_state)
        current_turn = (game_state.get("turn") or {}).get("turn_number", 0) or 0

        # Build a lightweight prompt: reuse the same context formatter as
        # the per-window planner, but ask for an ordered turn plan in JSON
        # rather than a single action.
        try:
            from arenamcp.coach import CoachEngine

            formatter = CoachEngine.__new__(CoachEngine)
            context = formatter._format_game_context(game_state, for_planner=True)
        except Exception as e:
            logger.warning(f"plan_turn: context formatter failed: {e}")
            context = self._fallback_format(game_state)

        instructions = (
            "Output the FULL ordered list of plays for this turn (3-7 items). "
            "Skip mana abilities, casting-time sub-decisions, and search-prompts — "
            "list only the user-visible plays (Play Land, Cast X, Activate X, Attack)."
        )
        schema_example = (
            '{"turn_plan": {"steps": ['
            '{"action_type": "play_land", "card_name": "Forest", "rationale": "fix mana"},'
            '{"action_type": "cast_spell", "card_name": "Optimistic Scavenger", '
            '"target_names": [], "rationale": "early pressure"}'
            "]}}"
        )
        game_plan_block = self._strategy_context(game_state) + "\n\n"
        user_message = (
            f"TRIGGER: turn_plan (turn {current_turn})\n\n"
            f"{context}\n\n"
            f"{game_plan_block}"
            f"INSTRUCTIONS: {instructions}\n"
            f"Order the plays so they ADVANCE the game plan above.\n"
            f"Output ONLY a JSON object matching this shape (no prose, no markdown):\n"
            f"{schema_example}"
        )
        user_message = with_deck_reference(user_message, game_state)

        def _complete() -> str:
            return self._call_llm(
                TURN_PLAN_SYSTEM_PROMPT + "\n" + STRATEGIC_POLICY,
                user_message,
                4096,
                timeout_s=self._timeout,
                call_class="decision.turn_plan",
            )

        try:
            response = self._bounded_call(_complete)
        except concurrent.futures.TimeoutError:
            logger.warning("plan_turn LLM call timed out — leaving turn plan unset")
            return None
        except Exception as e:
            logger.warning(f"plan_turn LLM call failed: {e}")
            return None

        plan = self._parse_turn_plan_response(response, current_turn)
        if plan is None or not plan.steps:
            logger.info(f"plan_turn: no steps parsed from response={(response or '')[:200]!r}")
            return None

        self._active_turn_plan = plan
        logger.info(
            f"Turn plan locked (turn {current_turn}): "
            + ", ".join(
                (f"{s.action_type}:{s.card_name}" if s.card_name else s.action_type) for s in plan.steps
            )
        )
        return plan

    def _parse_turn_plan_response(
        self,
        response: str,
        current_turn: int,
    ) -> TurnPlan | None:
        """Defensively parse the JSON {"turn_plan": {"steps": [...]}} shape."""
        if not response:
            return None
        text = response.strip()
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if fence_match:
            text = fence_match.group(1).strip()
        text = re.sub(r",\s*([\]}])", r"\1", text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("plan_turn: response was not valid JSON")
            return None

        if not isinstance(data, dict):
            return None

        # Tolerate either {"turn_plan": {"steps": [...]}} or {"steps": [...]}.
        block = data.get("turn_plan")
        if isinstance(block, dict):
            steps_data = block.get("steps", [])
        else:
            steps_data = data.get("steps", [])

        # P0-8 belt-and-braces: a model that obeyed the actions envelope
        # anyway still described the turn — map actions→steps
        # (reasoning→rationale) instead of discarding the whole response.
        if (not isinstance(steps_data, list) or not steps_data) and isinstance(data.get("actions"), list):
            steps_data = [
                {**a, "rationale": a.get("rationale") or a.get("reasoning", "")}
                for a in data["actions"]
                if isinstance(a, dict)
            ]

        if not isinstance(steps_data, list):
            return None

        steps: list[TurnPlanStep] = []
        for item in steps_data:
            if not isinstance(item, dict):
                continue
            action_type = str(item.get("action_type") or "").strip().lower()
            if not action_type:
                continue
            # Only keep user-visible plays.
            if action_type not in self._TURN_PLAN_USER_VISIBLE_ACTIONS:
                continue
            card_name = str(item.get("card_name") or "").strip()
            target_names_raw = item.get("target_names") or []
            if isinstance(target_names_raw, list):
                target_names = [str(t).strip() for t in target_names_raw if str(t).strip()]
            else:
                target_names = []
            rationale = str(item.get("rationale") or "").strip()
            steps.append(
                TurnPlanStep(
                    action_type=action_type,
                    card_name=card_name,
                    target_names=target_names,
                    rationale=rationale,
                    status="pending",
                )
            )

        if not steps:
            return None

        return TurnPlan(turn_number=current_turn, steps=steps)

    def advance_turn_plan(self, executed_action: GameAction) -> str:
        """Advance the active turn plan for an executed action.

        Returns:
            "advanced" — a plan step (the current one, or a later one via
                look-ahead) matched and was marked done; any stepped-over
                steps are marked "skipped".
            "neutral"  — nothing to conclude: no active plan, or the action
                isn't user-visible (pass / pay costs / sub-decisions). NOT
                divergence — 3/6 match-2 plan invalidations on 2026-07-05
                were benign passes the old boolean couldn't distinguish
                (P2-8).
            "diverged" — a user-visible action matching no remaining step;
                the caller may invalidate/replan.
        """
        plan = self._active_turn_plan
        if plan is None or plan.current_idx >= len(plan.steps):
            return "neutral"
        if executed_action is None:
            return "neutral"

        executed_type = executed_action.action_type.value
        if executed_type not in self._TURN_PLAN_USER_VISIBLE_ACTIONS:
            return "neutral"

        def _matches(step: TurnPlanStep) -> bool:
            if step.action_type != executed_type:
                return False
            # For attack/block, we don't compare card names (aggregate).
            if executed_type in ("declare_attackers", "declare_blockers"):
                return True
            executed_name = self._strip_decoration(executed_action.card_name or "").lower()
            expected_name = self._strip_decoration(step.card_name or "").lower()
            return not (expected_name and executed_name and expected_name != executed_name)

        # Current step first, then look-ahead: a later step executing early
        # (e.g. the land-drop preflight already performed step 1) marks the
        # stepped-over ones "skipped" instead of reading as divergence.
        for offset, step in enumerate(plan.steps[plan.current_idx :]):
            if _matches(step):
                for skipped in plan.steps[plan.current_idx : plan.current_idx + offset]:
                    skipped.status = "skipped"
                plan.current_idx += offset
                plan.mark_current_done()
                return "advanced"
        return "diverged"

    def note_executed(self, action: GameAction) -> None:
        """Record a VERIFIED executed action for this turn's prompts (P1-7)."""
        if action is None:
            return
        rep = (
            f"{action.action_type.value}({action.card_name})"
            if action.card_name
            else action.action_type.value
        )
        if rep not in self._turn_executed:
            self._turn_executed.append(rep)

    def has_pending_attack_intent(self) -> bool:
        """True if the active turn plan still has an un-executed attack step.

        Used by the autopilot to avoid auto-confirming an *empty* attacker
        declaration (``Done (confirm attackers)`` with nobody attacking) when
        the locked turn plan for this turn intended to swing. Without this
        guard the bridge submits ``DeclareAttackersSubmit`` with no attackers
        and the planned attack silently evaporates.
        """
        plan = self._active_turn_plan
        if plan is None:
            return False
        return any(step.action_type == "declare_attackers" and step.status != "done" for step in plan.steps)

    def invalidate_turn_plan(self, reason: str = "") -> None:
        """Drop the active turn plan and stash the reason for the UI to show."""
        plan = self._active_turn_plan
        if plan is None:
            return
        # Preserve the reason on a synthetic empty plan so the UI gets one
        # last event with the explanation before the panel hides / replans.
        logger.info(f"Turn plan invalidated: {reason or '<no reason>'}")
        plan.last_replanned_reason = reason or "diverged"
        # Clear; a future plan_turn call will rebuild it.
        self._active_turn_plan = None

    def _format_turn_plan_for_prompt(self, plan: TurnPlan) -> str:
        """Render the active turn plan as a structured prompt block."""
        remaining_lines: list[str] = []
        done_lines: list[str] = []
        for idx, step in enumerate(plan.steps):
            label = self._humanize_turn_plan_step(step)
            if step.status == "done":
                done_lines.append(f"  ✓ {label}")
            elif idx == plan.current_idx:
                remaining_lines.append(f"  → {label} (currently expected next)")
            else:
                remaining_lines.append(f"  ☐ {label}")

        sections = [f"\nTURN PLAN (turn {plan.turn_number}) — remaining:"]
        if remaining_lines:
            sections.extend(remaining_lines)
        else:
            sections.append("  (no remaining steps)")
        if done_lines:
            sections.append("Already done: " + ", ".join(line.strip() for line in done_lines))
        return "\n".join(sections)

    @staticmethod
    def _humanize_turn_plan_step(step: TurnPlanStep) -> str:
        """Render a single step as a human-readable label."""
        action = step.action_type
        name = step.card_name.strip()
        if action == "play_land":
            return f"Play Land: {name}" if name else "Play Land"
        if action == "cast_spell":
            return f"Cast {name}" if name else "Cast spell"
        if action == "activate_ability":
            return f"Activate Ability: {name}" if name else "Activate Ability"
        if action == "declare_attackers":
            return "Declare Attackers"
        if action == "declare_blockers":
            return "Declare Blockers"
        if name:
            return f"{action}: {name}"
        return action

    def get_turn_plan_payload(self) -> dict[str, Any] | None:
        """Serialize the active turn plan into a dict for the pipe event.

        Returns None when there's no active plan.
        """
        plan = self._active_turn_plan
        if plan is None:
            return None
        return {
            "turn_number": plan.turn_number,
            "steps": [
                {
                    "action_type": s.action_type,
                    "card_name": s.card_name,
                    "target_names": list(s.target_names),
                    "rationale": s.rationale,
                    "status": s.status,
                }
                for s in plan.steps
            ],
            "current_idx": plan.current_idx,
            "replanned_reason": plan.last_replanned_reason,
        }

    @staticmethod
    def _resolve_raw_actions_for_matching(
        game_state: dict[str, Any],
        legal_actions_raw: list[dict[str, Any]] | None = None,
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
        def scryfall_lookup(grp_id: int) -> str | None:
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

        # When the GRE bridge is authoritative (an ActionsAvailable window),
        # MTGA only ever offers a Cast action you can actually pay for — the
        # bridge has already done castability filtering. In that case we must
        # NOT drop a "Cast X" just because our log-derived [OK] tag is missing:
        # doing so deletes castable creatures from the planner's options, which
        # is exactly how the autopilot ends up playing only a land (or nothing)
        # and discarding a full hand. Trust the bridge.
        bridge_authoritative = bool(
            (game_state.get("_bridge_request_type") or "").strip()
            or (game_state.get("_bridge_request_class") or "").strip()
        )

        for legal_action in legal_actions:
            lower = legal_action.lower()
            if lower.startswith("select target:") and "(yours)" in lower:
                if self._decision_source_is_harmful(None, game_state) is True:
                    logger.info("Withholding harmful friendly target: %s", legal_action)
                    continue
            play_match = re.match(r"(cast |activate(?: ability)?\s*:\s*|activate )(.+)", legal_action, re.I)
            if play_match:
                name = self._normalize_action_text(play_match.group(2))
                action_type = "Cast" if lower.startswith("cast ") else "Activate"
                card = find_source(game_state, {}, name)
                metadata = next(
                    (
                        entry
                        for entry in game_state.get("_bridge_actions") or []
                        if entry.get("instanceId") == card.get("instance_id")
                        and str(entry.get("actionType", "")).removeprefix("ActionType_") == action_type
                    ),
                    {},
                )
                reason = unsafe_play_reason(game_state, card, action_type, metadata)
                if reason:
                    logger.info("Withholding %s: %s", legal_action, reason)
                    continue
            if lower.startswith("cast "):
                has_ok = "[ok]" in lower

                if mana_pool is None:
                    try:
                        from arenamcp.rules_engine import RulesEngine

                        rules_engine_cls = RulesEngine
                        local_seat = next(
                            (p.get("seat_id") for p in game_state.get("players", []) if p.get("is_local")),
                            None,
                        )
                        if local_seat is not None:
                            mana_pool = RulesEngine._get_mana_pool(game_state, local_seat)
                        else:
                            mana_pool = {}
                    except Exception:
                        mana_pool = {}

                card_name = self._normalize_action_text(legal_action).replace("Cast ", "").strip()
                card_cost = ""
                card_hand_entry = None
                card_zone = ""
                # Issue #414: commanders cast from the COMMAND zone — the
                # hand-only lookup found no cost, so the payability gate
                # couldn't judge them and waved unpayable Hei Bai through
                # every single turn ("it's like it doesn't understand mana
                # cost").
                for zone in ("hand", "command"):
                    for card in game_state.get(zone, []) or []:
                        if card.get("name", "").lower() == card_name.lower():
                            card_cost = card.get("mana_cost", "")
                            card_hand_entry = card
                            card_zone = zone
                            break
                    if card_hand_entry is not None:
                        break

                # Local affordability: True/False when cost + pool are known,
                # else None (couldn't determine).
                local_affordable = None
                if card_cost and mana_pool is not None and rules_engine_cls is not None:
                    local_affordable = rules_engine_cls._can_afford(card_cost, mana_pool)

                # Commander tax is not in the printed cost. If MTGA's [OK] tag is
                # present, trust it. Otherwise, verify against our local mana pool
                # including commander tax (2 generic per previous cast) rather than
                # blindly dropping the commander.
                if card_zone == "command" and "[OK]" not in legal_action:
                    commander_casts = (
                        card_hand_entry.get("commander_casts", 0) if isinstance(card_hand_entry, dict) else 0
                    )
                    tax_cost = card_cost
                    if commander_casts > 0 and card_cost:
                        tax_cost = f"{{{commander_casts * 2}}}{card_cost}"
                    if local_affordable is False or (
                        rules_engine_cls
                        and mana_pool is not None
                        and not rules_engine_cls._can_afford(tax_cost, mana_pool)
                    ):
                        logger.info(
                            "Dropping command-zone cast %s: no autotap [OK] and local check unaffordable "
                            "(printed cost %s + tax %d)",
                            card_name,
                            card_cost or "?",
                            commander_casts * 2,
                        )
                        continue

                # X-cost spells (P3-1): allowed when the bridge is connected —
                # the plugin now enumerates the X chooser as casting-time
                # numeric entries ("X = n" → SubmitX; verified surfacing live
                # 2026-07-06 00:53 during a manual Silkguard cast), and the
                # per-turn rollback suppression bounds any residual wedge to
                # two attempts. Without the bridge the mouse path still can't
                # drive the X slider (live 2026-07-02: Silkguard resolved
                # X=0, Steelbane Hydra wedged the client) — keep dropping.
                if card_cost and "{X}" in card_cost.upper().replace(" ", ""):
                    if not game_state.get("_bridge_connected"):
                        logger.info(
                            "Dropping X-cost cast %s from autopilot (cost=%s): "
                            "no bridge to drive the X chooser",
                            card_name,
                            card_cost,
                        )
                        continue

                # Payability gate (#377). "[OK]" is appended only when MTGA
                # found an autotap solution — a real mana-payment path. WITHOUT
                # "[OK]" the bridge has no autotap solution, so a hard cast hits
                # PayCosts with nothing to pay and rolls back, retrying until
                # the rollback suppressor trips ("it tried to cast X, we didn't
                # have the mana"). Keep an un-[OK] cast only when our own mana
                # check says we can pay it; drop it when BOTH solvers agree
                # there's no mana path. When the cost is unknowable, fall back
                # to the old bridge-trusting behavior so we don't regress the
                # "autopilot plays only a land and discards its hand" bug.
                if not has_ok:
                    if local_affordable is False:
                        logger.info(
                            "Dropping unpayable cast %s (no autotap solution, "
                            "local check unaffordable, cost=%s)",
                            card_name,
                            card_cost,
                        )
                        continue
                    if local_affordable is None and not bridge_authoritative:
                        continue
                elif local_affordable is False:
                    # "[OK]" present but the local engine disagrees — trust
                    # MTGA's autotap solver (it handles hybrid / phyrexian /
                    # cost reductions / affinity the local check doesn't).
                    logger.debug(
                        "Cast %s: [OK]/autotap present but local check unaffordable — trusting bridge.",
                        card_name,
                    )

                # Block removal spells that would only have friendly targets.
                # Casting them just forces the user to either blow up their own
                # permanent or cancel — neither is worth the mana. See "Seam Rip
                # with only my own enchantment in play" self-destruct case.
                if card_hand_entry and self._removal_lacks_opponent_target(card_hand_entry, game_state):
                    logger.info(
                        "Filtering self-harming removal: %s (no legal opponent target)",
                        card_name,
                    )
                    continue

            filtered.append(legal_action)

        return filtered

    _ACTIONS_AVAILABLE_PREFLIGHT_REQUESTS: frozenset[str] = frozenset(
        {
            "",
            "ActionsAvailable",
            "ActionsAvailableRequest",
        }
    )

    def _should_force_land_drop(
        self,
        game_state: dict[str, Any],
        legal_actions: list[str],
        decision_context: dict[str, Any] | None,
    ) -> str | None:
        """Return a Play Land legal-action string if we should force it now.

        Conditions:
          - feature is enabled (land_drop_first)
          - decision is a normal priority window (ActionsAvailable / unset)
          - it's our turn
          - active player has played 0 lands this turn
          - a "Play Land: X" entry is in legal_actions
        """
        # A learned sequencing exception must reach strategic planning instead
        # of being preempted by the generic land-first shortcut.
        if self._has_deck_rule("development", "mana"):
            return None
        if not self._land_drop_first or not legal_actions:
            return None

        bridge_request = (game_state.get("_bridge_request_type") or "").strip()
        bridge_class = (game_state.get("_bridge_request_class") or "").strip()
        if (
            bridge_request not in self._ACTIONS_AVAILABLE_PREFLIGHT_REQUESTS
            or bridge_class not in self._ACTIONS_AVAILABLE_PREFLIGHT_REQUESTS
        ):
            return None

        dc_type = (decision_context or {}).get("type", "")
        if dc_type and dc_type != "actions_available":
            return None

        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            for player in game_state.get("players", []):
                if player.get("is_local"):
                    local_seat = player.get("seat_id")
                    break
        if local_seat is None:
            return None

        turn = game_state.get("turn", {}) or {}
        if turn.get("active_player") != local_seat:
            return None

        # Only force the preflight land drop if 0 lands have been played this turn.
        for player in game_state.get("players", []):
            if player.get("seat_id") == local_seat or player.get("is_local"):
                if (player.get("lands_played") or 0) > 0:
                    return None

        # Whenever MTGA GRE offers "Play Land: <Card>" in legal_actions during our turn,
        # prioritize playing the land.
        for legal in legal_actions:
            if legal.lower().startswith("play land:"):
                return legal
        return None

    def _build_preflight_plan(
        self,
        legal_action: str,
        *,
        trigger: str,
        turn_number: int,
        tag: str,
        fallback_reason: str,
    ) -> ActionPlan:
        """Build a single-action ActionPlan from a legal-action string.

        ``fallback_reason`` is required, not defaulted: a preflight plan is by
        definition not a model decision, and defaulting it to "" would let a new
        preflight path silently produce untagged training records (WP-0.4).
        """
        plan = ActionPlan(trigger=trigger, turn_number=turn_number)
        action = self._legal_action_to_action(legal_action)
        if not action:
            return plan
        plan.actions = [action]
        plan.overall_strategy = f"[{tag}] {legal_action}"
        plan.voice_advice = self._humanize_legal_action(legal_action)
        plan.fallback_reason = fallback_reason
        return plan

    # Oracle phrases that mark a spell as removal / hurts-its-target.
    # Kept in sync with the autopilot bridge-side safety check.
    _REMOVAL_ORACLE_PHRASES = (
        "destroy target",
        "exile target",
        "counter target",
        "return target",
        "sacrifices target",
        "sacrifice target",
        "deals damage to target",
        "damage to target creature",
        "damage to any target",
    )

    def _removal_lacks_opponent_target(self, card: dict[str, Any], game_state: dict[str, Any]) -> bool:
        """Use the same removal preflight as typed options and execution fallback."""
        from arenamcp.play_safety import removal_lacks_opponent_target

        return removal_lacks_opponent_target(card, game_state)

    def _build_action_prompt(
        self,
        game_state: dict[str, Any],
        trigger: str,
        legal_actions: list[str] | None = None,
        decision_context: dict[str, Any] | None = None,
        legacy_render: bool | None = None,
    ) -> str:
        """Build the user message with formatted game context.

        Reuses the compact format from CoachEngine._format_game_context().

        ``legacy_render``: task 11 — forwarded to
        CoachEngine._format_game_context. ``True`` enables the explicit
        legacy-render mode (offline training renders annotate assumed
        defaults instead of asserting fabricated facts); ``None`` (default)
        keeps live production rendering exactly as before. When the game_state
        itself carries a ``_legacy_render_mode`` key (set by
        gate_play_decisions.build_user_message) it wins over this argument.
        """
        game_state = prepare_match_context(game_state)
        # Import and use CoachEngine's formatter for consistency. The planner
        # variant drops heavy GRE JSON dumps and trims oracle text on
        # long-resident permanents — see _format_game_context(for_planner).
        try:
            from arenamcp.coach import CoachEngine

            formatter = CoachEngine.__new__(CoachEngine)
            if "_legacy_render_mode" in game_state and legacy_render is None:
                legacy_render = bool(game_state.get("_legacy_render_mode"))
            context = formatter._format_game_context(
                game_state, for_planner=True, legacy_render=legacy_render
            )
        except Exception as e:
            logger.warning(f"Failed to use CoachEngine formatter: {e}")
            context = self._fallback_format(game_state)

        # R1/P0-5: one list must feed both the prompt and the validator.
        # The context formatter builds its Legal: line from the raw
        # game_state, which can disagree with the filtered list this
        # planner validates against (Silkguard X-cost: the prompt said
        # "Cast Silkguard [OK]" while the validator had stripped it —
        # 6 identical propose→drop cycles on 2026-07-05). Rewrite the
        # line from the effective list and name the exclusions.
        if legal_actions is not None:
            full = list(game_state.get("legal_actions") or [])
            excluded = [a for a in full if a not in legal_actions]
            # R3: numbered menu. Mana/float activations are auto-paid by the
            # engine and were the #1 source of unmatchable proposals (6 drops
            # on 2026-07-05: "tap Talisman/Forest for mana") — keep them out
            # of the menu entirely.
            menu = [
                a
                for a in legal_actions
                if a.strip().lower() not in ("action: activate_mana", "action: floatmana")
            ]
            dec_ctx = decision_context or game_state.get("decision_context") or {}
            dec_type = str(dec_ctx.get("type") or "").lower()
            if not menu and dec_type == "discard":
                option_cards = dec_ctx.get("option_cards") or []
                if not option_cards:
                    option_cards = [c.get("name") for c in game_state.get("hand", []) if c.get("name")]
                if option_cards:
                    menu = [f"Discard {c}" for c in option_cards]
            self._last_menu = menu
            if menu:
                menu_lines = "\n".join(f"  {i + 1}. {a}" for i, a in enumerate(menu))
                if dec_type == "discard":
                    count = dec_ctx.get("count", 1)
                    eff_str = f"(pick {count} by number to discard)\n{menu_lines}"
                else:
                    eff_str = f"(pick by number)\n{menu_lines}"
            else:
                eff_str = 'NONE — say "pass priority"'
            context, n = re.subn(r"(?m)^Legal: .*$", f"Legal: {eff_str}", context, count=1)
            if n == 0:
                context = f"Legal: {eff_str}\n{context}"
            if excluded:
                context += "\nEXCLUDED (autopilot cannot execute these — do NOT propose them): " + ", ".join(
                    excluded[:6]
                )

        # Build trigger description
        dec_ctx = decision_context or game_state.get("decision_context") or {}
        from arenamcp.combat_identity import combat_identity_prompt

        context += combat_identity_prompt(game_state, dec_ctx)
        if "Commander recovery:" not in context:
            from arenamcp.commander_combat import commander_block_context

            context += "\n" + "\n".join(commander_block_context(game_state, dec_ctx))
        dec_type = str(dec_ctx.get("type") or "").lower()
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
        if trigger == "decision_required" and dec_type == "discard":
            count = dec_ctx.get("count", 1)
            trigger_desc = f"Discard decision. Choose {count} card(s) to discard from hand."
        else:
            trigger_desc = trigger_descriptions.get(trigger, f"Trigger: {trigger}")

        parts = [
            f"TRIGGER: {trigger_desc}",
            "",
            context,
        ]

        # Persistent GAME PLAN (strategic spine): the top-level frame every
        # tactical decision serves. Placed before the per-turn intent so the
        # model reads "here is how we win this game" first, then "here is the
        # plan for this turn", then the immediate decision.
        strategy_context = self._strategy_context(game_state)
        if strategy_context:
            parts.append(strategy_context)

        # Locked turn intent: a single high-level plan for the whole turn,
        # captured on the first non-trivial LLM call of the turn. Subsequent
        # windows see it as TURN PLAN and are pushed to continue executing
        # against it instead of re-deriving strategy from scratch.
        current_turn = game_state.get("turn", {}).get("turn_number", 0)
        if self._turn_intent and self._turn_memo_turn == current_turn:
            parts.append(
                f"\nTURN PLAN (locked at start of turn {current_turn}):\n"
                f"  {self._turn_intent}\n"
                "Stay committed to this plan unless the board has materially "
                "changed (opponent response, lethal threat, unexpected trigger)."
            )

        # Active multi-step turn plan: the ordered list of plays we
        # committed to at the start of the turn. Show it as a status
        # checklist so the LLM can see what's done, what's next, and
        # what's still pending — and follow the plan unless something
        # material changed.
        if self._active_turn_plan is not None and self._active_turn_plan.turn_number == current_turn:
            parts.append(self._format_turn_plan_for_prompt(self._active_turn_plan))

        # Turn-consistency context: if we already planned something this turn,
        # show the LLM what we promised and what's been executed, so it stays
        # committed to the same strategy instead of re-reasoning from scratch
        # (avoids the "play Forest → then cast Giant instead of Ogre" flip).
        if self._turn_memo and self._turn_memo_turn == current_turn:
            consistency_lines = [
                "\nTURN CONSISTENCY CONTEXT:",
                f"- Earlier this turn you planned: {self._turn_memo.overall_strategy}",
            ]
            if self._turn_memo.voice_advice:
                consistency_lines.append(f'- You told the player: "{self._turn_memo.voice_advice}"')
            if self._turn_executed:
                consistency_lines.append(f"- Already executed this turn: {', '.join(self._turn_executed)}")
            consistency_lines.append(
                "- STAY COMMITTED to the strategy above unless the board has "
                "materially changed (opponent response, unexpected trigger, "
                "lethal threat). Do NOT flip-flop to a different plan just "
                "because you could."
            )
            parts.append("\n".join(consistency_lines))

        # NOTE: legal_actions, GRE request type/class, and recent-GRE context
        # are already emitted by _format_game_context(for_planner=True) above.
        # We deliberately do NOT re-append them here — duplicating those
        # blocks ~doubled the prompt size in earlier versions.
        if decision_context:
            parts.append(f"\nDecision: {json.dumps(decision_context, indent=2)}")

        from arenamcp.combat_targets import attack_target_prompt

        targets = attack_target_prompt(game_state, dec_ctx)
        if targets:
            parts.append(targets)
            from arenamcp.combat_strategy import combat_choice

            choice = combat_choice({**game_state, "decision_context": dec_ctx})
            if choice is not None:
                parts.append("Computed recipient-aware attack: " + choice.explanation)

        parts.append("\nRespond with ONLY a JSON action plan matching the schema.")

        return with_deck_reference("\n".join(parts), game_state)

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
                    cparts = [f"{v}{k.replace('CounterType_', '')[:4]}" for k, v in counters.items()]
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
                    event_strs.append(f"{evt.get('source', '?')} dealt {evt.get('amount', 0)} damage")
                elif etype == "zone_transfer":
                    event_strs.append(f"{evt.get('card', '?')} moved zones")
                elif etype == "counter_added":
                    event_strs.append(f"+{evt.get('amount', 1)} counter on {evt.get('card', '?')}")
                elif etype == "token_created":
                    event_strs.append(f"Token created: {evt.get('card', '?')}")
                elif etype == "card_revealed":
                    event_strs.append(f"Revealed: {evt.get('card', '?')}")
                elif etype == "controller_changed":
                    event_strs.append(f"{evt.get('card', '?')} changed controller")
            if event_strs:
                parts.append(f"Recent: {'; '.join(event_strs)}")

        return "\n".join(parts)

    def _parse_response(
        self,
        response: str,
        legal_actions: list[str],
        decision_context: dict[str, Any] | None = None,
        bridge_request: str | None = None,
        game_state: dict[str, Any] | None = None,
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
            # #37 (live 2026-07-06): DSV4 sporadically emits unquoted garbage
            # tokens as the pick value ('"pick": 什么人') which invalidates the
            # whole JSON. The pick intent is usually still recoverable —
            # salvage the first integer pick and resolve it against the menu
            # before giving up.
            m = re.search(r'"pick"\s*:\s*(\d+)', json_str)
            if m and self._last_menu:
                idx = int(m.group(1)) - 1
                if 0 <= idx < len(self._last_menu):
                    action = self._legal_action_to_action(self._last_menu[idx])
                    if action is not None:
                        logger.warning(
                            f"Malformed plan JSON ({e}); salvaged pick "
                            f"{m.group(1)} → {self._last_menu[idx]!r}"
                        )
                        plan.actions = [action]
                        plan.overall_strategy = f"[pick-salvage] {self._last_menu[idx]}"
                        plan.voice_advice = self._humanize_legal_action(self._last_menu[idx])
                        return plan
            logger.error(f"Failed to parse action plan JSON: {e}")
            logger.debug(f"Raw response: {response[:500]}")
            return plan

        # Accept the two shapes models emit besides {"actions": [...]}: a bare
        # list of actions, and a single top-level action ({"pick": 1,
        # "reasoning": ...}). glm-5.3-flash does the latter; with only the
        # wrapper accepted, a correct pick parsed as 0 actions and the
        # fallback heuristic passed the turn with castable spells in hand
        # (bug_20260920_231337 replay, 2026-09-22).
        if isinstance(data, list):
            data = {"actions": data}
        elif not isinstance(data, dict):
            data = {}
        elif "actions" not in data and ("pick" in data or "action_type" in data):
            data = {**data, "actions": [data]}
            if not data.get("overall_strategy"):
                data["overall_strategy"] = str(data.get("reasoning", "") or "")

        # Extract overall strategy and voice advice
        plan.overall_strategy = data.get("overall_strategy", "")
        plan.voice_advice = data.get("voice_advice", "")

        # Parse actions
        for action_data in data.get("actions", []):
            # R3: a menu pick resolves to the exact legal-action string we
            # showed — the action is legal by construction, so name
            # hallucination is structurally impossible on this path.
            action = None
            pick = action_data.get("pick") if isinstance(action_data, dict) else None
            if pick is not None:
                try:
                    idx = int(pick) - 1
                except (TypeError, ValueError):
                    idx = -1
                if 0 <= idx < len(self._last_menu):
                    entry = self._last_menu[idx]
                    if entry.lower().startswith("block with:"):
                        if not action_data.get("blocker_assignments"):
                            logger.warning("Blocker menu pick %s omitted its attacker assignments", pick)
                            continue
                        action = self._parse_action({**action_data, "action_type": "declare_blockers"})
                    else:
                        action = self._legal_action_to_action(entry)
                    if action is not None:
                        action.reasoning = str(action_data.get("reasoning", "") or "")
                        logger.debug(f"Menu pick {pick} → {self._last_menu[idx]!r}")
                if action is None:
                    logger.warning(
                        f"Planner pick {pick!r} "
                        + (
                            f"({self._last_menu[idx]!r}) has no executable mapping"
                            if 0 <= idx < len(self._last_menu)
                            else f"out of menu range (1..{len(self._last_menu)})"
                        )
                        + "; trying structured fields"
                    )
            if action is None:
                action = self._parse_action(action_data)
            if action and game_state and game_state.get("battlefield"):
                try:
                    self._bind_combat_identities(action, game_state, decision_context or {})
                except (ValueError, TypeError, KeyError) as error:
                    logger.warning("Rejecting ambiguous combat assignment: %s", error)
                    continue
            if action and self._is_action_legal(action, legal_actions, decision_context, bridge_request):
                plan.actions.append(action)
            elif action:
                logger.warning(
                    "Dropping illegal planner action: %s (%s) bridge_request=%r decision=%r not in %s",
                    action.action_type.value,
                    action.card_name,
                    bridge_request,
                    (decision_context or {}).get("type"),
                    legal_actions,
                )

        # Spoken line derives from what was actually ACCEPTED (a rejected
        # target's instruction must not survive — test_planning_consistency).
        # Keep the model's phrasing only when it refers to an accepted card;
        # otherwise fall back to the terse action recap ("Discard Mutavault
        # to hand size." survives; advice naming a dropped target doesn't).
        if any(
            action.action_type == ActionType.DECLARE_ATTACKERS
            and (action.attacker_targets or action.target_names)
            for action in plan.actions
        ):
            plan.voice_advice = plan.spoken_actions()
            return plan
        if plan.actions and plan.voice_advice:
            accepted_names = []
            for action in plan.actions:
                accepted_names.extend(action.select_card_names)
                accepted_names.extend(action.target_names)
                accepted_names.extend(action.attacker_names)
                accepted_names.extend(action.blocker_assignments.keys())
                accepted_names.extend(action.blocker_assignments.values())
                if action.card_name:
                    accepted_names.append(action.card_name)
            advice_low = plan.voice_advice.casefold()
            if any(n and n.casefold() in advice_low for n in accepted_names):
                return plan
        plan.voice_advice = plan.spoken_actions()
        return plan

    @staticmethod
    def _bind_combat_identities(action: GameAction, state: dict, context: dict) -> None:
        from arenamcp.combat_identity import blocker_id_assignments, resolve_combatant

        if action.action_type == ActionType.DECLARE_BLOCKERS and context.get("raw_blockers"):
            action.blocker_instance_assignments = blocker_id_assignments(
                action.blocker_assignments, state, context["raw_blockers"]
            )
        if action.action_type == ActionType.DECLARE_ATTACKERS:
            identities = context.get("legal_attacker_ids") or [
                a["attackerInstanceId"] for a in context.get("raw_attackers") or []
            ]
            if identities:
                action.attacker_instance_ids = [
                    resolve_combatant(name, state, identities, local_side=True)
                    for name in action.attacker_names
                ]
                if len(set(action.attacker_instance_ids)) != len(action.attacker_instance_ids):
                    raise ValueError("The same attacker was selected more than once")

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
        if any(
            entry.lower().startswith(("block with:", "attack with:", "declare attackers:"))
            for entry in legal_actions
        ):
            return plan

        # A backend error sentinel is not advice — auto-picking a real game
        # action from it submitted blind passes during the 2026-07-05 outage.
        if response and is_backend_error_text(response):
            logger.warning("Planner fallback skipped: backend error, not model output")
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
        # WP-0.4: the authoritative tag. The "[auto-pick]" prefix above stays for
        # humans reading bug reports; downstream tagging must not depend on it.
        plan.fallback_reason = FALLBACK_AUTO_PICK
        return plan

    # ------------------------------------------------------------------
    # Typed-decision planning (fable-improvements.md item 1, Phase B)
    # ------------------------------------------------------------------

    @staticmethod
    def _split_creature_list(payload: str) -> list[str]:
        """Split 'A (2/2), B (5/5)' into creature names, comma-safely.

        Card names contain commas ('Hei Bai, Forest Guardian'), so a blind
        comma split shreds them into bogus names that fail the combat
        legality subset check (#41, live 2026-07-06 — a planned attack was
        silently never submitted). (P/T) decorations mark the real
        boundaries when present; without them the payload is one name.
        """
        s = (payload or "").strip()
        if not s:
            return []
        if ")" in s:
            parts = re.split(r"\)\s*,\s*", s)
            return [(p if p.rstrip().endswith(")") else p + ")").strip() for p in parts if p.strip()]
        # Duplicate ordinals are unambiguous boundaries even when the names
        # contain commas ("Hei Bai, Forest Guardian #1, ... #2").
        boundaries = list(re.finditer(r"(#\d+)\s*,\s*", s))
        if boundaries:
            names, start = [], 0
            for boundary in boundaries:
                names.append(s[start : boundary.end(1)].strip())
                start = boundary.end()
            names.append(s[start:].strip())
            if all(re.search(r"#\d+$", name) for name in names):
                return names
        return [s]

    @staticmethod
    def _extract_first_json(text: str) -> str | None:
        """Pull the first JSON object out of a possibly prose-wrapped reply.

        Models routinely prefix a sentence before the JSON despite "reply
        ONLY with JSON" instructions (0/5 typed-decision parses on
        2026-07-05 failed this way). Strips markdown fences, extracts the
        first {...} block, and drops trailing commas.
        """
        s = (text or "").strip()
        fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
        if fence:
            s = fence.group(1).strip()
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        return re.sub(r",\s*([\]}])", r"\1", m.group(0))

    _PAY_DECLINE_SYSTEM_PROMPT = (
        "You decide whether to PAY an optional cost in a Magic: The "
        "Gathering Arena game (a 'you may pay ...' trigger or ability). "
        "Paying commits you to the effect that follows, including choosing "
        "targets for it. If the effect's targeting restriction means no "
        "opponent permanent is a legal target (so it would be forced onto "
        "your own permanents), you MUST decline. Reply ONLY with JSON: "
        '{"pay": true, "reasoning": "<one short sentence>"} — no prose '
        "before or after."
    )

    def plan_pay_or_decline(
        self,
        source_name: str,
        oracle_text: str,
        game_state: dict[str, Any],
    ) -> bool | None:
        """One-shot pay/decline call for an out-of-band optional cost.

        Returns True (pay), False (decline), or None when the LLM path is
        unavailable or unparseable — the caller picks the conservative
        default for the effect type.
        """
        if llm_circuit_open(self._backend):
            logger.info("plan_pay_or_decline skipped: model server unavailable (circuit open)")
            return None
        game_state = prepare_match_context(game_state)
        user_message = with_deck_reference(
            "\n".join(
                [
                    f"Optional cost from: {source_name or 'unknown source'}",
                    f"Effect text: {oracle_text or 'unknown'}",
                    self._decision_game_context(game_state),
                    self._strategy_context(game_state),
                    "",
                    "Should you pay this optional cost?",
                ]
            ),
            game_state,
        )
        try:
            response = self._call_llm(
                self._PAY_DECLINE_SYSTEM_PROMPT,
                user_message,
                256,
                timeout_s=min(self._timeout, 8.0),
                call_class="decision.pay",
            )
        except Exception as e:
            logger.info(f"plan_pay_or_decline LLM call failed: {e}")
            return None
        json_str = self._extract_first_json(response)
        if not json_str:
            logger.info(f"plan_pay_or_decline unparseable: {(response or '')[:120]!r}")
            return None
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            logger.info(f"plan_pay_or_decline bad JSON: {json_str[:120]!r}")
            return None
        pay = data.get("pay")
        if isinstance(pay, bool):
            logger.info(f"plan_pay_or_decline: pay={pay} ({str(data.get('reasoning', ''))[:100]})")
            return pay
        return None

    _DECISION_SYSTEM_PROMPT = (
        "You decide one pending choice in a Magic: The Gathering Arena game. "
        "You get the game state and a list of legal options, each with an "
        "option_id. Output ONLY a JSON object — no prose before or after, "
        "no markdown, English only: "
        '{"option_ids": ["<id>", ...], "reasoning": "<one short sentence>"}. '
        'Example: {"option_ids": ["tgt:123"], "reasoning": "kills the '
        'biggest threat"}. '
        "Pick between min_select and max_select options FROM THE LIST — any "
        "other id is invalid. Prefer plays that advance your board and "
        "remove the biggest threat; never pick options marked 'cannot "
        "auto-pay'. Arena-confirmed payable options are authoritative: do not "
        "reject them based on estimated mana or printed costs. Each option is "
        "payable individually now, not necessarily together; reassess after each play. "
        "Use the deck strategy to compare plays. A commander in the command zone is an available "
        "engine, not a future draw: when payable, compare deploying it against other setup spells. "
        "Count the bodies and abilities from token copies, including how they increase mana production "
        "on later turns; respect summoning sickness and colored mana requirements. Prefer establishing "
        "a useful engine over repeated setup unless survival, interaction, or a stronger concrete line "
        "justifies waiting. When choosing another spell over a payable commander, explain that tradeoff. "
        "Searching lands into hand supplies future land drops; it does not add mana sources to the "
        "battlefield or bypass land-drop limits. Do not call it immediate mana acceleration. "
        "Crew activates a Vehicle already on the battlefield; it does not cast it "
        "again or retrigger its enters ability. Crew only for a concrete benefit "
        "such as attacking, blocking, or a synergy, not merely because an opponent "
        "cast a spell. Avoid redundant crewing when the Vehicle is already a creature. "
        "A Vehicle that entered this turn cannot attack without haste, even after crewing. "
        "Summoning-sick creatures may pay crew costs, but that does not give the Vehicle haste. "
        "An enters-or-attacks trigger does not trigger from crewing; check that an attack is actually possible. "
        "Animating an artifact or land does not untap it, give it haste, or retrigger entering. "
        "If it is tapped or the payment taps it, it cannot attack or block without an actual untap effect. "
        "Check current card types: leftover power/toughness does not mean a temporary animation is still active. "
        "Do not pay to animate it without a concrete combat or other payoff. Payable does not mean free. "
        "Until-end-of-turn animation expires this turn: it cannot supply a future-turn blocker or "
        "Great Henge discount. Name a use before it expires; compare any discount with the activation "
        "cost and the greatest power you already control. All-creature-types animation can increase "
        "tribal mana only when the supplied rules count those creature types and the producers can tap this turn. "
        "For haste-granting equipment, reassess after the wearer taps or new creatures enter: "
        "moving it to an untapped summoning-sick creature can enable a useful attack or tap ability. "
        "Equip only when Arena offers it. Do not shuffle equipment without a concrete benefit, "
        "float mana without a use, or abandon valuable shroud/hexproof protection merely for an untapped wearer. "
        "Evaluate every clause of a spell: a team buff has no combat value without friendly creatures, "
        "but an unconditional draw-a-card clause can still justify cycling it to find lands or early plays. "
        "Compare that draw against spending mana needed for a useful creature or interaction; explain "
        "when casting only for the draw. Never buff an opposing creature merely to draw. "
        "For library searches, compare the offered cards' rules text and choose a complementary set "
        "up to max_select when the extra cards help. Explain the specific role of each choice. "
        "Distinguish cards going to hand from cards entering the battlefield: putting a creature onto "
        "the battlefield does not trigger 'when you cast' abilities. Consider cost and time to cast "
        "cards going to hand, and do not assume an unchosen spell mode such as entwine is active. "
        "When passing your main phase, explain the concrete constraint (unpayable creatures, "
        "no useful targets, or holding interaction), and consider all playable lands and useful payable plays. "
        "TEMPORARY EFFECTS: 'until end of turn' -X/-Y or +X/+Y expires at the end of THIS turn; on your own "
        "turn outside combat it does nothing against their attack, so use it only when it kills now "
        "(toughness <= Y) or in this turn's combat. SEQUENCING: each option is payable alone — casting one "
        "spell can leave another unpayable (a cheaper spell after a bigger one, or a card castable only now, "
        "such as under threshold), so order the plays you want. A sorcery-speed cast missing only while "
        "your own spell is on the stack returns once it resolves: pass to resolve it rather than discarding "
        "that card. Never discard a castable card for an effect that kills nothing."
    )

    def plan_decision_options(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """Choose option ids for a typed PendingDecision.

        Verified commander returns use the same deterministic policy as the
        legacy planner. Other choices use the LLM with mechanical validation,
        then a deterministic pick from the same option set.
        """
        self._last_decision_reasoning = ""
        self._last_decision_option_ids = []
        self._last_decision_target_controllers = {}
        self._last_decision_unusual_targets = {}
        self._last_decision_trace = {}
        self.last_llm_failure = ""
        context = game_state.get("decision_context") or {}
        raw = context.get("raw") or {}
        accept = decision.find("optional:accept")
        if (
            decision.request_type == "OptionalAction"
            and context.get("type") == "optional_action"
            and context.get("commander_return") is True
            and (
                self._committed_commander_return(game_state, context)
                or not self._has_deck_rule("commander_zone")
            )
            and accept is not None
            and all(value > 0 for value in decision.request_id)
            and (raw.get("gameStateId"), raw.get("msgId")) == decision.request_id
            and (
                not accept.meta.get("recipients")
                or set(accept.meta["recipients"]) == set(context.get("recipient_ids") or [])
            )
        ):
            # The log parser verifies prompt 144, ZoneTransfer, and ownership.
            # Apply the legacy commander policy in the typed path too, bound
            # to this exact request so a later optional ETB cannot inherit it.
            # A model decline here previously let Portal steal the commander.
            names = ", ".join(context.get("recipient_names") or []) or "your commander"
            self._last_decision_option_ids = ["optional:accept"]
            self._last_decision_reasoning = (
                f"Return {names} to the command zone to preserve access to your commander."
            )
            self._last_decision_trace = {"policy": "commander_return", "validated_ids": ["optional:accept"]}
            logger.info("typed-decision: returning %s to the command zone without an LLM choice", names)
            return ["optional:accept"]
        if decision.request_type == "OptionalAction" and accept is not None:
            # An opposing ward trigger asking us to pay: we aimed at that
            # permanent on purpose, and declining counters our whole effect.
            warded = ward_trigger_source(game_state, _as_int(accept.meta.get("sourceId")))
            ward = ward_of(warded)
            if (
                ward is not None
                and ward_payable(ward, game_state, targeting_mana(game_state, None)) is not False
            ):
                name = (warded or {}).get("name") or "their permanent"
                self._last_decision_option_ids = ["optional:accept"]
                self._last_decision_reasoning = f"Pay {name}'s {ward.label} so our effect is not countered."
                self._last_decision_trace = {"policy": "ward_payment", "validated_ids": ["optional:accept"]}
                logger.info("typed-decision: paying %s for %s without an LLM choice", ward.label, name)
                return ["optional:accept"]
        if decision.request_type == "Mulligan" and {"mull:keep", "mull:mull"} <= decision.option_ids():
            return self._plan_mulligan(decision, game_state)
        if decision.request_type == "Group" and "LondonMulligan" in str(decision.source_label or ""):
            bottom = self._plan_mulligan_bottom(decision, game_state)
            if bottom:
                return bottom
        decision = filter_play_options(decision, game_state)
        if not decision.options:
            if decision.request_type == "Search" and decision.selection_is_valid([]):
                return []
            return [DECLINE_DECISION]
        if decision.request_type == "SelectN":
            # "As it enters, choose a color" (Room of Refuge, live 2026-10-07):
            # the colour our hand needs and our lands lack is a counting job.
            from arenamcp.color_choice import pick_color

            color = pick_color(decision, game_state)
            if color is not None and color.obvious:
                self._last_decision_option_ids = [color.option_id]
                self._last_decision_reasoning = color.reason
                self._last_decision_trace = {"policy": "color_choice", "validated_ids": [color.option_id]}
                logger.info("typed-decision: choosing %s without an LLM choice", color.reason)
                return [color.option_id]
        if decision.request_type == "Search" and any(
            option.meta.get("identity_known") is False for option in decision.options
        ):
            logger.info("Search has unidentified cards; declining a blind tutor choice")
            return [DECLINE_DECISION]
        try:
            chosen = self._llm_decision_options(decision, game_state)
            valid = decision.option_ids()
            if decision.request_type == "ActionsAvailable":
                valid = {option.option_id for option in decision.options if option.payable is not False}
            if decision.request_type in {"Search", "CastingTimeOptions"}:
                # Never silently truncate, substitute the first card, or
                # narrate reasoning for a different set than we submit.
                if not decision.selection_is_valid(chosen):
                    return [DECLINE_DECISION]
                if decision.request_type == "CastingTimeOptions":
                    # 2026-10-06 G1 T14: "destroy" over "gain 4 life" at 4 life.
                    chosen = self._apply_mode_guard(decision, game_state, chosen)
                return chosen
            chosen = [c for c in chosen if c in valid]
            if decision.request_type == "ActionsAvailable" and len(chosen) == 1:
                from arenamcp.decisions import reasoning_choice_conflict

                # 2026-10-06 G1 T8: the reasoning said "Cycling Undulating
                # Witness" but the answer was idx:1 (Tam's Resistance).
                meant = [
                    option
                    for option in reasoning_choice_conflict(decision, chosen, self._last_decision_reasoning)
                    if option in valid
                ]
                if meant:
                    logger.warning(
                        "typed-decision: reasoning describes %s but chose %s; following the reasoning (%s)",
                        meant,
                        chosen,
                        self._last_decision_reasoning[:160],
                    )
                    chosen = meant[:1]
                    self._last_decision_option_ids = chosen
            if decision.request_type == "ActionsAvailable" and len(chosen) == 1:
                chosen = self._reject_false_kill_claim(decision, game_state, chosen)
            if decision.request_type == "ActionsAvailable" and len(chosen) == 1:
                chosen = self._apply_role_guard(decision, game_state, chosen)
            if chosen and decision.min_weight is not None:
                chosen = list(dict.fromkeys(chosen))
                if decision.selection_is_valid(chosen):
                    return chosen
                chosen = []
            if chosen and decision.request_type == "SelectTargets":
                sentinels = ([DECLINE_DECISION], [NO_TARGETS_DECISION])
                chosen = self._gate_harmful_llm_target_picks(decision, game_state, chosen)
                if chosen not in sentinels:
                    chosen = self._prefer_lethal_damage_target(decision, game_state, chosen)
                if chosen not in sentinels:
                    chosen = self._verify_shrink_pick(decision, game_state, chosen)
                if chosen not in sentinels:
                    chosen = self._avoid_unpayable_ward_targets(decision, game_state, chosen)
                self._last_decision_trace["validated_ids"] = [] if chosen in sentinels else chosen
                self._last_decision_trace["target_validation"] = (
                    "declined"
                    if chosen == [DECLINE_DECISION]
                    else "no_targets"
                    if chosen == [NO_TARGETS_DECISION]
                    else "validated"
                )
                if chosen == [DECLINE_DECISION]:
                    return chosen
                if chosen == [NO_TARGETS_DECISION]:
                    return []
                if not expand_target_selection(decision, chosen):
                    return [DECLINE_DECISION]
            if chosen:
                limit = max(decision.min_select or 1, 1)
                limit = max(limit, min(len(chosen), decision.max_select or 1))
                return chosen[:limit]
            logger.info(
                "plan_decision_options: LLM answer had no valid ids for %s",
                decision.request_type,
            )
            self.last_llm_failure = "bad_answer"
        except Exception as e:
            self.last_llm_failure = "unavailable" if is_llm_unavailable_error(e) else "bad_answer"
            logger.info(f"plan_decision_options LLM path failed: {e}")
        trace = getattr(self, "_last_decision_trace", None)
        if isinstance(trace, dict):
            trace["llm_failure"] = self.last_llm_failure
        if decision.request_type == "ActionsAvailable":
            return self._board_math_fallback(decision, game_state)
        if decision.request_type == "SelectTargets":
            picked = self.targeting_fallback_choice(decision, game_state)
            if picked == [DECLINE_DECISION]:
                # Harmful targeting forced onto own permanents OR beneficial
                # targeting forced onto opponent — never let the blind pick submit it.
                return picked
            if picked == [NO_TARGETS_DECISION]:
                return []
            if picked:
                logger.info(f"plan_decision_options: controller-aware target fallback picked {picked}")
                return picked
            return [DECLINE_DECISION]
        if decision.request_type == "SelectN":
            from arenamcp.color_choice import pick_color

            color = pick_color(decision, game_state)
            if color is not None:
                # Never White-by-position: the best-scoring colour, even when close.
                self._last_decision_option_ids = [color.option_id]
                self._last_decision_reasoning = color.reason
                logger.info("plan_decision_options: colour fallback picked %s", color.reason)
                return [color.option_id]
        return self.deterministic_option_pick(decision)

    def _board_math_fallback(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """The model gave no usable priority choice: the searched line's play, else land then board math."""
        picked, why = board_math_option_pick(decision, game_state)
        source, board_pick = "board_math", picked
        line = line_fallback_option_pick(decision, game_state, picked)
        if line is not None:
            (picked, why), source = line, "line"
        lead = (
            "Model unavailable"
            if getattr(self, "last_llm_failure", "") == "unavailable"
            else "No usable model answer"
        )
        self._last_decision_option_ids = picked
        self._last_decision_reasoning = f"{lead}; {why}."
        trace = getattr(self, "_last_decision_trace", None)
        if isinstance(trace, dict):
            trace.update(fallback=source, validated_ids=picked, fallback_reason=why)
            if source == "line":
                trace["board_math_pick"] = board_pick
        logger.warning(
            "typed-decision fallback (%s): %s -> %s%s",
            lead.lower(),
            why,
            picked,
            f" [line pick; board math: {board_pick}]" if source == "line" else "",
        )
        return picked

    def _apply_role_guard(self, decision: Any, game_state: dict[str, Any], chosen: list[str]) -> list[str]:
        """Behind on board: replace a card-draw/rock/cycling pick with a survival play.

        Deterministic and narrow (see :func:`arenamcp.board_assessment.role_guard`):
        only in survival mode, never with lethal on board, never over a land,
        creature, removal, counter or pass, and only when the alternative lowers
        the projected life loss over the next two opponent attacks. When it
        keeps the pick, the line guard (:meth:`_apply_line_guard`) looks next.
        """
        try:
            from arenamcp.board_assessment import assess, role_guard

            assessment = assess(game_state)
            verdict = role_guard(assessment, decision, chosen[0], game_state)
        except Exception as error:
            logger.debug("role guard skipped: %s", error)
            return chosen
        if verdict is None or verdict.option_id == chosen[0]:
            return self._apply_line_guard(decision, game_state, chosen, assessment)
        replaced = decision.find(chosen[0])
        logger.warning(
            "%s [model chose %s: %s]",
            verdict.reason,
            replaced.label if replaced else chosen[0],
            (self._last_decision_reasoning or "")[:160],
        )
        self._last_decision_reasoning = verdict.summary or verdict.reason.removeprefix("Role guard: ")
        self._last_decision_option_ids = [verdict.option_id]
        trace = getattr(self, "_last_decision_trace", None)
        if isinstance(trace, dict):
            trace["role_guard"] = {"replaced": chosen[0], "with": verdict.option_id, "reason": verdict.reason}
        return [verdict.option_id]

    def _apply_line_guard(
        self, decision: Any, game_state: dict[str, Any], chosen: list[str], assessment: Any
    ) -> list[str]:
        """The searched lines show the pick strictly worse than the best line: log it, or replace it.

        See :func:`arenamcp.line_guard.line_guard`: never with lethal on board,
        inside a winning line, on a pass, on their turn, with a stack, with an
        unknown-P/T creature, on a cast whose effect the search does not model
        (``line_guard.unmodelled_cast``; the decision trace's 'lines' entry
        names those casts) or without a usable search. Shadow by default
        (ARENAMCP_LINE_GUARD): the replacement is only logged and traced;
        'on' replaces the pick the way the role guard does.
        """
        result = getattr(assessment, "line_search", None)
        if assessment is None or result is None:
            return chosen  # no search, or ARENAMCP_LINE_SEARCH=0
        try:
            from arenamcp import line_guard

            verdict = line_guard.line_guard(
                result,
                decision,
                chosen[0],
                game_state,
                survival_mode=assessment.survival_mode,
                lethal_now=assessment.lethal_now,
                our_turn=assessment.our_turn,
                unknown_bodies=[u for u in assessment.unknowns if "unknown power/toughness" in u],
            )
        except Exception as error:
            logger.debug("line guard skipped: %s", error)
            return chosen
        payable = {option.option_id for option in decision.options if option.payable is not False}
        if verdict is None or verdict.option_id == chosen[0] or verdict.option_id not in payable:
            return chosen
        return self._guard_outcome(decision, chosen, verdict, applies=verdict.applies)

    def _apply_mode_guard(self, decision: Any, game_state: dict[str, Any], chosen: list[str]) -> list[str]:
        """A 'choose one' mode that dies sooner than another mode: log it, or replace it.

        See :func:`arenamcp.line_guard.mode_guard` (2026-10-06 G1 T14: Archive
        Arbiter's destroy mode at 4 life instead of gaining 4). Shadow by
        default (ARENAMCP_MODE_GUARD); 'on' applies only a non-contingent
        verdict that ``selection_is_valid`` accepts. Never with lethal on board
        (or when the board can't be assessed); ARENAMCP_LINE_SEARCH=0 turns it
        off. CastingTimeOptions only: Search choices are never changed.
        """
        if decision.request_type != "CastingTimeOptions":
            return chosen
        try:
            from arenamcp import line_guard
            from arenamcp.board_assessment import _line_search_enabled, assess

            if line_guard.guard_mode("mode") == "off" or not _line_search_enabled():
                return chosen
            if not any((option.meta or {}).get("choiceKind") == "modal" for option in decision.options):
                return chosen
            assessment = assess(game_state)
            if assessment is None:
                return chosen  # lethal unknown: never steer
            verdict = line_guard.mode_guard(
                None, decision, chosen, game_state, lethal_now=assessment.lethal_now
            )
            if verdict is None or verdict.option_id in chosen:
                return chosen
            # Review 2026-10-07: an unmodelled ('other') mode is worth 0 to the search,
            # so a verdict for or against it is contingent too: the chosen 'create a
            # 4/4 token' mode was replaced by 'gain 3 life', which died two turns sooner.
            comparison = line_guard.mode_comparison(game_state, decision)
            notes = [
                f"'{_option_text(decision, option_id)}' ({role}) not modelled"
                for option_id, role in ((chosen[0], "chosen"), (verdict.option_id, "replacement"))
                if comparison is not None and comparison.modes.get(option_id) == "other"
            ]
            if notes:
                verdict.contingent = [*verdict.contingent, *notes]
                verdict.reason += f" [contingent: {'; '.join(notes)}]"
        except Exception as error:
            logger.debug("mode guard skipped: %s", error)
            return chosen
        applies = bool(
            verdict.applies and not verdict.contingent and decision.selection_is_valid([verdict.option_id])
        )
        return self._guard_outcome(decision, chosen, verdict, applies=applies)

    def _guard_outcome(self, decision: Any, chosen: list[str], verdict: Any, *, applies: bool) -> list[str]:
        """Trace and log a line/mode guard verdict; replace the pick only when it applies."""
        kind = "Mode guard" if verdict.kind == "mode" else "Line guard"
        old, new = _option_text(decision, chosen[0]), _option_text(decision, verdict.option_id)
        trace = getattr(self, "_last_decision_trace", None)
        if isinstance(trace, dict):
            trace[f"{verdict.kind}_guard"] = {**verdict.as_trace(), "applied": applies}
        if not applies:
            setting = "shadow"
            if verdict.setting == "on":
                setting = "on, contingent: not applied" if verdict.contingent else "on, not applied"
            logger.info(
                "%s (%s): would replace %s with %s — %s",
                kind,
                setting,
                old,
                new,
                verdict.reason.removeprefix(f"{kind}: "),
            )
            return chosen
        logger.warning(
            "%s [model chose %s: %s]", verdict.reason, old, (self._last_decision_reasoning or "")[:160]
        )
        self._last_decision_reasoning = verdict.summary or verdict.reason.removeprefix(f"{kind}: ")
        self._last_decision_option_ids = [verdict.option_id]
        return [verdict.option_id]

    @staticmethod
    def _line_prompt_context(decision: Any, state: dict[str, Any]) -> tuple[Any, str, dict[str, str]]:
        """What the per-decision prompt shows from the line search: (tag source, line, unmodelled).

        ActionsAvailable on our turn: the board's line search, its 'LINES'
        summary, and the casts whose effect the search drops (option id ->
        "card (why)", see :func:`_unmodelled_options`), which the LINES line
        names. A modal CastingTimeOptions menu: the per-mode
        comparison (shared with the mode guard) and its 'MODES' line.
        Otherwise, with ARENAMCP_LINE_SEARCH=0, or on any error: (None, '', {}).
        """
        try:
            from arenamcp import line_guard

            if decision.request_type == "ActionsAvailable":
                from arenamcp.board_assessment import assess

                assessment = assess(state)
                result = getattr(assessment, "line_search", None)
                if assessment is None or result is None or not assessment.our_turn:
                    return None, "", {}
                text = line_guard.lines_summary(
                    result, unmodelled=assessment.unmodelled, pending=assessment.pending
                )
                unmodelled = _unmodelled_options(result, decision, state)
                if text and unmodelled:
                    # The 'best' line is the best of what the search can value. The note comes on
                    # top of the full line: this is the prompt's only LINES copy, alternatives kept.
                    note = " | not modelled, judge these yourself: " + "; ".join(unmodelled.values())
                    text += note if len(note) <= 110 else note[:109] + "…"
                return result, text, unmodelled
            if decision.request_type == "CastingTimeOptions":
                comparison = line_guard.mode_comparison(state, decision)
                return comparison, _modes_line(comparison, decision), {}
        except Exception as error:  # the strategic layer never blocks a decision
            logger.debug("line prompt context skipped: %s", error)
        return None, "", {}

    @staticmethod
    def _line_tag(source: Any, option: Any, state: dict[str, Any], unmodelled: dict[str, str]) -> str:
        """One option's '[LINE ...]' tag (at most 60 characters), or '' on any error.

        A cast the search can't value is tagged as such instead of with its
        undervalued line, and no option is called 'best' while one is (or,
        on a modal menu, while any mode is unmodelled).
        """
        try:
            from arenamcp.line_guard import option_note
            from arenamcp.line_search import ModeComparison

            if option.option_id in unmodelled:
                return "[LINE: effect not modelled]"
            tag = option_note(source, option, state)
            uncertain = bool(unmodelled) or (
                isinstance(source, ModeComparison) and "other" in source.modes.values()
            )
            return tag.replace("[LINE best: ", "[LINE: ", 1) if uncertain else tag
        except Exception as error:  # the strategic layer never blocks a decision
            logger.debug("line tag skipped: %s", error)
            return ""

    def _mulligans_taken(self, game_state: dict[str, Any]) -> int | None:
        """Mulligans already taken this game: the GRE count, else the ones we submitted.

        The macOS bridge snapshot carries the player's MulliganCount; the log
        state does not, so fall back to what this planner chose this game.
        """
        from arenamcp.mulligan_policy import mulligans_from_state

        known = mulligans_from_state(game_state)
        if known is not None:
            return known
        track = getattr(self, "_mulligan_track", None)
        if track and track[0] == (game_state.get("match_id") or ""):
            return track[1]
        return None

    def _note_mulligan(self, game_state: dict[str, Any], option: str, taken: int | None) -> None:
        match = game_state.get("match_id") or ""
        self._mulligan_track = (match, (taken or 0) + 1) if option == "mull:mull" else (match, 0)

    def _plan_mulligan(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """Keep or mulligan: a deterministic policy for clear hands, else the LLM.

        2026-10-06 (match 3da54de9) both games started on five cards: the LLM,
        with no mulligan policy, count, or per-card costs, mulliganed two
        clear six-card keeps. See arenamcp.mulligan_policy.
        """
        from arenamcp import mulligan_policy

        taken = self._mulligans_taken(game_state)
        state = {**game_state, "_mulligans_taken": taken} if taken is not None else game_state
        verdict = mulligan_policy.mulligan_verdict(mulligan_policy.situation(state))
        if verdict is not None:
            choice, reason = verdict
            option = "mull:keep" if choice == "keep" else "mull:mull"
            self._last_decision_option_ids = [option]
            self._last_decision_reasoning = reason[:1].upper() + reason[1:] + "."
            self._last_decision_trace = {
                "policy": "mulligan_guard",
                "mulligans_taken": taken,
                "validated_ids": [option],
                "reasoning": reason,
            }
            logger.warning(
                "Mulligan guard: %s (mulligans taken: %s): %s — deterministic policy, LLM not consulted",
                choice.upper(),
                "unknown" if taken is None else taken,
                reason,
            )
            self._note_mulligan(game_state, option, taken)
            return [option]
        chosen: list[str] = []
        try:
            answer = self._llm_decision_options(decision, state)
            chosen = [c for c in answer if c in decision.option_ids()][:1]
        except Exception as error:
            self.last_llm_failure = "unavailable" if is_llm_unavailable_error(error) else "bad_answer"
            logger.info("mulligan LLM path failed: %s", error)
        if not chosen:
            chosen = self.deterministic_option_pick(decision)
        self._note_mulligan(game_state, chosen[0], taken)
        return chosen

    def _plan_mulligan_bottom(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """London-mulligan bottoming: the LLM's pick unless it is clearly worse than the land/curve pick."""
        from arenamcp import mulligan_policy

        ids = [int(option.meta.get("instance_id") or 0) for option in decision.options]
        count = int(decision.min_select or 0)
        suggested = mulligan_policy.bottom_choice(game_state, ids, count)
        if not suggested or count != decision.max_select:
            return []
        suggested_ids = [f"grp:{identity}" for identity in suggested]
        chosen: list[str] = []
        try:
            answer = self._llm_decision_options(decision, game_state)
            chosen = list(dict.fromkeys(c for c in answer if c in decision.option_ids()))
        except Exception as error:
            logger.info("mulligan bottom LLM path failed: %s", error)
        if len(chosen) == count:
            picked = [int(c[4:]) for c in chosen]
            score = mulligan_policy.kept_score(game_state, ids, picked)
            best = mulligan_policy.kept_score(game_state, ids, suggested)
            lands = mulligan_policy.kept_land_count(game_state, ids, picked)
            total = mulligan_policy.kept_land_count(game_state, ids, [])
            needed = min(total, 2)
            if score is not None and best is not None and best - score <= 1.0 and lands >= needed:
                return chosen
            logger.warning(
                "Mulligan bottom guard: model bottomed %s (keeps %d land(s), score %.1f); bottoming %s "
                "instead (score %.1f)",
                mulligan_policy.card_names(game_state, picked),
                lands,
                score if score is not None else float("nan"),
                mulligan_policy.card_names(game_state, suggested),
                best if best is not None else float("nan"),
            )
        reason = (
            f"Bottom {mulligan_policy.card_names(game_state, suggested)}: keep lands toward two or three "
            "and the cheapest castable plays."
        )
        self._last_decision_option_ids = suggested_ids
        self._last_decision_reasoning = reason
        self._last_decision_trace = {"policy": "mulligan_bottom", "validated_ids": suggested_ids}
        return suggested_ids

    def get_decision_reasoning(self, option_ids: list[str]) -> str:
        """Return the model's reason only for the options it actually selected."""
        if option_ids == getattr(self, "_last_decision_option_ids", None):
            return getattr(self, "_last_decision_reasoning", "")
        return ""

    def _decision_source_is_harmful(self, decision: Any, game_state: dict[str, Any]) -> bool | None:
        return target_effect_is_harmful(self._decision_source_oracle(decision, game_state))

    def _decision_source_oracle(self, decision: Any, game_state: dict[str, Any]) -> str:
        """Classify the targeting decision's source spell as harmful.

        Source resolution: decision source_label matched on the stack/hand/command,
        else top of stack. Unknown or mixed effects return None, not beneficial.
        """
        stack = game_state.get("stack", []) or []
        source_label = str(getattr(decision, "source_label", "") or "").strip().lower()
        picked_entry = None
        # The request's own source instance is exact. Name/top-of-stack
        # guesses break when triggers share the stack: Seam Rip's exile
        # trigger was classified from Optimistic Scavenger's +1/+1 trigger
        # sitting on top, declining the correct enemy target (2026-09-24).
        source_id = self._decision_source_instance(game_state)
        if source_id:
            for zone in ("stack", "battlefield", "command"):
                picked_entry = next(
                    (
                        entry
                        for entry in game_state.get(zone, []) or []
                        if isinstance(entry, dict) and _as_int(entry.get("instance_id")) == source_id
                    ),
                    None,
                )
                if picked_entry is not None:
                    break
        if picked_entry is None and source_label and not source_id:
            for zone in ("stack", "hand", "command", "battlefield"):
                for entry in game_state.get(zone, []) or []:
                    if str(entry.get("name") or "").strip().lower() == source_label:
                        picked_entry = entry
                        break
                if picked_entry is not None:
                    break
        if picked_entry is None and stack and not source_id:
            picked_entry = stack[-1]
        context = game_state.get("decision_context") or {}
        context_source = _as_int(context.get("source_id") or context.get("sourceId"))
        context_matches = not source_id or source_id == context_source
        parent_id = _as_int((picked_entry or {}).get("parent_instance_id"))
        parent = self._target_objects(game_state).get(parent_id, {})
        oracle = str(
            (
                (context.get("source_oracle_text") or context.get("source_card_oracle_text"))
                if context_matches
                else ""
            )
            or (picked_entry or {}).get("oracle_text")
            or parent.get("oracle_text")
            or ""
        )
        parent_oracle = str(
            (context.get("source_card_oracle_text") if context_matches else "")
            or parent.get("oracle_text")
            or ((picked_entry or {}).get("source_card") or {}).get("oracle_text")
            or ""
        )
        # The request names the ability doing the targeting: for a modal
        # spell that is the chosen mode. 2026-10-06 18:56 (bug_20261006_185803):
        # Stingerquill Charm's mode 1 "deals 3 damage to any target" was read
        # with its deathtouch mode as one text, and the 3 damage went to our own
        # Yuriko. The whole card text stays the answer when the targeting
        # ability is unknown or says nothing either way ("Enchant creature").
        modes = "\n".join(
            text
            for text in (
                _ability_rules_text(aid) for aid in self._targeting_ability_ids(game_state, context_matches)
            )
            if text
        )
        if modes and target_effect_has_polarity(source_effect_text(modes, parent_oracle)):
            return source_effect_text(modes, parent_oracle).lower()
        return source_effect_text(oracle, parent_oracle).lower()

    @staticmethod
    def _targeting_ability_ids(game_state: dict[str, Any], context_matches: bool) -> list[int]:
        """targetingAbilityGrpId of each target slot, from the bridge payload and this request's log context."""
        payload = game_state.get("_bridge_request_payload") or {}
        sources = [payload.get("targetSelections"), payload.get("target_selections")]
        if context_matches:
            context = game_state.get("decision_context") or {}
            sources += [(context.get("raw") or {}).get("targets"), context.get("targets")]
        ids: list[int] = []
        for selections in sources:
            for selection in selections if isinstance(selections, list) else []:
                ability_id = (
                    _as_int(selection.get("targetingAbilityGrpId")) if isinstance(selection, dict) else 0
                )
                if ability_id and ability_id not in ids:
                    ids.append(ability_id)
        return ids

    @staticmethod
    def _decision_source_instance(game_state: dict[str, Any]) -> int:
        """Instance id of the pending request's source (bridge payload or log context)."""
        for container in (game_state.get("_bridge_request_payload"), game_state.get("decision_context")):
            if isinstance(container, dict):
                value = _as_int(container.get("sourceId") or container.get("source_id"))
                if value:
                    return value
        return 0

    def _battlefield_controllers(
        self, game_state: dict[str, Any]
    ) -> tuple[int | None, dict[int, int | None]]:
        """(local_seat, {instance_id: controller_seat}) for target labeling."""
        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            for p in game_state.get("players", []) or []:
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
                    break
        controllers: dict[int, int | None] = {}
        for c in self._target_objects(game_state).values():
            try:
                iid = int(c.get("instance_id") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                # Ownership does not establish current control of a stolen
                # permanent. Unknown control must remain unknown.
                controllers[iid] = _as_int(c.get("controller_seat_id")) or None
        for player in game_state.get("players", []) or []:
            seat = _as_int(player.get("seat_id"))
            if seat:
                controllers[seat] = seat
        return local_seat, controllers

    @staticmethod
    def _target_objects(game_state: dict[str, Any]) -> dict[int, dict]:
        objects = {}
        zones = game_state.get("zones") or {}
        for zone in ("battlefield", "stack", "graveyard", "exile", "command", "hand"):
            cards = game_state.get(zone, zones.get(zone, [])) or []
            for card in cards:
                if isinstance(card, dict) and (iid := _as_int(card.get("instance_id"))):
                    objects.setdefault(iid, card)
        return objects

    def get_last_decision_trace(self) -> dict:
        """Bounded targeting facts, not a full prompt or backend configuration."""
        return dict(getattr(self, "_last_decision_trace", {}))

    def _prefer_lethal_damage_target(
        self, decision: Any, game_state: dict[str, Any], chosen: list[str]
    ) -> list[str]:
        """Point fixed-damage removal at a creature it kills, or keep the spell.

        2026-10-05: Wrath of the Bloodmane (4 damage) was aimed at a 5/5 while
        a 1/2 it would kill was also attacking; the 5/5's damage was lethal.
        """
        from arenamcp.play_safety import damage_would_kill, fixed_damage_removal

        parsed = fixed_damage_removal({"oracle_text": self._decision_source_oracle(decision, game_state)})
        targets = [oid for oid in chosen if str(oid).startswith("tgt:")]
        if not parsed or len(targets) != 1:
            return chosen
        damage = parsed[0]
        cards = {card.get("instance_id"): card for card in game_state.get("battlefield", []) or []}
        local_seat, controllers = self._battlefield_controllers(game_state)

        def killable(iid: int) -> bool | None:
            card = cards.get(iid)
            if card is None or controllers.get(iid) == local_seat:
                return None
            if "planeswalker" in str(card.get("type_line") or "").lower():
                return True
            if "creature" not in str(card.get("type_line") or "").lower():
                return None
            return damage_would_kill(card, damage)

        picked = int(str(targets[0])[4:])
        if killable(picked) is not False:
            return chosen
        alternatives = []
        for option in decision.options:
            oid = str(option.option_id)
            if oid.startswith("tgt:") and oid != targets[0] and killable(int(oid[4:])) is True:
                card = cards[int(oid[4:])]
                alternatives.append(
                    (card.get("power") or 0, card.get("toughness") or 0, oid, card.get("name"))
                )
        name = cards.get(picked, {}).get("name", picked)
        if not alternatives:
            logger.warning(
                "Keeping the spell: %d damage kills no legal opposing target (picked %s)", damage, name
            )
            return [DECLINE_DECISION]
        best = max(alternatives)
        logger.warning("Retargeting: %d damage does not kill %s; %s dies instead", damage, name, best[3])
        return [best[2]]

    def _opposing_creatures(self, game_state: dict[str, Any]) -> list[dict[str, Any]]:
        local_seat, controllers = self._battlefield_controllers(game_state)
        return [
            card
            for card in game_state.get("battlefield", []) or []
            if local_seat is not None
            and controllers.get(_as_int(card.get("instance_id"))) not in (None, local_seat)
            and "creature" in str(card.get("type_line") or "").lower()
        ]

    def _reject_false_kill_claim(
        self, decision: Any, game_state: dict[str, Any], chosen: list[str]
    ) -> list[str]:
        """Drop a lone shrink/burn play whose reasoning claims a kill the effect cannot make.

        2026-10-07 18:33:29 (bug_20261007_183358): "Discarding uncastable
        Proft for {B} kills Hallway Heckler" — -3/-1 leaves a 2/3 at -1/2.
        The deterministic verdict wins: the empty answer falls through to
        the board-math fallback, which never picks an activation.
        """
        from arenamcp.play_safety import claimed_kills, kill_verdict, play_kill_reach

        option = decision.find(chosen[0])
        meta = (option.meta if option is not None else None) or {}
        action_type = str(meta.get("actionType") or "").removeprefix("ActionType_").lower()
        if action_type not in ("cast", "activate"):
            return chosen
        card = find_source(game_state, meta)
        reach = play_kill_reach(card, activation=action_type == "activate", metadata=meta)
        if reach is None:
            return chosen
        victims = claimed_kills(self._last_decision_reasoning, self._opposing_creatures(game_state))
        survivors = [victim for victim in victims if kill_verdict(reach, victim) is False]
        if not survivors:
            return chosen
        names = ", ".join(f"{v.get('name')} ({v.get('power')}/{v.get('toughness')})" for v in survivors)
        logger.warning(
            "typed-decision: the reasoning claims %s %s but %s of %s cannot kill it; not playing %s (%s)",
            "kills" if len(survivors) == 1 else "kill",
            names,
            f"-X/-{reach[1]}" if reach[0] == "shrink" else f"{reach[1]} damage",
            card.get("name") or chosen[0],
            chosen[0],
            self._last_decision_reasoning[:160],
        )
        if isinstance(self._last_decision_trace, dict):
            self._last_decision_trace["false_kill_claim"] = [v.get("name") for v in survivors]
        return []

    def _verify_shrink_pick(self, decision: Any, game_state: dict[str, Any], chosen: list[str]) -> list[str]:
        """Hold a model's -N/-N target to the kill check when it claims a kill or no combat can use a survivor.

        2026-10-07 18:33:31: Proft's -3/-1 was aimed at the 6/4 Apex Witchstalker
        "to remove the biggest crackback threat" in our main phase; it wore off
        at our end step. Their turn, or our combat under way, keeps the pick.
        """
        from arenamcp.play_safety import claimed_kills, shrink_has_use_without_kill

        if not any(str(oid).startswith("tgt:") for oid in chosen):
            return chosen
        claims = claimed_kills(self._last_decision_reasoning, self._opposing_creatures(game_state))
        if not claims and shrink_has_use_without_kill(game_state):
            return chosen
        verdict = self._prefer_lethal_shrink_target(decision, game_state, chosen)
        if verdict != chosen:
            logger.warning(
                "typed-decision: %s; overriding the model's %s (%s)",
                "its claimed kill is not borne out by the board"
                if claims
                else "a -N/-N survivor on our turn outside combat recovers before their attack",
                chosen,
                self._last_decision_reasoning[:160],
            )
        return verdict

    def targeting_fallback_choice(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """The model gave no usable target: the controller-aware pick, aimed so removal kills.

        :meth:`_targeting_fallback_pick` sorts opposing targets by power, but
        the board-math fallback casts removal only because it kills something
        (board_assessment ``_kills``). 2026-10-07 review, model down: 2 damage
        was cast for the 2/2 it kills, then aimed at a 6/6. Fixed damage and
        -N/-N go to a creature they kill, else the spell is kept (declined);
        then unpayable ward is avoided, as for model picks. Returns the same
        sentinels as the pick ([DECLINE_DECISION], [NO_TARGETS_DECISION]).
        """
        sentinels = ([DECLINE_DECISION], [NO_TARGETS_DECISION])
        picked = self._targeting_fallback_pick(decision, game_state)
        if not picked or picked in sentinels:
            return picked
        picked = self._prefer_lethal_damage_target(decision, game_state, picked)
        if picked not in sentinels:
            picked = self._prefer_lethal_shrink_target(decision, game_state, picked)
        if picked and picked not in sentinels:
            picked = self._avoid_unpayable_ward_targets(decision, game_state, picked)
        return picked

    def _prefer_lethal_shrink_target(
        self, decision: Any, game_state: dict[str, Any], chosen: list[str]
    ) -> list[str]:
        """Point -N/-N removal at a creature it kills, or keep the spell (fallback picks).

        Model picks keep their -N/-N target: shrinking a blocker or attacker
        that survives can still win a combat the model reasoned about.
        """
        from arenamcp.board_assessment import removal_reach

        oracle = self._decision_source_oracle(decision, game_state)
        reach = removal_reach({"oracle_text": oracle}) if oracle else None
        targets = [oid for oid in chosen if str(oid).startswith("tgt:")]
        if not reach or reach[0] != "shrink" or not reach[1] or len(targets) != 1:
            return chosen
        amount = int(reach[1])
        cards = {card.get("instance_id"): card for card in game_state.get("battlefield", []) or []}
        local_seat, controllers = self._battlefield_controllers(game_state)

        def killable(iid: int) -> bool | None:
            card = cards.get(iid)
            if card is None or controllers.get(iid) == local_seat:
                return None
            if "creature" not in str(card.get("type_line") or "").lower():
                return None
            toughness = card.get("toughness")
            if type(toughness) is not int:
                return None
            if toughness <= amount:
                return True
            if card.get("damaged_this_turn") or card.get("is_attacking") or card.get("is_blocking"):
                return None
            return False

        try:
            picked = int(str(targets[0])[4:])
        except ValueError:
            return chosen
        if killable(picked) is not False:
            return chosen
        alternatives = []
        for option in decision.options:
            oid = str(option.option_id)
            if not oid.startswith("tgt:") or oid == targets[0]:
                continue
            try:
                iid = int(oid[4:])
            except ValueError:
                continue
            if killable(iid) is True:
                card = cards[iid]
                alternatives.append(
                    (card.get("power") or 0, card.get("toughness") or 0, oid, card.get("name"))
                )
        name = cards.get(picked, {}).get("name", picked)
        if not alternatives:
            logger.warning(
                "Keeping the spell: -%d/-%d kills no legal opposing target (picked %s)", amount, amount, name
            )
            return [DECLINE_DECISION]
        best = max(alternatives)
        logger.warning(
            "Retargeting: -%d/-%d does not kill %s; %s dies instead", amount, amount, name, best[3]
        )
        return [best[2]]

    def _gate_harmful_llm_target_picks(
        self,
        decision: Any,
        game_state: dict[str, Any],
        chosen: list[str],
    ) -> list[str]:
        """Override mis-targeted LLM picks (harmful spells on own board or beneficial on enemy)."""
        is_harmful = self._decision_source_is_harmful(decision, game_state)
        if not self._decision_source_oracle(decision, game_state):
            logger.warning("Declining targets: effect source is unresolved")
            return [DECLINE_DECISION]
        local_seat, controllers = self._battlefield_controllers(game_state)
        if local_seat is None:
            logger.warning("Declining targets: local seat is unknown")
            return [DECLINE_DECISION]
        picked_own = False
        picked_opp = False
        intent = getattr(self, "_last_decision_target_controllers", {})
        exceptions = getattr(self, "_last_decision_unusual_targets", {})
        intentional_own = intentional_opp = True
        for oid in chosen:
            if not str(oid).startswith("tgt:"):
                continue
            try:
                iid = int(str(oid)[4:])
            except ValueError:
                continue
            ctrl = controllers.get(iid)
            if ctrl is None:
                logger.warning("Declining target %s: current controller is unknown", oid)
                return [DECLINE_DECISION]
            expected = "self" if ctrl == local_seat else "opponent"
            acknowledged = intent.get(oid)
            if acknowledged is not None and acknowledged != expected:
                logger.warning(
                    "Declining target %s: intended controller %r disagrees with seat %s",
                    oid,
                    acknowledged,
                    ctrl,
                )
                return [DECLINE_DECISION]
            if is_harmful is None and acknowledged != expected:
                logger.warning("Declining unclassified target %s without explicit controller intent", oid)
                return [DECLINE_DECISION]
            justification = exceptions.get(oid)
            justified = (
                acknowledged == expected and isinstance(justification, str) and bool(justification.strip())
            )
            if ctrl == local_seat:
                picked_own = True
                intentional_own = intentional_own and justified
            elif ctrl is not None:
                picked_opp = True
                intentional_opp = intentional_opp and justified

        if is_harmful and picked_own and not intentional_own:
            override = self._targeting_fallback_pick(decision, game_state)
            if override:
                logger.warning(f"Overriding harmful LLM target pick {chosen} (own permanent) with {override}")
                return override

        if is_harmful is False and picked_opp and not intentional_opp:
            if effect_mentions_harm(self._decision_source_oracle(decision, game_state)):
                # Moving a pick onto our own board needs an unambiguous
                # benefit; damage/removal wording anywhere means it isn't.
                logger.warning(
                    "Keeping LLM target pick %s (opponent permanent): the effect is not cleanly "
                    "beneficial, so it is not moved onto our own permanent",
                    chosen,
                )
                return chosen
            override = self._targeting_fallback_pick(decision, game_state)
            if override:
                logger.warning(
                    f"Overriding beneficial LLM target pick {chosen} (opponent permanent) with {override}"
                )
                return override

        return chosen

    def _targeting_fallback_pick(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """Controller-aware fallback for SelectTargets when the LLM failed.

        When the source spell is harmful, prefer the opponent's biggest threat.
        When it is beneficial (e.g. Feather of Flight), prefer our own biggest creature
        and NEVER buff the opponent's creatures.
        """
        candidates: list[int] = []
        for o in decision.options:
            if o.option_id.startswith("tgt:"):
                try:
                    candidates.append(int(o.option_id[4:]))
                except ValueError:
                    continue
        if not candidates:
            return []

        local_seat, controllers = self._battlefield_controllers(game_state)
        if local_seat is None:
            return []

        battlefield: dict[int, dict[str, Any]] = {}
        for card in game_state.get("battlefield", []) or []:
            try:
                iid = int(card.get("instance_id") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                battlefield[iid] = card

        harmful_opt = self._decision_source_is_harmful(decision, game_state)
        if harmful_opt is None:
            return []
        harmful = harmful_opt

        def _power(iid: int) -> int:
            try:
                return int(battlefield.get(iid, {}).get("power") or 0)
            except (TypeError, ValueError):
                return 0

        own = [iid for iid in candidates if controllers.get(iid) == local_seat]
        theirs = [iid for iid in candidates if controllers.get(iid) not in (None, local_seat)]

        if harmful:
            if not theirs and own:
                choice = self._no_targets_or_decline(decision)
                logger.warning(
                    "Targeting fallback: harmful source with only own permanents as candidates — %s",
                    "choosing no (more) targets"
                    if choice == [NO_TARGETS_DECISION]
                    else "declining instead of sacrificing one",
                )
                if choice == [NO_TARGETS_DECISION]:
                    self._note_target_choice([], "Only our own permanents are left to target; choosing none.")
                return choice
            # Prefer targets whose ward we can pay (or that have none).
            doomed = self._unpayable_ward_targets(decision, game_state)
            pool = sorted(theirs, key=lambda iid: (f"tgt:{iid}" in doomed, -_power(iid)))
        else:
            if not own and theirs:
                logger.warning(
                    "Targeting fallback: beneficial source with only opponent "
                    "permanents as candidates — declining to avoid buffing enemy"
                )
                return [DECLINE_DECISION]
            if own and effect_mentions_harm(self._decision_source_oracle(decision, game_state)):
                logger.warning(
                    "Targeting fallback: the effect is not cleanly beneficial — not aiming it at our own permanent blind"
                )
                return []
            pool = sorted(own, key=_power, reverse=True)
        if not pool:
            return []
        n = max(1, int(decision.min_select or 1))
        picked = [f"tgt:{iid}" for iid in pool[:n]]
        return picked if expand_target_selection(decision, picked) else [DECLINE_DECISION]

    @staticmethod
    def _no_targets_or_decline(decision: Any) -> list[str]:
        """Answer an optional SelectTargets with no (more) targets, else decline.

        A cancellable request with nothing selected yet is a cast or activation
        in progress: cancelling keeps the card and the mana. Triggers cannot be
        cancelled (Seasoned Cryomancer's stun, AllowCancel_No), and a request
        that already holds a target only needs its selection committed — what
        the user did by hand at 17:48:41 on 2026-10-06.
        """
        if not decision.selection_is_valid([]):
            return [DECLINE_DECISION]
        if decision.can_cancel and not any(slot.selected for slot in decision.slots):
            return [DECLINE_DECISION]
        return [NO_TARGETS_DECISION]

    def _note_target_choice(self, option_ids: list[str], reason: str) -> None:
        """Narrate a deterministic target override instead of the model's stale reason."""
        self._last_decision_option_ids = option_ids
        self._last_decision_reasoning = reason
        trace = getattr(self, "_last_decision_trace", None)
        if isinstance(trace, dict):
            trace["target_override"] = reason

    def _unpayable_ward_targets(self, decision: Any, game_state: dict[str, Any]) -> dict[str, str]:
        """Opposing target options whose ward we cannot pay, mapped to why.

        Mana is what stays untapped once the source is paid for: a spell is
        targeted before its cost is paid, a trigger's cost is already paid.
        """
        try:
            return self._find_unpayable_ward_targets(decision, game_state)
        except Exception as error:  # ward awareness must never break targeting
            logger.debug("ward check skipped: %s", error)
            return {}

    def _find_unpayable_ward_targets(self, decision: Any, game_state: dict[str, Any]) -> dict[str, str]:
        local_seat, controllers = self._battlefield_controllers(game_state)
        if local_seat is None:
            return {}
        battlefield = {
            _as_int(card.get("instance_id")): card
            for card in game_state.get("battlefield", []) or []
            if isinstance(card, dict)
        }
        source = self._target_objects(game_state).get(self._decision_source_instance(game_state))
        mana = targeting_mana(game_state, source)
        doomed: dict[str, str] = {}
        for option in decision.options:
            iid = _as_int(str(option.option_id)[4:]) if str(option.option_id).startswith("tgt:") else 0
            card = battlefield.get(iid)
            if card is None or controllers.get(iid) in (None, local_seat):
                continue
            ward = ward_of(card)
            if ward is not None and ward_payable(ward, game_state, mana, local_seat) is False:
                mana_text = f" with {mana} mana available" if ward.mana is not None else ""
                doomed[option.option_id] = f"{card.get('name') or iid} has {ward.label}{mana_text}"
        return doomed

    def _avoid_unpayable_ward_targets(
        self, decision: Any, game_state: dict[str, Any], chosen: list[str]
    ) -> list[str]:
        """Never aim at a permanent whose ward we cannot pay; ward counters the whole effect.

        2026-10-06 17:48 (bug_20261006_174855): Seasoned Cryomancer's stun went
        at Unflinching Hortimancer (Ward {1}) with every land tapped, so the
        ward trigger countered it. Prefer another legal enemy target; with none
        left, answer an optional request with no targets (or cancel a cast in
        progress). A required target keeps the existing choice.
        """
        doomed = self._unpayable_ward_targets(decision, game_state)
        dropped = [oid for oid in chosen if oid in doomed]
        if not dropped:
            return chosen
        kept = [oid for oid in chosen if oid not in doomed]
        if self._decision_source_is_harmful(decision, game_state) is True:
            local_seat, controllers = self._battlefield_controllers(game_state)
            power = {
                _as_int(card.get("instance_id")): _as_int(card.get("power"))
                for card in game_state.get("battlefield", []) or []
                if isinstance(card, dict)
            }
            spare = [
                option.option_id
                for option in decision.options
                if str(option.option_id).startswith("tgt:")
                and option.option_id not in chosen
                and option.option_id not in doomed
                and controllers.get(_as_int(option.option_id[4:])) not in (None, local_seat)
            ]
            spare.sort(key=lambda oid: power.get(_as_int(oid[4:]), 0), reverse=True)
            kept += spare[: len(dropped)]
        why = "; ".join(doomed[oid] for oid in dropped)
        reason = f"{why}, which would counter the whole effect."
        if kept and expand_target_selection(decision, kept):
            logger.warning("Ward: %s — targeting %s instead of %s", why, kept, chosen)
            self._note_target_choice(kept, reason)
            return kept
        if not kept and decision.selection_is_valid([]):
            answer = self._no_targets_or_decline(decision)
            logger.warning(
                "Ward: %s — %s instead of %s",
                why,
                "choosing no targets" if answer == [NO_TARGETS_DECISION] else "cancelling",
                chosen,
            )
            if answer == [NO_TARGETS_DECISION]:
                self._note_target_choice([], reason)
            return answer
        logger.info("Ward: %s, but the target is required; keeping %s", why, chosen)
        return chosen

    _DECISION_MAX_TOKENS = 2048

    def _llm_decision_options(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        # Circuit open: fail before formatting a 25k-character prompt, so the
        # caller's deterministic pick lands at once instead of after 12 s.
        if llm_circuit_open(getattr(self, "_backend", None)):
            raise LLMUnavailableError("model server unavailable (circuit open)")
        # Tags and LINES read the snapshot the guards assess (one search, cached).
        line_source, lines_text, unmodelled = self._line_prompt_context(decision, game_state)
        tag_state = game_state
        line_tags: dict[str, str] = {}
        game_state = prepare_match_context(game_state)
        lines = [
            f"PENDING DECISION: {decision.request_type}"
            + (f" (source: {decision.source_label})" if decision.source_label else ""),
            f"Choose at least {decision.min_select} and at most {decision.max_select} option(s).",
            "OPTIONS:",
        ]
        if decision.min_weight is not None:
            lines.insert(
                2,
                f"Required total contribution: {decision.min_weight} to {decision.max_weight}. "
                "Sum the selected options' contributions, not their count. "
                "For crew, tap creatures totaling the required power; preserve useful attackers/blockers. "
                "Summoning-sick creatures may crew. Crewing does not let a newly entered Vehicle attack.",
            )
        for slot in decision.slots:
            lines.insert(
                -1,
                f"Target slot {slot.target_idx}: choose {slot.needs} to "
                f"{max(0, slot.max_targets - slot.selected)} additional targets from "
                + ", ".join(f"tgt:{instance_id}" for instance_id in slot.candidate_ids),
            )
        # #38: without controller labels the model cannot tell its own
        # permanents from the opponent's in a target list (live 2026-07-06:
        # it aimed Utter Insignificance at the user's own Nessian Wanderer).
        local_seat, controllers = self._battlefield_controllers(game_state)
        target_objects = self._target_objects(game_state)
        target_trace = []
        if decision.request_type == "SelectTargets":
            lines.insert(1, f"YOU ARE SEAT {local_seat}; control determines YOURS/opponent, not ownership.")
            lines.insert(
                2,
                "SOURCE EFFECT: "
                + (self._decision_source_oracle(decision, game_state) or "unknown")
                + power_only_note(self._decision_source_oracle(decision, game_state)),
            )
            lines.append(
                "For each selected target also return target_controllers keyed by option_id, "
                "with value self or opponent matching its CURRENT controller below. "
                "For deliberately harming your own target or benefiting an opponent, include "
                "unusual_target_reasons keyed by that same option_id explaining the concrete benefit. "
                "Do not call your own creature an opponent's threat. Unknown control is not permission "
                "to infer a side from the card name or deck reference."
            )
        commander_ids = {
            card.get("instance_id")
            for card in game_state.get("command", []) or []
            if local_seat is not None and card.get("owner_seat_id") == local_seat
        }
        land_drop = decision.request_type == "ActionsAvailable" and untapped_land_drop(
            game_state, decision.options
        )
        battlefield_ids = {
            _as_int(card.get("instance_id")) for card in game_state.get("battlefield", []) or []
        }
        ward_mana = None
        if decision.request_type == "SelectTargets":
            ward_mana = targeting_mana(
                game_state, target_objects.get(self._decision_source_instance(game_state))
            )
        warded_targets = False
        for o in decision.options:
            note = ""
            label = o.label
            if o.payable is False:
                note = "  [cannot auto-pay — do not pick]"
            elif o.payable is True:
                note = "  [Arena confirms payable now]"
            if line_source is not None and o.payable is not False:
                tag = self._line_tag(line_source, o, tag_state, unmodelled)
                if tag:
                    note += f"  {tag}"
                    line_tags[o.option_id] = tag
            if o.meta.get("actionType") == "ActionType_Cast" and o.meta.get("instanceId") in commander_ids:
                note += "  [YOUR COMMANDER — command zone]"
            if "weight" in o.meta:
                note += f"  [contribution: {o.meta['weight']}]"
            if o.meta.get("actionType") == "ActionType_Cast":
                note += linked_cast_note(game_state, o.meta)
                note += ward_cast_note(game_state, o.meta, land_drop=land_drop)
                note += power_only_note((find_source(game_state, o.meta) or {}).get("oracle_text"))
                note += shrink_note(game_state, find_source(game_state, o.meta) or {}, activation=False)
            if decision.request_type == "CastingTimeOptions":
                note += power_only_note(label)
            if o.meta.get("actionType") == "ActionType_Activate":
                source = find_source(game_state, o.meta)
                note += shrink_note(game_state, source, activation=True, metadata=o.meta)
                note += " " + json.dumps(
                    {
                        "manaCost": o.meta.get("manaCost"),
                        "source_tapped": source.get("is_tapped"),
                        "turn_entered_battlefield": source.get("turn_entered_battlefield"),
                        "current_card_types": source.get("card_types"),
                        "payment_taps_source": any(
                            payment.get("instanceId") == source.get("instance_id")
                            for payment in o.meta.get("autoTapActions") or []
                        ),
                    }
                )
            if decision.request_type in {"CastingTimeOptions", "OptionalAction", "Search"}:
                note += " " + json.dumps(o.meta, ensure_ascii=False)
            side = ""
            if o.option_id.startswith("tgt:"):
                try:
                    ctrl = controllers.get(int(o.option_id[4:]))
                except ValueError:
                    ctrl = None
                if ctrl is not None and local_seat is not None:
                    side = " (YOURS)" if ctrl == local_seat else " (opponent's)"
                else:
                    side = " (CURRENT CONTROLLER UNKNOWN)"
                card = target_objects.get(int(o.option_id[4:]), {})
                label = card.get("name") or label
                facts = {
                    "option_id": o.option_id,
                    "name": label,
                    "controller_seat_id": ctrl,
                    "owner_seat_id": card.get("owner_seat_id"),
                }
                ward = ward_of(card) if _as_int(o.option_id[4:]) in battlefield_ids else None
                if ward is not None and ctrl not in (None, local_seat) and ward_mana is not None:
                    warded_targets = True
                    facts["ward"] = ward.label
                    facts["ward_payable_now"] = ward_payable(ward, game_state, ward_mana, local_seat)
                target_trace.append(facts)
                note += " " + json.dumps(
                    {key: value for key, value in facts.items() if key not in {"option_id", "name"}}
                )
            lines.append(f"- {o.option_id}: {label}{side}{note}")
        if warded_targets:
            lines.insert(
                lines.index("OPTIONS:"),
                "WARD: targeting an opponent's permanent that has ward counters this WHOLE spell or "
                f"ability (every target) unless you pay the ward cost. Mana available to pay it: {ward_mana}. "
                "Do not pick a target marked ward_payable_now=false.",
            )
        if lines_text:
            # Next to the menu: the strategy block comes after ~20k characters of game state.
            lines.insert(lines.index("OPTIONS:"), lines_text)
        if decision.request_type == "CastingTimeOptions":
            lines.append(
                "Choose all required modes together from the SAME childIndex. "
                "Honor that child's min/max counts; do not mix modes with Done or another child."
            )
        if decision.request_type == "Mulligan":
            from arenamcp.mulligan_policy import MULLIGAN_POLICY

            # The hand facts (count, resulting size, play/draw, per-card
            # castability) come from the GAME STATE mulligan section below.
            lines.append(MULLIGAN_POLICY)
        elif decision.request_type == "Group" and "LondonMulligan" in str(decision.source_label or ""):
            from arenamcp import mulligan_policy

            ids = [int(o.meta.get("instance_id") or 0) for o in decision.options]
            suggested = mulligan_policy.bottom_choice(game_state, ids, decision.min_select)
            lines.append(
                f"LONDON MULLIGAN BOTTOM: the option_ids you choose go to the BOTTOM of your library; you keep "
                f"{len(ids) - decision.min_select}. Keep lands toward 2-3 (3 when keeping 6), cheap castable "
                "plays and bombs; bottom expensive, uncastable, or redundant cards."
            )
            lines.extend(mulligan_policy.describe(game_state, decision.min_select)[1:])
            if suggested:
                lines.append(
                    "Land/curve suggestion: bottom " + mulligan_policy.card_names(game_state, suggested) + "."
                )
        lines.append("")
        lines.append("GAME STATE:")
        context_state = game_state
        if decision.request_type == "ActionsAvailable":
            context_state = {
                **game_state,
                "_bridge_request_type": "ActionsAvailable",
                "_bridge_request_class": "ActionsAvailableRequest",
                "_bridge_can_pass": decision.can_pass,
                "_bridge_actions": [
                    {**option.meta, "hasAutoTap": option.payable is True}
                    if option.meta
                    else {"actionType": "ActionType_Pass"}
                    for option in decision.options
                    if option.meta or option.option_id == "pass"
                ],
            }
        lines.append(self._decision_game_context(context_state))
        # The LINES line is shown once, above OPTIONS: not again in the strategy block.
        strategy_context = self._strategy_context(game_state, with_lines=not lines_text.startswith("LINES"))
        if strategy_context:
            lines.append(strategy_context)
        user_message = with_deck_reference("\n".join(lines), game_state)
        self._last_decision_trace = {
            "request_type": decision.request_type,
            "request_id": list(decision.request_id),
            "source": decision.source_label,
            "source_instance_id": self._decision_source_instance(game_state),
            "source_oracle_text": self._decision_source_oracle(decision, game_state)[:1200]
            if decision.request_type == "SelectTargets"
            else "",
            "local_seat_id": local_seat,
            "targets": target_trace[:80],
        }
        if lines_text or line_tags:
            self._last_decision_trace["lines"] = {"summary": lines_text, "tags": line_tags}
            if unmodelled:
                self._last_decision_trace["lines"]["unmodelled"] = dict(unmodelled)

        # Tighter than the general planning timeout: typed decisions
        # (mulligan, targeting, selection) sit inside short MTGA action
        # windows, and the deterministic fallback needs time to submit
        # before the window closes (2026-07-01: mulligan window expired
        # while the LLM call was still blocked).
        # 512 tokens left no room for the answer: glm-5.3-flash spent all
        # 512 on reasoning (finish_reason=length, 7x on 2026-10-04) and the
        # autopilot passed its turn 12 with Tooth and Nail castable. The
        # time budget above, not the token cap, bounds latency.
        response = self._call_llm(
            self._DECISION_SYSTEM_PROMPT + "\n" + STRATEGIC_POLICY,
            user_message,
            self._DECISION_MAX_TOKENS,
            timeout_s=min(self._timeout, 12.0),
            call_class=(
                "decision.mulligan"
                if decision.request_type == "Mulligan" or "LondonMulligan" in str(decision.source_label or "")
                else "decision.typed"
            ),
            first_token_timeout_s=self._FIRST_TOKEN_TIMEOUT_S,
        )

        # P1-1: models prose-prefix the JSON despite "reply ONLY with JSON"
        # (0/5 typed-decision parses on 2026-07-05, one reply in Chinese) —
        # extract the first JSON object instead of parsing the whole string,
        # and log a preview when even that fails so the failure is
        # diagnosable from the log.
        json_str = self._extract_first_json(response)
        if not json_str:
            logger.info(f"typed-decision: no JSON object in response: {(response or '')[:160]!r}")
            raise ValueError("typed-decision response contained no JSON object")
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            logger.info(f"typed-decision: bad JSON: {json_str[:160]!r}")
            raise
        if not isinstance(data.get("option_ids"), list):
            raise ValueError("typed-decision response must contain an option_ids list")
        ids = data["option_ids"]
        if any(type(option_id) not in (str, int) for option_id in ids):
            raise ValueError("typed-decision option_ids must contain only ids")
        chosen = [str(option_id) for option_id in ids]
        reason = data.get("reasoning")
        self._last_decision_target_controllers = (
            data.get("target_controllers") if isinstance(data.get("target_controllers"), dict) else {}
        )
        self._last_decision_unusual_targets = (
            data.get("unusual_target_reasons") if isinstance(data.get("unusual_target_reasons"), dict) else {}
        )
        self._last_decision_reasoning = " ".join(reason.split()[:50]) if isinstance(reason, str) else ""
        self._last_decision_option_ids = chosen
        self._last_decision_trace.update(
            selected_ids=chosen,
            reasoning=self._last_decision_reasoning,
            target_controllers=self._last_decision_target_controllers,
        )
        logger.info(
            "typed-decision choice %s: %s", chosen, self._last_decision_reasoning or "reason not supplied"
        )
        return chosen

    def _decision_game_context(self, game_state: dict[str, Any]) -> str:
        """The planner's full board view (card text, mana, combat math).

        Typed decisions — every bridge priority window since Phase E — used
        the names-only fallback formatter. 2026-09-24 the model passed turns 7
        and 9 stuck on three lands with Archdruid's Charm castable: it could
        not see that the charm fetches a land.
        """
        try:
            from arenamcp.coach import CoachEngine

            formatter = CoachEngine.__new__(CoachEngine)
            context = formatter._format_game_context(game_state, for_planner=True)
            if context and context.strip():
                return context
        except Exception as e:
            logger.warning(f"typed-decision context formatter failed: {e}")
        return self._fallback_format(game_state)

    @staticmethod
    def deterministic_option_pick(decision: Any) -> list[str]:
        """Mechanical fallback: pick from the option set, never outside it."""
        opts = list(decision.options)
        if not opts:
            return []
        if decision.request_type == "ActionsAvailable":
            opts = [option for option in opts if option.payable is not False]
            if not opts:
                return []
            for o in opts:
                if (o.meta or {}).get("actionType") == "ActionType_Play":
                    return [o.option_id]
            for o in opts:
                if o.option_id == "pass":
                    return [o.option_id]
            return [opts[0].option_id]
        if decision.request_type == "Mulligan":
            return ["mull:keep"]
        if decision.request_type in {"CastingTimeOptions", "OptionalAction", "Search"}:
            return [DECLINE_DECISION]
        if decision.min_weight is not None:
            from itertools import combinations, islice

            candidates = (
                list(selection)
                for count in range(max(1, decision.min_select), min(len(opts), decision.max_select) + 1)
                for selection in combinations([option.option_id for option in opts], count)
            )
            for selection in islice(candidates, 10000):
                if decision.selection_is_valid(selection):
                    return selection
            return []
        n = max(1, int(decision.min_select or 1))
        return [o.option_id for o in opts[:n]]


# --- priority play without the model ---------------------------------------------

# Cast roles that need a target or a moment only the model would pick
# (counterspells, combat tricks): never cast blind on our own main phase.
_REACTIVE_CAST_ROLES = frozenset({"counter", "pump"})


def _accepts_keyword(fn: Callable, name: str) -> bool:
    """``fn`` takes the keyword argument ``name`` (or any keyword); False when it can't be inspected."""
    try:
        parameters = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        (p.name == name and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)) or p.kind == p.VAR_KEYWORD
        for p in parameters
    )


def _local_seat(state: dict[str, Any]) -> Any:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next(
            (
                player.get("seat_id")
                for player in state.get("players") or []
                if isinstance(player, dict) and player.get("is_local")
            ),
            None,
        )
    return seat


def _own_main_phase_with_empty_stack(state: dict[str, Any]) -> bool:
    turn = state.get("turn") or {}
    local = _local_seat(state)
    if local is None or turn.get("active_player") != local:
        return False
    return "main" in str(turn.get("phase") or "").lower() and not (state.get("stack") or [])


def _line_text(step: Any) -> str:
    """Speakable board-math line for this turn ("plays Island, then casts A and B")."""
    parts = []
    if step.land:
        parts.append(f"plays {step.land}")
    casts = list(step.casts)
    if casts:
        parts.append("casts " + (", ".join(casts[:-1]) + " and " + casts[-1] if len(casts) > 1 else casts[0]))
    return "the board-math line " + (", then ".join(parts) if parts else "casts nothing payable this turn")


def board_math_option_pick(decision: Any, game_state: dict[str, Any]) -> tuple[list[str], str]:
    """A priority-window choice without the model: land drop, then the board-math line.

    bug_20261006_185403 (18:53:53): with the model timing out, the old
    fallback ("play a land, otherwise pass") passed turn 10 with four payable
    creatures while board_assessment scheduled Geist of Saint Thalia +
    Theoretical Necromancer. This applies only on our own main phase with an
    empty stack; elsewhere the land-else-pass pick stands (no blind
    instant-speed plays). It is recomputed every window, so the next
    scheduled spell follows from the new board. When the searched best line
    casts nothing this turn (holding is never the fallback's choice), the
    greedy schedule's casts stand in. It never picks an unpayable option, an
    X spell, an activation, or a counterspell / combat trick.

    Returns ``(option_ids, reason)``.
    """
    from arenamcp.mana import mana_cost_to_cmc

    fallback = ActionPlanner.deterministic_option_pick(decision)
    if decision.request_type != "ActionsAvailable" or not _own_main_phase_with_empty_stack(game_state):
        return fallback, "outside our main phase only a land drop or a pass is safe"
    assessment, option_role = None, None
    step, plan_text, scheduled = None, "", set()
    greedy: set[str] = set()
    try:
        from arenamcp.board_assessment import assess, option_role

        assessment = assess(game_state)
        if assessment is not None and assessment.our_turn and assessment.lookahead:
            first = assessment.lookahead[0]
            plan_text, scheduled = _line_text(first), set(first.casts)
            step = first
            # The searched best line may hold every spell this turn (the opponent policy
            # values our crackback, so a line that attacks and casts nothing can edge out
            # the cast: 2026-10-07, this very board after Necromancer resolved). Without
            # the model, holding is never the fallback: the greedy schedule's T casts
            # (the search's pinned baseline) stand in when the best line casts nothing.
            if not scheduled:
                baseline = getattr(getattr(assessment, "line_search", None), "baseline", None)
                steps = getattr(baseline, "steps", None) or ()
                greedy = set(getattr(steps[0], "casts", ()) or ()) if steps else set()
    except Exception as error:  # the strategic layer never blocks a decision
        logger.debug("board-math fallback: no assessment: %s", error)
        assessment = None
    options = [option for option in decision.options if option.payable is not False]

    def name_of(option: Any) -> str:
        return str(find_source(game_state, option.meta or {}).get("name") or "")

    lands = [option for option in options if (option.meta or {}).get("actionType") == "ActionType_Play"]
    if lands:
        wanted = str(step.land or "") if step is not None else ""
        pick = (
            next((option for option in lands if wanted and name_of(option) == wanted), None)
            or next((option for option in lands if untapped_land_drop(game_state, [option])), None)
            or lands[0]
        )
        return [pick.option_id], plan_text or "no board assessment, so the land drop comes first"

    picks: list[tuple[int, Any]] = []
    for option in options:
        meta = option.meta or {}
        if option.payable is not True or meta.get("actionType") != "ActionType_Cast":
            continue
        card = find_source(game_state, meta)
        name = str(card.get("name") or "")
        cost = str(card.get("mana_cost") or "")
        if not name or "{x}" in cost.lower():
            continue
        try:
            role = option_role(option, game_state) if option_role is not None else "other"
        except Exception:
            role = "other"
        if role in _REACTIVE_CAST_ROLES:
            continue
        if step is not None:
            if name not in scheduled and name not in greedy:
                continue
        elif assessment is not None or role not in ("creature", "planeswalker"):
            continue
        picks.append((mana_cost_to_cmc(cost), option))
    if picks:
        # Most expensive first: Arena's autotap then keeps cheaper colours open
        # for the rest of the line, re-planned from the new board next window.
        _, option = max(picks, key=lambda item: item[0])
        if step is not None and not scheduled:
            casts = sorted(greedy)
            plan_text = (
                "the best searched line holds this turn, so the greedy schedule's cast stands in: "
                + (", ".join(casts[:-1]) + " and " + casts[-1] if len(casts) > 1 else casts[0])
            )
        return [option.option_id], plan_text or "no board assessment, so the biggest payable creature"
    if any(option.option_id == "pass" for option in options):
        return ["pass"], plan_text or "nothing safe to cast"
    return fallback, "no pass option, so the first legal option"


def _option_text(decision: Any, option_id: str) -> str:
    """An option's label for logs, without the ability text in brackets or a final period."""
    option = decision.find(option_id)
    label = str(getattr(option, "label", "") or option_id)
    return label.split(" [", 1)[0].strip().rstrip(".") or option_id


def _unmodelled_options(result: Any, decision: Any, state: dict[str, Any]) -> dict[str, str]:
    """Payable casts whose effect the line search drops: option id -> "card (why)".

    The search values tokens it can't read, an aura, a pump and the like as
    nothing (``line_guard.unmodelled_cast``, the check the line guard itself
    applies), so the line starting with that cast is no evidence against it:
    its tag must not call it worse, and the LINES line names it (review
    2026-10-07: two hasty 3/1s that were exactly lethal were tagged 'dead T11').
    """
    from arenamcp.line_guard import unmodelled_cast

    found: dict[str, str] = {}
    for option in getattr(decision, "options", ()) or ():
        meta = option.meta or {}
        if option.payable is False or "cast" not in str(meta.get("actionType") or "").lower():
            continue
        why = unmodelled_cast(result, option, state)
        if why:
            found[option.option_id] = why
    return found


def _modes_line(comparison: Any, decision: Any, max_chars: int = 320) -> str:
    """The 'MODES ...' line for a modal menu: each mode's searched outcome, or ''.

    The strategy block's role and board facts come from ``assess``, which
    does not resolve this pending choice (2026-10-06 G1 T14: 'ALL-IN: no
    defensive line survives' next to a gain-4 mode that survives T15), so
    the line says which of the two already counts the choice.
    """
    try:
        from arenamcp.line_guard import _life_parts

        if comparison is None or not getattr(comparison, "complete", False):
            return ""
        head = (
            "MODES (each searched with this choice resolved; the role and board facts below were "
            "computed before it resolves): "
        )
        parts = []
        for option in decision.options:
            line = comparison.lines.get(option.option_id)
            if line is None:
                continue
            kind = comparison.modes.get(option.option_id) or _option_text(decision, option.option_id)
            if kind == "other":
                kind = _option_text(decision, option.option_id).split(": ", 1)[-1][:48] + " (not modelled)"
            outcome = ", ".join(_life_parts(line, modal=True)) or line.outcome_text()
            parts.append(f"{kind} — {outcome}")
        if len(parts) < 2:
            return ""
        text = head + " | ".join(parts)
        return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
    except Exception as error:  # the strategic layer never blocks a decision
        logger.debug("modes line skipped: %s", error)
        return ""


def line_fallback_option_pick(
    decision: Any, game_state: dict[str, Any], board_pick: list[str]
) -> tuple[list[str], str] | None:
    """The best searched line's first play when the model gave no usable answer, or None.

    ``line_guard.line_fallback_pick`` ranks the best line's own plays: a burn
    spell that wins now, removal on a creature that can attack, a creature,
    then the land that pays for the turn. Since the board-math pick
    (``board_math_option_pick``) already follows the best line's T step, it
    stays when it is that line's own land (land first) or another of its casts
    while the line's pick is not aimed (most expensive first, so Arena's
    autotap keeps cheaper colours open). The line's pick is used when it is
    aimed (burn / removal on an attacker) or the board-math pick is not part of
    the best line (e.g. a pass). Our main phase with an empty stack only, and
    casts only when Arena confirms them payable; None (the board-math pick)
    without a usable search, with ARENAMCP_LINE_SEARCH=0, or on any error.
    """
    if decision.request_type != "ActionsAvailable" or not _own_main_phase_with_empty_stack(game_state):
        return None
    try:
        from arenamcp.board_assessment import assess
        from arenamcp.line_guard import line_fallback_pick
        from arenamcp.line_search import action_key

        assessment = assess(game_state)
        result = getattr(assessment, "line_search", None)
        if assessment is None or result is None:
            return None
        picked = line_fallback_pick(result, decision, game_state)
        if not picked or len(picked) != 1 or picked == board_pick:
            return None
        option = decision.find(picked[0])
        if option is None:
            return None
        meta = option.meta or {}
        if meta.get("actionType") != "ActionType_Play" and option.payable is not True:
            return None  # the board-math rule: never cast on an unconfirmed autotap
        best = result.best
        aimed = dict(best.steps[0].targets) if best.steps else {}
        line_aimed = str(find_source(game_state, meta).get("name") or "") in aimed
        current = decision.find(board_pick[0]) if len(board_pick) == 1 else None
        if current is not None and current.option_id != "pass":
            key = action_key(current, game_state)
            if key is not None and key in best.first_actions:
                if (current.meta or {}).get("actionType") == "ActionType_Play" or not line_aimed:
                    return None  # the same line, in the board-math order
        return picked, f"the best searched line is {best.summary()}"
    except Exception as error:  # the strategic layer never blocks a decision
        logger.debug("line fallback pick skipped: %s", error)
        return None


def board_math_legacy_plan(
    game_state: dict[str, Any],
    legal_actions: list[str] | None,
    trigger: str = "",
    *,
    lead: str = "Model unavailable",
    fallback_reason: str = FALLBACK_LLM_UNAVAILABLE,
) -> ActionPlan:
    """:func:`board_math_option_pick` for the legacy legal-action-string path.

    Only "Play Land: X", "Cast X" (from hand, or our commander from the command
    zone; payable only with "[OK]") and "Pass" take part. Returns an empty plan
    when none of them is legal, so the caller's other nets (safe defaults,
    manual-required) still apply.
    """
    from arenamcp.decisions import DecisionOption, PendingDecision

    turn = int(((game_state.get("turn") or {}).get("turn_number")) or 0)
    plan = ActionPlan(trigger=trigger, turn_number=turn, fallback_reason=fallback_reason)
    hand: dict[str, dict] = {}
    cards = list(game_state.get("hand") or [])
    if game_state.get("command"):
        # Our commanders in the command zone: the board math casts them like hand cards.
        from arenamcp.board_model import our_commanders

        cards += [c.card for c in our_commanders(game_state)]
    for card in cards:
        if isinstance(card, dict) and card.get("name"):
            hand.setdefault(str(card["name"]).casefold(), card)
    options: list[Any] = []
    texts: dict[str, str] = {}
    for index, entry in enumerate(legal_actions or []):
        text = str(entry or "").strip()
        lower = text.lower()
        if lower == "pass":
            if "pass" not in texts:
                options.append(DecisionOption("pass", "Pass"))
                texts["pass"] = text
            continue
        if lower.startswith("play land:"):
            kind, rest, payable = "ActionType_Play", text.split(":", 1)[1], None
        elif lower.startswith("cast "):
            kind, rest, payable = "ActionType_Cast", text[5:], "[ok]" in lower
        else:
            continue
        card = hand.get(re.sub(r"\s*\[[^\]]*\]", "", rest).strip().casefold())
        if card is None:
            continue
        option_id = f"legal:{index}"
        meta = {"actionType": kind, "instanceId": card.get("instance_id"), "grpId": card.get("grp_id")}
        options.append(DecisionOption(option_id, text, payable=payable, meta=meta))
        texts[option_id] = text
    if not options:
        return plan
    decision = PendingDecision((0, 0), "ActionsAvailable", tuple(options), can_pass="pass" in texts)
    picked, why = board_math_option_pick(decision, game_state)
    if len(picked) != 1 or picked[0] not in texts:
        return plan
    if picked[0] == "pass":
        action = GameAction(action_type=ActionType.PASS_PRIORITY)
    else:
        action = ActionPlanner.__new__(ActionPlanner)._legal_action_to_action(texts[picked[0]])
        if action is None:
            return plan
    action.reasoning = f"{lead}; {why}."
    plan.actions = [action]
    plan.overall_strategy = f"[local-fallback] {why}"
    plan.voice_advice = plan.spoken_actions()
    logger.warning("Legacy fallback (%s): %s -> %s", lead.lower(), why, texts[picked[0]])
    return plan
