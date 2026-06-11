"""Persistent, adaptive GAME PLAN layer shared by the autopilot and the coach.

The :class:`GamePlan` is the *strategic spine* that sits between the static deck
archetype summary (``coach._deck_strategy``) and the per-decision tactical
executor (:class:`arenamcp.action_planner.ActionPlanner`). It names 1-2 win
conditions and the concrete path to each, plus the current threat assessment and
"what to develop next", and is then threaded into every tactical decision so the
autopilot/coach *develop toward a win* instead of reacting one snapshot at a
time.

Cadence is deliberately slow: a plan is (re)formed only on **material** board
changes — a new turn where creatures/life/power actually moved, a key threat
resolving, or the plan going stale — not on every priority window. This keeps the
plan coherent and avoids paying the ~5s LLM cost per pass.

Both the autopilot (``action_planner``) and the coach (``coach.py``) construct
their own :class:`GamePlanManager` from the same backend, so there is exactly one
implementation of the strategic layer feeding both paths.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Strong, compact instruction. The model returns STRICT JSON so we can render a
# stable prompt block for the planner and a one-line intro for spoken advice.
GAME_PLAN_PROMPT = """You are a Magic: The Gathering strategic planner forming a PERSISTENT GAME PLAN.
Given the current board, hand, mana, life totals and deck archetype, decide HOW THIS GAME IS WON and the concrete path to get there.

Think a few turns ahead, not just this decision. Pick the realistic win condition for THIS board, then the steps to reach it, the biggest thing that can stop you, and the single most important thing to develop next.

Respond with ONLY a JSON object, no prose, no markdown:
{
  "win_conditions": ["primary win con (<=8 words)", "optional backup win con"],
  "path": "concrete path to the primary win con in turn shorthand (<=25 words), e.g. 'race for lethal ~T6 with creatures + auras, attack every turn'",
  "threat": "the opponent's biggest threat / what beats us (<=15 words)",
  "develop_next": "the single most important thing to develop or set up next (<=12 words)"
}"""


@dataclass
class GamePlan:
    """A persistent strategic plan for the current game."""

    win_conditions: list[str] = field(default_factory=list)
    path: str = ""
    threat: str = ""
    develop_next: str = ""
    turn_formed: int = 0
    raw: str = ""

    def is_empty(self) -> bool:
        return not (self.win_conditions or self.path or self.develop_next)

    def as_planner_block(self) -> str:
        """Multi-line block injected into the ActionPlanner per-decision prompt."""
        wins = "; ".join(w for w in self.win_conditions if w) or "(undetermined)"
        lines = [
            f"\nGAME PLAN (formed turn {self.turn_formed} — your strategic spine for this game):",
            f"  Win condition(s): {wins}",
        ]
        if self.path:
            lines.append(f"  Path to win: {self.path}")
        if self.threat:
            lines.append(f"  Biggest threat: {self.threat}")
        if self.develop_next:
            lines.append(f"  Develop next: {self.develop_next}")
        lines.append(
            "  Choose the action that best ADVANCES this plan. Develop toward the "
            "win — do NOT just react. Do NOT pass a turn that fails to advance the "
            "plan when a plan-advancing play (a castable creature/spell, a land "
            "drop, an attack) is legal."
        )
        return "\n".join(lines)

    def as_coach_intro(self) -> str:
        """One-line plan framing prepended to spoken coach advice."""
        primary = next((w for w in self.win_conditions if w), "")
        bits = []
        if self.path:
            bits.append(self.path)
        elif primary:
            bits.append(primary)
        if primary and self.path:
            bits.append(f"win: {primary}")
        return "Plan: " + "; ".join(b for b in bits if b) if bits else ""

    def as_payload(self) -> dict[str, Any]:
        """JSON-safe structured form for UI emission (desktop strategy card)."""
        return {
            "win_conditions": [w for w in self.win_conditions if w],
            "path": self.path,
            "threat": self.threat,
            "develop_next": self.develop_next,
            "turn_formed": self.turn_formed,
        }


def _round(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


class GamePlanManager:
    """Owns the current :class:`GamePlan` and decides when to (re)form it.

    Reform cadence is gated by a *material-change signature* of the board so the
    LLM is only consulted when the strategic picture actually shifted, and at
    most once per turn — never once per priority window.
    """

    # Material-change thresholds (deltas vs the signature at last reform).
    _LIFE_DELTA = 3
    _POWER_DELTA = 2
    _HAND_DELTA = 2
    # Force a refresh at least this often even if the board looks static, so a
    # long grind doesn't run forever on a turn-2 plan.
    _STALE_TURNS = 4

    # After this many consecutive stalls on plan-advancing plays, force a
    # reform and tell the model its current line is unexecutable so it picks a
    # different one (fixes the write-only plan that re-emitted "Cast Rush of
    # Dread" for five turns while the executor never landed it).
    _STALL_REFORM_THRESHOLD = 3

    def __init__(self, backend: Any, timeout: float = 10.0):
        self._backend = backend
        self._timeout = timeout
        self._plan: Optional[GamePlan] = None
        self._seed: Optional[str] = None  # deck archetype summary, if available
        self._last_sig: Optional[tuple] = None
        self._last_reform_turn: int = -1
        # Execution-feedback: stalls since the last reform + the last thing that
        # couldn't be executed, so the next reform avoids the stuck line.
        self._stall_count: int = 0
        self._stall_hint: str = ""

    # ----- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        """Clear all per-game state (call at the start of a new match)."""
        self._plan = None
        self._last_sig = None
        self._last_reform_turn = -1
        self._stall_count = 0
        self._stall_hint = ""

    def note_stall(self, what: str) -> None:
        """Record that a plan-advancing play could not be executed.

        Called by the autopilot when it has to auto_respond/escape/pause on a
        decision the plan wanted. Enough of these forces the next reform to pick
        a different, executable line rather than re-emitting the stuck plan.
        """
        self._stall_count += 1
        if what:
            self._stall_hint = what.strip()

    def seed(self, deck_strategy: Optional[str]) -> None:
        """Store the static deck archetype summary used to seed the first plan."""
        if deck_strategy and deck_strategy.strip():
            self._seed = deck_strategy.strip()

    @property
    def current(self) -> Optional[GamePlan]:
        return self._plan

    def plan_text(self) -> str:
        """Planner-prompt block for the current plan ("" if none yet)."""
        return self._plan.as_planner_block() if self._plan else ""

    def coach_intro(self) -> str:
        return self._plan.as_coach_intro() if self._plan else ""

    # ----- reform decision -------------------------------------------------
    def maybe_reform(
        self, game_state: dict[str, Any], *, force: bool = False
    ) -> Optional[GamePlan]:
        """(Re)form the plan iff the board changed materially; else return current.

        Cheap to call on every trigger — the LLM is only invoked when
        :meth:`_should_reform` says the strategic picture moved.
        """
        try:
            sig = self._signature(game_state)
        except Exception as e:  # never let plan formation break the decision loop
            logger.debug("game-plan signature failed: %s", e)
            return self._plan

        turn_num = sig[0]
        # New game detection: turn counter went backwards => fresh match.
        if self._last_reform_turn >= 0 and turn_num < self._last_reform_turn:
            self.reset()

        # Execution feedback: a plan that repeatedly can't be enacted must be
        # reconsidered even if the board looks materially unchanged.
        stalled = self._stall_count >= self._STALL_REFORM_THRESHOLD

        if not (force or stalled or self._should_reform(sig)):
            return self._plan

        plan = self._reform(game_state, turn_num)
        if plan is not None:
            self._plan = plan
            self._last_sig = sig
            self._last_reform_turn = turn_num
        # Clear stall feedback after a reform attempt regardless of outcome, so
        # we don't immediately reform again next window.
        self._stall_count = 0
        self._stall_hint = ""
        return self._plan

    def _should_reform(self, sig: tuple) -> bool:
        if self._plan is None or self._last_sig is None:
            return True
        turn_num = sig[0]
        # At most once per turn — avoids missing our own main-phase window to a
        # mid-turn re-plan during think time.
        if turn_num <= self._last_reform_turn:
            return False
        if turn_num - self._last_reform_turn >= self._STALE_TURNS:
            return True
        (_, my_life, opp_life, my_cr, opp_cr, my_pow, opp_pow, hand) = sig
        (_, l_my_life, l_opp_life, l_my_cr, l_opp_cr, l_my_pow, l_opp_pow, l_hand) = (
            self._last_sig
        )
        if my_cr != l_my_cr or opp_cr != l_opp_cr:
            return True
        if abs(my_life - l_my_life) >= self._LIFE_DELTA:
            return True
        if abs(opp_life - l_opp_life) >= self._LIFE_DELTA:
            return True
        if abs(my_pow - l_my_pow) >= self._POWER_DELTA:
            return True
        if abs(opp_pow - l_opp_pow) >= self._POWER_DELTA:
            return True
        if abs(hand - l_hand) >= self._HAND_DELTA:
            return True
        return False

    # ----- board reading ---------------------------------------------------
    def _local_seat(self, game_state: dict[str, Any]) -> Optional[int]:
        for p in game_state.get("players", []):
            if p.get("is_local"):
                return p.get("seat_id")
        return None

    def _signature(self, game_state: dict[str, Any]) -> tuple:
        """Compact tuple capturing the strategically-material board state."""
        turn = game_state.get("turn", {}) or {}
        turn_num = _round(turn.get("turn_number", 0))
        local_seat = self._local_seat(game_state)

        players = game_state.get("players", []) or []
        my_life = opp_life = 20
        for p in players:
            life = _round(p.get("life_total", 20), 20)
            if p.get("is_local"):
                my_life = life
            else:
                opp_life = life

        my_cr = opp_cr = my_pow = opp_pow = 0
        bf = game_state.get("battlefield", []) or []
        if not bf:
            # Some snapshots nest zones; fall back to any list-valued "battlefield".
            bf = game_state.get("zones", {}).get("battlefield", []) if isinstance(
                game_state.get("zones"), dict
            ) else []
        for card in bf:
            if "creature" not in str(card.get("type_line", "")).lower():
                continue
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            power = _round(card.get("power", 0))
            if controller == local_seat:
                my_cr += 1
                my_pow += power
            else:
                opp_cr += 1
                opp_pow += power

        hand_size = 0
        for p in players:
            if p.get("is_local"):
                hand_size = _round(p.get("hand_size", p.get("hand_count", 0)))
                break
        if not hand_size:
            hand = game_state.get("hand")
            if isinstance(hand, list):
                hand_size = len(hand)

        return (turn_num, my_life, opp_life, my_cr, opp_cr, my_pow, opp_pow, hand_size)

    # ----- LLM call --------------------------------------------------------
    def _build_context(self, game_state: dict[str, Any]) -> str:
        """Rich board/hand context, reusing the coach formatter when possible."""
        try:
            from arenamcp.coach import CoachEngine

            formatter = CoachEngine.__new__(CoachEngine)
            ctx = formatter._format_game_context(game_state, for_planner=True)
            if ctx and ctx.strip():
                return ctx
        except Exception as e:
            logger.debug("game-plan context formatter unavailable: %s", e)
        # Minimal fallback from the signature.
        sig = self._signature(game_state)
        return (
            f"Turn {sig[0]}. Your life {sig[1]}, opponent {sig[2]}. "
            f"Your board: {sig[3]} creatures ({sig[5]} power). "
            f"Opponent board: {sig[4]} creatures ({sig[6]} power). "
            f"Cards in hand: {sig[7]}."
        )

    def _reform(self, game_state: dict[str, Any], turn_num: int) -> Optional[GamePlan]:
        context = self._build_context(game_state)
        user_parts = [context]
        if self._seed:
            user_parts.append(f"\nDECK ARCHETYPE:\n{self._seed}")
        if self._stall_count >= self._STALL_REFORM_THRESHOLD and self._stall_hint:
            user_parts.append(
                f"\nPRIOR PLAN STALLED: the previous plan-advancing play "
                f"\"{self._stall_hint}\" could NOT be executed across several "
                f"attempts. Do not rely on that line again — choose a DIFFERENT, "
                f"executable win condition / next play this time."
            )
        user_parts.append("\nForm the GAME PLAN as JSON now.")
        user_message = "\n".join(user_parts)

        try:
            response = self._complete(GAME_PLAN_PROMPT, user_message)
        except Exception as e:
            logger.warning("game-plan LLM call failed (keeping prior plan): %s", e)
            return None

        plan = self._parse(response, turn_num)
        if plan is None or plan.is_empty():
            logger.debug("game-plan parse produced nothing usable")
            return None
        logger.info(
            "GamePlan (turn %d): win=%s | path=%s",
            turn_num,
            plan.win_conditions,
            plan.path,
        )
        return plan

    def _complete(self, system_prompt: str, user_message: str) -> str:
        """Call the backend, tolerating the small signature differences across clients."""
        try:
            return self._backend.complete(
                system_prompt,
                user_message,
                1024,
                temperature=0.0,
                request_timeout_s=self._timeout,
            )
        except TypeError:
            # Local backends may not accept request_timeout_s / temperature.
            try:
                return self._backend.complete(system_prompt, user_message, 1024)
            except TypeError:
                return self._backend.complete(system_prompt, user_message)

    @staticmethod
    def _parse(response: str, turn_num: int) -> Optional[GamePlan]:
        if not response or not isinstance(response, str):
            return None
        text = response.strip()
        if text.startswith("Error"):
            return None
        # Strip markdown fences.
        if text.startswith("```"):
            text = text.split("```", 2)[1] if "```" in text[3:] else text
            if text.lower().startswith("json"):
                text = text[4:]
        # Extract the first {...} block.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        blob = text[start : end + 1]
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            # Tolerate trailing commas.
            try:
                data = json.loads(blob.replace(",}", "}").replace(",]", "]"))
            except json.JSONDecodeError:
                return None
        if not isinstance(data, dict):
            return None

        wins_raw = data.get("win_conditions") or data.get("win_condition") or []
        if isinstance(wins_raw, str):
            wins = [wins_raw.strip()] if wins_raw.strip() else []
        elif isinstance(wins_raw, list):
            wins = [str(w).strip() for w in wins_raw if str(w).strip()][:2]
        else:
            wins = []

        return GamePlan(
            win_conditions=wins,
            path=str(data.get("path", "") or "").strip(),
            threat=str(data.get("threat", "") or "").strip(),
            develop_next=str(data.get("develop_next", "") or "").strip(),
            turn_formed=turn_num,
            raw=blob,
        )
