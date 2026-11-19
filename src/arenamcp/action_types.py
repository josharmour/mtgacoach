"""Shared action types, prompts, fallback sentinels, and helper utilities for ActionPlanner."""

from __future__ import annotations

import concurrent.futures
import inspect
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from arenamcp.match_context import STRATEGIC_POLICY_CORE, dynamic_strategic_policy, strategic_policy
from arenamcp.situational_rules import SUBJECTS as SITUATIONAL_SUBJECTS

__all__ = [
    "ACTION_SCHEMA",
    "AUTOPILOT_SYSTEM_PROMPT",
    "ActionPlan",
    "ActionType",
    "BLOCK_DANGER_LIFE",
    "BLOCK_DANGER_MARGIN",
    "DECLINE_DECISION",
    "FALLBACK_ATTACK_PUSH",
    "FALLBACK_AUTO_PICK",
    "FALLBACK_BIG_BOARD",
    "FALLBACK_LLM_UNAVAILABLE",
    "FALLBACK_NO_ACTIONS",
    "FALLBACK_PREFLIGHT_LAND_DROP",
    "GameAction",
    "LLMUnavailableError",
    "NO_TARGETS_DECISION",
    "POWER_ONLY_NOTE",
    "SecondLookError",
    "STRATEGIC_POLICY_CORE",
    "TurnPlan",
    "TurnPlanStep",
    "_ACTIONS_AVAILABLE_BRIDGE_REQUESTS",
    "_ARENA_TARGET_MARKS",
    "_AUTOPILOT_SITUATIONAL_LINES",
    "_BACKEND_FAILURE_TYPES",
    "_accepted_extras",
    "_as_int",
    "_autopilot_task_rules",
    "_dynamic_autopilot_rules",
    "_is_opponent_turn",
    "_strip_attacker_annotations",
    "_walker_loyalty",
    "autopilot_system_prompt",
    "is_decision_cancelled_error",
    "is_llm_unavailable_error",
    "is_time_budget_error",
    "linked_cast_note",
    "power_only_note",
]


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _walker_loyalty(card: dict[str, Any]) -> int | None:
    """A planeswalker's current loyalty (its loyalty counters first), None when unknown (#130)."""
    from arenamcp.combat_strategy import loyalty

    return loyalty(card)


def _is_opponent_turn(game_state: dict[str, Any], local_seat: Any) -> bool:
    """Whether the opponent is the active player (False when either seat is unknown)."""
    active = _as_int((game_state.get("turn") or {}).get("active_player"))
    local = _as_int(local_seat)
    return bool(active and local and active != local)


# Arena's target highlight, as the typed-decision prompt states it per target.
_ARENA_TARGET_MARKS = {
    "Hot": "good target",
    "Cold": "bad target for you",
    "ReplaceRole": "already wears this Role from you: the new Role only replaces it (no gain)",
}

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
    distribution: dict[str, int] | None = None  # target_name -> amount
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
    # The model chose this action (a plan without a fallback_reason), as opposed
    # to an auto-confirm, AFK default or deterministic fallback.
    model_choice: bool = False
    # Why a deterministic override replaced what was asked for; spoken after the action.
    override_note: str = ""
    # The model's own reasoning when a guard rewrote ``reasoning`` (ledger #36),
    # kept so reviews can see what the model meant.
    model_reasoning: str = ""
    # A combat guard (crackback, losing attack) emptied this attack declaration on
    # purpose: a decided hold, which the bridge must not refill with the solver's pick.
    guard_hold: bool = False
    # The line search's attack, pushed over a declaration that ends worse (ledger #178): the
    # bridge sends it as it is, with no losing-attack thinning (the search already judged
    # every block) and no solver override.
    line_push: bool = False

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
        if self.modal_index and f"mode {self.modal_index}" not in (self.card_name or "").lower():
            parts.append(f"mode {self.modal_index}")
        if self.scry_position:
            parts.append(f"scry {self.scry_position}")
        return " | ".join(parts)


# WP-0.4 fallback reason codes. A non-empty reason means a DEFAULT PATH, not the
# model, chose the action; bug reports and logs use it to tell the two apart.
# Note "[pick-salvage]" is deliberately absent: that is the model's own pick
# index recovered from malformed JSON, so the decision is still the model's.
FALLBACK_AUTO_PICK = "planner_auto_pick"
FALLBACK_PREFLIGHT_LAND_DROP = "planner_preflight_land_drop"
FALLBACK_NO_ACTIONS = "planner_no_actions"
FALLBACK_BIG_BOARD = "planner_big_board_solver"
FALLBACK_ATTACK_PUSH = "planner_attack_push"
FALLBACK_LLM_UNAVAILABLE = "llm_unavailable"


class LLMUnavailableError(RuntimeError):
    """The model was not asked (circuit open) or answered with an error sentinel."""


class SecondLookError(ValueError):
    """A typed-decision pick a deterministic check set aside, with no better model answer in time."""


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
    """The optional complete() keywords (call_class, first_token_timeout_s) this backend accepts."""
    try:
        parameters = inspect.signature(complete).parameters
    except (TypeError, ValueError):
        return {}
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return dict(extras)
    return {key: value for key, value in extras.items() if key in parameters}


def is_time_budget_error(error: BaseException) -> bool:
    """The call ran out of its own time budget (the model was too slow), not a dead server."""
    if getattr(error, "kind", None) in ("first_token_timeout", "stream_stall"):
        return True
    text = str(error).lower()
    return (
        "time budget exhausted" in text
        or "no answer in time" in text
        or "no first token within" in text
        or "stream stalled" in text
    )


def is_decision_cancelled_error(error: BaseException) -> bool:
    """The caller cancelled the decision because the Arena window moved on."""
    if getattr(error, "kind", None) == "cancelled":
        return True
    text = str(error).lower()
    return (
        "decision cancelled" in text
        or "request cancelled" in text
        or "request changed during planning" in text
    )


def is_llm_unavailable_error(error: BaseException) -> bool:
    """A model-server failure (circuit open, timeout, transport, HTTP), not a bad answer."""
    if isinstance(
        error, (LLMUnavailableError, TimeoutError, ConnectionError, concurrent.futures.TimeoutError)
    ):
        return True
    return any(cls.__name__ in _BACKEND_FAILURE_TYPES for cls in type(error).__mro__)


@dataclass
class ActionPlan:
    """A complete plan of actions to execute."""

    actions: list[GameAction] = field(default_factory=list)
    overall_strategy: str = ""
    voice_advice: str = ""
    trigger: str = ""
    turn_number: int = 0
    fallback_reason: str = ""
    fallback_detail: str = ""

    def spoken_actions(self) -> str:
        """Describe validated actions, never a separate model-generated recommendation."""
        from arenamcp.damage_assignment import spoken_split
        from arenamcp.narration import combat_declaration, counted_creatures, spoken_list, spoken_name

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
                    f"Attack with {counted_creatures([spoken_name(name) for name in action.attacker_names])}"
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
            elif kind == ActionType.ASSIGN_DAMAGE and spoken_split(action.distribution):
                line = "Assign " + spoken_split(action.distribution)
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
            if action.override_note:
                line += f" — {action.override_note.rstrip('.')}"
            lines.append(f"{line}.")
        return " ".join(lines)

    def __str__(self) -> str:
        lines = [f"Plan ({self.trigger}, turn {self.turn_number}): {self.overall_strategy}"]
        for i, action in enumerate(self.actions, 1):
            lines.append(f"  {i}. {action}")
        return "\n".join(lines)


@dataclass
class TurnPlanStep:
    """One user-visible play in a multi-step turn plan."""

    action_type: str
    card_name: str = ""
    target_names: list[str] = field(default_factory=list)
    rationale: str = ""
    status: str = "pending"


@dataclass
class TurnPlan:
    """An ordered list of plays the autopilot intends to make this turn."""

    turn_number: int
    steps: list[TurnPlanStep] = field(default_factory=list)
    current_idx: int = 0
    last_replanned_reason: str = ""

    def __post_init__(self) -> None:
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
                if self.steps[self.current_idx].status == "pending":
                    self.steps[self.current_idx].status = "current"


# JSON schema embedded in the system prompt for constrained output
ACTION_SCHEMA = """{
  "actions": [{
    "pick": "integer: the 1-based number of a Legal: menu entry; OMIT for structured actions",
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
- LANDFALL ORDER: an [OK] permanent with a landfall trigger ("whenever a land you control enters") that you will cast this turn comes BEFORE the land drop, so the land triggers it.
- EXCEPTION: declare_attackers/declare_blockers carry the full set in one action — do NOT add a "done" click.
- ATTACK TARGETS: Supply attacker_names AND attacker_targets mapping each chosen creature to its legal recipient. When a planeswalker and the opponent are available, explicitly choose who each creature attacks; split attacks when useful. Compare killing the planeswalker (loyalty, abilities, future value) with lethal or pressure on the player. Damage to a planeswalker is NOT damage to the opponent. Prefer the recipient-aware combat search over the older player-only attack line when both appear. The search models visible combat approximately, not hidden tricks, triggered abilities, or future loyalty activations; never invent unknown loyalty. Say the chosen recipients in voice_advice; never leave that choice to a default UI target.
- MANY ATTACKERS: attacker_names may be ["all"] (every attacker on the menu) or ["all except <names>"]; with attacker_targets {"all": "Opponent"} when every one attacks the player. A menu line "N-M. Attack with: Rabbit #1..#12 (1/1): 12 identical" stands for twelve interchangeable tokens; name them "Rabbit #1" ... "Rabbit #12".
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
- distribute: map each chosen target name (or its offered instance ID for same-name targets) to its amount. Respect the offered limits.
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

# AUTOPILOT_SYSTEM_PROMPT rule lines that need their subject in the game
# (situational_rules.SUBJECTS).
_AUTOPILOT_SITUATIONAL_LINES = (
    ("animation", "- ANIMATION: "),
    ("equipment", "- EQUIPMENT: "),
    ("library", "- TUTORS & SEARCH LIBRARY: "),
)


def _autopilot_task_rules(match_subjects: Iterable[str]) -> str:
    """Autopilot rules filtered to core rules plus ``match_subjects``."""
    wanted = set(match_subjects)
    dropped = tuple(prefix for subject, prefix in _AUTOPILOT_SITUATIONAL_LINES if subject not in wanted)
    if not dropped:
        return AUTOPILOT_SYSTEM_PROMPT
    return "\n".join(line for line in AUTOPILOT_SYSTEM_PROMPT.split("\n") if not line.startswith(dropped))


def _dynamic_autopilot_rules(dyn_subjects: Iterable[str]) -> str:
    """Situational strategic and autopilot rules active dynamically beyond ``match_subjects``."""
    wanted = set(dyn_subjects)
    if not wanted:
        return ""
    parts: list[str] = []
    policy_tail = dynamic_strategic_policy(wanted)
    if policy_tail:
        parts.append(policy_tail)
    prefixes = tuple(prefix for subject, prefix in _AUTOPILOT_SITUATIONAL_LINES if subject in wanted)
    if prefixes:
        lines = [line for line in AUTOPILOT_SYSTEM_PROMPT.split("\n") if line.startswith(prefixes)]
        if lines:
            parts.append("\n".join(lines))
    if not parts:
        return ""
    return "SITUATIONAL RULES:\n" + "\n".join(parts)


def autopilot_system_prompt(subjects: Any = SITUATIONAL_SUBJECTS) -> str:
    """The plan_actions system prompt for the rule subjects in play (all by default)."""
    wanted = set(subjects)
    return strategic_policy(wanted) + "\n" + _autopilot_task_rules(wanted)


def _strip_attacker_annotations(tail: str) -> str:
    """Drop trailing annotations from a "Attack with: X" / "Block with: X" tail."""
    cleaned = tail
    for marker in (" (", " ["):
        cut = cleaned.find(marker)
        if cut >= 0:
            cleaned = cleaned[:cut]
    return cleaned


def linked_cast_note(game_state: dict[str, Any], meta: dict[str, Any], lookup: Any = None) -> str:
    """Rules for a cast whose card is not its source object (prepared spell, Adventure, back face)."""
    import arenamcp.action_planner as _ap

    source = _ap.find_source(game_state, meta)
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
