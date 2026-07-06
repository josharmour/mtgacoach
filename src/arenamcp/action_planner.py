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

# Sentinel returned by plan_decision_options when the safe move is to
# DECLINE the pending window (cancel / pause for manual) rather than pick
# any option. Live 2026-07-05: a harmful SelectTargets whose only legal
# candidates were the user's own permanents must not be auto-submitted.
DECLINE_DECISION = "__decline__"


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
    # Land play via the MDFC back face ("Action: PlayMDFC" menu entries) —
    # the matcher must resolve it to the raw PlayMDFC action, not a plain
    # Play (#39, live 2026-07-06).
    mdfc: bool = False

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


@dataclass
class TurnPlanStep:
    """One user-visible play in a multi-step turn plan.

    Mana abilities, casting-time sub-decisions, and search prompts are
    intentionally NOT modeled here — those are mid-spell mechanical
    decisions, not plays. The status field is updated as steps execute.
    """
    action_type: str        # "play_land", "cast_spell", "activate_ability", "declare_attackers", etc.
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
        return self.steps[self.current_idx:]

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


AUTOPILOT_SYSTEM_PROMPT = """You are an MTG Arena autopilot. Given the game state and trigger, output a JSON action plan to execute.

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
- TRUST [OK] tags. "[OK]" on a Cast or Activate Ability action means MTGA's mana solver already verified the cost is payable from your current mana — including hybrid, phyrexian, cost reductions, and affinity. Do NOT recompute the cost yourself or claim you "lack the mana": the Mana summary shows floating mana only, not what your untapped lands can produce. If an action is [OK] in the legal list, you CAN take it. Failing to play affordable [OK] actions wastes the turn. Every action in the legal list is offered by MTGA as currently legal — a missing [OK] on an Activate Ability does NOT mean it is unaffordable; tap/sacrifice activations cost no mana at all (e.g. cracking a fetch land — which also triggers landfall).
- If a pending decision is shown, resolve that decision (not a new cast/play).
- ONE action per plan. Don't sequence (no "play land" + "cast spell").
- EXCEPTION: declare_attackers/declare_blockers carry the full set in one action — do NOT add a "done" click.
- [SS] = summoning sick (can't attack). * prefix = token. [3P1P] = 3 +1/+1 counters.
- If a TURN PLAN is shown, follow it. Stay committed to the locked turn plan unless a material change (opponent response, lethal threat, unexpected trigger) makes it obsolete.
- PROTECTIVE / LIFE-PAYMENT ABILITIES: Do NOT activate an ability that pays life (or other resources) for indestructible/hexproof/protection/a temporary buff unless there is a concrete threat to the creature right now — it is blocked by a creature that would kill it, it is targeted by removal/burn on the stack, or it must survive incoming damage this step. An unblocked attacker facing no removal needs no protection; activating "just in case" only loses life. When in doubt, pass.
- BOARD PRESSURE: When the opponent's board is wider than yours or grew by multiple creatures this turn, prioritize interaction (removal, profitable blocks, combat tricks) over advancing your own plan. At low life (below ~15), block with large or indestructible creatures — an indestructible blocker loses nothing by blocking.
- VOICE CLARITY: voice_advice must state ONE concrete action in plain spoken language and name the specific creatures involved. For blocks: either name the block ("Block their <attacker> with your <blocker>") or, if not blocking, say "Don't block — take <N> from <attacker>". For attacks, name who swings. Never give self-contradictory advice (e.g. saying both "let it trade" and "take the hit"), and never reference a creature that is not on the board. When a "Computed optimal blocks:" line is present, your voice_advice must match it.
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

SCHEMA:
""" + ACTION_SCHEMA


# P0-8 (2026-07-05): plan_turn used to reuse AUTOPILOT_SYSTEM_PROMPT, whose
# "Output ONLY JSON matching the schema" hard-demanded the actions envelope
# while the user message asked for turn_plan — a model-dependent coin flip
# that yielded ZERO turn plans for match 1 (the 12B obeyed the system prompt
# all 3 times, perfectly valid JSON in the wrong shape).
TURN_PLAN_SYSTEM_PROMPT = """You are an MTG Arena turn planner. Given the game state, output the ordered list of user-visible plays for this whole turn.

RULES:
- TRUST [OK] tags: MTGA's mana solver already verified those costs are payable.
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


class ActionPlanner:
    """Converts game state + trigger into structured JSON action commands via LLM."""

    # Ring buffer size for recent planning diagnostics (kept for debug reports)
    _DIAG_BUFFER_SIZE = 10

    def __init__(
        self,
        backend: Any,
        timeout: float = 5.0,
        land_drop_first: bool = True,
    ):
        """Initialize the action planner.

        Args:
            backend: An LLMBackend instance (same interface as CoachEngine uses).
            timeout: Maximum seconds to wait for LLM response.
            land_drop_first: When True, deterministically play a land if one is
                legal and we've played 0 lands this turn — short-circuits the
                LLM. Set False for landfall-synergy decks.
        """
        self._backend = backend
        self._timeout = timeout
        self._land_drop_first = land_drop_first
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
        self._turn_memo: Optional[ActionPlan] = None
        # Executed actions this turn (by string repr); used to tell the LLM
        # "you already did X" in subsequent priority windows.
        self._turn_executed: list[str] = []
        # Locked turn intent: the first non-trivial overall_strategy the LLM
        # produced this turn, captured and held for the rest of the turn so
        # subsequent priority windows reason as "continue the plan" instead
        # of re-deriving strategy from scratch (the flip-flop pattern).
        self._turn_intent: Optional[str] = None
        # Active multi-step turn plan: the ordered list of user-visible plays
        # we intend to make this turn. Built once on the first non-trivial
        # own-turn LLM call (an additional `plan_turn` LLM call), then
        # advanced as actions execute and replaced wholesale on divergence.
        # Cleared on turn change.
        self._active_turn_plan: Optional[TurnPlan] = None
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

    def set_game_plan(self, plan_text: Optional[str]) -> None:
        """Set the persistent strategic GAME PLAN block injected into prompts.

        Owned by a :class:`arenamcp.game_plan.GamePlanManager`; the autopilot
        refreshes it before planning. Persists across turns (it is NOT cleared on
        turn change, unlike the per-turn intent/memo).
        """
        self._game_plan = (plan_text or "").strip()

    def clear_game_plan(self) -> None:
        self._game_plan = ""

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
        forced_land = self._should_force_land_drop(
            game_state, effective_legal_actions, decision_context
        )
        if forced_land:
            preflight_plan = self._build_preflight_plan(
                forced_land,
                trigger=trigger,
                turn_number=current_turn,
                tag="land-drop-first",
            )
            if preflight_plan.actions:
                raw = self._resolve_raw_actions_for_matching(
                    game_state, legal_actions_raw
                )
                if raw:
                    self._attach_gre_refs(preflight_plan, raw, game_state)
                diag["preflight"] = "land_drop_first"
                diag["elapsed_ms"] = (time.perf_counter() - start) * 1000
                diag["planned_actions"] = 1
                diag["strategy"] = preflight_plan.overall_strategy
                self._record_diagnostic(diag)
                self._turn_memo = preflight_plan
                self._turn_memo_turn = current_turn
                logger.info(
                    f"Planner preflight: {preflight_plan.overall_strategy}"
                )
                return preflight_plan

        # P2-6: a menu with no real choice needs no LLM. 7+ full calls on
        # 2026-07-05 fired on Wait-only / pass-only windows (incl. every
        # combat trigger while the opponent held priority) and every
        # response was discarded or auto-picked anyway.
        _trivial = {"pass", "action: activate_mana", "action: floatmana"}
        has_real_choice = any(
            a.strip().lower() not in _trivial
            and not a.strip().lower().startswith("wait (")
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
                    "Planner short-circuit: trivial window "
                    f"({effective_legal_actions}) — no LLM call"
                )
                return plan

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
        system_prompt = AUTOPILOT_SYSTEM_PROMPT
        user_message = self._build_action_prompt(
            game_state, trigger, effective_legal_actions, decision_context
        )
        if want_turn_plan:
            user_message += (
                "\n\nADDITIONALLY: this is the first decision of your turn. "
                "Include a top-level \"turn_plan\" key in the SAME JSON "
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
        # Backends that don't accept the kwarg (older local backends) fall
        # back to their default temperature.
        import concurrent.futures

        def _complete() -> str:
            # request_timeout_s is what gives the underlying SDK a hard
            # deadline. Without it, a hung backend keeps the worker thread
            # alive for ~10 minutes (OpenAI SDK default), which then keeps
            # this with-block from exiting.
            try:
                # raise_on_error: never let the "Error getting advice: ..."
                # sentinel string reach the JSON parser — during a backend
                # outage the parse yields 0 actions and _fallback_plan would
                # submit a real game action (blind passes, 2026-07-05).
                return self._backend.complete(
                    system_prompt,
                    user_message,
                    4096,
                    temperature=0.0,
                    request_timeout_s=self._timeout,
                    raise_on_error=True,
                )
            except TypeError:
                try:
                    return self._backend.complete(
                        system_prompt, user_message, 4096, temperature=0.0
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

        # Belt-and-braces for backends that still return the error sentinel
        # as a string (raise_on_error TypeError fallback, third-party
        # backends): never feed it to the parser / fallback picker.
        if response and response.lstrip().startswith("Error getting advice"):
            logger.error(f"Backend returned error sentinel; no plan: {response[:160]}")
            diag["failure"] = "llm_error_sentinel"
            self._record_diagnostic(diag)
            return ActionPlan(trigger=trigger)

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

        # Capture the turn intent: the first non-trivial overall_strategy
        # produced this turn becomes the locked plan that subsequent windows
        # follow. Skip preflight/fallback tags and pure-pass plans — those
        # are reactive rather than strategic.
        self._maybe_capture_turn_intent(plan, game_state, current_turn)

        logger.info(f"Planned {len(plan.actions)} actions: {plan.overall_strategy}")
        return plan

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
    _TURN_PLAN_USER_VISIBLE_ACTIONS = frozenset({
        "play_land",
        "cast_spell",
        "activate_ability",
        "declare_attackers",
        "declare_blockers",
    })

    def _is_own_actions_available_window(
        self,
        game_state: dict[str, Any],
        decision_context: Optional[dict[str, Any]],
    ) -> bool:
        """Are we in a normal own-turn ActionsAvailable priority window?"""
        bridge_request = (game_state.get("_bridge_request_type") or "").strip()
        bridge_class = (game_state.get("_bridge_request_class") or "").strip()
        # Allow empty (test states) or ActionsAvailable-family.
        ok_requests = self._ACTIONS_AVAILABLE_PREFLIGHT_REQUESTS
        if (
            bridge_request and bridge_request not in ok_requests
        ) or (
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
        decision_context: Optional[dict[str, Any]] = None,
    ) -> Optional[TurnPlan]:
        """One-shot LLM call that lays out the user-visible plays for the turn.

        Stores the result on `self._active_turn_plan`. Returns the plan or
        None if the call failed / produced no useful steps.
        """
        import concurrent.futures

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
            ']}}'
        )
        game_plan_block = f"{self._game_plan}\n\n" if self._game_plan else ""
        user_message = (
            f"TRIGGER: turn_plan (turn {current_turn})\n\n"
            f"{context}\n\n"
            f"{game_plan_block}"
            f"INSTRUCTIONS: {instructions}\n"
            f"Order the plays so they ADVANCE the game plan above.\n"
            f"Output ONLY a JSON object matching this shape (no prose, no markdown):\n"
            f"{schema_example}"
        )

        def _complete() -> str:
            try:
                return self._backend.complete(
                    TURN_PLAN_SYSTEM_PROMPT,
                    user_message,
                    4096,
                    temperature=0.0,
                    request_timeout_s=self._timeout,
                    raise_on_error=True,
                )
            except TypeError:
                try:
                    return self._backend.complete(
                        TURN_PLAN_SYSTEM_PROMPT, user_message, 4096, temperature=0.0
                    )
                except TypeError:
                    return self._backend.complete(TURN_PLAN_SYSTEM_PROMPT, user_message)

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_complete)
                response = future.result(timeout=self._timeout)
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
                (f"{s.action_type}:{s.card_name}" if s.card_name else s.action_type)
                for s in plan.steps
            )
        )
        return plan

    def _parse_turn_plan_response(
        self,
        response: str,
        current_turn: int,
    ) -> Optional[TurnPlan]:
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
        if (not isinstance(steps_data, list) or not steps_data) and isinstance(
            data.get("actions"), list
        ):
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
            executed_name = self._strip_decoration(
                executed_action.card_name or ""
            ).lower()
            expected_name = self._strip_decoration(step.card_name or "").lower()
            return not (
                expected_name and executed_name and expected_name != executed_name
            )

        # Current step first, then look-ahead: a later step executing early
        # (e.g. the land-drop preflight already performed step 1) marks the
        # stepped-over ones "skipped" instead of reading as divergence.
        for offset, step in enumerate(plan.steps[plan.current_idx:]):
            if _matches(step):
                for skipped in plan.steps[plan.current_idx:plan.current_idx + offset]:
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
        return any(
            step.action_type == "declare_attackers" and step.status != "done"
            for step in plan.steps
        )

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

    def get_turn_plan_payload(self) -> Optional[dict[str, Any]]:
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
            if lower.startswith("cast "):
                has_ok = "[ok]" in lower

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

                card_name = self._normalize_action_text(legal_action).replace("Cast ", "").strip()
                card_cost = ""
                card_hand_entry = None
                for card in game_state.get("hand", []):
                    if card.get("name", "").lower() == card_name.lower():
                        card_cost = card.get("mana_cost", "")
                        card_hand_entry = card
                        break

                # X-cost spells are off the autopilot menu for now: the
                # "Select a value for X" casting-time window is not
                # discoverable through the bridge (FindPendingInteraction
                # returns nothing), so autopilot can neither choose X nor
                # cancel — live 2026-07-02, Silkguard resolved with X=0
                # (wasted) and Steelbane Hydra wedged the client on the X
                # slider until the user clicked Cancel. The coach may still
                # ADVISE these casts; the user makes them manually.
                if card_cost and "{X}" in card_cost.upper().replace(" ", ""):
                    logger.info(
                        "Dropping X-cost cast %s from autopilot (cost=%s): "
                        "bridge cannot drive the X chooser",
                        card_name,
                        card_cost,
                    )
                    continue

                # Local affordability: True/False when cost + pool are known,
                # else None (couldn't determine).
                local_affordable = None
                if card_cost and mana_pool and rules_engine_cls is not None:
                    local_affordable = rules_engine_cls._can_afford(card_cost, mana_pool)

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
                        "Cast %s: [OK]/autotap present but local check "
                        "unaffordable — trusting bridge.",
                        card_name,
                    )

                # Block removal spells that would only have friendly targets.
                # Casting them just forces the user to either blow up their own
                # permanent or cancel — neither is worth the mana. See "Seam Rip
                # with only my own enchantment in play" self-destruct case.
                if card_hand_entry and self._removal_lacks_opponent_target(
                    card_hand_entry, game_state
                ):
                    logger.info(
                        "Filtering self-harming removal: %s (no legal opponent target)",
                        card_name,
                    )
                    continue

            filtered.append(legal_action)

        return filtered or legal_actions

    _ACTIONS_AVAILABLE_PREFLIGHT_REQUESTS: frozenset[str] = frozenset({
        "",
        "ActionsAvailable",
        "ActionsAvailableRequest",
    })

    def _should_force_land_drop(
        self,
        game_state: dict[str, Any],
        legal_actions: list[str],
        decision_context: Optional[dict[str, Any]],
    ) -> Optional[str]:
        """Return a Play Land legal-action string if we should force it now.

        Conditions:
          - feature is enabled (land_drop_first)
          - decision is a normal priority window (ActionsAvailable / unset)
          - it's our turn
          - active player has played 0 lands this turn
          - a "Play Land: X" entry is in legal_actions
        """
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

        local_player = next(
            (
                p
                for p in game_state.get("players", [])
                if p.get("seat_id") == local_seat
            ),
            None,
        )
        # If lands_played is missing, assume 1 (don't force a drop on
        # incomplete state — the LLM path is safer than dropping the wrong
        # land or double-tapping into an "already played" failure).
        lands_played = (local_player or {}).get("lands_played", 1)
        if lands_played != 0:
            return None

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
    ) -> ActionPlan:
        """Build a single-action ActionPlan from a legal-action string."""
        plan = ActionPlan(trigger=trigger, turn_number=turn_number)
        action = self._legal_action_to_action(legal_action)
        if not action:
            return plan
        plan.actions = [action]
        plan.overall_strategy = f"[{tag}] {legal_action}"
        plan.voice_advice = self._humanize_legal_action(legal_action)
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

    def _removal_lacks_opponent_target(
        self,
        card: dict[str, Any],
        game_state: dict[str, Any],
    ) -> bool:
        """Is this card removal whose only legal targets are friendly?

        Returns True only when:
          - Oracle text reads like removal / harmful targeting, AND
          - The battlefield has no opponent permanent matching any
            plausible target type mentioned in the oracle.
        Conservative by design — returns False whenever we can't confirm
        both conditions, so non-removal spells (auras, buffs, fight
        spells with any viable target) keep the fast path.
        """
        oracle = (card.get("oracle_text") or "").lower()
        if not oracle:
            return False

        is_removal = any(phrase in oracle for phrase in self._REMOVAL_ORACLE_PHRASES)
        if not is_removal:
            return False

        # Bail on spells that can target players — "Shock target creature
        # or player" has a player fallback, so it's never self-only.
        if "any target" in oracle or "target player" in oracle or "target opponent" in oracle:
            return False

        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break
        if local_seat is None:
            return False

        battlefield = game_state.get("battlefield", []) or []
        opp_permanents = [
            c for c in battlefield
            # gamestate emits controller_seat_id (never controller_id);
            # controller beats owner so stolen permanents classify right.
            if (c.get("controller_seat_id") or c.get("owner_seat_id")) != local_seat
            and "land" not in str(c.get("type_line") or "").lower()
        ]

        def _opp_of_type(pred) -> bool:
            return any(pred(c) for c in opp_permanents)

        # Narrow by oracle target type. If the spell specifies
        # enchantment/artifact/creature and no opponent has that type,
        # the only legal target is friendly → self-harm.
        if "target enchantment" in oracle:
            if not _opp_of_type(lambda c: "enchantment" in str(c.get("type_line") or "").lower()):
                return True
        if "target artifact" in oracle and "enchantment" not in oracle:
            if not _opp_of_type(lambda c: "artifact" in str(c.get("type_line") or "").lower()):
                return True
        if ("target creature" in oracle
                and "or enchantment" not in oracle
                and "or planeswalker" not in oracle):
            if not _opp_of_type(lambda c: "creature" in str(c.get("type_line") or "").lower()):
                return True
        if "target nonland permanent" in oracle or "target permanent" in oracle:
            if not opp_permanents:
                return True
        if "target planeswalker" in oracle:
            if not _opp_of_type(lambda c: "planeswalker" in str(c.get("type_line") or "").lower()):
                return True
        return False

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
        # Import and use CoachEngine's formatter for consistency. The planner
        # variant drops heavy GRE JSON dumps and trims oracle text on
        # long-resident permanents — see _format_game_context(for_planner).
        try:
            from arenamcp.coach import CoachEngine
            formatter = CoachEngine.__new__(CoachEngine)
            context = formatter._format_game_context(game_state, for_planner=True)
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
                a for a in legal_actions
                if a.strip().lower() not in (
                    "action: activate_mana", "action: floatmana"
                )
            ]
            self._last_menu = menu
            if menu:
                menu_lines = "\n".join(
                    f"  {i + 1}. {a}" for i, a in enumerate(menu)
                )
                eff_str = f"(pick by number)\n{menu_lines}"
            else:
                eff_str = 'NONE — say "pass priority"'
            context, n = re.subn(
                r"(?m)^Legal: .*$", f"Legal: {eff_str}", context, count=1
            )
            if n == 0:
                context = f"Legal: {eff_str}\n{context}"
            if excluded:
                context += (
                    "\nEXCLUDED (autopilot cannot execute these — do NOT "
                    "propose them): " + ", ".join(excluded[:6])
                )

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

        # Persistent GAME PLAN (strategic spine): the top-level frame every
        # tactical decision serves. Placed before the per-turn intent so the
        # model reads "here is how we win this game" first, then "here is the
        # plan for this turn", then the immediate decision.
        if self._game_plan:
            parts.append(self._game_plan)

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
        if (
            self._active_turn_plan is not None
            and self._active_turn_plan.turn_number == current_turn
        ):
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

        # NOTE: legal_actions, GRE request type/class, and recent-GRE context
        # are already emitted by _format_game_context(for_planner=True) above.
        # We deliberately do NOT re-append them here — duplicating those
        # blocks ~doubled the prompt size in earlier versions.
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
                        plan.voice_advice = self._humanize_legal_action(
                            self._last_menu[idx]
                        )
                        return plan
            logger.error(f"Failed to parse action plan JSON: {e}")
            logger.debug(f"Raw response: {response[:500]}")
            return plan

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
                    action = self._legal_action_to_action(self._last_menu[idx])
                    if action is not None:
                        action.reasoning = str(action_data.get("reasoning", "") or "")
                        logger.debug(
                            f"Menu pick {pick} → {self._last_menu[idx]!r}"
                        )
                if action is None:
                    logger.warning(
                        f"Planner pick {pick!r} out of menu range "
                        f"(1..{len(self._last_menu)}); trying structured fields"
                    )
            if action is None:
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

        # A backend error sentinel is not advice — auto-picking a real game
        # action from it submitted blind passes during the 2026-07-05 outage.
        if response and response.lstrip().startswith("Error getting advice"):
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
            return [
                (p if p.rstrip().endswith(")") else p + ")").strip()
                for p in parts
                if p.strip()
            ]
        return [s]

    @staticmethod
    def _extract_first_json(text: str) -> Optional[str]:
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
    ) -> Optional[bool]:
        """One-shot pay/decline call for an out-of-band optional cost.

        Returns True (pay), False (decline), or None when the LLM path is
        unavailable or unparseable — the caller picks the conservative
        default for the effect type.
        """
        local_seat = game_state.get("local_seat_id")
        own: list[str] = []
        theirs: list[str] = []
        for obj in game_state.get("battlefield", []) or []:
            name = obj.get("name") or "?"
            pt = ""
            if obj.get("power") is not None or obj.get("toughness") is not None:
                pt = f" ({obj.get('power')}/{obj.get('toughness')})"
            ctrl = obj.get("controller_seat_id") or obj.get("owner_seat_id")
            (own if ctrl == local_seat else theirs).append(f"{name}{pt}")
        user_message = "\n".join([
            f"Optional cost from: {source_name or 'unknown source'}",
            f"Effect text: {oracle_text or 'unknown'}",
            f"Your battlefield: {', '.join(own) or '(empty)'}",
            f"Opponent battlefield: {', '.join(theirs) or '(empty)'}",
            "",
            "Should you pay this optional cost?",
        ])
        try:
            try:
                response = self._backend.complete(
                    self._PAY_DECLINE_SYSTEM_PROMPT,
                    user_message,
                    256,
                    temperature=0.0,
                    request_timeout_s=min(self._timeout, 8.0),
                    raise_on_error=True,
                )
            except TypeError:
                response = self._backend.complete(
                    self._PAY_DECLINE_SYSTEM_PROMPT, user_message
                )
        except Exception as e:
            logger.info(f"plan_pay_or_decline LLM call failed: {e}")
            return None
        json_str = self._extract_first_json(response)
        if not json_str:
            logger.info(
                f"plan_pay_or_decline unparseable: {(response or '')[:120]!r}"
            )
            return None
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            logger.info(f"plan_pay_or_decline bad JSON: {json_str[:120]!r}")
            return None
        pay = data.get("pay")
        if isinstance(pay, bool):
            logger.info(
                f"plan_pay_or_decline: pay={pay} "
                f"({str(data.get('reasoning', ''))[:100]})"
            )
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
        "auto-pay'."
    )

    def plan_decision_options(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        """Choose option ids for a typed PendingDecision.

        LLM-first with mechanical validation against the option set, then
        a deterministic pick from the same set. Never raises; returns []
        only when the decision has no options at all.
        """
        try:
            chosen = self._llm_decision_options(decision, game_state)
            valid = decision.option_ids()
            chosen = [c for c in chosen if c in valid]
            if chosen and decision.request_type == "SelectTargets":
                chosen = self._gate_harmful_llm_target_picks(
                    decision, game_state, chosen
                )
                if chosen == [DECLINE_DECISION]:
                    return chosen
            if chosen:
                limit = max(decision.min_select or 1, 1)
                limit = max(limit, min(len(chosen), decision.max_select or 1))
                return chosen[:limit]
            logger.info(
                "plan_decision_options: LLM answer had no valid ids for %s",
                decision.request_type,
            )
        except Exception as e:
            logger.info(f"plan_decision_options LLM path failed: {e}")
        if decision.request_type == "SelectTargets":
            picked = self._targeting_fallback_pick(decision, game_state)
            if picked == [DECLINE_DECISION]:
                # Harmful targeting forced onto own permanents — never let
                # the blind deterministic pick submit it either.
                return picked
            if picked:
                logger.info(
                    "plan_decision_options: controller-aware target fallback "
                    f"picked {picked}"
                )
                return picked
        return self.deterministic_option_pick(decision)

    # Mirror of autopilot._HARMFUL_SOURCE_ORACLE_PHRASES (kept local to
    # avoid an action_planner→autopilot import cycle).
    _HARMFUL_TARGET_ORACLE_PHRASES = (
        "destroy target",
        "exile target",
        "sacrifice target",
        "counter target",
        "return target",
        "opponent sacrifices target",
        "damage to target",
        "gets -",
        "gets −",
        "loses all abilities",
        "loses flying",
    )

    def _decision_source_is_harmful(
        self, decision: Any, game_state: dict[str, Any]
    ) -> Optional[bool]:
        """Classify the targeting decision's source spell as harmful.

        Source resolution: decision source_label matched on the stack, else
        top of stack. Returns None when no oracle text resolves — callers
        must treat that as "cannot judge", not "safe".
        """
        stack = game_state.get("stack", []) or []
        source_label = str(getattr(decision, "source_label", "") or "").strip().lower()
        picked_entry = None
        if source_label:
            for entry in stack:
                if str(entry.get("name") or "").strip().lower() == source_label:
                    picked_entry = entry
                    break
        if picked_entry is None and stack:
            picked_entry = stack[-1]
        oracle = str((picked_entry or {}).get("oracle_text") or "").lower()
        if not oracle:
            return None
        return any(p in oracle for p in self._HARMFUL_TARGET_ORACLE_PHRASES)

    def _battlefield_controllers(
        self, game_state: dict[str, Any]
    ) -> tuple[Optional[int], dict[int, Optional[int]]]:
        """(local_seat, {instance_id: controller_seat}) for target labeling."""
        local_seat = game_state.get("local_seat_id")
        if local_seat is None:
            for p in game_state.get("players", []) or []:
                if p.get("is_local"):
                    local_seat = p.get("seat_id")
                    break
        controllers: dict[int, Optional[int]] = {}
        for c in game_state.get("battlefield", []) or []:
            try:
                iid = int(c.get("instance_id") or 0)
            except (TypeError, ValueError):
                continue
            if iid:
                controllers[iid] = (
                    c.get("controller_seat_id") or c.get("owner_seat_id")
                )
        return local_seat, controllers

    def _gate_harmful_llm_target_picks(
        self,
        decision: Any,
        game_state: dict[str, Any],
        chosen: list[str],
    ) -> list[str]:
        """Override harmful-source LLM picks that hit our own permanents.

        Live 2026-07-06 00:58: the typed-decision LLM picked the user's own
        Nessian Wanderer for Utter Insignificance despite planning "remove
        opponent's key creature". When the source is harmful and the pick is
        own-controlled while other candidates exist, defer to the
        controller-aware fallback (opponent's biggest threat, or
        DECLINE_DECISION when only own permanents are targetable).
        """
        if self._decision_source_is_harmful(decision, game_state) is not True:
            return chosen
        local_seat, controllers = self._battlefield_controllers(game_state)
        if local_seat is None:
            return chosen
        picked_own = False
        for oid in chosen:
            if not str(oid).startswith("tgt:"):
                continue
            try:
                iid = int(str(oid)[4:])
            except ValueError:
                continue
            if controllers.get(iid) == local_seat:
                picked_own = True
                break
        if not picked_own:
            return chosen
        override = self._targeting_fallback_pick(decision, game_state)
        if override:
            logger.warning(
                f"Overriding harmful LLM target pick {chosen} (own permanent) "
                f"with {override}"
            )
            return override
        return chosen

    def _targeting_fallback_pick(
        self, decision: Any, game_state: dict[str, Any]
    ) -> list[str]:
        """Controller-aware fallback for SelectTargets when the LLM failed.

        The blind ``opts[:n]`` pick targeted the user's OWN Shuri with
        Depower (removal) live on 2026-07-01. When the source spell's
        oracle reads as harmful, prefer the opponent's biggest threat;
        when it reads as beneficial, prefer our own biggest creature.
        Returns [] (defer to the blind pick) when the oracle text is
        unknown — a wrong confident pick is worse than an arbitrary one
        we can already see in bug reports.
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

        local_seat = None
        for p in game_state.get("players", []) or []:
            if p.get("is_local"):
                local_seat = p.get("seat_id")
                break

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

        own = [
            iid for iid in candidates
            if local_seat is not None
            and (battlefield.get(iid, {}).get("controller_seat_id")
                 or battlefield.get(iid, {}).get("owner_seat_id")) == local_seat
        ]
        theirs = [
            iid for iid in candidates
            if iid in battlefield and iid not in own
        ]

        if harmful:
            # Opponent's biggest threat. When the spell can only hit our
            # own permanents, DO NOT pick one — signal decline so the
            # caller cancels or hands to the user (2026-07-05: this branch
            # "threw away the least valuable one" and destroyed the user's
            # own Spirit).
            if not theirs and own:
                logger.warning(
                    "Targeting fallback: harmful source with only own "
                    "permanents as candidates — declining instead of "
                    "sacrificing one"
                )
                return [DECLINE_DECISION]
            pool = sorted(theirs, key=_power, reverse=True)
        else:
            # Beneficial: our biggest creature; never buff the opponent's
            # board just because it's the first candidate.
            pool = sorted(own, key=_power, reverse=True) or sorted(theirs, key=_power)
        if not pool:
            return []
        n = max(1, int(decision.min_select or 1))
        return [f"tgt:{iid}" for iid in pool[:n]]

    def _llm_decision_options(self, decision: Any, game_state: dict[str, Any]) -> list[str]:
        lines = [
            f"PENDING DECISION: {decision.request_type}"
            + (f" (source: {decision.source_label})" if decision.source_label else ""),
            f"Choose at least {decision.min_select} and at most "
            f"{decision.max_select} option(s).",
            "OPTIONS:",
        ]
        # #38: without controller labels the model cannot tell its own
        # permanents from the opponent's in a target list (live 2026-07-06:
        # it aimed Utter Insignificance at the user's own Nessian Wanderer).
        local_seat, controllers = self._battlefield_controllers(game_state)
        for o in decision.options:
            note = ""
            if o.payable is False:
                note = "  [cannot auto-pay — do not pick]"
            side = ""
            if o.option_id.startswith("tgt:") and local_seat is not None:
                try:
                    ctrl = controllers.get(int(o.option_id[4:]))
                except ValueError:
                    ctrl = None
                if ctrl is not None:
                    side = " (YOURS)" if ctrl == local_seat else " (opponent's)"
            lines.append(f"- {o.option_id}: {o.label}{side}{note}")
        lines.append("")
        lines.append("GAME STATE:")
        lines.append(self._fallback_format(game_state))
        user_message = "\n".join(lines)

        try:
            # Tighter than the general planning timeout: typed decisions
            # (mulligan, targeting, selection) sit inside short MTGA action
            # windows, and the deterministic fallback needs time to submit
            # before the window closes (2026-07-01: mulligan window expired
            # while the LLM call was still blocked).
            response = self._backend.complete(
                self._DECISION_SYSTEM_PROMPT,
                user_message,
                512,
                temperature=0.0,
                request_timeout_s=min(self._timeout, 12.0),
                raise_on_error=True,
            )
        except TypeError:
            response = self._backend.complete(
                self._DECISION_SYSTEM_PROMPT, user_message
            )

        # P1-1: models prose-prefix the JSON despite "reply ONLY with JSON"
        # (0/5 typed-decision parses on 2026-07-05, one reply in Chinese) —
        # extract the first JSON object instead of parsing the whole string,
        # and log a preview when even that fails so the failure is
        # diagnosable from the log.
        json_str = self._extract_first_json(response)
        if not json_str:
            logger.info(
                f"typed-decision: no JSON object in response: {(response or '')[:160]!r}"
            )
            raise ValueError("typed-decision response contained no JSON object")
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            logger.info(f"typed-decision: bad JSON: {json_str[:160]!r}")
            raise
        ids = data.get("option_ids") or []
        return [str(i) for i in ids if isinstance(i, (str, int))]

    @staticmethod
    def deterministic_option_pick(decision: Any) -> list[str]:
        """Mechanical fallback: pick from the option set, never outside it."""
        opts = list(decision.options)
        if not opts:
            return []
        if decision.request_type == "ActionsAvailable":
            for o in opts:
                if o.option_id.startswith("idx:") and o.payable:
                    return [o.option_id]
            for o in opts:
                if (o.meta or {}).get("actionType") == "ActionType_Play":
                    return [o.option_id]
            for o in opts:
                if o.option_id == "pass":
                    return [o.option_id]
            return [opts[0].option_id]
        if decision.request_type == "Mulligan":
            return ["mull:keep"]
        n = max(1, int(decision.min_select or 1))
        return [o.option_id for o in opts[:n]]

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

    def _legal_action_to_action(self, legal_action: str) -> Optional[GameAction]:
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
            return GameAction(action_type=ActionType.ACTIVATE_ABILITY, card_name=self._strip_decoration(act[9:]))
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
        "SelectReplacement":       {ActionType.SELECT_REPLACEMENT, ActionType.SELECT_N, ActionType.CLICK_BUTTON},
        "SelectReplacementRequest": {ActionType.SELECT_REPLACEMENT, ActionType.SELECT_N, ActionType.CLICK_BUTTON},
        "Search":                  {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchRequest":           {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchFromGroups":        {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "SearchFromGroupsRequest": {ActionType.SEARCH_LIBRARY, ActionType.SELECT_N},
        "Distribution":            {ActionType.DISTRIBUTE},
        "DistributionReq":         {ActionType.DISTRIBUTE},
        "DistributionRequest":     {ActionType.DISTRIBUTE},
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
        "GroupRequest":            {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "Order":                   {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.ORDER_COMBAT_DAMAGE},
        "OrderRequest":            {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.ORDER_COMBAT_DAMAGE},
        "SelectFromGroups":        {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.SELECT_N},
        "SelectFromGroupsRequest": {ActionType.ORDER_TRIGGERS, ActionType.ORDER_BLOCKERS, ActionType.SELECT_N},
        "SelectNGroup":            {ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "SelectNGroupRequest":     {ActionType.SELECT_N, ActionType.SELECT_TARGET},
        "AssignDamage":            {ActionType.ASSIGN_DAMAGE, ActionType.DISTRIBUTE},
        "AssignDamageRequest":     {ActionType.ASSIGN_DAMAGE, ActionType.DISTRIBUTE},
        "SelectCounters":          {ActionType.SELECT_COUNTERS, ActionType.SELECT_N},
        "SelectCountersRequest":   {ActionType.SELECT_COUNTERS, ActionType.SELECT_N},
        "Gather":                  {ActionType.DISTRIBUTE, ActionType.SELECT_N},
        "GatherRequest":           {ActionType.DISTRIBUTE, ActionType.SELECT_N},
        "AutoTapActions":          {ActionType.PAY_COSTS, ActionType.MODAL_CHOICE},
        "AutoTapActionsRequest":   {ActionType.PAY_COSTS, ActionType.MODAL_CHOICE},
        "DeclareAttackers":        {ActionType.DECLARE_ATTACKERS},
        "DeclareBlockers":         {ActionType.DECLARE_BLOCKERS},
        "OptionalAction":          {ActionType.CLICK_BUTTON},
        "OptionalActionMessage":   {ActionType.CLICK_BUTTON},
        "OptionalActionMessageRequest": {ActionType.CLICK_BUTTON},
        "Intermission":            {ActionType.CLICK_BUTTON},
        "IntermissionRequest":     {ActionType.CLICK_BUTTON},
        "StringInput":             {ActionType.MODAL_CHOICE, ActionType.SELECT_N},
        "StringInputRequest":      {ActionType.MODAL_CHOICE, ActionType.SELECT_N},
    }

    def _is_action_legal(
        self,
        action: GameAction,
        legal_actions: list[str],
        decision_context: Optional[dict[str, Any]] = None,
        bridge_request: Optional[str] = None,
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
        decision_context: Optional[dict[str, Any]],
        bridge_request: Optional[str],
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
            ActionType.SELECT_TARGET, ActionType.SELECT_N,
        ):
            return True
        if ctx_type == "selection_generic" and action.action_type in (
            ActionType.SELECT_N, ActionType.SELECT_TARGET,
            ActionType.SELECT_REPLACEMENT, ActionType.SEARCH_LIBRARY,
        ):
            return True
        return False

    def _is_legal_combat_declaration(
        self, action: GameAction, legal_actions: list[str]
    ) -> bool:
        """Validate a DECLARE_ATTACKERS / DECLARE_BLOCKERS plan.

        Combat declarations come in as one "Attack with: X" / "Block with: X"
        string per creature, while the planner emits a full set in a single
        GameAction. We compare the planner's set against the union of legal
        creature names rather than exact-matching on any single entry.
        """
        legal_names, in_combat_context = self._collect_combat_legal_names(
            action.action_type, legal_actions
        )
        if not in_combat_context:
            return False

        if action.action_type == ActionType.DECLARE_ATTACKERS:
            plan_names = {
                n.strip().lower() for n in action.attacker_names if n and n.strip()
            }
        else:
            plan_names = {
                k.strip().lower()
                for k in action.blocker_assignments.keys()
                if k and k.strip()
            }
        return plan_names.issubset(legal_names)

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

        return legal_names, in_combat_context

    def _is_legal_default(
        self, action: GameAction, legal_actions: list[str]
    ) -> bool:
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
                    if (
                        legal.target_names[0].strip().lower()
                        == action.target_names[0].strip().lower()
                    ):
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
