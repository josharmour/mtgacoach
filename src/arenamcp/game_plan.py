"""Persistent, adaptive GAME PLAN layer shared by the autopilot and the coach.

The :class:`GamePlan` is the *strategic spine* that sits between the static deck
archetype summary (``coach._deck_strategy``) and the per-decision tactical
executor (:class:`arenamcp.action_planner.ActionPlanner`). It names 1-2 win
conditions and the concrete path to each, plus the current threat assessment and
"what to develop next", and is then threaded into every tactical decision so the
autopilot/coach *develop toward a win* instead of reacting one snapshot at a
time.

Cadence is deliberately slow: a plan is (re)formed only on **material** changes
with a cooldown and one background call at a time. Tactical decisions keep
using the existing plan and current state while it refreshes.

The autopilot and coach can share one :class:`GamePlanManager`, preserving the
same strategy across modes without competing background model requests.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from arenamcp.backend_health import is_backend_error_text

logger = logging.getLogger(__name__)


# Strong, compact instruction. The model returns STRICT JSON so we can render a
# stable prompt block for the planner and a one-line intro for spoken advice.
GAME_PLAN_PROMPT = """You are a Magic: The Gathering strategic planner forming a PERSISTENT GAME PLAN.
Given the current board, hand, mana, life totals and deck archetype, decide HOW THIS GAME IS WON and the concrete path to get there.

Think a few turns ahead, not just this decision. Pick the realistic win condition for THIS board, then the steps to reach it, the biggest thing that can stop you, and the single most important thing to develop next.
Each turn's planned play MUST be mana-legal. Do NOT list multiple spells for a single turn unless their COMBINED mana cost is <= total available mana for that turn.
Use the complete deck and remaining library to identify realistic engines, outs and backup plans; cards in the library are possibilities, not cards in hand or guaranteed draws. Preserve the prior plan when still sound, and adapt when its assumptions change.
Removal and tutoring are conditional decisions: compare the current threat, timing, mana and opportunity cost. Hold interaction when that protects the winning line; remove a threat when it prevents loss or unlocks progress. A tutor should find the currently useful legal card still in the library, with a feasible follow-up, rather than repeat an old preferred target.

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
            "  Develop toward this win, but treat the plan as conditional guidance. "
            "The current board, stack, legal choices and immediate survival/lethal "
            "override an older line. Re-evaluate removal targets and tutor choices "
            "now; do not spend interaction or tutor just because it is available. "
            "Holding mana or passing is correct when it protects the stronger line."
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
    on meaningful changes, with background requests rate-limited independently
    of tactical decisions.
    """

    # Material-change thresholds (deltas vs the signature at last reform).
    _LIFE_DELTA = 3
    _POWER_DELTA = 2
    _HAND_DELTA = 2
    # Force a refresh at least this often even if the board looks static, so a
    # long grind doesn't run forever on a turn-2 plan.
    _STALE_TURNS = 4
    _REFRESH_INTERVAL_S = 15.0

    # After this many consecutive stalls on plan-advancing plays, force a
    # reform and tell the model its current line is unexecutable so it picks a
    # different one (fixes the write-only plan that re-emitted "Cast Rush of
    # Dread" for five turns while the executor never landed it).
    _STALL_REFORM_THRESHOLD = 3

    def __init__(self, backend: Any, timeout: float = 10.0):
        self._backend = backend
        self._timeout = timeout
        self._plan: GamePlan | None = None
        self._seed: str | None = None  # deck archetype summary, if available
        self._last_sig: tuple | None = None
        self._last_reform_turn: int = -1
        # Execution-feedback: stalls since the last reform + the last thing that
        # couldn't be executed, so the next reform avoids the stuck line.
        self._stall_count: int = 0
        self._stall_hint: str = ""
        self._lock = threading.RLock()
        self._generation = 0
        self._match_id: str | None = None
        self._observed_turn = 0
        self._last_seed: str | None = None
        self._inflight = False
        self._last_attempt_at: float | None = None

    # ----- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        """Clear all per-game state (call at the start of a new match)."""
        with self._lock:
            self._generation += 1
            self._plan = None
            self._seed = None
            self._last_seed = None
            self._last_sig = None
            self._last_reform_turn = -1
            self._stall_count = 0
            self._stall_hint = ""
            self._match_id = None
            self._observed_turn = 0
            self._last_attempt_at = None

    def observe(self, game_state: dict[str, Any]) -> None:
        """Invalidate old-match plans before any tactical prompt can use them."""
        match_id = game_state.get("match_id")
        turn = _round((game_state.get("turn") or {}).get("turn_number"))
        with self._lock:
            if (match_id and self._match_id and match_id != self._match_id) or (
                0 < turn < self._observed_turn
            ):
                self.reset()
            if match_id:
                self._match_id = match_id
            self._observed_turn = max(turn, self._observed_turn)

    def note_stall(self, what: str) -> None:
        """Record that a plan-advancing play could not be executed.

        Called by the autopilot when it has to auto_respond/escape/pause on a
        decision the plan wanted. Enough of these forces the next reform to pick
        a different, executable line rather than re-emitting the stuck plan.
        """
        with self._lock:
            self._stall_count += 1
            if what:
                self._stall_hint = what.strip()

    def seed(self, deck_strategy: str | None) -> None:
        """Store the static deck archetype summary used to seed the first plan."""
        with self._lock:
            self._seed = (
                deck_strategy.strip() if isinstance(deck_strategy, str) and deck_strategy.strip() else None
            )

    def export_for_reload(self) -> dict:
        """Retain strategy, not pending actions or an in-flight model request."""
        from dataclasses import asdict

        with self._lock:
            return {
                "plan": asdict(self._plan) if self._plan else None,
                "seed": self._seed,
                "last_sig": self._last_sig,
                "last_reform_turn": self._last_reform_turn,
            }

    def restore_after_reload(self, saved: dict, state: dict) -> None:
        """Restore a validated same-match handoff without an extra model call."""

        def freeze(value):
            return tuple(freeze(item) for item in value) if isinstance(value, list) else value

        with self._lock:
            self.observe(state)
            raw_plan = saved.get("plan")
            if isinstance(raw_plan, dict):
                self._plan = GamePlan(
                    **{key: value for key, value in raw_plan.items() if key in GamePlan.__dataclass_fields__}
                )
            self.seed(saved.get("seed"))
            self._last_seed = self._seed
            self._last_sig = freeze(saved.get("last_sig"))
            self._last_reform_turn = saved.get("last_reform_turn", -1)
            self._last_attempt_at = time.monotonic()

    @property
    def current(self) -> GamePlan | None:
        return self._plan

    def plan_text(self) -> str:
        """Planner-prompt block for the current plan ("" if none yet)."""
        plan = self._plan
        return plan.as_planner_block() if plan else ""

    def coach_intro(self) -> str:
        plan = self._plan
        return plan.as_coach_intro() if plan else ""

    # ----- reform decision -------------------------------------------------
    def request_reform(
        self,
        game_state: dict[str, Any],
        *,
        on_updated: Callable[[], None] | None = None,
    ) -> bool:
        """Schedule one bounded background refresh without delaying a decision.

        Repeated windows do not queue work. A live stack takes priority over
        speculative strategy, and failures share the cooldown with successes.
        """
        with self._lock:
            self.observe(game_state)
            suspended = getattr(self, "background_suspended_fn", None)
            if callable(suspended) and suspended():
                return False
            if self._inflight or game_state.get("stack") or game_state.get("game_over"):
                return False
            if not _round((game_state.get("turn") or {}).get("turn_number")):
                return False
            now = time.monotonic()
            if self._last_attempt_at is not None and now - self._last_attempt_at < self._REFRESH_INTERVAL_S:
                return False
            sig = self._signature(game_state)
            if self._stall_count < self._STALL_REFORM_THRESHOLD and not self._should_reform(sig):
                return False
            snapshot = deepcopy(game_state)
            generation = self._generation
            self._inflight = True
            self._last_attempt_at = now

        def refresh() -> None:
            try:
                self.maybe_reform(snapshot, _expected_generation=generation)
                with self._lock:
                    publish = generation == self._generation
                if publish and on_updated is not None:
                    on_updated()
            except Exception as error:
                logger.debug("background game-plan refresh failed: %s", error)
            finally:
                with self._lock:
                    self._inflight = False

        try:
            threading.Thread(target=refresh, daemon=True, name="game-plan-reform").start()
        except Exception:
            with self._lock:
                self._inflight = False
            raise
        return True

    def maybe_reform(
        self,
        game_state: dict[str, Any],
        *,
        force: bool = False,
        _expected_generation: int | None = None,
    ) -> GamePlan | None:
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
        with self._lock:
            if _expected_generation is not None and _expected_generation != self._generation:
                return self._plan
            # request_reform already observed the snapshot before dispatch.
            # Re-observing an older snapshot after a new turn arrives would
            # mistake normal background lag for the next match starting.
            if _expected_generation is None:
                self.observe(game_state)
            generation = self._generation
            stalled = self._stall_count >= self._STALL_REFORM_THRESHOLD
            if not (force or stalled or self._should_reform(sig)):
                return self._plan
            seed = self._seed
            stall_count = self._stall_count

        plan = self._reform(game_state, turn_num)
        with self._lock:
            if generation != self._generation:
                return self._plan
            if plan is not None:
                self._plan = plan
                self._last_sig = sig
                self._last_seed = seed
                self._last_reform_turn = turn_num
            self._stall_count = max(0, self._stall_count - stall_count)
            if not self._stall_count:
                self._stall_hint = ""
            return self._plan

    def _should_reform(self, sig: tuple) -> bool:
        if self._plan is None or self._last_sig is None:
            return True
        if self._seed != self._last_seed:
            return True
        turn_num = sig[0]
        if turn_num - self._last_reform_turn >= self._STALE_TURNS:
            return True
        # Identity matters: a tutor changes one hand card without changing hand
        # size, and a noncreature engine can change the entire winning line.
        if sig[8:] != self._last_sig[8:]:
            return True
        (_, my_life, opp_life, my_cr, opp_cr, my_pow, opp_pow, hand) = sig[:8]
        (_, l_my_life, l_opp_life, l_my_cr, l_opp_cr, l_my_pow, l_opp_pow, l_hand) = self._last_sig[:8]
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
        return abs(hand - l_hand) >= self._HAND_DELTA

    # ----- board reading ---------------------------------------------------
    def _local_seat(self, game_state: dict[str, Any]) -> int | None:
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
            bf = (
                game_state.get("zones", {}).get("battlefield", [])
                if isinstance(game_state.get("zones"), dict)
                else []
            )
        for card in bf:
            types = card.get("card_types") or []
            is_creature = (
                any(str(t).removeprefix("CardType_") == "Creature" for t in types)
                if types
                else "creature" in str(card.get("type_line", "")).lower()
            )
            if not is_creature:
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

        def identities(cards: list[dict]) -> tuple:
            # Deliberately omit tapped status and state ids: paying mana and
            # passing priority should not trigger another strategy request.
            return tuple(
                sorted(
                    (
                        str(card.get("instance_id") or card.get("grp_id") or card.get("name") or ""),
                        str(card.get("name") or ""),
                        str(card.get("controller_seat_id") or card.get("owner_seat_id") or ""),
                    )
                    for card in cards
                    if isinstance(card, dict)
                )
            )

        return (
            turn_num,
            my_life,
            opp_life,
            my_cr,
            opp_cr,
            my_pow,
            opp_pow,
            hand_size,
            identities(bf),
            identities(game_state.get("hand") or []),
            identities(game_state.get("graveyard") or []),
            identities(game_state.get("exile") or []),
        )

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

    def _reform(self, game_state: dict[str, Any], turn_num: int) -> GamePlan | None:
        from arenamcp.match_context import prepare_match_context, with_deck_reference

        game_state = prepare_match_context(game_state)
        context = self._build_context(game_state)
        user_parts = [context]
        if self._seed:
            user_parts.append(f"\nDECK ARCHETYPE:\n{self._seed}")
        if self._plan:
            user_parts.append(self._plan.as_planner_block())
        if self._stall_count >= self._STALL_REFORM_THRESHOLD and self._stall_hint:
            user_parts.append(
                f"\nPRIOR PLAN STALLED: the previous plan-advancing play "
                f'"{self._stall_hint}" could NOT be executed across several '
                f"attempts. Do not rely on that line again — choose a DIFFERENT, "
                f"executable win condition / next play this time."
            )
        user_parts.append("\nForm the GAME PLAN as JSON now.")
        user_message = with_deck_reference("\n".join(user_parts), game_state)

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
    def _parse(response: str, turn_num: int) -> GamePlan | None:
        if not response or not isinstance(response, str):
            return None
        text = response.strip()
        if is_backend_error_text(text):
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
