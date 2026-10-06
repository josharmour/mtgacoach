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

Grounding (2026-10-06): plans used to be free-form ("Empty-library Fblthp win"
with 25 cards in the library, "ramp via Murmuring Volume" while dying). Every
plan is now formed from :mod:`arenamcp.board_assessment` facts (clocks, race,
role, lethal flags, mana budget per turn), must name a role and a mana-legal
T/T+1/T+2 turn plan, and is validated against those facts before use:
unrealistic win conditions are dropped, a role that contradicts a lethal or
dead-in-two assessment is replaced, and unaffordable or not-in-hand casts are
trimmed. Per-decision prompts lead with the freshly recomputed ROLE, this
turn's step and the facts (:meth:`GamePlanManager.strategy_block`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from arenamcp.backend_health import is_backend_error_text

logger = logging.getLogger(__name__)


# Strong, compact instruction. The model returns STRICT JSON so we can render a
# stable prompt block for the planner and a one-line intro for spoken advice.
GAME_PLAN_PROMPT = """You are a Magic: The Gathering strategic planner forming a PERSISTENT GAME PLAN.
Given the current board, hand, mana, life totals, the deterministic BOARD FACTS and the Oracle-grounded deck playbook, decide WHO IS THE BEATDOWN, how this game is won, and what to do on each of our next three turns, like a strong human player looking 2-3 turns ahead.

BOARD FACTS are computed from the live board (clocks through best blocks, race, lethal flags, mana budget per turn). Treat them as hard facts:
- ROLE must equal the ASSESSED ROLE unless role_reason names a concrete board fact the assessment missed. If the facts say OPPONENT HAS LETHAL ON BOARD or DEAD IN 2, the role is defender or control/stabilize unless we have lethal first.
- The turn plan covers T (this turn if it is ours, else our next turn), T+1 and T+2 (our following turns). Each turn's "cast" list may name ONLY cards in our hand now (or castable from our graveyard), each card at most once, and their combined mana value must fit that turn's MANA BUDGET with the colours available. A card we hope to draw goes in "hold" as "if drawn: <name>", never in "cast".
- As defender/control, prioritise creatures that block and removal on attackers over card draw, mana rocks or cycling until the clock is under control. As aggressor, maximise damage; spend removal on blockers.
- Win conditions must be realistic for the CURRENT state: no empty-library/alternate wins while the library is large, and no combo whose pieces are not in hand or on the battlefield (label a needed draw explicitly).
Use the complete deck and remaining library to identify realistic engines, outs and backup plans; cards in the library are possibilities, not cards in hand or guaranteed draws. Preserve the prior plan when still sound, and adapt when its assumptions change.
Removal and tutoring are conditional decisions: compare the current threat, timing, mana and opportunity cost. Hold interaction when that protects the winning line; remove a threat when it prevents loss or unlocks progress. A tutor should find the currently useful legal card still in the library, with a feasible follow-up, rather than repeat an old preferred target.
Use the playbook's conditional decision rules, not just its archetype or finishers. Evaluate commander deployment/recovery from its actual rules and current tax. Do not treat a desirable library card as an available plan.

Respond with ONLY a JSON object, no prose, no markdown:
{
  "role": "aggressor | defender | race | control/stabilize",
  "role_reason": "<=20 words; required when role differs from the assessed role",
  "turns": [
    {"turn": "T", "land": "land from hand to play or ''", "cast": ["exact card names from hand"], "attack": "who attacks, or 'none'", "hold": "mana/cards held back and why, or ''"},
    {"turn": "T+1", "land": "", "cast": [], "attack": "", "hold": ""},
    {"turn": "T+2", "land": "", "cast": [], "attack": "", "hold": ""}
  ],
  "win_conditions": ["primary win con realistic now (<=8 words)", "optional backup"],
  "path": "concrete path to the primary win con in turn shorthand (<=25 words)",
  "threat": "the opponent's biggest threat / what beats us (<=15 words)",
  "develop_next": "the single most important thing to develop next (<=12 words)",
  "switch_if": ["board change that flips the role or plan, e.g. 'they lose their flyer -> attack'"],
  "active_mechanisms": ["relevant deck playbook mechanism ID and current applicability"],
  "resource_priorities": ["what to preserve versus spend/recover, with a reason"],
  "assumptions": ["mana, timing, survival or availability condition to recheck"]
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
    active_mechanisms: list[str] = field(default_factory=list)
    resource_priorities: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    # Grounded fields (board_assessment): role, the validated T/T+1/T+2 steps
    # (absolute turn numbers), when to switch, and what validation repaired.
    role: str = ""
    role_reason: str = ""
    turn_plan: list[dict] = field(default_factory=list)
    switch_if: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not (self.win_conditions or self.path or self.develop_next or self.turn_plan)

    def step_for(self, turn: int) -> dict | None:
        return next((step for step in self.turn_plan if step.get("turn") == turn), None)

    def as_planner_block(self, *, with_role_and_turns: bool = True) -> str:
        """Multi-line block injected into the ActionPlanner per-decision prompt.

        ``with_role_and_turns=False`` omits the role and turn-plan lines when
        :func:`compose_strategy_block` has already rendered them up front.
        """
        wins = "; ".join(w for w in self.win_conditions if w) or "(undetermined)"
        lines = [f"\nGAME PLAN (formed turn {self.turn_formed} — your strategic spine for this game):"]
        if self.role and with_role_and_turns:
            reason = f" — {self.role_reason}" if self.role_reason else ""
            lines.append(f"  Role: {self.role.upper()}{reason}")
        if self.turn_plan and with_role_and_turns:
            lines.append(
                "  Turn plan: " + " | ".join(f"T{step['turn']}: {step_text(step)}" for step in self.turn_plan)
            )
        lines.append(f"  Win condition(s): {wins}")
        if self.path:
            lines.append(f"  Path to win: {self.path}")
        if self.switch_if:
            lines.append("  Switch if: " + "; ".join(self.switch_if))
        if self.threat:
            lines.append(f"  Biggest threat: {self.threat}")
        if self.develop_next:
            lines.append(f"  Develop next: {self.develop_next}")
        lines.extend(f"  Active mechanism: {item}" for item in self.active_mechanisms)
        lines.extend(f"  Resource priority: {item}" for item in self.resource_priorities)
        lines.extend(f"  Recheck assumption: {item}" for item in self.assumptions)
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
            "active_mechanisms": self.active_mechanisms,
            "resource_priorities": self.resource_priorities,
            "assumptions": self.assumptions,
            "role": self.role,
            "role_reason": self.role_reason,
            "turn_plan": [dict(step) for step in self.turn_plan],
            "switch_if": list(self.switch_if),
            "issues": list(self.issues),
        }


def step_text(step: dict) -> str:
    """'play Island; cast Undulating Witness; attack: none; hold: UU for Countersculpt'."""
    bits = []
    if step.get("land"):
        bits.append(f"play {step['land']}")
    casts = [c for c in step.get("cast") or [] if c]
    bits.append("cast " + " + ".join(casts) if casts else "no cast")
    attack = str(step.get("attack") or "").strip()
    if attack:
        bits.append(f"attack: {attack}")
    hold = str(step.get("hold") or "").strip()
    if hold:
        bits.append(f"hold: {hold}")
    return "; ".join(bits)


# --- validation against board facts -------------------------------------------

# Our-library alternate wins ("win when the trigger resolves with an empty
# library", "mill-out", "draw out the deck"). Opponent-library mill is a
# different plan and is not rejected here.
_LIBRARY_WIN = re.compile(
    r"empty[- ]library|library (?:is |to |at )?(?:zero|0|empty)|\bmill[- ]?out\b|library[- ]out"
    r"|deck(?:s|ing)? (?:my|our)sel(?:f|ves)|draw (?:out )?(?:our|my) (?:whole |entire )?(?:deck|library)"
    r"|(?:my|our) library (?:runs|is) out|deplet\w* (?:our |my |the )?library|empty (?:our|my|the) library"
    r"|mill (?:ourselves|myself|our library|my library)",
    re.IGNORECASE,
)
_OPPONENT_LIBRARY = re.compile(
    r"\b(?:opponent|opp|their)(?:'s)? (?:library|deck)\b|\bmill (?:the )?(?:opponent|them)\b", re.I
)
_DRAW_LABEL = re.compile(
    r"\bif drawn\b|\bdraw(?:s|ing)? into\b|\bdrawn\b|\btop-?deck|\bdig\b|\bfind\b|\btutor|\bsearch|\bredraw",
    re.I,
)
# A library this small can realistically run out within the plan's horizon.
_SMALL_LIBRARY = 5


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _card_names(cards: list) -> dict[str, dict]:
    """Lookup of a card by full name and by the part before a comma."""
    names: dict[str, dict] = {}
    for card in cards or []:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        full = _plain(card["name"])
        names.setdefault(full, card)
        short = _plain(str(card["name"]).split(",")[0])
        if len(short) >= 4:
            names.setdefault(short, card)
    return names


def _mentions(text: str, name: str) -> bool:
    return bool(name) and re.search(rf"(?:^| ){re.escape(name)}(?: |$)", _plain(text)) is not None


def _library_only_names(state: dict) -> set[str]:
    """Our deck cards not visible in hand/battlefield/graveyard/exile/stack (so in the library)."""
    catalog = state.get("deck_catalog") if isinstance(state.get("deck_catalog"), dict) else {}
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )
    visible = set()
    for zone in ("hand", "battlefield", "graveyard", "exile", "stack", "command"):
        for card in state.get(zone) or []:
            if isinstance(card, dict) and card.get("owner_seat_id", local) == local and card.get("name"):
                visible.add(_plain(card["name"]))
                visible.add(_plain(str(card["name"]).split(",")[0]))
    names = set()
    for info in catalog.values():
        name = info.get("name") if isinstance(info, dict) else None
        if not name or "land" in str(info.get("type_line") or "").lower():
            continue
        full, short = _plain(name), _plain(str(name).split(",")[0])
        if full not in visible and short not in visible:
            names.add(full)
            if len(short) >= 4:
                names.add(short)
    return names


def _flashback_cost(card: dict) -> str | None:
    text = str(card.get("oracle_text") or "")
    match = re.search(r"flashback\s*[\u2014\-—:]?\s*((?:\{[^}]*\})+)", text, re.I)
    return match.group(1) if match else None


def validate_plan(plan: GamePlan, assessment: Any, state: dict) -> GamePlan:
    """Repair ``plan`` in place against the board facts; record what changed in ``plan.issues``.

    * role: unknown roles take the assessed role; aggressor is rejected while
      the opponent has lethal / we are dead in two (unless we have lethal);
      lethal-now forces aggressor; any other disagreement needs a reason.
    * win conditions: our-library alternate wins are dropped while the library
      has more than a handful of cards; wins naming a card that is still in the
      library are dropped unless labelled as a draw. If none survive, a
      grounded win condition from the assessment replaces them.
    * turn plan: casts must be in hand (or flashback-able from our graveyard),
      used once, and fit that turn's mana budget and colours; a library card is
      moved to "hold: if drawn: X"; the most expensive excess is trimmed.
    """
    from arenamcp.board_assessment import ROLE_AGGRESSOR, ROLE_CONTROL, ROLE_DEFENDER, ROLES

    issues: list[str] = []
    if assessment is None:
        if plan.turn_plan:
            issues.append("turn plan dropped: no board facts to check it against")
        plan.turn_plan = []
        plan.issues = issues
        return plan

    # --- role -----------------------------------------------------------------
    role = (plan.role or "").strip().lower()
    aliases = {
        "control": ROLE_CONTROL,
        "stabilize": ROLE_CONTROL,
        "beatdown": ROLE_AGGRESSOR,
        "defense": ROLE_DEFENDER,
    }
    role = aliases.get(role, role)
    in_danger = (
        assessment.opp_lethal_on_board
        or (assessment.dead_in is not None and assessment.dead_in <= 2)
        or (
            assessment.race == "behind" and assessment.their_clock is not None and assessment.their_clock <= 2
        )
    )
    if role not in ROLES:
        if role:
            issues.append(f"role '{plan.role}' is not a known role; using the assessed {assessment.role}")
        role = assessment.role
    elif assessment.lethal_now and role != ROLE_AGGRESSOR:
        issues.append(f"role {role} rejected: we have lethal on board now")
        role = ROLE_AGGRESSOR
    elif in_danger and role == ROLE_AGGRESSOR and not assessment.lethal_next_turn:
        issues.append(f"role aggressor rejected: {'; '.join(assessment.flags) or assessment.role_reason}")
        role = assessment.role
    elif role != assessment.role and len((plan.role_reason or "").split()) < 3:
        issues.append(f"role {role} differs from the assessed {assessment.role} without a concrete reason")
        role = assessment.role
    if role == assessment.role and not plan.role_reason:
        plan.role_reason = assessment.role_reason
    plan.role = role

    # --- win conditions -------------------------------------------------------
    library = assessment.library_count
    library_only = _library_only_names(state)
    kept: list[str] = []
    for win in plan.win_conditions:
        if _LIBRARY_WIN.search(win) and not _OPPONENT_LIBRARY.search(win):
            if library is not None and library > _SMALL_LIBRARY:
                issues.append(f"win condition '{win}' rejected: our library still has {library} cards")
                continue
        needed = sorted(name for name in library_only if _mentions(win, name))
        if needed and not _DRAW_LABEL.search(win):
            issues.append(f"win condition '{win}' rejected: needs {', '.join(needed)} (still in the library)")
            continue
        kept.append(win)
    if not kept:
        kept = [_grounded_win(assessment, state)]
        issues.append(f"win condition replaced from board facts: {kept[0]}")
    plan.win_conditions = kept[:2]
    if plan.path and _LIBRARY_WIN.search(plan.path) and library is not None and library > _SMALL_LIBRARY:
        issues.append(f"path rejected (library-out plan with {library} cards left): {plan.path}")
        plan.path = ""

    # --- turn plan ------------------------------------------------------------
    plan.turn_plan = _validate_turns(plan.turn_plan, assessment, state, issues)
    plan.issues = issues
    return plan


def _grounded_win(assessment: Any, state: dict) -> str:
    from arenamcp.board_assessment import ROLE_AGGRESSOR, ROLE_RACE

    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )
    ours = sorted(
        (
            c
            for c in state.get("battlefield") or []
            if isinstance(c, dict)
            and (c.get("controller_seat_id") or c.get("owner_seat_id")) == local
            and "creature" in f"{c.get('type_line') or ''} {c.get('card_types') or ''}".lower()
        ),
        key=lambda c: -(c.get("power") or 0),
    )
    if assessment.role in (ROLE_AGGRESSOR, ROLE_RACE) and ours:
        names = " + ".join(str(c.get("name")) for c in ours[:2])
        return f"Combat damage with {names} (our clock {assessment.our_clock or '—'})"
    deploy = next((step.casts for step in assessment.lookahead if step.casts), [])
    if deploy:
        return f"Stabilize behind {deploy[0]}, then win with our biggest creatures"
    return "Stabilize the board, then win with creature damage"


def _turn_index(value: Any, k: int) -> int:
    text = str(value or "").strip().upper().replace(" ", "")
    if text in ("T", "T+0"):
        return 0
    match = re.fullmatch(r"T\+(\d)", text)
    return int(match.group(1)) if match else k


def _validate_turns(raw_turns: list, assessment: Any, state: dict, issues: list[str]) -> list[dict]:
    from arenamcp.board_assessment import NON_BOARD_ROLES, card_role
    from arenamcp.mulligan_policy import _pip_matching, hand_card

    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    hand_names = _card_names(hand)
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )
    graveyard = [
        c
        for c in state.get("graveyard") or []
        if isinstance(c, dict) and c.get("owner_seat_id", local) == local and _flashback_cost(c)
    ]
    grave_names = _card_names(graveyard)
    library_only = _library_only_names(state)
    used: set[int] = set()
    rocks: list[SimpleNamespace] = []
    steps: list[dict] = []
    for k, raw in enumerate(raw_turns[:3]):
        if not isinstance(raw, dict):
            continue
        index = min(_turn_index(raw.get("turn"), k), len(assessment.lookahead) - 1)
        if index < 0 or index >= len(assessment.lookahead):
            continue
        budget = assessment.lookahead[index]
        label = budget.label
        sources = [SimpleNamespace(produces=frozenset(colors)) for colors in budget.source_colors] + list(
            rocks
        )
        casts_raw = raw.get("cast") or []
        if isinstance(casts_raw, str):
            casts_raw = [part.strip() for part in re.split(r",|\+| and ", casts_raw) if part.strip()]
        holds = [str(raw.get("hold") or "").strip()] if str(raw.get("hold") or "").strip() else []
        chosen: list[tuple[str, int, tuple, dict]] = []
        for name in casts_raw:
            key = _plain(name)
            card = hand_names.get(key) or hand_names.get(_plain(str(name).split(",")[0]))
            cost_text = None
            if card is None:
                card = grave_names.get(key) or grave_names.get(_plain(str(name).split(",")[0]))
                cost_text = _flashback_cost(card) if card else None
            if card is None:
                if key in library_only or _plain(str(name).split(",")[0]) in library_only:
                    holds.append(f"if drawn: {name}")
                    issues.append(f"{label}: {name} is not in hand (library) — kept only as 'if drawn'")
                else:
                    issues.append(f"{label}: dropped {name} (not in hand or castable from the graveyard)")
                continue
            identity = id(card)
            if identity in used:
                issues.append(f"{label}: dropped {card.get('name')} (already cast earlier in the plan)")
                continue
            info = hand_card(dict(card, mana_cost=cost_text or card.get("mana_cost") or ""))
            chosen.append((str(card.get("name")), info.mana_value, info.pips, card))
        # Trim until the turn is mana-legal: when defending, card draw / rocks /
        # selection go first; otherwise (and then) the most expensive cast.
        survival = bool(getattr(assessment, "survival_mode", False))
        colors = "".join(sorted(set("".join(budget.source_colors)) - {"C"}))
        dropped = False
        while chosen:
            total = sum(c[1] for c in chosen)
            pips = tuple(p for c in chosen for p in c[2])
            if total <= len(sources) and _pip_matching(pips, sources):
                break
            drop = max(
                chosen,
                key=lambda c: (survival and card_role(c[3]) in NON_BOARD_ROLES, c[1]),
            )
            chosen.remove(drop)
            dropped = True
            issues.append(
                f"{label}: dropped {drop[0]} — not mana-legal (plan needs {total} mana, "
                f"budget {len(sources)}{f' {colors}' if colors else ''})"
            )
        if dropped and not chosen:
            # Trimming emptied the turn: use the board-math deployment instead
            # when it is still unused and affordable after this plan's earlier turns.
            fill = []
            for name in budget.casts:
                card = hand_names.get(_plain(name))
                if card is not None and id(card) not in used:
                    info = hand_card(card)
                    fill.append((str(card.get("name")), info.mana_value, info.pips, card))
            total = sum(c[1] for c in fill)
            if (
                fill
                and total <= len(sources)
                and _pip_matching(tuple(p for c in fill for p in c[2]), sources)
            ):
                chosen = fill
                holds.append("board-math deployment")
                issues.append(
                    f"{label}: filled with the board-math deployment: {', '.join(c[0] for c in fill)}"
                )
        for name, _mv, _pips, card in chosen:
            used.add(id(card))
            text = str(card.get("oracle_text") or "").lower()
            type_line = str(card.get("type_line") or "").lower()
            if "creature" not in type_line and re.search(r"\{o?t\}[^:]*:\s*add\b", text):
                rocks.append(SimpleNamespace(produces=frozenset("WUBRGC")))
        land = str(raw.get("land") or "").strip()
        if land and _plain(land) not in hand_names:
            if index == 0:
                issues.append(f"{label}: land {land} is not in hand")
            land = ""
        steps.append(
            {
                "turn": budget.turn,
                "label": label,
                "land": land,
                "cast": [c[0] for c in chosen],
                "attack": str(raw.get("attack") or "").strip()[:80],
                "hold": "; ".join(holds)[:120],
                "mana": len(sources),
            }
        )
    return steps


def compose_strategy_block(assessment: Any, plan: GamePlan | None) -> str:
    """ROLE + this turn + facts (fresh) followed by the game plan's spine.

    The assessment is recomputed from the decision's own snapshot, so the role,
    clocks and lethal flags are current even when the plan was formed earlier
    in the turn. A validated plan step for this turn replaces the board-math
    deployment suggestion.
    """
    if assessment is None:
        return plan.as_planner_block().strip() if plan else ""
    this_turn = next_turns = None
    role_note = ""
    if plan is not None and not plan.is_empty():
        step = plan.step_for(assessment.plan_turn)
        if step is not None:
            this_turn = f"{step_text(step)} [game plan T{plan.turn_formed}]"
        later = [s for s in plan.turn_plan if s.get("turn", 0) > assessment.plan_turn]
        if later:
            next_turns = " | ".join(f"T{s['turn']}: {step_text(s)}" for s in later)
        if plan.role and plan.role != assessment.role:
            role_note = (
                f" [game plan (turn {plan.turn_formed}) said {plan.role.upper()}; "
                "these board facts are newer — follow them]"
            )
    block = assessment.prompt_block(this_turn=this_turn, next_turns=next_turns, role_note=role_note)
    if plan is not None and not plan.is_empty():
        block += "\n" + plan.as_planner_block(with_role_and_turns=False)
    return block


def grounded_facts_block(game_state: dict | None) -> str:
    """Fresh board facts (no game plan) for prompts without a plan manager."""
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(game_state) if isinstance(game_state, dict) else None
    except Exception as error:  # never break a prompt on the strategic layer
        logger.debug("board assessment unavailable: %s", error)
        return ""
    return assessment.prompt_block() if assessment else ""


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

    # The strategic call runs in the background once per turn, so it gets a
    # larger reasoning/time budget than per-decision calls (12 s, 2048 tokens,
    # low reasoning effort): full thinking, 6000 tokens, 45 s.
    _PLAN_MAX_TOKENS = 6000
    _PLAN_TIMEOUT_S = 45.0

    def __init__(self, backend: Any, timeout: float | None = None):
        self._backend = backend
        self._timeout = self._PLAN_TIMEOUT_S if timeout is None else timeout
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

    def strategy_block(self, game_state: dict[str, Any] | None) -> str:
        """Fresh ROLE + this turn + facts for ``game_state``, then the plan spine."""
        from arenamcp.board_assessment import assess

        plan = self._plan
        assessment = assess(game_state) if isinstance(game_state, dict) else None
        return compose_strategy_block(assessment, plan)

    def ui_payload(self, game_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Plan payload for the desktop plan card, with current board facts.

        Facts (role, clocks, flags, board-math lookahead) are recomputed from
        ``game_state`` so the card shows "behind on board" before the first
        model plan arrives; ``{}`` when there is neither a plan nor a board.
        """
        payload: dict[str, Any] = dict(self._plan.as_payload()) if self._plan else {}
        if isinstance(game_state, dict):
            try:
                from arenamcp.board_assessment import assess

                assessment = assess(game_state)
            except Exception as error:
                logger.debug("game-plan facts unavailable: %s", error)
                assessment = None
            if assessment is not None:
                payload["facts"] = assessment.as_payload()
                payload.setdefault("role", assessment.role)
                if not payload.get("role"):
                    payload["role"] = assessment.role
                if not payload.get("role_reason"):
                    payload["role_reason"] = assessment.role_reason
        return payload

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
            our_turn = self._our_turn(game_state)
            if self._stall_count < self._STALL_REFORM_THRESHOLD and not self._should_reform(
                sig, our_turn=our_turn
            ):
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
            if not (force or stalled or self._should_reform(sig, our_turn=self._our_turn(game_state))):
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

    def _should_reform(self, sig: tuple, *, our_turn: bool = False) -> bool:
        if self._plan is None or self._last_sig is None:
            return True
        if self._seed != self._last_seed:
            return True
        turn_num = sig[0]
        if turn_num - self._last_reform_turn >= self._STALE_TURNS:
            return True
        # Re-plan at the start of each of our turns: T/T+1/T+2 moved on.
        if our_turn and turn_num > self._last_reform_turn:
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

    def _our_turn(self, game_state: dict[str, Any]) -> bool:
        local = game_state.get("local_seat_id") or self._local_seat(game_state)
        active = (game_state.get("turn") or {}).get("active_player")
        return local is not None and active == local

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
            identities(game_state.get("command") or []),
            tuple(
                sorted((str(gid), count) for gid, count in (game_state.get("commander_casts") or {}).items())
            ),
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
        from arenamcp.board_assessment import assess
        from arenamcp.match_context import prepare_match_context, with_deck_reference

        game_state = prepare_match_context(game_state)
        context = self._build_context(game_state)
        assessment = assess(game_state)
        user_parts = [context]
        if assessment is not None:
            logger.info(
                "Board facts (turn %d, %.1fms): %s | %s",
                turn_num,
                assessment.elapsed_ms,
                assessment.headline(),
                assessment.facts_line(),
            )
            when = "this turn" if assessment.our_turn else "our next turn"
            user_parts.append(
                "\nBOARD FACTS (deterministic; recomputed from the live board — treat as hard facts):\n"
                + assessment.planning_block()
                + f"\nT = turn {assessment.plan_turn} ({when}); T+1 = turn {assessment.plan_turn + 2}; "
                f"T+2 = turn {assessment.plan_turn + 4}. ASSESSED ROLE: {assessment.role}."
            )
        if self._seed:
            user_parts.append(f"\nDECK PLAYBOOK / STRATEGY:\n{self._seed}")
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
        try:
            validate_plan(plan, assessment, game_state)
        except Exception as error:  # a validator bug must not discard the plan
            logger.warning("game-plan validation failed (plan kept unvalidated): %s", error)
        if assessment is not None:
            plan.facts = assessment.as_payload()
        for issue in plan.issues:
            logger.info("GamePlan validation (turn %d): %s", turn_num, issue)
        logger.info(
            "GamePlan (turn %d): role=%s | win=%s | path=%s | turns=%s",
            turn_num,
            plan.role or "?",
            plan.win_conditions,
            plan.path,
            " | ".join(f"T{step['turn']}: {step_text(step)}" for step in plan.turn_plan) or "-",
        )
        return plan

    def _complete(self, system_prompt: str, user_message: str) -> str:
        """Call the backend, tolerating the small signature differences across clients.

        Background strategy: full reasoning and a larger token/time budget than
        the per-decision calls, which stay on low reasoning effort.
        """
        try:
            return self._backend.complete(
                system_prompt,
                user_message,
                self._PLAN_MAX_TOKENS,
                temperature=0.0,
                request_timeout_s=self._timeout,
                background=True,
                enable_thinking=True,
            )
        except TypeError:
            pass
        try:
            return self._backend.complete(
                system_prompt,
                user_message,
                self._PLAN_MAX_TOKENS,
                temperature=0.0,
                request_timeout_s=self._timeout,
            )
        except TypeError:
            # Local backends may not accept request_timeout_s / temperature.
            try:
                return self._backend.complete(system_prompt, user_message, self._PLAN_MAX_TOKENS)
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

        def items(key):
            value = data.get(key)
            return (
                [item.strip() for item in value if isinstance(item, str) and item.strip()][:8]
                if isinstance(value, list)
                else []
            )

        turns = data.get("turns") or data.get("turn_plan") or []
        if isinstance(turns, dict):
            turns = [
                dict(value, turn=key) if isinstance(value, dict) else {"turn": key}
                for key, value in turns.items()
            ]
        return GamePlan(
            win_conditions=wins,
            path=str(data.get("path", "") or "").strip(),
            threat=str(data.get("threat", "") or "").strip(),
            develop_next=str(data.get("develop_next", "") or "").strip(),
            turn_formed=turn_num,
            raw=blob,
            active_mechanisms=items("active_mechanisms"),
            resource_priorities=items("resource_priorities"),
            assumptions=items("assumptions"),
            role=str(data.get("role", "") or "").strip().lower(),
            role_reason=str(data.get("role_reason", "") or "").strip(),
            # Kept raw until validate_plan maps them to absolute turns.
            turn_plan=[t for t in turns if isinstance(t, dict)][:3] if isinstance(turns, list) else [],
            switch_if=items("switch_if"),
        )
