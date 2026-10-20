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

Lines (2026-10-07, multi-turn planning WP9): the plan call sees the line
search's CANDIDATE LINES, and the validated turn plan is replayed through the
search (``line_search.evaluate_plan``): each turn's mana comes from the plan's
own land drops (a land that enters tapped pays from the next turn), and a plan
whose line dies sooner than the best line has its T step replaced by the best
one while we are surviving, but only with ARENAMCP_LINE_GUARD=on: the guards
ship in shadow mode, which logs the replacement as an issue. The opponent's
instant-speed interaction risk
(``opponent_tricks``, advisory) is one more fact line, without card names.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from arenamcp.backend_health import is_backend_error_text
from arenamcp.plan_ledger import PlanLedger

logger = logging.getLogger(__name__)


# ----- LLM call discipline ----------------------------------------------------
#
# Shared by the game plan, the deck playbook (coach.py) and the win-in-N worker
# (standalone.py). Evidence, 2026-10-06 standalone.log: about half of the one
# shared vLLM server's slot time went to background calls (139 game-plan calls
# in 1.8 h, 19 deck-playbook runs for 4-5 decks, 31 win-in-N calls of which
# none was ever read), and decision calls overlapping one of them ran p50
# 3.1 s instead of 1.8 s. Background strategy therefore runs one call at a
# time, labelled and deprioritised, and never while the model server's
# circuit breaker is open.

# vLLM priority scheduling: lower runs sooner, default 0. Background strategy
# yields to decisions and to the user's other clients (Hermes) on the server.
BACKGROUND_PRIORITY = 10


def accepted_call_kwargs(backend: Any, **optional: Any) -> dict[str, Any]:
    """The ``optional`` keyword arguments that ``backend.complete`` accepts.

    ``call_class``, ``priority`` and ``cancel_event`` are newer ProxyBackend
    keywords; an older or test backend would raise TypeError, and every
    caller's TypeError fallback then drops ALL keywords (budget, effort,
    schema). ``None`` values are never passed.
    """
    wanted = {key: value for key, value in optional.items() if value is not None}
    complete = getattr(backend, "complete", None)
    if not wanted or complete is None:
        return {}
    try:
        params = inspect.signature(complete).parameters
    except (TypeError, ValueError):
        return {}
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return wanted
    return {key: value for key, value in wanted.items() if key in params}


def _backend_flag(backend: Any, name: str) -> bool | None:
    """Call a boolean backend method if its class defines one; None otherwise.

    Looked up on the class so a ``Mock`` backend (whose attributes all exist
    and return truthy mocks) never reads as "breaker open".
    """
    if backend is None or getattr(type(backend), name, None) is None:
        return None
    try:
        value = getattr(backend, name)()
    except Exception as error:
        logger.debug("backend %s() failed: %s", name, error)
        return None
    return value if isinstance(value, bool) else None


def llm_available(backend: Any) -> bool:
    """False only while the model server's circuit breaker is open."""
    flag = _backend_flag(backend, "available")
    return True if flag is None else flag


def background_llm_allowed(backend: Any) -> bool:
    """Whether background strategy may call the model now.

    Stricter than :func:`llm_available`: the breaker reports background work
    blocked after a few fresh failures, before it opens for everyone.
    """
    blocked = _backend_flag(backend, "background_blocked")
    if blocked is not None:
        return not blocked
    return llm_available(backend)


def circuit_snapshot(backend: Any) -> dict[str, Any]:
    """The backend's breaker snapshot (``{}`` when it has none)."""
    if backend is None or getattr(type(backend), "circuit_snapshot", None) is None:
        return {}
    try:
        snapshot = backend.circuit_snapshot()
    except Exception as error:
        logger.debug("circuit snapshot failed: %s", error)
        return {}
    return dict(snapshot) if isinstance(snapshot, dict) else {}


def is_unavailable_text(text: Any) -> bool:
    """True for the proxy's skip sentinel: nothing was asked, the server is down.

    "[BACKEND ERROR] model server unavailable (circuit open; retry in Ns)",
    or "(backend down; background call skipped)" for background work.
    """
    lowered = str(text).lower()
    return is_backend_error_text(text) and (
        "model server unavailable" in lowered or "circuit open" in lowered
    )


def is_skipped_call_text(text: Any) -> bool:
    """A skip or a client-side cancellation (superseded/dropped), never a server failure."""
    return is_unavailable_text(text) or (
        is_backend_error_text(text) and "request cancelled" in str(text).lower()
    )


class StrategicLane:
    """At most one background strategic model job in flight, process-wide.

    Jobs: the game plan, the deck playbook and the win-in-N worker. The coach,
    the autopilot and re-initialised coaches each build their own backend and
    manager, so this cannot live on an instance. Acquiring returns a token
    that only its owner can release; a holder older than ``MAX_HOLD_S`` is
    presumed leaked and may be replaced.
    """

    MAX_HOLD_S = 300.0

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._token: object | None = None
        self._job = ""
        self._since = 0.0

    @property
    def holder(self) -> str | None:
        with self._cond:
            return self._job if self._token is not None else None

    def _free_locked(self) -> bool:
        if self._token is None:
            return True
        if time.monotonic() - self._since > self.MAX_HOLD_S:
            logger.warning(
                "Strategic lane: %s held for over %.0fs; presuming it leaked", self._job, self.MAX_HOLD_S
            )
            return True
        return False

    def _take_locked(self, job: str) -> object:
        token = object()
        self._token, self._job, self._since = token, job, time.monotonic()
        return token

    def try_acquire(self, job: str) -> object | None:
        """Take the lane now, or return None when another job holds it."""
        with self._cond:
            return self._take_locked(job) if self._free_locked() else None

    def acquire(self, job: str, timeout: float, *, abort: Callable[[], bool] | None = None) -> object | None:
        """Wait up to ``timeout`` seconds for the lane (None on timeout or abort)."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while not self._free_locked():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or (abort is not None and abort()):
                    return None
                self._cond.wait(min(remaining, 1.0))
            return self._take_locked(job)

    def release(self, token: object | None) -> None:
        with self._cond:
            if token is not None and token is self._token:
                self._token, self._job = None, ""
                self._cond.notify_all()

    def wait_idle(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds until no job holds the lane."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._token is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, 0.25))
            return True


STRATEGIC_LANE = StrategicLane()


# Strong, compact instruction. The model returns STRICT JSON so we can render a
# stable prompt block for the planner and a one-line intro for spoken advice.
GAME_PLAN_PROMPT = """You are a Magic: The Gathering strategic planner forming a PERSISTENT GAME PLAN.
Given the current board, hand, mana, life totals, the deterministic BOARD FACTS and the Oracle-grounded deck playbook, decide WHO IS THE BEATDOWN, how this game is won, and what to do on each of our next three turns, like a strong human player looking 2-3 turns ahead.

BOARD FACTS are computed from the live board (clocks through best blocks, race, lethal flags, mana budget per turn). Treat them as hard facts:
- ROLE must equal the ASSESSED ROLE unless role_reason names a concrete board fact the assessment missed. If the facts say OPPONENT HAS LETHAL ON BOARD or DEAD IN 2, the role is defender or control/stabilize unless we have lethal first, or the assessed role is race because only attacking survives (or none of ours can block their lethal attack): then the role is race and T attacks as that line does.
- The turn plan covers T (this turn if it is ours, else our next turn), T+1 and T+2 (our following turns). Each turn's "cast" list may name ONLY cards in our hand now (or castable from our graveyard), each card at most once, and their combined mana value must fit that turn's MANA BUDGET with the colours available. A card we hope to draw goes in "hold" as "if drawn: <name>", never in "cast".
- When BOARD FACTS lists CANDIDATE LINES (a deterministic 2-turn search), build "turns" from one of them and keep its attack/hold posture unless you name a concrete card or combat reason.
- As defender/control, prioritise creatures that block and removal on attackers over card draw, mana rocks or cycling until the clock is under control. As aggressor, maximise damage; spend removal on blockers.
- Win conditions must be realistic for the CURRENT state: no empty-library/alternate wins while the library is large, and no combo whose pieces are not in hand or on the battlefield (label a needed draw explicitly).
Use the deck list and remaining library to identify realistic engines, outs and backup plans; cards in the library are possibilities, not cards in hand or guaranteed draws. Rules are quoted for the cards seen this game and for the playbook's key cards; a library card listed by name only has no quoted rules, so do not invent abilities for it. Preserve the prior plan when still sound, and adapt when its assumptions change.
Removal and tutoring are conditional decisions: compare the current threat, timing, mana and opportunity cost. Hold interaction when that protects the winning line; remove a threat when it prevents loss or unlocks progress. A tutor should find the currently useful legal card still in the library, with a feasible follow-up, rather than repeat an old preferred target.
Use the playbook's conditional decision rules, not just its archetype or finishers. Evaluate commander deployment/recovery from its actual rules and current tax. Do not treat a desirable library card as an available plan.
The MATCH LEDGER (when present) is the record of this match: each turn's planned step, what we actually did, and how life and board moved. Learn from it: keep the prior plan's win condition while the ledger shows progress toward it; change it when the ledger shows it failing (two of our turns without damage or board gain while pursuing it, a planned cast repeatedly not made, a large life loss). Carry forward the prior plan's lessons that still hold and add new ones from the ledger; a lesson is a short observation about this match, not a general principle.

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
  "assumptions": ["mana, timing, survival or availability condition to recheck"],
  "lessons": ["what has worked or failed so far THIS match, from the ledger (<=12 words each, up to 4)"]
}"""


# The assessed race reasons that attacking is what survives (``board_assessment._role``, ledger #161).
_RACE_TO_SURVIVE = re.compile(
    r"\bonly attacking survives\b|\battacking survives\b|\bnone of ours can block it\b"
)


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
    # What has worked and not worked so far this match (carried across reforms
    # and refreshed from the match ledger; see plan_ledger.py).
    lessons: list[str] = field(default_factory=list)
    # Reasoning effort the plan was formed with ("low" for a quick first plan
    # that a high-effort reform then deepens at the next quiet moment).
    effort: str = ""
    # The opposing board the plan was written from (board_basis): creatures with
    # power/toughness and threat keywords, noncreature permanents, their life.
    # Prompts flag the plan's lines once that board has changed.
    basis: dict = field(default_factory=dict)

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
        lines.extend(f"  Lesson so far: {item}" for item in self.lessons)
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
            "lessons": list(self.lessons),
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


def _our_commanders(state: dict) -> list:
    """Our commanders in the command zone, priced now (``board_model.our_commanders``); [] outside
    commander games."""
    if not state.get("command"):
        return []
    from arenamcp.board_model import our_commanders

    return our_commanders(state)


def _castable_cards(state: dict) -> list[dict]:
    """The cards a plan may cast besides flashback: our hand, then our commanders in the command
    zone (cast like hand cards)."""
    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    return hand + [c.card for c in _our_commanders(state)]


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
      the opponent has lethal / we are dead in two (unless we have lethal, or
      the assessed role is aggressor too, e.g. for a searched lethal line);
      lethal-now forces aggressor; any other disagreement needs a reason.
    * win conditions: our-library alternate wins are dropped while the library
      has more than a handful of cards; wins naming a card that is still in the
      library are dropped unless labelled as a draw. If none survive, a
      grounded win condition from the assessment replaces them.
    * turn plan: casts must be in hand (or flashback-able from our graveyard, or
      our commander in the command zone at its cost now, tax included), used
      once, and fit that turn's mana budget and colours; a library card is
      moved to "hold: if drawn: X"; the most expensive excess is trimmed. With
      the line search, each turn's budget follows the plan's own land drops.
      A creature cast that turn (without haste) is dropped from its attack.
    * line check: the turn plan replayed through the line search is compared
      with the best line; a cast's note picks its mode and target ("Archive
      Arbiter (choose: gain 4 life)", "Shock (on Cadet)"). Dying sooner (or
      winning later) is an issue; while surviving is the priority, the T step
      is replaced by the best line's only with ARENAMCP_LINE_GUARD=on (shadow,
      the default, only notes what it would replace), never against the
      +2/+0 trick proxy or for a modal choice of a card with an unmodelled
      mode. A value gap of ``PLAN_LINE_GAP`` in the same outcome is noted. A
      plan casting a card whose effect the search does not model, or noting a
      mode it does not model (:func:`unmodelled_effect`), is not judged.
    * claims: "X has <keyword>", "X can't block" and "Y is unblockable" about
      creatures on the battlefield (or in our hand) are checked against their
      keywords and blocking rules; a false one is rewritten in place
      (:func:`_check_keyword_claims`). "<burn> kills <their creature>" is
      checked against the damage that burn (with the other burn the same step
      casts) deals it and the damage that kills it; a false one is annotated
      (:func:`_check_kill_claims`).
    * mana holds: holding mana with nothing to spend it on at instant speed,
      or "banking" it, is replaced by "spend spare mana this turn"
      (:func:`_check_mana_holds`).
    """
    from arenamcp.board_assessment import ROLE_AGGRESSOR, ROLE_CONTROL, ROLE_DEFENDER, ROLE_RACE, ROLES

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
    elif getattr(assessment, "all_in", False) and role != ROLE_AGGRESSOR:
        issues.append(f"role {role} rejected: no defensive line survives their next attack (all-in)")
        role = ROLE_AGGRESSOR
    elif (
        in_danger
        and role == ROLE_AGGRESSOR
        and assessment.role != ROLE_AGGRESSOR  # e.g. a LETHAL LINE: the facts agree
        and not assessment.lethal_next_turn
        and not getattr(assessment, "all_in", False)
    ):
        issues.append(f"role aggressor rejected: {'; '.join(assessment.flags) or assessment.role_reason}")
        role = assessment.role
    elif (
        role in (ROLE_DEFENDER, ROLE_CONTROL)
        and assessment.role == ROLE_RACE
        and _RACE_TO_SURVIVE.search(assessment.role_reason or "")
    ):
        # Ledger #161 (c0af235f T12): "control/stabilize" while only the attacking line survived;
        # the declaration echoed it ("Stabilize behind the token wall") and held.
        issues.append(f"role {role} rejected: {assessment.role_reason.split(':')[0]}")
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
    _check_keyword_claims(plan, assessment, state, issues)
    _check_kill_claims(plan, assessment, state, issues)
    _check_leaver_blocks(plan, state, issues)
    _strip_unmet_modified_plan_attacks(plan, state, issues)
    _check_plan_line(plan, assessment, state, issues)
    _check_mana_holds(plan, assessment, state, issues)
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
    from arenamcp.combat_keywords import has_combat_keyword
    from arenamcp.mulligan_policy import _pip_matching, hand_card

    hand = [c for c in state.get("hand") or [] if isinstance(c, dict)]
    hand_names = _card_names(hand)
    # Our commanders in the command zone are cast like hand cards, at their cost now (tax included).
    commanders = _our_commanders(state)
    command_names = _card_names([c.card for c in commanders])
    command_costs = {id(c.card): c.cost for c in commanders}
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
    # The plan replayed through the line search: each planned turn's sources
    # from the plan's own land drops (a land that enters tapped pays from the
    # next turn), and the lands it can't play. Without it, the board math's.
    replay = _evaluate_plan_line(raw_turns, assessment, state, keep_land_turns=True)
    planned_sources = {b["label"]: list(b["source_colors"]) for b in (replay.budgets if replay else [])}
    unplayable_lands = {
        issue.split(":", 1)[0]
        for issue in (replay.issues if replay else [])
        if re.match(r"T(?:\+\d)?: land .* is not playable", issue)
    }
    creature_names = _our_creature_names(state)
    haste_for_all = _our_global_haste(state)
    # Back in hand after each of our turns (Fleeting Effigy): cast again on a later turn.
    leavers = _end_step_leavers(state)
    # Our permanents that make our spells cheaper (#170, match 639c5fe1 T6: with Pearl Medallion
    # out the plan's T7 was Hare Apparent {W} + Leonardo {1}{W}, 3 of 4 mana; the trim read the
    # printed 2 + 3 and dropped Leonardo: "needs 5 mana, budget 4"). One the plan casts counts
    # from its next turn.
    reducers = _static_reducers(
        [
            card
            for card in state.get("battlefield") or []
            if isinstance(card, dict)
            and (card.get("controller_seat_id") or card.get("owner_seat_id")) == local
        ]
    )
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
        source_colors = planned_sources.get(label, list(budget.source_colors))
        sources = [SimpleNamespace(produces=frozenset(colors)) for colors in source_colors] + list(rocks)
        casts_raw = raw.get("cast") or []
        if isinstance(casts_raw, str):
            casts_raw = [part.strip() for part in re.split(r",|\+| and ", casts_raw) if part.strip()]
        holds = [str(raw.get("hold") or "").strip()] if str(raw.get("hold") or "").strip() else []
        # (as stored: the card's name plus any note, mana value, pips, card)
        chosen: list[tuple[str, int, tuple, dict]] = []
        this_turn: set[int] = set()
        for name in casts_raw:
            # "Archive Arbiter (gain 4 life)" from CANDIDATE LINES, the line check's own
            # "X (choose: …)" / "X (on Y)" and "landcycle X" name the card in hand.
            base, note, cycle = _cast_parts(
                str(name),
                lambda n: (
                    _named_card(n, hand_names) or _named_card(n, grave_names) or _named_card(n, command_names)
                ),
            )
            card = _named_card(base, hand_names)
            cost_text = None
            if card is None and not cycle:
                card = _named_card(base, grave_names)
                cost_text = _flashback_cost(card) if card else None
            if card is None and not cycle:
                card = _named_card(base, command_names)
                cost_text = command_costs.get(id(card)) if card else None
                if card is not None and not cost_text:
                    issues.append(
                        f"{label}: cost of {card.get('name')} unknown — not checked against the mana"
                    )
            if card is None:
                if _plain(base) in library_only or _plain(base.split(",")[0]) in library_only:
                    holds.append(f"if drawn: {base}")
                    issues.append(f"{label}: {base} is not in hand (library) — kept only as 'if drawn'")
                else:
                    issues.append(f"{label}: dropped {name} (not in hand or castable from the graveyard)")
                continue
            identity = id(card)
            if identity in used or identity in this_turn:
                # Another copy of the same card (ledger #164, 2026-10-19 14:46-14:48: five
                # Hare Apparents in hand, every plan turn after the first said "no cast"):
                # the name lookup keeps only the first copy, so look for an unused twin.
                twin = next(
                    (
                        other
                        for other in list(hand or []) + list(graveyard or [])
                        if isinstance(other, dict)
                        and other is not card
                        and _plain(other.get("name")) == _plain(card.get("name"))
                        and id(other) not in used
                        and id(other) not in this_turn
                    ),
                    None,
                )
                if twin is None:
                    when = "this turn" if identity in this_turn else "earlier in the plan"
                    issues.append(f"{label}: dropped {card.get('name')} (already cast {when})")
                    continue
                card, identity = twin, id(twin)
            this_turn.add(identity)
            info = hand_card(dict(card, mana_cost=cost_text or card.get("mana_cost") or ""))
            stored = str(card.get("name")) + (f" ({note})" if note else "")
            if cycle:
                if info.landcycling is None:
                    issues.append(f"{label}: dropped {name} ({card.get('name')} has no landcycling)")
                    continue
                chosen.append((f"landcycle {card.get('name')}", info.landcycling, (), card))
                continue
            chosen.append((stored, _reduced_mana_value(card, info, reducers), info.pips, card))
        # Trim until the turn is mana-legal: when defending, card draw / rocks /
        # selection go first; otherwise (and then) the most expensive cast.
        survival = bool(getattr(assessment, "survival_mode", False))
        colors = "".join(sorted(set("".join(source_colors)) - {"C"}))
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
                card = hand_names.get(_plain(name)) or command_names.get(_plain(name))
                if card is not None and id(card) not in used:
                    cost = command_costs.get(id(card))
                    info = hand_card(card if cost is None else dict(card, mana_cost=cost))
                    fill.append(
                        (str(card.get("name")), _reduced_mana_value(card, info, reducers), info.pips, card)
                    )
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
        attack = str(raw.get("attack") or "").strip()[:80]
        ready = _ready_attacker_names(state, assessment, index)
        if attack and leavers:
            attack = _leaver_attacks(attack, chosen, sources, leavers, ready, creature_names, label, issues)
        for name, _mv, _pips, card in chosen:
            if str(card.get("name")) not in leavers:
                used.add(id(card))
            if not name.startswith("landcycle "):
                reducers = reducers + _static_reducers([card])
            text = str(card.get("oracle_text") or "").lower()
            type_line = str(card.get("type_line") or "").lower()
            if (
                not name.startswith("landcycle ")
                and "creature" not in type_line
                and re.search(r"\{o?t\}[^:]*:\s*add\b", text)
            ):
                rocks.append(SimpleNamespace(produces=frozenset("WUBRGC")))
        land = str(raw.get("land") or "").strip()
        if land and _plain(land) not in hand_names:
            if index == 0:
                issues.append(f"{label}: land {land} is not in hand")
            land = ""
        elif land and label in unplayable_lands:
            issues.append(f"{label}: land {land} can't be played then (no land drop left, or played earlier)")
            land = ""
        # A creature cast this turn can't attack this turn (unless it has haste), but
        # a copy of it already on our battlefield that can attack then still may.
        sick = [
            str(card.get("name"))
            for name, _mv, _pips, card in chosen
            if not name.startswith("landcycle ")
            and "creature" in str(card.get("type_line") or "").lower()
            and not haste_for_all
            and not has_combat_keyword(card, "haste")
            and str(card.get("name")) not in ready
        ]
        if sick and attack:
            attack = _without_sick_attackers(attack, sick, creature_names, label, issues)
        steps.append(
            {
                "turn": budget.turn,
                "label": label,
                "land": land,
                "cast": [c[0] for c in chosen],
                "attack": attack,
                "hold": "; ".join(holds)[:120],
                "mana": len(sources),
            }
        )
    return steps


# --- the plan's line (line_search) -------------------------------------------------

# Same outcome and timing as the best line, but this much lower in value: noted.
PLAN_LINE_GAP = 5.0

_NO_ATTACK = re.compile(r"^(?:none|no attacks?|nobody|no one|nothing|hold|don t attack|do not attack)\b")
_ALL_ATTACK = re.compile(r"\b(?:all|everything|everyone|all in)\b")
_NAME_STOPWORDS = frozenset({"the", "of", "and", "with", "from", "into", "attack", "none", "hold", "token"})
# A cast name's trailing note: the line check writes "Archive Arbiter (choose: gain 4 life)"
# and "X (on Y)", CANDIDATE LINES "Archive Arbiter (gain 4 life)".
_ANY_NOTE = re.compile(r"\s*\(([^()]*)\)\s*$")


def unmodelled_effect(card: dict | None, result: Any = None) -> str:
    """What casting ``card`` does that the line search does not model; '' when it models it all.

    The one check the search, the guards and the board facts share
    (``line_search_moves.unmodelled_effect``): tokens it can't read, a
    creature's other triggered ability, modes that are all unmodelled, a pump,
    an aura or planeswalker, and (with ``result``, the board's
    ``LineSearchResult``) player damage it has no face variant for. A line
    starting with such a cast undervalues it, so the plan's line check does
    not judge a plan that casts it. '' on any error (treated as modelled).
    """
    try:
        from arenamcp.line_search_moves import unmodelled_effect as canonical

        return canonical(card, result)
    except Exception:
        logger.debug("unmodelled-effect check failed", exc_info=True)
        return ""


def _local_seat(state: dict) -> Any:
    return state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )


# 2026-10-19 match 10a78106: Fleeting Effigy ("At the beginning of your end step,
# return this creature to its owner's hand") was planned as a permanent. The
# validator dropped its recasts six times as "already cast earlier in the plan",
# the T3 step attacked with it without recasting it (3 mana of casts on 2
# lands), and the T9-T13 holds kept {2}{R} "to pump-block Bristly Bill in their
# combat" while it was back in our hand; the 10:32:09 and 10:33:28 decisions
# repeated the premise. Such a creature is a hand card every turn: it may be
# recast, it attacks only on a turn it is cast, and it never blocks.
_LEAVES_AT_END = re.compile(
    r"at the beginning of (?:your|the|the next) end step, (?:return|sacrifice|exile) (?:this|it|~)"
)


def _leaves_at_end(card: dict | None) -> bool:
    """The permanent returns to hand (or is sacrificed/exiled) at the beginning of our end step."""
    if not isinstance(card, dict):
        return False
    from arenamcp.combat_strategy import _rules_text

    return bool(_LEAVES_AT_END.search(_rules_text(card)))


def _end_step_leavers(state: dict) -> dict[str, dict]:
    """Our creature cards (battlefield, hand) that leave at our end step, by name."""
    local = _local_seat(state)
    found: dict[str, dict] = {}
    for zone in ("hand", "battlefield"):
        for card in state.get(zone) or []:
            if not isinstance(card, dict) or not card.get("name") or not _is_creature_card(card):
                continue
            if (
                zone == "battlefield"
                and (card.get("controller_seat_id") or card.get("owner_seat_id")) != local
            ):
                continue
            if _leaves_at_end(card):
                found.setdefault(str(card["name"]), card)
    return found


def _is_creature_card(card: dict) -> bool:
    return "creature" in f"{card.get('type_line') or ''} {card.get('card_types') or ''}".lower()


def _our_creature_names(state: dict) -> list[str]:
    """Our creatures on the battlefield and the creature cards in our hand and command zone, by name."""
    local = _local_seat(state)
    names: list[str] = []
    zones = {"battlefield": state.get("battlefield"), "hand": state.get("hand")}
    if state.get("command"):
        zones["command"] = [c.card for c in _our_commanders(state)]
    for zone, cards in zones.items():
        for card in cards or []:
            if not isinstance(card, dict) or not card.get("name") or not _is_creature_card(card):
                continue
            if (
                zone == "battlefield"
                and (card.get("controller_seat_id") or card.get("owner_seat_id")) != local
            ):
                continue
            if card["name"] not in names:
                names.append(str(card["name"]))
    return names


def _our_global_haste(state: dict) -> bool:
    """A permanent of ours gives our creatures haste."""
    from arenamcp.board_assessment import _side_rules

    try:
        battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
        return bool(_side_rules(battlefield, _local_seat(state)).get("haste"))
    except Exception:
        return False


# A permanent that makes some of its controller's spells cheaper, as one whole sentence:
# "White spells you cast cost {1} less to cast." (Pearl Medallion), "Instant and sorcery
# spells you cast cost {1} less to cast.", "Spells you cast cost {1} less to cast." A
# condition or count after it ("for each", "if", "during") is not read: no reduction.
_STATIC_REDUCER = re.compile(r"(?P<what>[a-z ,]*?)\s*spells you cast cost \{o?(?P<n>\d+)\} less to cast\.?")
_COLOR_WORDS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}


def _static_reducers(cards: list[dict]) -> list[tuple[list[list[str]], int]]:
    """(qualifier groups, generic mana off) for each static cost reduction on ``cards`` (#170).

    "instant and sorcery" is two groups (either), "white creature" one (both);
    no qualifier at all reduces every spell.
    """
    found: list[tuple[list[list[str]], int]] = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        text = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).lower()
        for sentence in re.split(r"\n|(?<=\.)\s+", text):
            match = _STATIC_REDUCER.fullmatch(sentence.strip())
            if not match:
                continue
            groups: list[list[str]] = [[]]
            for word in match.group("what").replace(",", " ").split():
                if word in ("and", "or"):
                    groups.append([])
                else:
                    groups[-1].append(word)
            found.append(([group for group in groups if group], int(match.group("n"))))
    return found


def _reduced_mana_value(card: dict, info: Any, reducers: list[tuple[list[list[str]], int]]) -> int:
    """The spell's mana value less our static reductions, which pay generic mana only (#170)."""
    from arenamcp.cost_reduction import reduced_mana_value

    return reduced_mana_value(info.mana_value, len(info.pips), _reduction_for(card, info.pips, reducers))


def _spell_colors(card: dict, pips: tuple) -> set[str]:
    colors = set()
    for color in card.get("colors") or card.get("color") or []:
        word = str(color).lower().replace("cardcolor_", "")
        colors.add(_COLOR_WORDS.get(word, word.upper()[:1]))
    if not colors:
        colors = {color for pip in pips for color in pip} - {"C"}
    return colors & set(_COLOR_WORDS.values())


def _reduction_for(card: dict, pips: tuple, reducers: list[tuple[list[list[str]], int]]) -> int:
    """The generic mana our static reducers take off this spell (#170)."""
    if not reducers:
        return 0
    types = f"{card.get('type_line') or ''} {' '.join(str(t) for t in card.get('card_types') or [])}".lower()
    colors = _spell_colors(card, pips)

    def fits(word: str) -> bool:
        if word in _COLOR_WORDS:
            return _COLOR_WORDS[word] in colors
        if word == "colorless":
            return not colors
        if word == "multicolored":
            return len(colors) > 1
        if word == "monocolored":
            return len(colors) == 1
        if word.startswith("non") and len(word) > 3:
            return not fits(word[3:])
        return bool(re.search(rf"\b{re.escape(word)}s?\b", types))

    return sum(
        amount for groups, amount in reducers if not groups or any(all(fits(w) for w in g) for g in groups)
    )


def _named_creatures(text: str, candidates: list[str]) -> list[str]:
    """The candidates ``text`` names: in full, by the part before a comma, or by a word no other has.

    "attack Arbiter (opp tapped)" names Archive Arbiter (standalone.log
    2026-10-06 15:44:49).
    """
    words = Counter(word for name in candidates for word in set(_plain(name).split()))
    found = []
    for name in candidates:
        full, short = _plain(name), _plain(str(name).split(",")[0])
        keys = {full} | ({short} if len(short) >= 4 else set())
        keys |= {w for w in full.split() if len(w) >= 4 and words[w] == 1 and w not in _NAME_STOPWORDS}
        if any(_mentions(text, key) for key in keys):
            found.append(name)
    return found


def _without_sick_attackers(
    attack: str, sick: list[str], creatures: list[str], label: str, issues: list[str]
) -> str:
    """``attack`` without a creature cast that turn: the other creatures it names, else ''."""
    plain = _plain(attack)
    if not plain or _NO_ATTACK.match(plain) or _ALL_ATTACK.search(plain):
        return attack  # "everything": a creature that can't attack yet simply doesn't
    named = _named_creatures(attack, list(dict.fromkeys([*creatures, *sick])))
    cast_now = [name for name in named if name in sick]
    if not cast_now:
        return attack
    for name in cast_now:
        issues.append(f"{label}: {name} can't attack the turn it is cast")
    return ", ".join(name for name in named if name not in sick)


def _leaver_attacks(
    attack: str,
    chosen: list[tuple[str, int, tuple, dict]],
    sources: list,
    leavers: dict[str, dict],
    ready: set[str],
    creatures: list[str],
    label: str,
    issues: list[str],
) -> str:
    """``attack`` once each end-step leaver it names is cast this turn: added to ``chosen``
    when the turn's mana pays for it (and it has haste), else taken out of the attack.

    10a78106 T3: "cast Boltwave + Hired Claw; attack: Fleeting Effigy" on 2
    lands. Effigy had gone back to hand at our previous end step, so the
    attack needed a third mana. A leaver already on the battlefield now and
    ready (cast earlier this turn) attacks as it is.
    """
    from arenamcp.combat_keywords import has_combat_keyword
    from arenamcp.mulligan_policy import _pip_matching, hand_card

    plain = _plain(attack)
    if not plain or _NO_ATTACK.match(plain) or _ALL_ATTACK.search(plain):
        return attack
    named = _named_creatures(attack, creatures)
    cast_here = {_plain(c[3].get("name")) for c in chosen if not c[0].startswith("landcycle ")}
    out = []
    for name in named:
        card = leavers.get(name)
        if card is None or _plain(name) in cast_here or name in ready:
            continue
        info = hand_card(card)
        spent = sum(c[1] for c in chosen)
        fits = spent + info.mana_value <= len(sources) and _pip_matching(
            tuple(p for c in chosen for p in c[2]) + info.pips, sources
        )
        if fits and has_combat_keyword(card, "haste"):
            chosen.append((str(card["name"]), info.mana_value, info.pips, card))
            issues.append(f"{label}: casts {name} to attack with it (it returns to our hand at our end step)")
            continue
        out.append(name)
        why = (
            f"recasting it needs {info.mana_value} more mana ({spent} of {len(sources)} already planned)"
            if not fits
            else "it can't attack the turn it is recast"
        )
        issues.append(f"{label}: {name} can't attack: it returns to our hand at our end step and {why}")
    if not out:
        return attack
    return ", ".join(name for name in named if name not in out)


def _strip_unmet_modified_plan_attacks(plan: GamePlan, state: dict, issues: list[str]) -> None:
    """Strip planned attacks with unmodified creatures whose evasion condition is unmet and fatal."""
    from arenamcp.combat_keywords import unmet_modified_flag
    from arenamcp.combat_strategy import losing_attackers

    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    local = _local_seat(state)
    unmet_creatures: dict[str, tuple[int, str]] = {}
    for card in battlefield:
        if (card.get("controller_seat_id") or card.get("owner_seat_id")) != local:
            continue
        if not _is_creature_card(card) or not card.get("name") or card.get("instance_id") is None:
            continue
        flag = unmet_modified_flag(card, battlefield=battlefield)
        if flag:
            unmet_creatures[str(card["name"])] = (int(card["instance_id"]), flag)
    if not unmet_creatures:
        return

    plan_text = " ".join([*(plan.win_conditions or []), plan.path or "", plan.role_reason or ""]).strip()
    losing_by_name: dict[str, str] = {}
    for name, (inst_id, _flag) in unmet_creatures.items():
        losing = losing_attackers(state, [inst_id], strategy_text=plan_text)
        if inst_id in losing:
            losing_by_name[name] = losing[inst_id]
    if not losing_by_name:
        return

    creatures = _our_creature_names(state)
    for step in plan.turn_plan:
        if step.get("cast"):
            break
        attack = str(step.get("attack") or "").strip()
        plain = _plain(attack)
        if not plain or _NO_ATTACK.match(plain):
            continue
        named = list(creatures) if _ALL_ATTACK.search(plain) else _named_creatures(attack, creatures)
        dropped = [name for name in named if name in losing_by_name]
        if not dropped:
            continue
        kept = [name for name in named if name not in losing_by_name]
        step["attack"] = ", ".join(kept)
        for name in dropped:
            issues.append(f"{step.get('label')}: held back {name} ({losing_by_name[name]})")


# --- keyword and blocking claims ---------------------------------------------------
#
# 2026-10-19 match a74bbc90: the T8 plan said "attack: Slickshot Show-Off
# (flying; their only blocker Moseo has shadow and cannot block", the T10 one
# "Slickshot Show-Off (flying, unblockable - only flyer Moseo has shadow)".
# Moseo, Vein's New Dean has flying and no shadow, and blocks Slickshot. Nothing
# checked the claim: every decision prompt carried it and the next reform
# repeated it. A claim about a creature on the battlefield (or in our hand) is
# now checked against its keywords and blocking rules and, when false, rewritten
# in place: "Moseo has no shadow [checked] and CAN block [checked]".

_CLAIM_KEYWORDS = (
    "flying", "reach", "menace", "trample", "deathtouch", "lifelink", "first strike", "double strike",
    "vigilance", "haste", "hexproof", "indestructible", "defender", "shadow", "fear", "intimidate",
    "skulk", "horsemanship",
)  # fmt: skip
_CLAIM_KW = "|".join(re.escape(k) for k in sorted(_CLAIM_KEYWORDS, key=len, reverse=True))
# "Moseo has shadow", "Moseo (2/1) still has no flying": the verb right after the name.
_HAS_CLAIM = re.compile(
    r"^\s*(?:\([^()]*\)\s*)?,?\s*(?:still |now |also |already |only )?(?:has|have|gains?|gained)\s+"
    rf"(?P<neg>no\s+)?(?P<kw>{_CLAIM_KW})\b"
)
# "Slickshot Show-Off (flying, unblockable": a keyword list right after the name.
_PAREN_LIST = re.compile(r"^\s*\((?P<items>[^;()|]*)")
_CANT_BLOCK_CLAIM = re.compile(r"\b(?:can't|cannot|can not|won't|unable to)\s+block\b")
_UNBLOCKABLE_CLAIM = re.compile(r"\b(?:unblockable|can't be blocked|cannot be blocked|can not be blocked)\b")
# A blocking claim under a condition ("once tapped", "if we kill it") or credited to an
# effect ("unblockable under Tetsuko", "use Passage to make X unblockable") is not a
# board fact this check can see. (Summoning sickness is not a condition: a creature that
# just arrived still blocks.) In 2,611 logged plans, every other "unblockable" was such a
# grant; only the flying claims are checked.
_CLAIM_CONDITION = re.compile(
    r"\b(?:if|unless|once|after|when|whenever|until|while|tapped|taps?|dies|dead|killed|kills?|removed?"
    r"|bounced?|exiled?|verified|probably|likely|maybe|might|this turn|via|under|with|using|through"
    r"|thanks|because|from|by|pumped?|equipped|enchanted)\b"
)
_CLAIM_CAUSE = re.compile(
    r"\b(?:make|makes|making|made|render|renders|let|lets|give|gives|turn|turns)\b[^;.|]*$"
)
# "Moseo attacks and we cannot block": the claim is about someone else ("it" is Moseo).
_OTHER_SUBJECT = re.compile(r"\b(?:we|you|i|our|they|their|he|she|us|them|which|that|who)\b")
_CLAIM_WINDOW = 60
_CLAIM_NAME_STOPWORDS = _NAME_STOPWORDS | {"show", "off", "new", "old", "big", "little", "lord", "lady"}


def _claim_keys(cards: list[dict]) -> list[tuple[str, str]]:
    """(lower-case text key, creature name): full names, the part before a comma, unique words."""
    names = list(dict.fromkeys(str(c.get("name")) for c in cards if c.get("name")))
    words = Counter(word for name in names for word in set(_plain(name).split()))
    keys: list[tuple[str, str]] = []
    for name in names:
        lowered = name.lower().replace("’", "'")
        keys.append((lowered, name))
        short = lowered.split(",")[0].strip()
        if len(short) >= 4 and short != lowered:
            keys.append((short, name))
        for word in _plain(name).split():
            if len(word) >= 4 and words[word] == 1 and word not in _CLAIM_NAME_STOPWORDS:
                keys.append((word, name))
    return keys


def _name_spans(text: str, keys: list[tuple[str, str]]) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, creature name) occurrences in ``text``, longest key first."""
    found: list[tuple[int, int, str]] = []
    for key, name in sorted(keys, key=lambda item: -len(item[0])):
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
            if not any(match.start() < end and start < match.end() for start, end, _ in found):
                found.append((match.start(), match.end(), name))
    return sorted(found)


def _keyword_truth(cards: list[dict], keyword: str, battlefield: list[dict]) -> bool | None:
    """Whether a creature with this name has ``keyword`` (None: its rules are unknown, or a
    permanent on the battlefield may grant it)."""
    from arenamcp.combat_keywords import _KEYWORDS, has_combat_keyword
    from arenamcp.combat_strategy import _rules_text

    known = False
    for card in cards:
        listed = [str(k).lower() for k in card.get("keywords") or ()]
        known = known or bool(card.get("oracle_text") or listed)
        if keyword in _KEYWORDS and has_combat_keyword(card, keyword):
            return True
        if keyword in listed or re.search(rf"(?m)^\s*{re.escape(keyword)}\b", _rules_text(card)):
            return True
        if keyword in str(card.get("granted_abilities") or "").lower():
            return True
    if not known:
        return None
    grant = re.compile(rf"\b(?:have|has|gains?)\b[^.\n]*\b{re.escape(keyword)}\b")
    if any(grant.search(_rules_text(source)) for source in battlefield):
        return None  # "creatures you control have flying", an Aura: the snapshot may not show it
    return False


def _block_restricted(blocker: dict, battlefield: list[dict]) -> bool | None:
    """True when ``blocker`` can't block (its own text or an attached Aura), None when another
    permanent may stop it ("creatures can't block this turn"), else False."""
    from arenamcp.combat_strategy import _CANT_BLOCK, _rules_text

    if _CANT_BLOCK.search(_rules_text(blocker)):
        return True
    identity = blocker.get("instance_id")
    for source in battlefield:
        if source is blocker:
            continue
        text = _rules_text(source)
        attached = source.get("attached_to_id") or source.get("parent_instance_id")
        if identity is not None and attached == identity and _CANT_BLOCK.search(text):
            return True
        if re.search(r"\b(?:creatures|(?:target|each|that) creature)\b[^.\n]*\bcan't block\b", text):
            return None  # not "this creature can't block": another creature's own text
    return False


def _claim_corrections(
    text: str,
    *,
    ours: list[dict],
    theirs: list[dict],
    hand: list[dict],
    battlefield: list[dict],
    our_turn: bool,
) -> tuple[str, list[str]]:
    """``text`` with its false creature claims rewritten, and what was corrected."""
    from arenamcp.combat_keywords import annotate_cant_be_blocked, has_combat_keyword
    from arenamcp.combat_solver import _can_block

    lowered = text.lower().replace("’", "'")
    creatures = [*ours, *theirs, *hand]
    spans = _name_spans(lowered, _claim_keys(creatures)) if len(lowered) == len(text) else []
    if not spans:
        return text, []
    by_name: dict[str, list[dict]] = {}
    for card in creatures:
        by_name.setdefault(str(card.get("name")), []).append(card)
    our_names = {str(c.get("name")) for c in [*ours, *hand]}
    their_names = {str(c.get("name")) for c in theirs}
    marked = [dict(card) for card in battlefield]
    annotate_cant_be_blocked(marked)
    unblockable = {str(c.get("name")) for c in marked if c.get("cant_be_blocked")}
    attackers = [name for _start, _end, name in spans if name in our_names]
    edits: list[tuple[int, int, str]] = []
    notes: list[str] = []
    for index, (start, end, name) in enumerate(spans):
        stop = spans[index + 1][0] if index + 1 < len(spans) else len(lowered)
        window = lowered[end : min(stop, end + _CLAIM_WINDOW)]
        window = re.split(r"[;.|\n]", window)[0]
        caused = bool(_CLAIM_CAUSE.search(lowered[max(0, start - 30) : start]))
        cards = by_name.get(name, [])
        # keywords: "has (no) X", or the first items of a "(flying, haste" list
        claims: list[tuple[int, int, str, bool]] = []
        has = _HAS_CLAIM.search(window)
        if has:
            claims.append((has.start("kw") - (len(has.group("neg")) if has.group("neg") else 0),
                           has.end("kw"), has.group("kw"), not has.group("neg")))  # fmt: skip
        listing = _PAREN_LIST.match(window)
        if listing:
            offset = listing.start("items")
            for item in re.finditer(r"[^,]+", listing.group("items")):
                word = item.group(0).strip()
                if word not in _CLAIM_KEYWORDS:
                    break
                begin = offset + item.start() + item.group(0).index(word)
                claims.append((begin, begin + len(word), word, True))
        for begin, finish, keyword, positive in claims:
            truth = _keyword_truth(cards, keyword, battlefield)
            if truth is None or truth == positive:
                continue
            fact = f"has no {keyword}" if positive else f"HAS {keyword}"
            have = [kw for kw in _CLAIM_KEYWORDS if _keyword_truth(cards, kw, []) and kw != keyword]
            notes.append(f"{name} {fact}" + (f" (it has {', '.join(have)})" if have else ""))
            edits.append((end + begin, end + finish, f"{'no ' if positive else ''}{keyword} [checked]"))
        conditional = caused or bool(_CLAIM_CONDITION.search(window))
        cant = _CANT_BLOCK_CLAIM.search(window)
        if cant and _OTHER_SUBJECT.search(window[: cant.start()]):
            cant = None
        if cant and not conditional and name in their_names:
            blockers = [c for c in theirs if str(c.get("name")) == name]
            restricted = [_block_restricted(c, battlefield) for c in blockers]
            hitters = [c for c in [*ours, *hand] if str(c.get("name")) in attackers and _is_creature_card(c)]
            if (
                blockers
                and hitters
                and not any(c.get("is_tapped") for c in blockers)
                and all(flag is False for flag in restricted)
                and not any(has_combat_keyword(c, "menace") for c in hitters)
            ):
                blocked = sorted({str(a.get("name")) for a in hitters for b in blockers if _can_block(a, b)})
                if blocked:
                    notes.append(f"{name} can block {', '.join(blocked)}")
                    edits.append((end + cant.start(), end + cant.end(), "CAN block [checked]"))
        free = _UNBLOCKABLE_CLAIM.search(window)
        if free and _OTHER_SUBJECT.search(window[: free.start()]):
            free = None
        if free and not conditional and name in our_names and name not in unblockable:
            attacker = next((c for c in [*ours, *hand] if str(c.get("name")) == name), None)
            eligible = [
                b
                for b in theirs
                if not (our_turn and b.get("is_tapped")) and _block_restricted(b, battlefield) is False
            ]
            known = all(_block_restricted(b, battlefield) is not None for b in theirs)
            flying = attacker is not None and has_combat_keyword(attacker, "flying")
            can = [b for b in eligible if flying and _can_block(attacker, b)]
            needed = 2 if attacker is not None and has_combat_keyword(attacker, "menace") else 1
            if known and flying and len(can) >= needed:
                names = ", ".join(dict.fromkeys(str(b.get("name")) for b in can))
                notes.append(f"{name} can be blocked by {names}")
                edits.append((end + free.start(), end + free.end(), f"can be blocked by {names} [checked]"))
    for begin, finish, replacement in sorted(edits, reverse=True):
        text = text[:begin] + replacement + text[finish:]
    return text, notes


def _check_keyword_claims(plan: GamePlan, assessment: Any, state: dict, issues: list[str]) -> None:
    """Rewrite false keyword / blocking claims in the plan's text (see the comment above)."""
    try:
        local = _local_seat(state)
        battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
        on_board = [c for c in battlefield if _is_creature_card(c) and c.get("name")]
        ours = [c for c in on_board if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local]
        theirs = [c for c in on_board if (c.get("controller_seat_id") or c.get("owner_seat_id")) != local]
        hand = [c for c in _castable_cards(state) if _is_creature_card(c) and c.get("name")]
        context = {
            "ours": ours,
            "theirs": theirs,
            "hand": hand,
            "battlefield": battlefield,
            "our_turn": bool(getattr(assessment, "our_turn", False)),
        }

        def fixed(value: str, where: str) -> str:
            if not value:
                return value
            text, notes = _claim_corrections(value, **context)
            issues.extend(f"{where}: plan claim corrected: {note}" for note in notes)
            return text

        for attr in ("path", "threat", "develop_next", "role_reason"):
            setattr(plan, attr, fixed(getattr(plan, attr), attr))
        for attr in (
            "win_conditions",
            "switch_if",
            "assumptions",
            "resource_priorities",
            "active_mechanisms",
        ):
            setattr(plan, attr, [fixed(item, attr) for item in getattr(plan, attr)])
        for step in plan.turn_plan:
            for key in ("attack", "hold"):
                step[key] = fixed(str(step.get(key) or ""), str(step.get("label") or "T"))
    except Exception as error:  # a claim check must never cost the plan
        logger.debug("plan claim check failed: %s", error, exc_info=True)


# --- kill claims -------------------------------------------------------------------
#
# 2026-10-19 match 115944ce (11:14:41): the T8 plan's path said "T8 Iroh's kills
# Trostani" — Iroh's Demonstration deals 4 to one creature and Trostani, Three
# Whispers was a 7/7. Every decision prompt carried the claim; Iroh's was cast at
# Trostani again and again, and at 11:15:07 the model read "dies_to_damage 7"
# and still followed the plan ("the plan confirms 4 damage removes the only
# clock"). A claim that our named burn kills a named opposing creature is now
# checked against the damage that burn can deal it — together with the other
# burn the same plan step casts, so a combined kill (Shock + Iroh's) stands —
# and the damage that kills it now; a false one is annotated in place.

_KILL_VERB = re.compile(r"\bkill(?:s|ing)?\b")
# Combat or earlier damage may add to the burn: such a claim is not judged.
_KILL_UNJUDGED = re.compile(
    r"\b(?:attack\w*|block\w*|combat|trades?|trading|fights?|deathtouch|first strike|double strike|swing\w*"
    r"|marked|damaged|wounded|already|prior|again|second|third|twice|ping\w*|if drawn|drawn)\b"
)
_KILL_NEGATED = re.compile(r"(?:\bnot|n't|\bnever|\bno longer|\bfails? to|\bunable to)\s+(?:\w+\s+){0,2}$")
# The object of the kill verb: the creature named next ("kills the tapped Trostani").
_KILL_OBJECT_GAP = re.compile(
    r"^\s*(?:off\s+)?(?:(?:the|their|its|a|an|tapped|untapped|big|anthemed|only)\s+)*$"
)
_KICKED_DAMAGE = re.compile(r"\b(?:this spell|it) was kicked\b[^.\n]*\bdeals (\d+) damage\b")
# Name words that are ordinary plan words ("kills three 1/1s" is not Trostani, Three Whispers).
_KILL_KEY_STOPWORDS = frozenset(
    {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "them", "they", "their"}
)
_TURN_LABEL = re.compile(r"\bT(?:\+(?P<plus>\d)|(?P<abs>\d+))?\b")


def _claim_burn(card: dict, body: dict) -> int | None:
    """Damage (or toughness loss) ``card``'s cast text can deal ``body``; None when not plain burn.

    Kicked damage counts (the larger reach never calls a kill false); ward is
    ignored (it can be paid). X damage, destroy, fight and bounce are not
    judged.
    """
    from arenamcp.board_assessment import _cast_text, burn_damage, removal_reach

    type_line = str(card.get("type_line") or "").lower()
    if "instant" not in type_line and "sorcery" not in type_line:
        return None
    reach = removal_reach(card)
    text = _cast_text(card)
    if reach is None or reach[0] not in ("damage", "shrink") or re.search(r"\bx damage\b|[-−–]x/", text):
        return None
    if reach[0] == "shrink":
        return int(reach[1] or 0)
    damage = burn_damage(card, body, ward_mana=99)
    kicked = _KICKED_DAMAGE.search(text)
    if damage and kicked:
        damage = max(damage, int(kicked.group(1)))
    return damage


def _kill_corrections(
    text: str,
    *,
    spells: list[dict],
    ours: list[dict],
    theirs: list[dict],
    step_casts: Callable[[str], list[str]],
) -> tuple[str, list[str]]:
    """``text`` with its false "<burn> kills <their creature>" claims annotated, and what was found."""
    from arenamcp.play_safety import lethal_damage

    lowered = text.lower().replace("’", "'")
    if len(lowered) != len(text) or not _KILL_VERB.search(lowered):
        return text, []
    cards = [*spells, *ours, *theirs]
    keys = [(key, name) for key, name in _claim_keys(cards) if key not in _KILL_KEY_STOPWORDS]
    spans = _name_spans(lowered, keys)
    if not spans:
        return text, []
    spell_names = {str(c.get("name")) for c in spells}
    our_names = {str(c.get("name")) for c in ours} - spell_names
    their_names = {str(c.get("name")) for c in theirs} - spell_names - our_names
    copies = Counter(str(c.get("name")) for c in spells)
    by_name = {str(c.get("name")): c for c in spells}
    edits: list[tuple[int, str]] = []
    notes: list[str] = []
    bounds = [0, *[m.end() for m in re.finditer(r"[;.|\n]", lowered)], len(lowered) + 1]
    for begin, finish in zip(bounds, bounds[1:], strict=False):
        clause = lowered[begin:finish]
        named = [(s - begin, e - begin, n) for s, e, n in spans if begin <= s and e <= finish]
        if _KILL_UNJUDGED.search(clause) or any(n in our_names for _s, _e, n in named):
            continue
        for verb in _KILL_VERB.finditer(clause):
            if _KILL_NEGATED.search(clause[: verb.start()]):
                continue
            target = next(
                (
                    (s, e, n)
                    for s, e, n in named
                    if s >= verb.end() and _KILL_OBJECT_GAP.match(clause[verb.end() : s])
                ),
                None,
            )
            used = Counter(n for _s, _e, n in named if n in spell_names)
            if target is None or target[2] not in their_names or not used:
                continue
            # Copies of one name: the easiest to kill decides.
            known = [
                (dies, body)
                for body in theirs
                if str(body.get("name")) == target[2] and (dies := lethal_damage(body)) is not None
            ]
            if not known:
                continue
            dies_to, body = min(known, key=lambda item: item[0])
            toughness = body.get("modified_toughness")
            toughness = toughness if type(toughness) is int else body.get("toughness")
            # Other burn the plan step casts counts toward a combined kill.
            # The turn named last before the verb ("T7 Shock face -> T8 Iroh's kills ...").
            labels = list(_TURN_LABEL.finditer(text[begin : begin + verb.start()]))
            label = labels[-1].group(0) if labels else ""
            cast = Counter(name for name in step_casts(label) if name in spell_names)
            for name, count in cast.items():
                used[name] = max(used[name], count)
            pieces = {}
            for name, count in used.items():
                damage = _claim_burn(by_name[name], {**body, "toughness": toughness})
                if damage is None:
                    pieces = None
                    break
                pieces[name] = damage * min(count, copies[name])
            if pieces is None or sum(pieces.values()) >= dies_to:
                continue
            total = sum(pieces.values())
            if len(pieces) == 1:
                burn = f"{next(iter(pieces))} deals {total}"
            else:
                burn = " + ".join(f"{name} {damage}" for name, damage in pieces.items()) + f" deal {total}"
            fact = f"{burn}; {target[2]} dies to {dies_to}"
            notes.append(f"no kill: {fact}")
            edits.append((begin + target[1], f" [checked: NOT a kill — {fact}]"))
            break
    for at, insert in sorted(edits, reverse=True):
        text = text[:at] + insert + text[at:]
    return text, notes


# --- claims in a decision's reasoning (ledger #84) -------------------------------------
#
# 2026-10-19: the per-decision reasoning states abilities the cards don't have.
# 11:23:15 / 11:23:50 (match b2a6f83a): "Casting Giant Cindermaw gives haste trample
# pressure", "two hasty trampling attackers" (Cindermaw has no haste). 11:40:47 (match
# a358cce5 T9): "Casting Fleeting Effigy ... grows Slickshot via the noncreature-spell
# trigger" (Effigy is a creature spell). a358cce5 T11: a Hired Claw ping expected from a
# summoning-sick Claw. The plan's keyword check (#23) now also reads decision
# reasoning, with three more claims: "hasty X" / "X gives haste", a noncreature-spell
# or prowess trigger from casting a creature, and a summoning-sick creature attacking,
# pinging or tapping this turn.

_HASTE_CLAIM = re.compile(r"\bhast(?:e|y)\b")
_NONCREATURE_TRIGGER = re.compile(r"\bnon-?creature[- ]spell\b|\bprowess\b|\bspell trigger\b")
# The cast doing it (ledger #108): "grows Slickshot via the noncreature-spell trigger" is a
# claim; "the hasty attacker with Prowess/Valiant upside" or "a hasty prowess engine" names
# the card's own ability. A verb describing every later spell ("grows with each burn spell")
# is that ability too.
_CAST_TRIGGER_VERB = re.compile(r"\b(?:trigger(?:s|ed|ing)?|grow(?:s|ing|n)?|pump(?:s|ed|ing)?)\b")
_EVERY_SPELL = re.compile(r"\b(?:each|every|whenever|later|future)\b")
_ATTACK_CLAIM = re.compile(
    r"\b(?:attack(?:s|ing)?|swing(?:s|ing)?|ping(?:s|ing)?|crew(?:s|ing)?|tap(?:s|ping)? (?:it|~)"
    r"|attack trigger)\b"
)
# Not this turn, or not a claim: "next turn", "later", "can't attack", "T13".
_NOT_NOW = re.compile(
    r"\b(?:next|later|future|following|eventually|can't|cannot|won't|not|no|until|after this turn)\b"
    r"|\bt\d+\b|\bturn \d+\b"
)
_CLAUSE_END = re.compile(r"[;.|\n]|\bthen\b|\bwhile\b|\band then\b")
# The card's own ability described, not a this-turn claim (ledger #117, 2026-10-19 12:52:10:
# "the free-value Hired Claw — its attack ping chips for 1" on a Claw cast that turn; also
# "develops a recurring ping attacker", "starts its ping/pump engine"). The window after a
# card's name starts right after it, so "Hired Claw's ping" leads with "'s".
_POSSESSIVE_LEAD = re.compile(r"(?:\bits|^'s)\s+(?:[a-z/+-]+\s+)?$")
_GENERIC_LEAD = re.compile(r"\b(?:recurring|repeatable|repeated|ongoing|future|steady)\s+(?:[a-z/+-]+\s+)?$")
_GENERIC_TAIL = re.compile(r"[a-z/+-]*\s*(?:engine|attacker|threat|source)\b")
_THIS_TURN = re.compile(r"\bthis (?:turn|combat)\b|\bimmediately\b|\bright (?:now|away)\b")


def _describes_card(window: str, claim: re.Match) -> bool:
    """The claim word only describes the card ("its attack ping", "a recurring ping attacker")."""
    lead, tail = window[: claim.start()], window[claim.end() :]
    return bool(_POSSESSIVE_LEAD.search(lead) or _GENERIC_LEAD.search(lead) or _GENERIC_TAIL.match(tail))


def _summoning_sick(card: dict, turn: int, global_haste: bool) -> bool:
    from arenamcp.combat_keywords import has_combat_keyword

    entered = card.get("turn_entered_battlefield")
    return (
        _is_creature_card(card)
        and isinstance(entered, int)
        and entered >= turn > 0
        and not global_haste
        and not has_combat_keyword(card, "haste")
    )


def decision_claim_notes(text: str, state: dict | None, *, cast_ids: Any = ()) -> list[str]:
    """False claims about our and their cards in a decision's reasoning, as short facts.

    ``cast_ids``: instance ids of the cards the chosen option casts (a creature
    cast now is summoning sick, and its cast triggers no noncreature-spell
    ability). Each fact is a board fact, e.g. "Giant Cindermaw has no haste".
    [] when nothing is contradicted or the board is unknown; never raises.
    """
    try:
        return _decision_claim_notes(str(text or ""), state, set(cast_ids or ()))
    except Exception as error:  # a claim check must never cost the decision
        logger.debug("decision claim check failed: %s", error, exc_info=True)
        return []


def _decision_claim_notes(text: str, state: dict | None, cast_ids: set) -> list[str]:
    if not text or not isinstance(state, dict):
        return []
    local = _local_seat(state)
    if local is None:
        return []
    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    on_board = [c for c in battlefield if _is_creature_card(c) and c.get("name")]
    ours = [c for c in on_board if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local]
    theirs = [c for c in on_board if (c.get("controller_seat_id") or c.get("owner_seat_id")) != local]
    hand = [c for c in _castable_cards(state) if c.get("name")]
    turn_info = state.get("turn") or {}
    turn = _round(turn_info.get("turn_number"))
    our_turn = turn_info.get("active_player") == local
    notes: list[str] = []

    # Keyword and blocking claims, as in the plan (#23).
    _corrected, found = _claim_corrections(
        text,
        ours=ours,
        theirs=theirs,
        hand=[c for c in hand if _is_creature_card(c)],
        battlefield=battlefield,
        our_turn=our_turn,
    )
    notes.extend(found)

    lowered = text.lower().replace("’", "'")
    if len(lowered) != len(text):
        return notes
    creatures = [*ours, *theirs, *[c for c in hand if _is_creature_card(c)]]
    spans = _name_spans(lowered, _claim_keys([*creatures, *hand]))
    by_name: dict[str, list[dict]] = {}
    for card in [*creatures, *hand]:
        by_name.setdefault(str(card.get("name")), []).append(card)

    def window(index: int) -> str:
        """From after this name to the next name or the end of its clause."""
        end = spans[index][1]
        stop = spans[index + 1][0] if index + 1 < len(spans) else len(lowered)
        part = lowered[end:stop]
        cut = _CLAUSE_END.search(part)
        return part[: cut.start()] if cut else part

    # "hasty X", "X gives haste": X a creature without haste (no grant on the board).
    for index, (start, _end, name) in enumerate(spans):
        cards = [c for c in by_name.get(name, []) if _is_creature_card(c)]
        if not cards:
            continue
        before = lowered[max(0, start - 12) : start]
        # Close by and in the same phrase: "gives haste", "two hasty trampling attackers".
        near = window(index)[:30].split(",")[0]
        claimed = _HASTE_CLAIM.search(near) or re.search(r"\bhasty\s+(?:the\s+)?$", before)
        if claimed and _keyword_truth(cards, "haste", battlefield) is False:
            fact = f"{name} has no haste"
            if fact not in notes and not any(n.startswith(f"{name} has no haste") for n in notes):
                notes.append(fact)

    # A creature cast now: no noncreature-spell trigger, and summoning sick.
    cast = [c for c in hand if c.get("instance_id") in cast_ids]
    for card in cast:
        name = str(card.get("name"))
        if not _is_creature_card(card):
            continue
        for sentence in re.split(r"[;.\n]", lowered):
            keys = {key for key, owner in _claim_keys([card]) if owner == name}
            if not any(re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", sentence) for key in keys):
                continue
            other_spells = [
                c
                for c in hand
                if not _is_creature_card(c)
                and c.get("instance_id") not in cast_ids
                and _named_creatures(sentence, [str(c.get("name"))])
            ]
            cast_trigger = any(
                not _EVERY_SPELL.search(sentence[verb.end() : verb.end() + 30])
                for verb in _CAST_TRIGGER_VERB.finditer(sentence)
            )
            if _NONCREATURE_TRIGGER.search(sentence) and cast_trigger and not other_spells:
                fact = (
                    f"{name} is a creature spell: casting it triggers no noncreature-spell ability (prowess)"
                )
                if fact not in notes:
                    notes.append(fact)

    # A summoning-sick creature of ours (or one cast now) attacking, pinging or tapping this turn.
    phase = str(turn_info.get("phase") or "")
    step = str(turn_info.get("step") or "")
    before_attacks = our_turn and (
        phase in ("Phase_Beginning", "Phase_Main1", "Beginning", "Main1")
        or (phase in ("Phase_Combat", "Combat") and step in ("", "Step_BeginCombat", "Step_DeclareAttack"))
    )
    if before_attacks:
        global_haste = _our_global_haste(state)
        sick = {str(c.get("name")) for c in ours if _summoning_sick(c, turn, global_haste)} | {
            str(c.get("name"))
            for c in cast
            if _is_creature_card(c)
            and not global_haste
            and _keyword_truth([c], "haste", battlefield) is False
        }
        able = {str(c.get("name")) for c in ours if not _summoning_sick(c, turn, global_haste)}
        for index, (start, _end, name) in enumerate(spans):
            if name not in sick or name in able:
                continue
            after = window(index)
            before = lowered[max(0, start - 40) : start]
            before = before[_last_clause_start(before) :]
            # A description counts as a claim when its own clause says this turn.
            this_turn = bool(_THIS_TURN.search(after))
            claim = next(
                (
                    match
                    for match in _ATTACK_CLAIM.finditer(after)
                    if this_turn or not _describes_card(after, match)
                ),
                None,
            ) or re.search(r"\battack(?:s|ing)? with\s+(?:the\s+)?$", before)
            if claim and not _NOT_NOW.search(after) and not _NOT_NOW.search(before):
                fact = f"{name} is summoning sick: it can't attack, tap or make an attack trigger this turn"
                if fact not in notes:
                    notes.append(fact)
    return notes


def _last_clause_start(text: str) -> int:
    ends = list(_CLAUSE_END.finditer(text))
    return ends[-1].end() if ends else 0


def _check_kill_claims(plan: GamePlan, assessment: Any, state: dict, issues: list[str]) -> None:
    """Annotate false kill claims in the plan's text (see the comment above)."""
    try:
        local = _local_seat(state)
        battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
        on_board = [c for c in battlefield if _is_creature_card(c) and c.get("name")]
        ours = [c for c in on_board if (c.get("controller_seat_id") or c.get("owner_seat_id")) == local]
        theirs = [c for c in on_board if (c.get("controller_seat_id") or c.get("owner_seat_id")) != local]
        castable = [c for c in _castable_cards(state) if c.get("name")]
        spells = [
            c
            for c in castable
            if not _is_creature_card(c) and "land" not in str(c.get("type_line") or "").lower()
        ]
        # Our creatures, in hand too: a clause naming one may count its combat or trigger.
        ours = ours + [c for c in castable if _is_creature_card(c)]
        if not theirs or not spells:
            return
        steps = {str(s.get("label") or ""): s for s in plan.turn_plan}
        by_turn = {f"T{s.get('turn')}": s for s in plan.turn_plan}

        def casts_of(step: dict | None) -> list[str]:
            return _cast_names(step.get("cast")) if step else []

        def fixed(value: str, where: str, step: dict | None = None) -> str:
            if not value:
                return value

            def step_casts(label: str) -> list[str]:
                return casts_of(step if step is not None else by_turn.get(label) or steps.get(label))

            text, notes = _kill_corrections(
                value, spells=spells, ours=ours, theirs=theirs, step_casts=step_casts
            )
            issues.extend(f"{where}: plan claim corrected: {note}" for note in notes)
            return text

        for attr in ("path", "threat", "develop_next", "role_reason"):
            setattr(plan, attr, fixed(getattr(plan, attr), attr))
        for attr in ("win_conditions", "switch_if", "assumptions", "resource_priorities"):
            setattr(plan, attr, [fixed(item, attr) for item in getattr(plan, attr)])
        for step in plan.turn_plan:
            for key in ("attack", "hold"):
                step[key] = fixed(str(step.get(key) or ""), str(step.get("label") or "T"), step)
    except Exception as error:  # a claim check must never cost the plan
        logger.debug("plan kill-claim check failed: %s", error, exc_info=True)


# A plan clause about their turn: blocks, their combat (10a78106 T9: "{2}{R} reserved to
# pump-block/trade Bristly Bill in their combat" with Fleeting Effigy).
_THEIR_TURN_USE = re.compile(
    r"\b(?:block(?:s|ed|ing|ers?)?|pump-block\w*|their (?:turn|combat|attacks?|end step)|through their"
    r"|on defen[cs]e|opponent's turn)\b",
    re.I,
)


def _check_leaver_blocks(plan: GamePlan, state: dict, issues: list[str]) -> None:
    """Plan clauses that use an end-step leaver on their turn (it is back in our hand by then).

    A hold clause doing so is dropped (the mana check then says what to spend
    it on); elsewhere the clause is annotated. Our own attacks are judged in
    :func:`_validate_turns`.
    """
    try:
        leavers = list(_end_step_leavers(state))
        if not leavers:
            return

        def check(value: str, where: str, *, drop: bool) -> str:
            if not value:
                return value
            out, changed = [], False
            for clause in value.split(";"):
                named = _named_creatures(clause, leavers)  # "pumps Effigy" names Fleeting Effigy
                if not named or not _THEIR_TURN_USE.search(clause) or "[checked:" in clause:
                    out.append(clause)
                    continue
                changed = True
                fact = f"{' and '.join(named)} return{'s' if len(named) == 1 else ''} to our hand at our end step"
                issues.append(f"{where}: {'dropped' if drop else 'flagged'} '{clause.strip()}' ({fact})")
                if drop:
                    continue
                out.append(f"{clause.rstrip()} [checked: {fact}; it can't block on their turn]")
            if not changed:
                return value
            if drop:
                note = f"{', '.join(leavers)} can't block on their turn [checked: back in our hand at our end step]"
                out = [c for c in out if c.strip()] + [f" {note}"]
            return ";".join(out).strip()

        for attr in ("path", "threat", "develop_next", "role_reason"):
            setattr(plan, attr, check(getattr(plan, attr), attr, drop=False))
        for attr in (
            "win_conditions",
            "switch_if",
            "assumptions",
            "resource_priorities",
            "active_mechanisms",
        ):
            setattr(plan, attr, [check(item, attr, drop=False) for item in getattr(plan, attr)])
        for step in plan.turn_plan:
            label = str(step.get("label") or "T")
            step["attack"] = check(str(step.get("attack") or ""), label, drop=False)
            step["hold"] = check(str(step.get("hold") or ""), label, drop=True)
    except Exception as error:  # a plan check must never cost the plan
        logger.debug("plan end-step check failed: %s", error, exc_info=True)


# --- mana holds ------------------------------------------------------------------------
#
# 2026-10-19 match 92c09c7a: the T8 step said "hold: {R}{R} spare - no instants
# in hand", T10 "{R}{R} spare - bank toward kicked Burst Lightning if drawn".
# With that hold in its prompt the model twice passed on Hired Claw's {1}{R}
# counter ("banking {1}{R} preserves flexibility for a drawn burn spell",
# 09:33:24 and 09:33:27): 2 mana wasted. Mana empties at the end of each step
# and phase, and nothing in hand could use it. A mana hold now needs an
# instant-speed use (an instant or flash card in hand, cycling, an unrestricted
# activated ability of a permanent that is still there on their turn) that the
# spare mana pays for, or must name what it is for ("{1}{R} for Hired Claw's
# counter"); a hold for a card "if drawn" counts only while that draw is still
# ahead. "Banking" mana never does, unless it is stored (a Treasure).

_MANA_SYMBOL = re.compile(r"\{[^}]*\}")
_MANA_CONTEXT = re.compile(
    r"\bmana\b|\b(?:white|blue|black|red|green|colorless)\s+(?:open|up|untapped)\b", re.I
)
_MANA_HOLD = re.compile(
    r"\b(?:spare|leftover|unused|idle|open|untapped|up|bank|banks|banked|banking|reserved?|floating|float)\b",
    re.I,
)
_MANA_BANK = re.compile(r"\bbank(?:s|ed|ing)?\b|\bsave (?:up )?mana\b|\bcarry (?:it )?over\b", re.I)
# Mana that can be stored across turns (a Treasure, mana counters): "bank" may be real.
_STORED_MANA = re.compile(r"\b(?:treasures?|tokens?|food|counters?|stor(?:e|ed|age))\b", re.I)
_SACRIFICE_FOR_MANA = re.compile(r"\bsacrifice\b[^:\n]*:\s*add\b")
# "no spare mana this turn", "nothing to hold": a statement, not a hold.
_NO_HOLD = re.compile(r"^\s*(?:no|nothing|zero|0)\b", re.I)
_DRAWN_CARD = re.compile(r"\bif drawn\b|\bdrawn\b|\btop-?deck|\bdraw(?:s)? into\b", re.I)
_ABILITY_LINE = re.compile(r"^(?P<cost>[^:\n]*\{[^}]*\}[^:\n]*):\s*(?P<effect>.+)$")
_RESTRICTED_ABILITY = re.compile(r"\bactivate only (?:as a sorcery|during your turn|if)\b")
_SPEND_NOTE = "spend spare mana this turn{sinks}: unused mana empties at the end of each step and phase"


def _mana_abilities(card: dict) -> list[tuple[int, bool]]:
    """(mana cost, worth holding mana for) of each activated non-mana ability with a mana cost."""
    from arenamcp.combat_strategy import _rules_text
    from arenamcp.mulligan_policy import _mana_value

    text = _rules_text(card)
    stays = not _LEAVES_AT_END.search(text)
    found = []
    for line in text.splitlines():
        match = _ABILITY_LINE.match(line.strip())
        if not match:
            continue
        cost, effect = match.group("cost"), match.group("effect")
        symbols = [s for s in _MANA_SYMBOL.findall(cost) if s.strip("{}").strip("o") not in ("t", "q")]
        if (
            not symbols
            or re.search(r"\badd\b", effect)
            or re.search(r"\bfrom your (?:graveyard|hand)\b", cost)
        ):
            continue
        if re.search(r"\b(?:equip|loyalty)\b", cost):
            continue
        found.append((_mana_value(cost), stays and not _RESTRICTED_ABILITY.search(effect)))
    return found


def _check_mana_holds(plan: GamePlan, assessment: Any, state: dict, issues: list[str]) -> None:
    """Replace a step's mana hold that nothing can spend at instant speed (see the comment above)."""
    try:
        from arenamcp.combat_strategy import _rules_text
        from arenamcp.mulligan_policy import _mana_value, hand_card

        local = _local_seat(state)
        hand = [c for c in state.get("hand") or [] if isinstance(c, dict) and c.get("name")]
        hand_names = _card_names(_castable_cards(state))
        permanents = [
            c
            for c in state.get("battlefield") or []
            if isinstance(c, dict) and (c.get("controller_seat_id") or c.get("owner_seat_id")) == local
        ]
        flashback = [
            c
            for c in state.get("graveyard") or []
            if isinstance(c, dict)
            and c.get("owner_seat_id", local) == local
            and _flashback_cost(c)
            and hand_card(c).instant_speed
        ]
        now_turn = _round(getattr(assessment, "turn", 0))
        our_turn = bool(getattr(assessment, "our_turn", False))
        stored = any(_SACRIFICE_FOR_MANA.search(_rules_text(c)) for c in permanents)
        cast_so_far: Counter[str] = Counter()
        for step in plan.turn_plan:
            spent, entered = 0, []
            for entry in step.get("cast") or []:
                text = str(entry)
                cycle = text.lower().startswith("landcycle ")
                card = _named_card(_ANY_NOTE.sub("", text.removeprefix("landcycle ")), hand_names)
                if card is None:
                    continue
                info = hand_card(card)
                spent += (info.landcycling or 0) if cycle else info.mana_value
                cast_so_far[_plain(card.get("name"))] += 1
                if not cycle and not info.instant_speed:
                    entered.append(card)
            hold = str(step.get("hold") or "")
            if not hold:
                continue
            spare = _round(step.get("mana"), 0) - spent
            left = Counter(cast_so_far)
            uses: list[str] = []
            still_in_hand: list[dict] = []
            for card in hand:
                key = _plain(card.get("name"))
                if left[key] > 0:
                    left[key] -= 1
                    continue
                still_in_hand.append(card)
                info = hand_card(card)
                cycling = re.search(
                    r"\b(?:\w*cycling|channel)\b\s*(?:[—\-—]\s*)?((?:\{[^}]*\})+)", _rules_text(card)
                )
                if (info.instant_speed and not info.is_land and info.mana_value <= spare) or (
                    cycling and _mana_value(cycling.group(1)) <= spare
                ):
                    uses.append(str(card.get("name")))
            uses += [str(c.get("name")) for c in flashback if _mana_value(_flashback_cost(c) or "") <= spare]
            sinks: list[str] = []
            for card in [*permanents, *entered]:
                for cost, reactive in _mana_abilities(card):
                    if cost <= spare and str(card.get("name")) not in sinks:
                        sinks.append(str(card.get("name")))
                    if reactive and cost <= spare:
                        uses.append(str(card.get("name")))
            # A clause naming what the mana is for ("reserve {6}{U}{B} for Recursive
            # Recruitment flashback") plans to spend it: kept.
            spendable = [
                str(c.get("name")) for c in [*still_in_hand, *flashback] if not hand_card(c).is_land
            ] + sinks
            draw_ahead = not (our_turn and step.get("turn") == now_turn)
            kept, dropped = [], []
            for clause in [part.strip() for part in hold.split(";") if part.strip()]:
                mana_hold = bool(
                    (_MANA_SYMBOL.search(clause) or _MANA_CONTEXT.search(clause))
                    and _MANA_HOLD.search(clause)
                    and not _NO_HOLD.match(clause)
                )
                waits_for_draw = draw_ahead and bool(_DRAWN_CARD.search(clause))
                names_use = any(_plan_mentions(name, clause.casefold()) for name in spendable)
                banks = bool(_MANA_BANK.search(clause)) and not (stored or _STORED_MANA.search(clause))
                if mana_hold and (banks or not (uses or waits_for_draw or names_use)):
                    dropped.append(clause)
                else:
                    kept.append(clause)
            if not dropped:
                continue
            listed = f" ({', '.join(sinks[:2])})" if sinks else ""
            if spare > 0:
                kept.append(_SPEND_NOTE.format(sinks=listed))
            step["hold"] = "; ".join(kept)
            why = (
                "mana can't be banked"
                if any(_MANA_BANK.search(c) and not (stored or _STORED_MANA.search(c)) for c in dropped)
                else "nothing in hand or on the board uses it at instant speed"
            )
            issues.append(
                f"{step.get('label')}: dropped the mana hold '{'; '.join(dropped)}' ({why}; "
                "unused mana empties at the end of each step and phase)"
            )
    except Exception as error:  # a hold check must never cost the plan
        logger.debug("plan mana-hold check failed: %s", error, exc_info=True)


def _cast_entries(value: Any) -> list[str]:
    """A step's cast list as entries, notes kept ("X (gain 4 life)"), without landcycling."""
    if isinstance(value, str):
        parts = [part.strip() for part in re.split(r",|\+| and ", value) if part.strip()]
    else:
        parts = [str(part).strip() for part in value or [] if str(part).strip()]
    return [part for part in parts if not part.lower().startswith("landcycle ")]


def _cast_names(value: Any) -> list[str]:
    """A step's cast list as card names: split, without notes ("X (gain 4 life)") and landcycling."""
    return [_ANY_NOTE.sub("", part) for part in _cast_entries(value)]


def _named_card(name: str, names: dict[str, dict]) -> dict | None:
    """The card ``name`` names in a ``_card_names`` lookup (full name, or the part before a comma)."""
    return names.get(_plain(name)) or names.get(_plain(str(name).split(",")[0]))


def _cast_parts(entry: str, known: Callable[[str], Any]) -> tuple[str, str, bool]:
    """(card name, trailing note, landcycled) of one plan cast entry.

    "Archive Arbiter (gain 4 life)" (CANDIDATE LINES), "Archive Arbiter
    (choose: gain 4 life)" / "X (on Y)" (the line check) and "landcycle X" all
    name a card: the note is split off when the name without it is ``known``
    (a card in hand or the graveyard) and the full text is not.
    """
    text = str(entry or "").strip()
    cycle = text.lower().startswith("landcycle ")
    if cycle:
        text = text[len("landcycle ") :].strip()
    if known(text):
        return text, "", cycle
    match = _ANY_NOTE.search(text)
    if match is None:
        return text, "", cycle
    return text[: match.start()].strip(), match.group(1).strip(), cycle


def _ready_attacker_names(state: dict, assessment: Any, index: int) -> set[str]:
    """Names of our creatures on the battlefield now that can attack on the plan's turn ``index``.

    At T on our own turn: untapped and not summoning-sick (it entered before
    this turn, or has haste). On a later turn, or at T when T is our next turn,
    every creature of ours that is on the battlefield now, except one that
    leaves at our end step (it is back in hand by then).
    """
    from arenamcp.combat_keywords import has_combat_keyword

    local = _local_seat(state)
    turn = _round(getattr(assessment, "turn", None), -1)
    now = index == 0 and bool(getattr(assessment, "our_turn", False))
    haste_for_all = _our_global_haste(state) if now else False
    names: set[str] = set()
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict) or not card.get("name") or not _is_creature_card(card):
            continue
        if (card.get("controller_seat_id") or card.get("owner_seat_id")) != local:
            continue
        if now:
            entered = _round(card.get("turn_entered_battlefield"), -1)
            sick = entered >= 0 and entered >= turn
            if card.get("is_tapped") or (
                sick and not haste_for_all and not has_combat_keyword(card, "haste")
            ):
                continue
        elif _leaves_at_end(card):
            continue
        names.add(str(card["name"]))
    return names


def _is_hand_land(card: dict) -> bool:
    types = f"{card.get('type_line') or ''} {card.get('card_types') or ''}".lower()
    return "land" in types and "creature" not in types


def _plan_eval_steps(
    steps: list, assessment: Any, state: dict, *, keep_land_turns: bool = False
) -> list[dict]:
    """The plan's turns as ``line_search.evaluate_plan`` reads them.

    Labels T/T+1/T+2, card names as in hand with their notes kept ("Archive
    Arbiter (choose: gain 4 life)", "Shock (on Cadet)": the replay casts that
    mode and target), attackers by full name (tokens in the plan's words, the
    replay names its tokens the same way: "Elemental token"). A turn
    that names no land plays one from hand when one is left (an omitted land
    drop is not a decision to skip it; at T only when the board math has a
    drop). Turns after T with no cast are left out, so they play greedily:
    the check judges the plan's choices, not its blanks ("if drawn: X").
    ``keep_land_turns`` keeps such a turn when it names a land (the mana
    budgets follow the plan's land drops).
    """
    from arenamcp.board_assessment import _enters_tapped
    from arenamcp.line_search_moves import LABELS

    hand = [c for c in state.get("hand") or [] if isinstance(c, dict) and c.get("name")]
    hand_names = _card_names(_castable_cards(state))
    lands_left = [c for c in hand if _is_hand_land(c)]
    creatures = _our_creature_names(state)
    lookahead = list(getattr(assessment, "lookahead", None) or [])
    planned: dict[int, dict] = {}
    for k, raw in enumerate(steps[: len(LABELS)]):
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or "").replace(" ", "").upper()
        index = LABELS.index(label) if label in LABELS else _turn_index(raw.get("turn"), k)
        if 0 <= index < len(LABELS):
            planned.setdefault(index, raw)
    out = []
    for index in sorted(planned):
        raw = planned[index]
        land = str(raw.get("land") or "").strip()
        taken = next((c for c in lands_left if _plain(c["name"]) == _plain(land)), None) if land else None
        if land and not any(_plain(c["name"]) == _plain(land) for c in hand if _is_hand_land(c)):
            land = ""  # not a land in hand at all (validation drops it): as if omitted
        # A filler drop never takes a copy a later planned turn names for itself.
        reserved = Counter(_plain(planned[k].get("land")) for k in planned if k > index)
        free = []
        for c in lands_left:
            if reserved[_plain(c["name"])] > 0:
                reserved[_plain(c["name"])] -= 1
            else:
                free.append(c)
        if not land and free and (index > 0 or (lookahead and lookahead[0].land)):
            preferred = _plain(lookahead[index].land) if index < len(lookahead) else ""
            taken = next((c for c in free if _plain(c["name"]) == preferred), None) or min(
                free, key=lambda c: (_enters_tapped(c), lands_left.index(c))
            )
            land = str(taken["name"])
        if taken is not None:
            lands_left.remove(taken)
        casts = []
        for entry in _cast_entries(raw.get("cast")):
            name, note, _cycle = _cast_parts(entry, lambda n: _named_card(n, hand_names))
            card = _named_card(name, hand_names)
            name = str(card["name"]) if card else name
            casts.append(f"{name} ({note})" if note else name)
        if index > 0 and not casts and not (keep_land_turns and str(raw.get("land") or "").strip()):
            continue
        plain = _plain(raw.get("attack"))
        if not plain or _NO_ATTACK.match(plain):
            attack = "none"
        elif _ALL_ATTACK.search(plain):
            attack = "all"
        else:
            named = _named_creatures(str(raw.get("attack")), creatures)
            if "token" in plain:
                # Tokens the plan's casts make exist only in the replay ("attack with Elemental
                # token, Elemental token" from CANDIDATE LINES): it matches them by name there.
                named.append(str(raw.get("attack")))
            attack = ", ".join(named) or "none"
        out.append({"label": LABELS[index], "land": land, "cast": casts, "attack": attack})
    return out


def _evaluate_plan_line(steps: list, assessment: Any, state: dict, *, keep_land_turns: bool = False) -> Any:
    """``line_search.evaluate_plan`` for these steps, or None (no usable search, or it failed)."""
    result = getattr(assessment, "line_search", None)
    if result is None or getattr(result, "truncated", True) or getattr(result, "dead_now", False):
        return None
    try:
        from arenamcp import line_search

        replay = _plan_eval_steps(steps, assessment, state, keep_land_turns=keep_land_turns)
        evaluation = line_search.evaluate_plan(result, replay, state)
    except Exception as error:  # the line check is advisory: never break plan validation
        logger.debug("plan line evaluation failed: %s", error, exc_info=True)
        return None
    return evaluation if evaluation.line is not None else None


def _check_plan_line(plan: GamePlan, assessment: Any, state: dict, issues: list[str]) -> None:
    """Compare the validated plan's line with the search's best line (see :func:`validate_plan`)."""
    if not plan.turn_plan:
        return
    evaluation = _evaluate_plan_line(plan.turn_plan, assessment, state)
    result = getattr(assessment, "line_search", None)
    best = getattr(result, "best", None)
    if evaluation is None or best is None:
        return
    try:
        from arenamcp.board_assessment import _leaf_race
        from arenamcp.line_search_moves import ALIVE

        line = evaluation.line
        skipped = _unchecked_plan(plan, evaluation, result, state)
        if skipped:
            # The replay would undervalue the plan: no verdict either way.
            issues.append(f"line check skipped, the search can't value: {skipped}")
            return
        # The best line's value carries the race term on its leaf; so must the plan's.
        race = _leaf_race(result, line) if line.cls == ALIVE else None
        value = line.v + (race or 0.0)
        if (line.cls, line.timing) < (best.cls, best.timing):
            outcome = f"dies T{line.dead_turn}" if line.outcome == "dead" else line.outcome_text()
            text = f"plan line {outcome}; best line {best.summary()}"
            later: list[str] = []
            if assessment.survival_mode:
                text += _t_step_override(plan, assessment, state, line, best, later)
            issues.append(text)
            issues.extend(later)
        elif (line.cls, line.timing) == (best.cls, best.timing) and best.v - value >= PLAN_LINE_GAP:
            issues.append(
                f"plan line {line.outcome_text()} trails the best line by {best.v - value:.1f} in value: "
                f"{best.summary()}"
            )
    except Exception as error:
        logger.debug("plan line check failed: %s", error, exc_info=True)


def _unchecked_plan(plan: GamePlan, evaluation: Any, result: Any, state: dict) -> str:
    """The planned casts the replay can't value, '; '-joined ('' when the line can be judged).

    The replay (``line_search.evaluate_plan``) casts each noted mode and
    lists the casts whose effect, or noted mode, it does not model
    (``PlanEvaluation.unmodelled``: "T: Elemental Surge (its tokens)",
    "T: Archive Arbiter (choose: destroy …): mode not modelled"): the plan's
    line looks worse than it is. A hand card it could not cast at all (an X
    burn spell) is checked with :func:`unmodelled_effect` too.
    """
    found = [str(entry) for entry in getattr(evaluation, "unmodelled", None) or []]
    hand = _card_names(_castable_cards(state))
    for step in plan.turn_plan:
        for entry in step.get("cast") or []:
            name, _note, cycle = _cast_parts(str(entry), lambda n: _named_card(n, hand))
            card = None if cycle else _named_card(name, hand)
            why = unmodelled_effect(card, result) if card is not None else ""
            text = f"{step.get('label')}: {card.get('name')} ({why})" if why else ""
            if text and not any(item.startswith(text) for item in found):
                found.append(text)
    return "; ".join(found)


def _t_step_override(
    plan: GamePlan, assessment: Any, state: dict, line: Any, best: Any, issues: list[str]
) -> str:
    """The line check's T-step override, as the issue's suffix; it changes the plan only when on.

    It is the line guard at plan time and follows ARENAMCP_LINE_GUARD
    (shadow by default: the rewritten T step reaches every per-decision
    prompt as 'THIS TURN …', so it waits for the user's review of the shadow
    logs like the guard does). 'off': no override; 'shadow': ' — Line guard
    (shadow): would replace T with …' and the plan is untouched; 'on': the T
    step is replaced. Never when the override would not survive the guards'
    own rules: worse under the +2/+0 trick proxy, or a modal choice of a card
    with a mode the search can't value (the mode guard never applies those).
    """
    from arenamcp import line_guard

    setting = line_guard.guard_mode("line")
    if setting == "off":
        return ""
    blocker = _override_blocker(assessment.line_search, line, best, state)
    if blocker:
        return f" — T kept ({blocker})"
    if setting != "on":
        preview = _replace_t_step(deepcopy(plan), assessment, state, line, best, [])
        return f" — Line guard (shadow): would replace T with {preview}" if preview else ""
    replaced = _replace_t_step(plan, assessment, state, line, best, issues)
    return f" — T replaced with {replaced}" if replaced else ""


def _override_blocker(result: Any, line: Any, best: Any, state: dict) -> str:
    """Why the best line's T step must not replace the plan's, or ''."""
    from arenamcp.line_search import proxy_outcome
    from arenamcp.line_search_moves import bullets, classify

    step = best.steps[0] if best.steps else None
    hand = _card_names(_castable_cards(state))
    for name, _mode in getattr(step, "modes", ()) or ():
        card = _named_card(name, hand) or {}
        modes = bullets(str(card.get("oracle_text") or ""))
        if any(classify(str(name), mode)[0] == "other" for mode in modes):
            return f"{name}'s other mode is not modelled"
    trick_best, trick_plan = proxy_outcome(result, best), proxy_outcome(result, line)
    if trick_best is None or trick_plan is None:
        return "no +2/+0 trick replay"
    if (trick_best.cls, trick_best.timing) < (trick_plan.cls, trick_plan.timing):
        return f"+2/+0 trick: {trick_best.outcome_text()} vs {trick_plan.outcome_text()}"
    return ""


def _replace_t_step(
    plan: GamePlan, assessment: Any, state: dict, line: Any, best: Any, issues: list[str]
) -> str:
    """Put the best line's T step (land, casts and modes, attack) into the plan's T step.

    Returns the T step's text, or '' when there is nothing to replace (no T
    step, or the plan already plays it). Later turns lose casts and lands the
    new T step now uses.
    """
    step = plan.step_for(assessment.plan_turn)
    if step is None or not best.steps or not line.steps:
        return ""
    new, old = best.steps[0], line.steps[0]
    if new.turn != step.get("turn") or new.sig[1:] == old.sig[1:]:
        return ""
    displaced = [name for name in _cast_names(step.get("cast")) if name not in new.casts]
    modes, targets = dict(new.modes), dict(new.targets)

    def noted(name: str) -> str:
        notes = [f"choose: {modes[name]}"] if modes.get(name) else []
        notes += [f"on {targets[name]}"] if targets.get(name) else []
        return f"{name} ({', '.join(notes)})" if notes else name

    step["land"] = new.land
    step["cast"] = [noted(name) for name in new.casts] + [f"landcycle {name}" for name in new.cycles]
    step["attack"] = ", ".join(new.attack) or "none"
    # The old hold planned around the old casts ("UU for Countersculpt" next to a 6-drop
    # that taps 6 of 7): the best line's own held instants, if any.
    step["hold"] = ", ".join(new.held)[:120]
    step["mana"] = new.mana
    # Each hand card (and commander) once across the plan: later turns lose what T now uses.
    # A creature that returns at our end step is a hand card again (Fleeting Effigy).
    in_hand = Counter(_plain(c.get("name")) for c in _castable_cards(state))
    left = in_hand - Counter(_plain(name) for name in [*new.casts, *new.cycles, new.land] if name)
    returning = {_plain(name) for name in _end_step_leavers(state)}
    for later in [s for s in plan.turn_plan if s.get("turn", 0) > step["turn"]]:
        kept = []
        for entry in later.get("cast") or []:
            name = _plain(_ANY_NOTE.sub("", str(entry)).removeprefix("landcycle "))
            if not in_hand[name] or name in returning:  # from the graveyard, or back in hand
                kept.append(entry)
            elif left[name] > 0:
                left[name] -= 1
                kept.append(entry)
            else:
                issues.append(
                    f"{later.get('label')}: dropped {entry} (the line check casts it on T{step['turn']})"
                )
        later["cast"] = kept
        land = _plain(later.get("land"))
        if land and in_hand[land]:
            if left[land] > 0:
                left[land] -= 1
            else:
                issues.append(
                    f"{later.get('label')}: dropped land {later['land']} (played on T{step['turn']})"
                )
                later["land"] = ""
    _move_displaced(plan, best, displaced, state, issues)
    return new.text()


def _move_displaced(plan: GamePlan, best: Any, displaced: list[str], state: dict, issues: list[str]) -> None:
    """The old T casts the best line plays on T+1 move to the plan's T+1 step, when its mana allows."""
    from arenamcp.mulligan_policy import hand_card

    if len(best.steps) < 2 or not displaced:
        return
    nxt = best.steps[1]
    step = next((s for s in plan.turn_plan if s.get("turn") == nxt.turn), None)
    if step is None:
        return
    commanders = _our_commanders(state)
    hand = _card_names(
        [c for c in state.get("hand") or [] if isinstance(c, dict)] + [c.card for c in commanders]
    )
    taxed = {id(c.card): c.cost for c in commanders if c.cost}
    planned = Counter(_plain(name) for s in plan.turn_plan for name in _cast_names(s.get("cast")))

    def cost(name: str) -> int:
        card = _named_card(name, hand)
        if card is not None and id(card) in taxed:
            return hand_card(dict(card, mana_cost=taxed[id(card)])).mana_value
        return hand_card(card).mana_value if card else 0

    for name in displaced:
        if name not in nxt.casts or planned[_plain(name)] or _named_card(name, hand) is None:
            continue
        casts = list(step.get("cast") or [])
        if sum(cost(c) for c in _cast_names(casts)) + cost(name) > _round(step.get("mana"), 0):
            continue
        step["cast"] = [*casts, name]
        planned[_plain(name)] += 1
        issues.append(f"{step.get('label')}: casts {name} (the best line plays it then)")


# --- render-time progress -------------------------------------------------------------

_DONE = " ✓"


def _with_progress(step: dict, assessment: Any, state: dict | None) -> dict:
    """``step`` with what is already done this turn marked ✓: the land played, permanents cast."""
    if not isinstance(state, dict) or not assessment.our_turn or step.get("turn") != assessment.turn:
        return step
    local = _local_seat(state)
    lands_played = next(
        (
            _round(p.get("lands_played"))
            for p in state.get("players") or []
            if isinstance(p, dict) and (p.get("seat_id") == local or p.get("is_local"))
        ),
        0,
    )
    entered_lands: Counter[str] = Counter()
    entered: Counter[str] = Counter()
    for card in state.get("battlefield") or []:
        if (
            not isinstance(card, dict)
            or (card.get("controller_seat_id") or card.get("owner_seat_id")) != local
        ):
            continue
        if _round(card.get("turn_entered_battlefield"), -1) != assessment.turn:
            continue
        (entered_lands if _is_hand_land(card) else entered)[_plain(card.get("name"))] += 1
    marked = dict(step)
    land = str(step.get("land") or "")
    if land and lands_played > 0 and entered_lands[_plain(land)] > 0:
        marked["land"] = land + _DONE
    casts = []
    for entry in step.get("cast") or []:
        name = _plain(_ANY_NOTE.sub("", str(entry)))
        if entered[name] > 0:
            entered[name] -= 1
            entry = f"{entry}{_DONE}"
        casts.append(entry)
    marked["cast"] = casts
    return marked


def _plan_mentions(name: str, text: str) -> bool:
    if not name or not text:
        return False
    full = name.casefold()
    if full in text:
        return True
    short = full.split(",")[0].strip()
    return len(short) > 3 and re.search(rf"\b{re.escape(short)}\b", text) is not None


def plan_card_losses(plan: GamePlan | None, state: dict | None) -> str:
    """Cards the plan names that are now in our graveyard or exile, with no copy still available.

    2026-10-10 FRA game 1: Proft was discarded on turn 5 and the plan kept
    naming "Proft at threshold as closer" for three more turns. The reform
    only runs every turn or two, so every decision prompt gets this line
    deterministically; "" when nothing named is gone.
    """
    if plan is None or plan.is_empty() or not isinstance(state, dict):
        return ""
    text = " ".join(
        [
            *plan.win_conditions,
            plan.path,
            plan.develop_next,
            *plan.active_mechanisms,
            *plan.resource_priorities,
        ]
        + [step_text(step) for step in plan.turn_plan]
    ).casefold()
    if not text.strip():
        return ""
    local = state.get("local_seat_id")
    if local is None:
        for player in state.get("players") or []:
            if player.get("is_local"):
                local = player.get("seat_id")
    available = {
        str(card.get("name") or "").casefold()
        for zone in ("hand", "battlefield", "command", "stack")
        for card in state.get(zone) or []
    }
    gone: dict[str, str] = {}
    for zone in ("graveyard", "exile"):
        for card in state.get(zone) or []:
            owner = card.get("owner_seat_id")
            if local is not None and owner is not None and owner != local:
                continue
            name = str(card.get("name") or "")
            if not name or name.casefold() in available or name in gone:
                continue
            if _plan_mentions(name, text):
                gone[name] = "graveyard" if zone == "graveyard" else "exile"
    if not gone:
        return ""
    listed = "; ".join(f"{name} (in our {zone})" for name, zone in gone.items())
    return (
        f"  PLAN CARDS NO LONGER AVAILABLE: {listed}. The plan was written before this. "
        "Do not plan to cast or hold them; a win condition built on them needs a recursion "
        "effect that is actually in hand, or a different route."
    )


# --- the board a plan was written from -----------------------------------------------
#
# 2026-10-19 match 41753907: the plan formed at 09:02:21 (from the 09:02:08
# board) said "T7: Effigy+Strike face, swing 4 unblocked -> opponent at -1;
# done". At 09:02:23 Unflinching Courage made Dawnwing Marshal a 4/4 flying,
# trample, lifelink attacker; the reform started for it yielded to our first T7
# decision 3 s later, and all of T7 ran on the old plan. Match f9e0ec3b: the T10
# plan was requested at 09:18:55, Lyra Dawnbringer resolved at 09:19:08, and the
# plan installed at 09:19:29 still said "attack: Emberheart Challenger, Hired Claw
# ... kill the flying blocker pre-combat", which reached the attack prompt. A
# plan now keeps the opposing board it was written from: prompts flag its lines
# once that board has changed, and a new threat re-plans (GamePlanManager:
# bounded, on the opponent's turn only).

# Keywords that change what an opposing creature does to our attacks or the race.
_THREAT_KEYWORDS = (
    "flying", "reach", "menace", "trample", "first strike", "double strike", "deathtouch", "lifelink",
    "hexproof", "indestructible",
)  # fmt: skip
# A small creature with one of these still stops, or kills, an attacker.
_STOPPER_KEYWORDS = frozenset({"deathtouch", "first strike", "double strike"})
# This much opposing life gained since the plan puts its damage counts off.
_LIFE_GAIN_STALE = 3


def _is_engine(card: dict) -> bool:
    """A noncreature, nonland, nontoken artifact, enchantment, planeswalker or battle."""
    if card.get("is_token") or "token" in str(card.get("object_kind") or "").lower():
        return False
    types = " ".join(
        [str(t) for t in card.get("card_types") or []] + [str(card.get("type_line") or "")]
    ).lower()
    if "creature" in types or "land" in types:
        return False
    return any(kind in types for kind in ("artifact", "enchantment", "planeswalker", "battle"))


def board_basis(state: dict | None) -> dict:
    """The opposing board a plan is written from ({} when our seat is unknown).

    ``creatures``: id, name, power/toughness, the threat keywords it has now
    and whether it is modified (counters or an attached Aura/Equipment, so a
    size change is lasting, not a combat trick); ``engines``: noncreature
    permanents (as the cadence's "new opposing permanent"); ``opp_life``;
    ``our_power``: the power of our biggest creature; ``ours``: our creatures
    (id, name) and ``our_hand``: the creature cards in our hand, for
    :func:`plan_attackers_gone`.
    """
    if not isinstance(state, dict):
        return {}
    local = _local_seat(state)
    if local is None:
        return {}
    from arenamcp.board_model import engine_facts
    from arenamcp.combat_keywords import has_combat_keyword, is_creature_modified

    battlefield = [c for c in state.get("battlefield") or [] if isinstance(c, dict)]
    efacts = engine_facts(state)
    creatures, engines, ours = [], [], []
    our_power = 0
    for card in battlefield:
        controller = card.get("controller_seat_id") or card.get("owner_seat_id")
        if controller == local and _is_creature_card(card):
            our_power = max(our_power, _round(card.get("power")))
            ours.append({"id": str(card.get("instance_id") or ""), "name": str(card.get("name") or "?")})
        if controller is None or controller == local:
            continue
        identity = str(card.get("instance_id") or card.get("name") or "")
        if _is_creature_card(card):
            power, toughness = _round(card.get("power"), -99), _round(card.get("toughness"), -99)
            if power == -99 or toughness == -99:
                continue
            entry: dict[str, Any] = {
                "id": identity,
                "name": str(card.get("name") or "?"),
                "power": power,
                "toughness": toughness,
                "keywords": [kw for kw in _THREAT_KEYWORDS if has_combat_keyword(card, kw)],
                "modified": is_creature_modified(card, battlefield=battlefield),
            }
            if _round(card.get("instance_id"), -1) in efacts:
                entry["engine"] = True
            creatures.append(entry)
        elif _is_engine(card):
            engines.append({"id": identity, "name": str(card.get("name") or "?")})
    life = next(
        (
            _round(p.get("life_total"), -99)
            for p in state.get("players") or []
            if isinstance(p, dict) and not p.get("is_local") and p.get("seat_id") != local
        ),
        -99,
    )
    return {
        "turn": _round((state.get("turn") or {}).get("turn_number")),
        "opp_life": None if life == -99 else life,
        "creatures": creatures,
        "engines": engines,
        "our_power": our_power,
        "ours": ours,
        "our_hand": sorted(
            {
                str(c.get("name"))
                for c in state.get("hand") or []
                if isinstance(c, dict) and c.get("name") and _is_creature_card(c)
            }
        ),
    }


def _with_keywords(keywords: list) -> str:
    return f" {', '.join(keywords)}" if keywords else ""


def board_changes(old: dict | None, new: dict | None, *, threats_only: bool = False) -> list[str]:
    """What changed on the opponent's side from board basis ``old`` to ``new``, most important first.

    Threats (``threats_only``): a new creature with a threat keyword (power 2
    or more, or deathtouch / first strike), an engine-role creature, or with
    power 4 or more, or at least our biggest creature's (2 or more); a creature
    that gained a threat keyword, or that grew by 2 and is modified; a new
    noncreature permanent.
    Otherwise also any new creature, any growth, and the opponent gaining
    ``_LIFE_GAIN_STALE`` life or more. Creatures that left or shrank are not
    changes (our own removal, mostly). [] when either side is unknown.
    """
    if not old or not new or "creatures" not in old or "creatures" not in new:
        return []
    before = {str(c.get("id")): c for c in old.get("creatures") or [] if isinstance(c, dict)}
    biggest = max(2, _round(new.get("our_power")))
    threats: list[str] = []
    minor: list[str] = []
    for card in new.get("creatures") or []:
        if not isinstance(card, dict):
            continue
        power, toughness = _round(card.get("power")), _round(card.get("toughness"))
        keywords = list(card.get("keywords") or [])
        was = before.get(str(card.get("id")))
        if was is None:
            threat = (
                bool(card.get("engine"))
                or (bool(keywords) and (power >= 2 or bool(_STOPPER_KEYWORDS & set(keywords))))
                or power >= 4
                or power >= biggest
            )
            text = f"{card.get('name')} {power}/{toughness}{_with_keywords(keywords)} entered"
            (threats if threat else minor).append(text)
            continue
        old_keywords = list(was.get("keywords") or [])
        gained = [kw for kw in keywords if kw not in old_keywords]
        grew = (power - _round(was.get("power")), toughness - _round(was.get("toughness")))
        threat = bool(gained) or (bool(card.get("modified")) and max(grew) >= 2)
        if threat or max(grew) > 0:
            text = (
                f"{card.get('name')} is now {power}/{toughness}{_with_keywords(keywords)} "
                f"(was {was.get('power')}/{was.get('toughness')}{_with_keywords(old_keywords)})"
            )
            (threats if threat else minor).append(text)
    old_engines = {str(e.get("id")) for e in old.get("engines") or [] if isinstance(e, dict)}
    for engine in new.get("engines") or []:
        if isinstance(engine, dict) and str(engine.get("id")) not in old_engines:
            threats.append(f"{engine.get('name')} entered")
    if threats_only:
        return threats
    before_life, after_life = old.get("opp_life"), new.get("opp_life")
    if (
        isinstance(before_life, int)
        and isinstance(after_life, int)
        and after_life - before_life >= _LIFE_GAIN_STALE
    ):
        minor.append(f"the opponent gained {after_life - before_life} life ({before_life} -> {after_life})")
    return threats + minor


def plan_attackers_gone(
    plan: GamePlan | None, state: dict | None = None, *, basis: dict | None = None
) -> list[str]:
    """Our creatures the plan's attacks still name that have left our battlefield since it was formed.

    2026-10-19 match c8ea2b6c (ledger #92): the T12 plan (formed on turn 11)
    attacked with Giant Cindermaw, our only creature; Elspeth, Storm Slayer
    destroyed it later that turn and T12 ran on the plan anyway. Names from
    the plan's board (``basis["ours"]``) mentioned in a remaining step's
    attack, not on our battlefield now (``basis`` or ``state``) and not in our
    hand to be cast again (Fleeting Effigy returns there every end step).
    """
    if plan is None or plan.is_empty() or not isinstance(plan.basis, dict):
        return []
    then = list(
        dict.fromkeys(str(c.get("name")) for c in plan.basis.get("ours") or [] if isinstance(c, dict))
    )
    now_basis = basis if basis is not None else board_basis(state)
    if not then or not now_basis or "ours" not in now_basis:
        return []
    present = {str(c.get("name")) for c in now_basis.get("ours") or [] if isinstance(c, dict)}
    present |= {str(name) for name in now_basis.get("our_hand") or []}
    turn = _round(now_basis.get("turn"))
    gone: list[str] = []
    for step in plan.turn_plan:
        if _round(step.get("turn")) < turn:
            continue
        for name in _named_creatures(str(step.get("attack") or ""), then):
            if name not in present and name not in gone:
                gone.append(name)
    return gone


def plan_removal_target_predates_threat(
    plan: GamePlan | None, board: dict | None, turn: int
) -> bool:
    """True when ``plan``'s step for ``turn`` names an opposing creature from ``plan.basis`` as a
    removal target and a new opposing threat has entered since (#134, match 16ed84d0 T7)."""
    if plan is None or plan.is_empty() or not isinstance(plan.basis, dict) or not board:
        return False
    step = plan.step_for(turn)
    if step is None:
        return False
    old_creatures = [
        str(c.get("name") or "")
        for c in plan.basis.get("creatures") or []
        if isinstance(c, dict) and c.get("name")
    ]
    if not old_creatures:
        return False
    text = " ".join(
        [*(str(c) for c in step.get("cast") or []), str(step.get("hold") or ""), str(plan.path or "")]
    ).casefold()
    if not any(_plan_mentions(name, text) for name in old_creatures):
        return False
    return bool(board_changes(plan.basis, board, threats_only=True))


def plan_board_changes(plan: GamePlan | None, state: dict | None, *, threats_only: bool = False) -> list[str]:
    """:func:`board_changes` from the board ``plan`` was written from to ``state``'s ([] when unknown)."""
    if plan is None or not plan.basis or not isinstance(state, dict):
        return []
    try:
        return board_changes(plan.basis, board_basis(state), threats_only=threats_only)
    except Exception as error:  # advisory: never break a prompt
        logger.debug("plan board comparison failed: %s", error)
        return []


def plan_board_note(plan: GamePlan, changes: list[str]) -> str:
    """The prompt line for a plan written before ``changes`` ('' without changes)."""
    if not changes:
        return ""
    return (
        f"  PLAN PREDATES THE OPPONENT'S BOARD (game plan turn {plan.turn_formed}): {'; '.join(changes)}. "
        "Its attacks, blocks, removal targets and damage counts assumed the old board; the facts above "
        "are current. Re-check the plan's lines against them before following it."
    )


def plan_refresh_note(plan: GamePlan | None, failed: tuple[int, str] | None) -> str:
    """The prompt line for a plan kept after its re-plan failed ('' otherwise).

    ``failed`` is (turn of the failed reform, why) from
    :class:`GamePlanManager`; the line names the plan's own turn.
    """
    if plan is None or plan.is_empty() or not failed:
        return ""
    turn, why = failed
    return (
        f"  PLAN NOT REFRESHED: the re-plan on turn {turn} failed ({why}), so this game plan is still the one "
        f"formed on turn {plan.turn_formed}. Treat its lines as stale: where the current board facts disagree "
        "with it, follow the facts."
    )


def compose_strategy_block(
    assessment: Any,
    plan: GamePlan | None,
    *,
    extra_facts: str = "",
    state: dict | None = None,
    with_lines: bool = True,
    refresh_failed: tuple[int, str] | None = None,
) -> str:
    """ROLE + this turn + facts (fresh) followed by the game plan's spine.

    The assessment is recomputed from the decision's own snapshot, so the role,
    clocks and lethal flags are current even when the plan was formed earlier
    in the turn. A validated plan step for this turn replaces the board-math
    deployment suggestion; with ``state``, what this turn already did is marked
    ✓ (the land played, permanents cast). ``extra_facts`` (indented fact
    lines, e.g. OPP INTERACTION) go with the facts, before the priority line.
    ``with_lines=False`` leaves out the search's '  LINES ...' line, for a
    prompt that already shows it (a typed decision, above its options).
    With ``state``, a plan written before the opponent's board changed says
    so on its step and in a PLAN PREDATES line (:func:`plan_board_changes`).
    ``refresh_failed`` (turn, why): the plan was kept after a failed re-plan;
    its step and a PLAN NOT REFRESHED line say so (:func:`plan_refresh_note`).
    """
    extra = extra_facts.rstrip()
    refresh = plan_refresh_note(plan, refresh_failed)
    if assessment is None:
        block = plan.as_planner_block().strip() if plan else ""
        if block and refresh:
            block += "\n" + refresh
        return f"{block}\n{extra}" if block and extra else block or extra.strip()
    this_turn = next_turns = None
    role_note = ""
    changes = plan_board_changes(plan, state) if plan is not None and not plan.is_empty() else []
    gone = plan_attackers_gone(plan, state) if state is not None else []
    if plan is not None and not plan.is_empty():
        step = plan.step_for(assessment.plan_turn)
        if step is not None:
            tag = f"game plan T{plan.turn_formed}"
            if refresh:
                tag += f", not refreshed (re-plan on turn {refresh_failed[0]} failed)"
            if gone:
                tag += f", {' and '.join(gone)} left the battlefield: its attacks are void"
            if changes:
                tag += ", written before the opponent's board changed: re-check its attacks"
            this_turn = f"{step_text(_with_progress(step, assessment, state))} [{tag}]"
        later = [s for s in plan.turn_plan if s.get("turn", 0) > assessment.plan_turn]
        if later:
            next_turns = " | ".join(f"T{s['turn']}: {step_text(s)}" for s in later)
        if plan.role and plan.role != assessment.role:
            role_note = (
                f" [game plan (turn {plan.turn_formed}) said {plan.role.upper()}; "
                "these board facts are newer — follow them]"
            )
    block = assessment.prompt_block(
        this_turn=this_turn, next_turns=next_turns, role_note=role_note, with_lines=with_lines
    )
    if extra:
        at = block.find("\n  Priority:")
        block = f"{block[:at]}\n{extra}{block[at:]}" if at >= 0 else f"{block}\n{extra}"
    if plan is not None and not plan.is_empty():
        block += "\n" + plan.as_planner_block(with_role_and_turns=False)
        if changes:
            block += "\n" + plan_board_note(plan, changes)
        if refresh:
            block += "\n" + refresh
        if gone:
            block += (
                f"\n  PLAN ATTACKER GONE: {', '.join(gone)} (in the game plan's attacks) is no longer on our "
                "battlefield. Every plan line that attacks with it is void; plan from the board facts above."
            )
        losses = plan_card_losses(plan, state)
        if losses:
            block += "\n" + losses
    return block


def grounded_facts_block(game_state: dict | None, *, with_lines: bool = True) -> str:
    """Fresh board facts (no game plan) for prompts without a plan manager.

    ``with_lines=False``: without the search's '  LINES ...' line (see
    :func:`compose_strategy_block`).
    """
    try:
        from arenamcp.board_assessment import assess

        assessment = assess(game_state) if isinstance(game_state, dict) else None
    except Exception as error:  # never break a prompt on the strategic layer
        logger.debug("board assessment unavailable: %s", error)
        return ""
    return assessment.prompt_block(with_lines=with_lines) if assessment else ""


# --- opponent interaction (opponent_tricks, advisory) -------------------------------------
#
# Kept out of BoardAssessment: the assessment cache key has no mana pools and
# no trick-table state, so a risk cached there would go stale (critique D4).

_TRICK_CACHE: OrderedDict[tuple, Any] = OrderedDict()
_TRICK_CACHE_LOCK = threading.Lock()
_TRICK_CACHE_SIZE = 16


def _trick_key(state: dict, table: Any) -> tuple:
    """The assessment signature plus what else the estimate reads, and the table it used."""
    from arenamcp.board_assessment import _signature

    zones = state.get("zones") if isinstance(state.get("zones"), dict) else {}
    pools = tuple(
        (str(p.get("seat_id")), json.dumps(p.get("mana_pool") or {}, sort_keys=True, default=str))
        for p in state.get("players") or []
        if isinstance(p, dict)
    )
    return (
        _signature(state),
        (str(table.set_code), table.version, table.built_at, id(table)),
        pools,
        json.dumps(state.get("revealed_cards") or [], sort_keys=True, default=str),
        zones.get("opponent_library_count"),
        tuple(str(c.get("instance_id")) for c in state.get("stack") or [] if isinstance(c, dict)),
        str(state.get("event_id") or state.get("format_name") or ""),
    )


def opponent_interaction(state: dict | None, service: Any) -> Any:
    """``opponent_tricks.trick_risk`` for ``state`` with ``service``'s in-memory table, or None.

    Never touches disk or network (the table is loaded by ``ensure`` off the
    decision path); None when there is no table for the match's set yet.
    """
    if not isinstance(state, dict) or service is None:
        return None
    try:
        from arenamcp.opponent_tricks import trick_risk

        table = service.get_for_state(state)
        if table is None:
            return None
        key = _trick_key(state, table)
        with _TRICK_CACHE_LOCK:
            cached = _TRICK_CACHE.get(key)
            if cached is not None:
                _TRICK_CACHE.move_to_end(key)
                return cached
        risk = trick_risk(state, table)
        with _TRICK_CACHE_LOCK:
            _TRICK_CACHE[key] = risk
            while len(_TRICK_CACHE) > _TRICK_CACHE_SIZE:
                _TRICK_CACHE.popitem(last=False)
        return risk
    except Exception as error:  # advisory: never break a prompt or the UI
        logger.debug("opponent interaction risk unavailable: %s", error)
        return None


def _line_telemetry(assessment: Any) -> str:
    """' | line: <best> (nodes=N, X ms[, bounded][, truncated])[ | greedy dead_in=X]' for the log."""
    result = getattr(assessment, "line_search", None)
    if result is None:
        return ""
    try:
        stats = dict(getattr(assessment, "search_stats", None) or result.stats())
        flags = "".join(f", {flag}" for flag in ("bounded", "truncated") if stats.get(flag))
        text = (
            f" | line: {result.best.summary()} "
            f"(nodes={stats.get('nodes')}, {float(stats.get('ms') or 0.0):.1f} ms{flags})"
        )
        greedy = getattr(assessment, "dead_in_greedy", None)
        if greedy != assessment.dead_in:
            text += f" | greedy dead_in={'none' if greedy is None else greedy}"
        return text
    except Exception as error:
        logger.debug("line telemetry unavailable: %s", error)
        return ""


def _round(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


_CRITICAL_LOCK = threading.Lock()
_critical_inflight = 0


_PLAN_KEYS = frozenset(
    {"win_conditions", "win_condition", "path", "turns", "turn_plan", "develop_next", "role", "threat"}
)
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
# Where an object can start: '{' and a quoted key (skips "{1}{R}" and bare '{' lines).
_OBJECT_START = re.compile(r'\{\s*"')
# An object whose first key is a plan key: the only ones worth repairing. The
# second form is written with escaped quotes ({\"role\": \"race\", ...}).
_PLAN_START = re.compile(r'\{\s*"(?:' + "|".join(sorted(_PLAN_KEYS)) + r')"')
_ESCAPED_PLAN_START = re.compile(r'\{\s*\\"(?:' + "|".join(sorted(_PLAN_KEYS)) + r')\\"')
# Bound on object starts tried when the answer wraps the JSON in prose.
_MAX_JSON_STARTS = 128
# Bound on syntax repairs per candidate object.
_MAX_JSON_REPAIRS = 12
_KEY_NAME = re.compile(r"[A-Za-z_]\w*")
# The keys GamePlan.is_empty() reads: an object with none of them is a draft ({"role": ...}).
_PLAN_CONTENT_KEYS = ("win_conditions", "win_condition", "path", "turns", "turn_plan", "develop_next")


def _has_plan_content(data: dict) -> bool:
    return any(data.get(key) for key in _PLAN_CONTENT_KEYS)


def _string_open(text: str, close: int) -> int | None:
    """Index of the quote that opens the JSON string closed at ``close`` (None if unbalanced)."""
    i = close
    while True:
        i = text.rfind('"', 0, i)
        if i == -1:
            return None
        slashes = i - len(text[:i].rstrip("\\"))
        if slashes % 2 == 0:
            return i


def _repair_at(text: str, error: json.JSONDecodeError) -> str | None:
    """``text`` with the syntax slip at ``error.pos`` fixed, or None when it is not one we fix.

    The slips GLM made in the 2026-10-18/19 plan answers (Langfuse; issue #114):
    a bare string member (``"hold": "x", ""}``), a key missing its opening
    quote (``resource_priorities": [``), a key name with no value
    (``resource_priorities,``) and trailing commas. Anything else (an
    unquoted value, prose inside the object, a cut-off answer) stays unusable.
    """
    pos, msg = error.pos, error.msg
    if msg.startswith("Illegal trailing comma"):  # Python 3.13+: pos is the comma
        return text[:pos] + text[pos + 1 :]
    head = text[:pos].rstrip()
    if text.startswith(("}", "]"), pos) and head.endswith(","):  # a trailing comma (3.12-)
        return head[:-1] + text[len(head) :]
    if msg.startswith("Expecting property name"):
        name = _KEY_NAME.match(text, pos)
        if name is None:
            return None
        if text.startswith('"', name.end()):  # half-quoted key
            return text[:pos] + '"' + text[pos:]
        rest = text[name.end() :].lstrip()
        rest_at = len(text) - len(rest)
        if rest.startswith(":"):  # unquoted key
            return text[:pos] + '"' + name.group() + '"' + text[name.end() :]
        if rest.startswith(","):  # a key name with no value: drop it and its comma
            return text[:pos] + text[rest_at + 1 :]
        if rest.startswith("}"):
            return text[:pos] + text[rest_at:]
        return None
    if msg.startswith("Expecting value") and text[:pos].rstrip().endswith(("[", ",")):
        # An unquoted list item (2026-10-19 16:21:40: '"assumptions": [ "...",
        # no instant trick or fog disrupts the committed T13 swing, Thalia blocks
        # at most one attacker ]'): quote the rest of its line, keeping its comma.
        if _KEY_NAME.match(text, pos) is None:
            return None
        eol = text.find("\n", pos)
        eol = len(text) if eol == -1 else eol
        line = text[pos:eol]
        cut = len(line)
        for stop in ("]", "}"):
            at = line.find(stop)
            if at != -1:
                cut = min(cut, at)
        body = line[:cut].rstrip()
        comma = body.endswith(",")
        body = body[:-1].rstrip() if comma else body
        if not body:
            return None
        return text[:pos] + json.dumps(body) + ("," if comma else "") + line[cut:] + text[eol:]
    if msg.startswith("Expecting ':' delimiter"):
        # A bare string where a key belongs: drop it and the comma before it.
        close = len(head) - 1
        opening = _string_open(text, close) if close >= 0 and text[close] == '"' else None
        before = text[:opening].rstrip() if opening is not None else ""
        if before.endswith(","):
            return before[:-1] + text[close + 1 :]
    return None


def _repaired_object(text: str) -> tuple[dict, str] | None:
    """The object at the start of ``text`` after up to ``_MAX_JSON_REPAIRS`` syntax repairs."""
    decoder = json.JSONDecoder()
    for _ in range(_MAX_JSON_REPAIRS + 1):
        try:
            data, size = decoder.raw_decode(text)
        except json.JSONDecodeError as error:
            repaired = _repair_at(text, error)
            if repaired is None or repaired == text:
                return None
            text = repaired
            continue
        return (data, text[:size]) if isinstance(data, dict) else None
    return None


def _plan_object(text: str) -> tuple[dict, str] | None:
    """The plan's JSON object in a model answer, and its source text (None without one).

    The outermost '{...}' is tried first, as before. Failing that (prose
    around the JSON with mana symbols like "{1}{R}", a second object after it,
    trailing commas before a newline), each object start is decoded in turn and
    the first object with a plan key wins, so a turn step nested in a broken
    outer object is never mistaken for the plan. The whole answer is searched:
    GLM often abandons a broken fenced draft and writes the plan again below
    it ("Wait — I need to output valid JSON", issue #114). Only when no object
    decodes is a plan-shaped object repaired (``_repair_at``).
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    blob = text[start : end + 1]
    for candidate in (blob, _TRAILING_COMMA.sub(r"\1", blob)):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data, blob
        break
    decoder = json.JSONDecoder()
    starts = [match.start() for match in _OBJECT_START.finditer(text, start)][:_MAX_JSON_STARTS]
    # A draft with a plan key but no plan in it ({"role": "race"}) is the
    # answer only when nothing better follows.
    fallback = None
    for start in starts:
        tail = text[start:]
        for candidate in (tail, _TRAILING_COMMA.sub(r"\1", tail)):
            try:
                data, size = decoder.raw_decode(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and _PLAN_KEYS.intersection(data):
                if _has_plan_content(data):
                    return data, candidate[:size]
                fallback = fallback or (data, candidate[:size])
            break
    tails = [text[start:] for start in starts if _PLAN_START.match(text, start)]
    tails += [
        text[match.start() :].replace('\\"', '"')
        for match in _ESCAPED_PLAN_START.finditer(text, start)
    ][:_MAX_JSON_STARTS]
    for tail in tails:
        found = _repaired_object(tail)
        if found is not None and _has_plan_content(found[0]):
            return found
    return fallback


# Full text of the newest unusable game-plan answers (the log keeps 300
# characters; #114 could only be diagnosed from the gateway's trace store).
UNUSABLE_PLAN_DIR = Path.home() / ".arenamcp" / "debug"
UNUSABLE_PLAN_KEEP = 20
_UNUSABLE_PLAN_PREFIX = "game_plan_unusable_"


def save_unusable_answer(text: str, *, turn_num: int, effort: str) -> Path | None:
    """Write an unusable plan answer in full under ``UNUSABLE_PLAN_DIR``, keeping the newest 20.

    Returns the file, or None when it could not be written (never raises).
    """
    now = time.time()
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + f"_{int(now * 1000) % 1000:03d}"
    try:
        UNUSABLE_PLAN_DIR.mkdir(parents=True, exist_ok=True)
        path = UNUSABLE_PLAN_DIR / f"{_UNUSABLE_PLAN_PREFIX}{stamp}.txt"
        header = (
            f"# game-plan answer with no usable plan: turn {turn_num}, {effort} effort, "
            f"{len(text)} chars, {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\n"
        )
        path.write_text(header + text, encoding="utf-8")
        for stale in sorted(UNUSABLE_PLAN_DIR.glob(f"{_UNUSABLE_PLAN_PREFIX}*.txt"))[:-UNUSABLE_PLAN_KEEP]:
            stale.unlink(missing_ok=True)
        return path
    except OSError as error:
        logger.debug("could not save the unusable game-plan answer: %s", error)
        return None


@contextlib.contextmanager
def critical_decision_call(active: bool = True):
    """Mark a high-effort decision call in flight; no new game-plan reform starts meanwhile."""
    global _critical_inflight
    if not active:
        yield
        return
    with _CRITICAL_LOCK:
        _critical_inflight += 1
    try:
        yield
    finally:
        with _CRITICAL_LOCK:
            _critical_inflight -= 1


def critical_decision_call_inflight() -> bool:
    with _CRITICAL_LOCK:
        return _critical_inflight > 0


def _decision_pending(game_state: dict[str, Any]) -> bool:
    """A decision (log or bridge) is waiting on us in this snapshot."""
    return bool(
        game_state.get("pending_decision")
        or game_state.get("_bridge_request_type")
        or game_state.get("_bridge_request_class")
    )


class GamePlanManager:
    """Owns the current :class:`GamePlan` and decides when to (re)form it.

    Cadence (2026-10-06: 139 plan calls in 1.8 h, 16 turns with 2-4 calls,
    because hand/graveyard/exile identities changed after nearly every action):
    plan our coming turn once during the opponent's turn, and re-form during a
    turn only when the board math flips the role or a lethal flag, the
    commander zone changes, the opponent adds a threat the plan was not
    written from (once per turn), the plan stalls, or no plan exists for this
    turn. Routine reforms wait for a moment with no decision pending.
    Background requests are rate-limited, one at a time process-wide
    (:data:`STRATEGIC_LANE`), and a request for a turn that has already passed
    is cancelled and its plan discarded.
    """

    # Force a refresh at least this often even if the board looks static, so a
    # long grind doesn't run forever on a turn-2 plan.
    _STALE_TURNS = 4
    _REFRESH_INTERVAL_S = 15.0

    # After this many consecutive stalls on plan-advancing plays, force a
    # reform and tell the model its current line is unexecutable so it picks a
    # different one (fixes the write-only plan that re-emitted "Cast Rush of
    # Dread" for five turns while the executor never landed it).
    _STALL_REFORM_THRESHOLD = 3

    # Reform reasons that can wait until no decision is pending, so the plan
    # call does not share the server with the decision call it would delay.
    _ROUTINE_REASONS = frozenset(
        {
            "plan our next turn",
            "stale plan",
            "deck playbook changed",
            "new opposing permanent",
            "new opposing threat",
            "resumed after a decision",
        }
    )
    # The reforms that may start during our own turn: without any plan, one is
    # worth more than one faster decision (2026-10-10 Brawl game); nor is a plan
    # whose attacker has died one (ledger #92).
    _OUR_TURN_REASONS = frozenset({"first plan", "planned attacker gone"})
    # A board-math role change between these two alone, with the lethal and
    # dead-soon flags unchanged, is noise (2026-10-07 replay: turn 6 went
    # aggressor -> race -> defender and turn 7 defender -> race on ordinary
    # snapshots; 38% of plan calls were such flips).
    _ROLE_NOISE = frozenset({"race", "defender"})

    # The strategic call runs in the background, at most once per turn side,
    # with a larger token/time budget than per-decision calls (12 s, 2048
    # tokens): 3000 tokens, 75 s, at HIGH reasoning effort.
    # 2026-10-06 17:45: unrestricted thinking on a ~34k-char prompt ran past 45 s
    # ("LLM streaming time budget exhausted") on the then single-lane gateway,
    # so the plan ran at low effort. 2026-10-10: the server has four slots and
    # priority scheduling, the per-decision calls stay at low effort, and the
    # prior plan keeps serving decisions while a slow reform runs, so this is
    # the one call that gets to think. Watch background.game_plan metrics
    # (reasoning_tokens, total_ms, status) after the change.
    # 2026-10-10 15:28: at high effort a stabilise-or-die board spent all
    # 3000 tokens reasoning (finish_reason=length, no plan). The JSON needs
    # ~800; the 75 s budget, not the cap, bounds a long think.
    _PLAN_MAX_TOKENS = 6000
    _PLAN_TIMEOUT_S = 75.0
    _PLAN_REASONING_EFFORT = "high"
    # A reform younger than this yields to a decision call; an older one finishes.
    _YIELD_GRACE_S = 4.0
    # With no plan at all, a reform outlives its turn up to this long before it
    # is superseded (a normal first plan on a big Brawl prompt takes ~35 s).
    _FIRST_PLAN_PATIENCE_S = 45.0
    # In-flight reforms cancelled for an opposing threat, per turn (see
    # _supersede_for_threat_locked; the later ones only re-aim a fresh call).
    _MAX_THREAT_SUPERSEDES = 3

    def set_recent_actions_source(self, source: Callable[[], str] | None) -> None:
        """Where the reform reads what we did since the last plan (ActionPlanner.recent_decisions_text)."""
        self._recent_actions_source = source

    def set_playbook_source(self, source: Callable[[], Any] | None) -> None:
        """Where the reform reads the DeckPlaybook object (to render it without the rules the deck reference quotes)."""
        self._playbook_source = source

    def _seed_text(self, game_state: dict[str, Any]) -> str | None:
        """The playbook text for this reform: rendered without the rules already in the deck reference."""
        source = getattr(self, "_playbook_source", None)
        if callable(source):
            try:
                playbook = source()
                if playbook is not None and hasattr(playbook, "render"):
                    quoted = game_state.get("deck_rules_grp_ids") or ()
                    return str(playbook.render(omit_oracle_for=quoted)).strip() or None
            except Exception as error:  # the seed string stands in
                logger.debug("playbook source failed: %s", error)
        return self._seed

    def yield_for_decision(self) -> None:
        """Cancel an in-flight reform so a per-decision call has the server to itself.

        The reform is re-requested at the next quiet moment ("resumed after a
        decision"), from the newer board. 2026-10-10 15:28: a 23 s high-effort
        reform overlapped the declare-blockers call, which then ran out its
        15 s budget and fell back to the safe-default blocks.
        """
        with self._lock:
            if not self._inflight or self._inflight_cancel is None or self._inflight_cancel.is_set():
                return
            # 2026-10-10 Brawl game: decisions came every few seconds on our turn
            # and every reform was cancelled before finishing (no plan by turn
            # 10). The first plan of a game is never cancelled: a plan is worth
            # more than one faster decision. With a plan in hand, a reform that
            # has already run a while is left to finish; only a fresh one steps
            # aside.
            if self._plan is None:
                logger.info("Game plan reform keeps running alongside a decision call: no plan exists yet")
                return
            # 2026-10-19 match 41753907: the reform started at 09:02:24 for
            # Unflinching Courage (Dawnwing Marshal now 4/4 flying, trample,
            # lifelink) yielded to our first T7 decision 3 s later; the resume
            # waited for the opponent's turn, so T7 ran on a plan that said
            # "swing 4 unblocked". A reform whose snapshot has a threat the
            # current plan was not written from replaces a stale plan: it runs on.
            newer = board_changes(self._plan.basis, getattr(self, "_inflight_basis", None), threats_only=True)
            gone = plan_attackers_gone(self._plan, basis=getattr(self, "_inflight_basis", None) or None)
            if gone:
                newer = [*newer, f"{' and '.join(gone)} left our battlefield"]
            if newer:
                logger.info(
                    "Game plan reform keeps running alongside a decision call: the current plan predates %s",
                    "; ".join(newer),
                )
                return
            started = getattr(self, "_inflight_started_at", None)
            running = time.monotonic() - float(started) if started else 0.0
            if running > self._YIELD_GRACE_S:
                logger.info("Game plan reform keeps running (%.0fs in) alongside a decision call", running)
                return
            logger.info("Game plan reform yields to a decision call; it will be re-requested afterwards")
            self._plan_cancels += 1
            self._plan_outcome = "yielded to a decision"
            self._abandon_inflight_locked()
            self._resume_after_yield = True
            # Let the re-request happen as soon as no decision is pending.
            self._last_attempt_at = None

    def set_turn_decisions_source(self, source: Callable[[int], list[str]] | None) -> None:
        """Where the ledger reads one turn's decisions (ActionPlanner.decisions_for_turn)."""
        self._turn_decisions_source = source

    def ledger_payload(self) -> dict[str, Any]:
        """The ledger for bug reports: the rendered block plus per-turn entries."""
        return {
            "block": self._ledger.prompt_block(),
            "lessons": self._ledger.lessons(),
            "turns": [
                {
                    "turn": e.turn,
                    "ours": e.ours,
                    "role": e.role,
                    "plan_formed": e.plan_formed,
                    "planned": e.planned,
                    "did": list(e.did),
                    "start": dict(e.start),
                    "end": dict(e.end),
                }
                for e in self._ledger.entries
            ],
        }

    @staticmethod
    def _is_mid_turn_start(game_state: dict[str, Any], turn: int) -> bool:
        """True when ``turn`` is first observed after a mid-turn reload (combat/post-combat or turn plays already made)."""
        if turn <= 1 or not isinstance(game_state, dict):
            return False
        turn_info = game_state.get("turn") or {}
        phase = str(turn_info.get("phase") or "")
        step = str(turn_info.get("step") or "")
        if phase in ("Phase_Combat", "Combat", "Phase_Main2", "Main2", "Phase_Ending", "Ending"):
            return True
        if step in (
            "Step_BeginCombat",
            "Step_DeclareAttack",
            "Step_DeclareBlock",
            "Step_CombatDamage",
            "Step_EndCombat",
            "Step_End",
            "Step_Cleanup",
            "BeginCombat",
            "DeclareAttack",
            "DeclareBlock",
            "CombatDamage",
            "EndCombat",
            "End",
        ):
            return True
        if int(turn_info.get("lands_played_this_turn") or turn_info.get("land_played_this_turn") or 0) > 0:
            return True
        if any(
            isinstance(a, dict) and int(a.get("turn") or 0) == turn
            for a in (game_state.get("action_history") or [])
        ):
            return True
        if any(
            isinstance(d, dict) and int(d.get("turn") or 0) == turn
            for d in (game_state.get("damage_events") or [])
        ):
            return True
        active = turn_info.get("active_player")
        if active is not None and any(
            isinstance(c, dict)
            and c.get("controller_seat_id") == active
            and (c.get("is_attacking") or c.get("is_tapped"))
            for c in (game_state.get("battlefield") or [])
        ):
            return True
        return False

    def _ledger_turn_started(self, game_state: dict[str, Any], turn: int) -> None:
        """Close the previous turn and open ``turn`` in the ledger (caller holds the lock; never raises)."""
        try:
            from arenamcp.board_assessment import assess
            from arenamcp.plan_ledger import snapshot_from_assessment

            assessment = assess(game_state)
            mid_turn = not self._ledger.entries and self._is_mid_turn_start(game_state, turn)
            self._ledger.turn_started(
                turn,
                ours=bool(getattr(assessment, "our_turn", self._our_turn(game_state))),
                snapshot=snapshot_from_assessment(assessment),
                role=str(getattr(assessment, "role", "") or ""),
                plan=self._plan,
                decisions_for_turn=self._turn_decisions_source,
                mid_turn=mid_turn,
            )
        except Exception as error:
            logger.debug("match ledger skipped for turn %s: %s", turn, error)

    _CALL_CLASS = "background.game_plan"

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
        # The match ledger (plan_ledger.py): per-turn plan step, decisions and
        # board swing, fed to every reform. Decisions come from the planner
        # through set_turn_decisions_source.
        self._ledger = PlanLedger()
        self._turn_decisions_source: Callable[[int], list[str]] | None = None
        # Plan status for the sidebar and the log: what the last reform attempt
        # did ("ok", "yielded to a decision", "superseded", "no usable plan",
        # "call failed", "arrived late"), how many attempts and cancels this
        # game, and the turn the missing-plan warning was last logged for.
        self._plan_outcome = ""
        self._plan_attempts = 0
        self._plan_cancels = 0
        self._no_plan_warned_turn = 0
        # Why the last _reform returned no plan ("" when cancelled or ok), and,
        # while a plan is kept after a failed reform, (turn of the failure,
        # why): decision prompts then flag the kept plan as not refreshed.
        self._reform_failure = ""
        self._refresh_failed: tuple[int, str] | None = None
        self._last_seed: str | None = None
        self._inflight = False
        self._last_attempt_at: float | None = None
        # Board-math role/lethal flags and commander zone at the last reform
        # (see _strategic_key); a change re-forms the plan mid-turn.
        self._last_key: tuple | None = None
        # The in-flight background reform: its cancel event, its strategic-lane
        # token and the turn its plan is for ("T"). Cancelled once that turn
        # has passed.
        self._inflight_cancel: threading.Event | None = None
        self._inflight_lane: object | None = None
        self._inflight_plan_turn = 0
        # The opposing board (board_basis) in the in-flight reform's snapshot and
        # at the last observe. A threat the in-flight snapshot lacks supersedes
        # it on the opponent's turn; one it has that the current plan lacks keeps
        # it running alongside decisions. One threat-driven reform per turn (a
        # fresh one may be re-aimed at a burst: (turn, supersedes that turn)).
        self._inflight_basis: dict = {}
        self._live_basis: dict = {}
        self._threat_turn = 0
        self._threat_superseded_turn = 0
        self._threat_supersedes: tuple[int, int] = (0, 0)
        self._consecutive_supersedes = 0
        # The opponent-trick table service (None: TrickTableService.shared();
        # tests inject their own) and its telemetry: the last 'Trick risk'
        # line's (match, turn, step), that turn's predicted p_any, and the
        # stack/graveyard ids at the last observe, for 'Trick observed'.
        self.trick_service: Any = None
        self._trick_logged: tuple | None = None
        self._trick_predicted: tuple | None = None
        self._trick_seen: tuple | None = None

    # ----- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        """Clear all per-game state (call at the start of a new match)."""
        with self._lock:
            self._abandon_inflight_locked()
            self._generation += 1
            self._last_key = None
            self._plan = None
            self._seed = None
            self._last_seed = None
            self._last_sig = None
            self._last_reform_turn = -1
            self._stall_count = 0
            self._stall_hint = ""
            self._match_id = None
            self._observed_turn = 0
            self._ledger.reset()
            self._plan_outcome = ""
            self._plan_attempts = 0
            self._plan_cancels = 0
            self._no_plan_warned_turn = 0
            self._refresh_failed = None
            self._last_attempt_at = None
            self._inflight_basis = {}
            self._live_basis = {}
            self._threat_turn = 0
            self._threat_superseded_turn = 0
            self._threat_supersedes = (0, 0)
            self._consecutive_supersedes = 0
            self._trick_logged = None
            self._trick_predicted = None
            self._trick_seen = None

    def observe(self, game_state: dict[str, Any]) -> None:
        """Invalidate old-match plans before any tactical prompt can use them.

        Also starts loading the match's opponent-trick table (non-blocking)
        and writes the trick telemetry (:meth:`_observe_tricks`).
        """
        match_id = game_state.get("match_id")
        turn = _round((game_state.get("turn") or {}).get("turn_number"))
        with self._lock:
            if (match_id and self._match_id and match_id != self._match_id) or (
                0 < turn < self._observed_turn
            ):
                self.reset()
            if match_id:
                self._match_id = match_id
            if turn > self._observed_turn:
                self._ledger_turn_started(game_state, turn)
            self._observed_turn = max(turn, self._observed_turn)
            try:
                self._live_basis = board_basis(game_state)
            except Exception as error:  # the board comparison is advisory
                logger.debug("board basis unavailable: %s", error)
            self._observe_tricks(game_state, turn)

    def _tricks(self) -> Any:
        """The opponent-trick table service, or None when it can't be had."""
        if self.trick_service is not None:
            return self.trick_service
        try:
            from arenamcp.opponent_tricks import TrickTableService

            return TrickTableService.shared()
        except Exception as error:
            logger.debug("trick table service unavailable: %s", error)
            return None

    def _observe_tricks(self, game_state: dict[str, Any], turn: int) -> None:
        """Trick telemetry, advisory only (caller holds the lock; never raises).

        * ``ensure_for_state``: loads or builds the set's table off-thread.
        * 'Trick risk (turn N, step): p_any=..., verdict=...' once per turn and
          step, at DeclareAttack and DeclareBlock.
        * 'Trick observed: <name> (predicted p_any=...)' when the opponent's
          stack (or, after a combat snapshot this turn, graveyard) gains an
          instant-speed card during combat.
        """
        try:
            service = self._tricks()
            if service is None or not isinstance(game_state, dict):
                return
            try:
                service.ensure_for_state(game_state)
            except Exception as error:  # a loader failure must not stop the telemetry
                logger.debug("trick table loading skipped: %s", error)
            from arenamcp.board_model import canonical_phase_step
            from arenamcp.opponent_tricks import observed_tricks

            phase, step = canonical_phase_step(game_state.get("turn") or {})
            match = self._match_id
            seen = self._trick_seen
            if phase == "Phase_Combat" and seen is not None and seen[0] == (match, turn):
                # Before this turn's first combat snapshot, only what is on the stack now is new in combat.
                current = game_state if seen[1] == "Phase_Combat" else dict(game_state, graveyard=[])
                predicted = self._trick_predicted
                p_any = f"{predicted[2]:.3f}" if predicted and predicted[:2] == (match, turn) else "n/a"
                for name in observed_tricks(seen[2], current):
                    logger.info("Trick observed: %s (predicted p_any=%s)", name, p_any)
            self._trick_seen = (
                (match, turn),
                phase,
                {
                    zone: [
                        {"instance_id": c.get("instance_id"), "name": c.get("name")}
                        for c in game_state.get(zone) or []
                        if isinstance(c, dict)
                    ]
                    for zone in ("stack", "graveyard")
                },
            )
            logged = (match, turn, step)
            if step not in ("Step_DeclareAttack", "Step_DeclareBlock") or self._trick_logged == logged:
                return
            risk = opponent_interaction(game_state, service)
            if risk is None or not risk.known:
                return
            self._trick_logged = logged
            self._trick_predicted = (match, turn, risk.p_any)
            logger.info(
                "Trick risk (turn %d, %s): p_any=%.3f, verdict=%s | %s",
                turn,
                step.removeprefix("Step_"),
                risk.p_any,
                risk.verdict_line(),
                risk.summary(),
            )
        except Exception as error:
            logger.debug("trick telemetry skipped: %s", error)

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
        """Planner-prompt block for the current plan ("" if none yet), flagged when its re-plan failed."""
        plan = self._plan
        if not plan:
            return ""
        refresh = plan_refresh_note(plan, getattr(self, "_refresh_failed", None))
        return plan.as_planner_block() + (f"\n{refresh}" if refresh else "")

    def coach_intro(self) -> str:
        plan = self._plan
        return plan.as_coach_intro() if plan else ""

    def strategy_block(self, game_state: dict[str, Any] | None, *, with_lines: bool = True) -> str:
        """Fresh ROLE + this turn + facts for ``game_state``, then the plan spine.

        Adds the opponent's instant-speed interaction verdict (no card names)
        when the match's trick table is loaded. ``with_lines=False`` leaves out
        the search's LINES line (the typed decision prompt shows it above its
        options; the coach prompt keeps it).
        """
        from arenamcp.board_assessment import assess

        plan = self._plan
        state = game_state if isinstance(game_state, dict) else None
        assessment = assess(state) if state is not None else None
        risk = opponent_interaction(state, self._tricks()) if state is not None else None
        verdict = risk.verdict_line() if risk is not None and risk.known else ""
        extra = f"  OPP INTERACTION: {verdict}" if verdict else ""
        block = compose_strategy_block(
            assessment,
            plan,
            extra_facts=extra,
            state=state,
            with_lines=with_lines,
            refresh_failed=getattr(self, "_refresh_failed", None),
        )
        # The ledger's deterministic lessons reach every tactical prompt at
        # once, not only after the next reform echoes them into the plan.
        lessons = [item for item in self._ledger.lessons() if item not in (plan.lessons if plan else ())]
        if lessons:
            block += "\n" + "\n".join(f"  Ledger this match: {item}" for item in lessons)
        return block

    def plan_status(self, game_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Whether a plan exists, how old it is, and what the last reform attempt did (for the sidebar)."""
        turn = (
            _round(((game_state or {}).get("turn") or {}).get("turn_number"))
            if isinstance(game_state, dict)
            else 0
        )
        with self._lock:
            plan = self._plan
            inflight = bool(self._inflight)
            started = getattr(self, "_inflight_started_at", None)
            running = round(time.monotonic() - float(started)) if inflight and started else 0
            status: dict[str, Any] = {
                "state": "ready" if plan is not None else ("forming" if inflight else "none"),
                "turn_formed": plan.turn_formed if plan is not None else 0,
                "age_turns": max(0, turn - plan.turn_formed) if plan is not None and turn else 0,
                "inflight": inflight,
                "inflight_s": running,
                "inflight_for_turn": self._inflight_plan_turn if inflight else 0,
                "last_outcome": self._plan_outcome,
                "attempts": self._plan_attempts,
                "cancels": self._plan_cancels,
                "effort": plan.effort if plan is not None else "",
            }
            if plan is None and turn >= 3 and self._no_plan_warned_turn != turn:
                self._no_plan_warned_turn = turn
                logger.warning(
                    "No game plan on turn %d (%d attempts, %d cancelled; last: %s; reform %s)",
                    turn,
                    self._plan_attempts,
                    self._plan_cancels,
                    self._plan_outcome or "never attempted",
                    f"running {running}s" if inflight else "not running",
                )
        return status

    def ui_payload(self, game_state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Plan payload for the desktop plan card, with current board facts.

        Facts (role, clocks, flags, board-math lookahead) are recomputed from
        ``game_state`` so the card shows "behind on board" before the first
        model plan arrives; ``{}`` when there is neither a plan nor a board.
        """
        payload: dict[str, Any] = dict(self._plan.as_payload()) if self._plan else {}
        payload["plan_status"] = self.plan_status(game_state)
        if isinstance(game_state, dict):
            try:
                from arenamcp.board_assessment import assess

                assessment = assess(game_state)
            except Exception as error:
                logger.debug("game-plan facts unavailable: %s", error)
                assessment = None
            if assessment is not None:
                payload["facts"] = assessment.as_payload()
                try:
                    from arenamcp.outlook import outlook

                    now = outlook(assessment)
                    trend = self._ledger.outlook_trend()
                    if now:
                        payload["outlook"] = {**now, "trend": trend[-6:]}
                except Exception as error:
                    logger.debug("outlook unavailable: %s", error)
                payload.setdefault("role", assessment.role)
                if not payload.get("role"):
                    payload["role"] = assessment.role
                if not payload.get("role_reason"):
                    payload["role_reason"] = assessment.role_reason
                # The trick estimate for the card (card names allowed there).
                risk = opponent_interaction(game_state, self._tricks())
                if risk is not None and risk.known:
                    payload["opp_interaction"] = risk.as_payload()
        entries = self._ledger.entries
        if entries:
            payload["ledger"] = {
                "lessons": self._ledger.lessons(),
                "turns": [e.line().strip().lstrip("- ").strip() for e in entries[-4:]],
            }
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
        Nothing is requested while the model server's breaker is open (the
        prior plan stays; the next call after it closes re-requests), while
        another background job holds :data:`STRATEGIC_LANE`, or, for routine
        reforms, while a decision is pending. A reform still in flight for a
        turn that has passed is cancelled, and so, on the opponent's turn, is
        one whose snapshot misses a threat they have added since (re-requested
        from the current board; one threat-driven reform per turn).
        """
        with self._lock:
            self.observe(game_state)
            if self._inflight:
                self._supersede_stale_inflight_locked(game_state)
                if self._inflight:
                    self._supersede_for_threat_locked(game_state)
                if self._inflight:
                    return False
            suspended = getattr(self, "background_suspended_fn", None)
            if callable(suspended) and suspended():
                return False
            if critical_decision_call_inflight():
                # Re-requested at the next window: nothing was attempted.
                logger.debug("Game plan reform waits: a critical decision call is in flight")
                return False
            if game_state.get("stack") or game_state.get("game_over"):
                return False
            if not _round((game_state.get("turn") or {}).get("turn_number")):
                return False
            now = time.monotonic()
            if self._last_attempt_at is not None and now - self._last_attempt_at < self._REFRESH_INTERVAL_S:
                return False
            sig = self._signature(game_state)
            our_turn = self._our_turn(game_state)
            if self._stall_count >= self._STALL_REFORM_THRESHOLD:
                reason = "plan stalled"
            else:
                reason = self._should_reform(
                    sig, our_turn=our_turn, key=self._strategic_key(game_state, sig), board=self._live_basis
                )
            if not reason:
                return False
            if reason == "new opposing threat" and self._threat_turn == sig[0]:
                return False  # one threat-driven reform per turn
            if (
                our_turn
                and reason not in self._OUR_TURN_REASONS
                and not (
                    reason == "new opposing threat"
                    and plan_removal_target_predates_threat(self._plan, self._live_basis, sig[0])
                )
            ):
                # 2026-10-11 audit: 72 of 168 plan calls were cancelled or
                # discarded, and decision calls overlapping one had ~2x TTFT
                # (1.1 s -> 1.9 s). A mid-turn reform replans a turn that is
                # mostly played by the time a ~20 s high-effort call returns;
                # every decision prompt already re-renders the live board
                # facts. Wait for the opponent's turn and plan our next turn
                # there ("plan our next turn" fires then), at full effort.
                logger.debug("Game plan reform (%s) deferred to the opponent's turn", reason)
                return False
            if reason in self._ROUTINE_REASONS and _decision_pending(game_state):
                return False
            if not background_llm_allowed(self._backend):
                logger.debug("Game plan reform (%s) skipped: model server unavailable", reason)
                return False
            token = STRATEGIC_LANE.try_acquire("game_plan")
            if token is None:
                logger.debug("Game plan reform (%s) waits: %s holds the lane", reason, STRATEGIC_LANE.holder)
                return False
            snapshot = deepcopy(game_state)
            generation = self._generation
            cancel = threading.Event()
            self._resume_after_yield = False
            self._inflight = True
            self._inflight_cancel = cancel
            self._inflight_lane = token
            self._inflight_plan_turn = self._plan_turn(snapshot)
            self._inflight_started_at = now
            self._inflight_basis = dict(self._live_basis)
            self._last_attempt_at = now
            self._plan_attempts += 1
            if reason == "new opposing threat" or self._threat_superseded_turn == sig[0]:
                self._threat_turn = sig[0]
                self._threat_superseded_turn = 0
            logger.info(
                "Game plan reform (%s): turn %d, plan for turn %d", reason, sig[0], self._inflight_plan_turn
            )

        def refresh() -> None:
            try:
                self.maybe_reform(snapshot, _expected_generation=generation, _cancel=cancel)
                with self._lock:
                    publish = generation == self._generation and not cancel.is_set()
                if publish and on_updated is not None:
                    on_updated()
            except Exception as error:
                logger.debug("background game-plan refresh failed: %s", error)
            finally:
                with self._lock:
                    # An abandoned reform (superseded or reset) already
                    # cleared these; a newer reform may own them by now.
                    if self._inflight_cancel is cancel:
                        self._inflight = False
                        self._inflight_cancel = None
                        self._inflight_lane = None
                STRATEGIC_LANE.release(token)

        try:
            threading.Thread(target=refresh, daemon=True, name="game-plan-reform").start()
        except Exception:
            with self._lock:
                self._inflight = False
                self._inflight_cancel = None
                self._inflight_lane = None
            STRATEGIC_LANE.release(token)
            raise
        return True

    def _sparse_late_hand(self, game_state: dict[str, Any], turn: int) -> bool:
        """True on turn 9+ when our hand has <=1 castable (non-land) card (#141, match d9a33232)."""
        if turn < 9 or not isinstance(game_state.get("hand"), list):
            return False
        if not (game_state.get("battlefield") or game_state.get("hand") or game_state.get("graveyard")):
            return False
        castable = sum(1 for c in _castable_cards(game_state) if not _is_hand_land(c))
        return castable <= 1

    def _low_effort_reform(self, game_state: dict[str, Any], turn_num: int) -> bool:
        """True when this reform should run at low effort (#141: first plan, after 2 supersedes in a row,
        or in the late game with <=1 castable card in hand)."""
        return (
            self._plan is None
            or self._consecutive_supersedes >= 2
            or self._sparse_late_hand(game_state, turn_num)
        )

    def _supersede_stale_inflight_locked(self, game_state: dict[str, Any]) -> None:
        """Cancel the in-flight reform once the turn it plans for has passed."""
        cancel = self._inflight_cancel
        turn = _round((game_state.get("turn") or {}).get("turn_number"))
        if cancel is None or cancel.is_set() or turn <= self._inflight_plan_turn:
            return
        started = getattr(self, "_inflight_started_at", None)
        running = time.monotonic() - float(started) if started else 0.0
        if (
            self._plan is None
            or self._consecutive_supersedes >= 2
            or (self._consecutive_supersedes >= 1 and self._sparse_late_hand(game_state, turn))
        ) and running <= self._FIRST_PLAN_PATIENCE_S:
            # 2026-10-10 Brawl, turn 20 after a restart: the first plan's call
            # (73k-character prompt) outlived the turn and was superseded, then
            # the next one too. With no plan at all (or after repeated late-game
            # supersedes, #141), a late plan is kept (see the apply path), so the
            # call runs on; only a request stuck past the patience bound is cancelled.
            logger.info(
                "Game plan reform for turn %d keeps running on turn %d (%.0fs in): %s",
                self._inflight_plan_turn,
                turn,
                running,
                "no plan exists yet"
                if self._plan is None
                else f"after {self._consecutive_supersedes} consecutive supersedes",
            )
            return
        logger.info(
            "Superseding the in-flight game plan for turn %d (now turn %d); its answer will be discarded",
            self._inflight_plan_turn,
            turn,
        )
        self._plan_cancels += 1
        self._consecutive_supersedes += 1
        self._plan_outcome = "superseded by a newer turn"
        self._abandon_inflight_locked()

    def _supersede_for_threat_locked(self, game_state: dict[str, Any]) -> None:
        """On the opponent's turn, cancel an in-flight reform whose snapshot misses a threat they added since.

        Match f9e0ec3b: the T10 plan requested at 09:18:55 was written before
        Lyra Dawnbringer resolved (09:19:08) and installed at 09:19:29 anyway.
        Its answer is known stale, so the call is cancelled and re-requested
        from the current board (the cooldown does not apply). Once per turn,
        except that a threat reform that has only just started is re-aimed at
        a burst of plays (match a51dea64: Valkyrie at 10:22:39, Ajani 3 s
        later), up to ``_MAX_THREAT_SUPERSEDES`` times. Never for the first
        plan (a quick low-effort call), never on our turn (a reform then would
        replan a turn that is mostly played; prompts flag the stale plan).
        """
        cancel = self._inflight_cancel
        turn = _round((game_state.get("turn") or {}).get("turn_number"))
        if cancel is None or cancel.is_set() or self._plan is None or self._our_turn(game_state):
            return
        if self._consecutive_supersedes >= 2 or (
            self._consecutive_supersedes >= 1 and self._sparse_late_hand(game_state, turn)
        ):
            return
        count = self._threat_supersedes[1] if self._threat_supersedes[0] == turn else 0
        if self._threat_turn == turn:
            started = getattr(self, "_inflight_started_at", None)
            running = time.monotonic() - float(started) if started else float("inf")
            if running > self._YIELD_GRACE_S or count >= self._MAX_THREAT_SUPERSEDES:
                return
        added = board_changes(self._inflight_basis, self._live_basis, threats_only=True)
        if not added:
            return
        self._threat_supersedes = (turn, count + 1)
        logger.info(
            "Superseding the in-flight game plan for turn %d: the opponent's board changed after its snapshot "
            "(%s); re-requesting from the current board",
            self._inflight_plan_turn,
            "; ".join(added),
        )
        self._plan_cancels += 1
        self._consecutive_supersedes += 1
        self._plan_outcome = "superseded by a board change"
        self._abandon_inflight_locked()
        self._threat_superseded_turn = turn
        self._last_attempt_at = None

    def _abandon_inflight_locked(self) -> None:
        """Cancel the in-flight reform and free its slot and the strategic lane now.

        The cancel event makes the proxy drop the request (socket shut down);
        the worker thread may still take a moment to unwind, so the manager
        and the lane are released here instead of in its ``finally`` (2026-10-07
        review: a superseded request queued on a busy server held both for up
        to its 75 s budget, so the plan for the new turn could not start).
        Its answer, if any, is discarded.
        """
        cancel = self._inflight_cancel
        if cancel is None:
            return
        cancel.set()
        lane = self._inflight_lane
        self._inflight = False
        self._inflight_cancel = None
        self._inflight_lane = None
        STRATEGIC_LANE.release(lane)

    def maybe_reform(
        self,
        game_state: dict[str, Any],
        *,
        force: bool = False,
        _expected_generation: int | None = None,
        _cancel: threading.Event | None = None,
    ) -> GamePlan | None:
        """(Re)form the plan iff the cadence calls for it; else return current.

        Cheap to call on every trigger — the LLM is only invoked when
        :meth:`_should_reform` names a reason. A plan whose request was
        cancelled, or whose turn has passed by the time it arrives, is
        discarded and the prior plan kept.
        """
        try:
            sig = self._signature(game_state)
            key = self._strategic_key(game_state, sig)
            board = board_basis(game_state)
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
            if not (
                force
                or stalled
                or self._should_reform(sig, our_turn=self._our_turn(game_state), key=key, board=board)
            ):
                return self._plan
            seed = self._seed
            stall_count = self._stall_count
            plan_turn = self._plan_turn(game_state)

        plan = self._reform(game_state, turn_num, cancel=_cancel)
        with self._lock:
            if generation != self._generation:
                return self._plan
            if _cancel is not None and _cancel.is_set():
                # Yielded or superseded: its replacement owns the status and the
                # stall count, and may already have landed.
                return self._plan
            if (
                plan is not None
                and self._observed_turn > plan_turn
                and self._plan is not None
                and self._consecutive_supersedes < 2
            ):
                logger.info(
                    "Discarded the game plan for turn %d: it arrived on turn %d",
                    plan_turn,
                    self._observed_turn,
                )
                self._plan_outcome = "arrived late"
                return self._plan
            if plan is not None and self._observed_turn > plan_turn:
                # 2026-10-10 Brawl: with no plan at all (or after 2+ consecutive
                # supersedes, #141), a late one still carries the win condition,
                # path and lessons; the validator's turn steps are re-grounded on
                # the next reform.
                logger.info(
                    "Keeping the late game plan for turn %d (arrived on turn %d): %s",
                    plan_turn,
                    self._observed_turn,
                    "there was no plan"
                    if self._plan is None
                    else f"after {self._consecutive_supersedes} consecutive supersedes",
                )
            if plan is None and not self._plan_outcome.startswith(("yielded", "superseded")):
                failure = getattr(self, "_reform_failure", "") or "no usable plan from the model"
                self._plan_outcome = failure
                if self._plan is not None:
                    # 2026-10-19 115944ce: the T6 decisions ran on a plan whose
                    # reform had failed, and nothing in their prompts said so.
                    self._refresh_failed = (turn_num, failure)
            if plan is not None:
                # Still the newest plan, but written before what changed since its
                # snapshot: prompts flag its lines; a threat re-plans on their turn.
                late = board_changes(plan.basis, self._live_basis)
                if late:
                    logger.info(
                        "GamePlan for turn %d predates changes on the opponent's board (%s); "
                        "prompts flag its stale lines",
                        plan_turn,
                        "; ".join(late),
                    )
                self._plan_outcome = "ok"
                self._plan = plan
                self._refresh_failed = None
                self._consecutive_supersedes = 0
                self._last_sig = sig
                self._last_key = key
                self._last_seed = seed
                self._last_reform_turn = turn_num
            self._stall_count = max(0, self._stall_count - stall_count)
            if not self._stall_count:
                self._stall_hint = ""
            return self._plan

    def _should_reform(
        self, sig: tuple, *, our_turn: bool = False, key: tuple | None = None, board: dict | None = None
    ) -> str:
        """Why the plan should be re-formed now ("" = keep it).

        Card identities, creature counts and life deltas are deliberately not
        reasons any more: they changed after nearly every action. The fresh
        board facts reach every decision prompt anyway (strategy_block); the
        plan itself only changes when the strategic picture flips, or when
        ``board`` (the live :func:`board_basis`) has an opposing threat the
        plan was not written from ("new opposing threat": a new evasive,
        lifelink or big creature, a lasting buff or keyword; 2026-10-19
        41753907 and f9e0ec3b, :func:`board_changes`). ``request_reform``
        allows that one once per turn.
        """
        if self._plan is None or self._last_sig is None:
            return "first plan"
        if board and plan_attackers_gone(self._plan, basis=board):
            return "planned attacker gone"
        if self._seed != self._last_seed:
            return "deck playbook changed"
        if getattr(self, "_resume_after_yield", False):
            return "resumed after a decision"
        turn_num = sig[0]
        if turn_num - self._last_reform_turn >= self._STALE_TURNS:
            return "stale plan"
        # Plan our coming turn during the opponent's turn, so it is ready when
        # our turn starts (a plan call takes ~30 s on a busy gateway).
        if not our_turn and turn_num > self._last_reform_turn:
            return "plan our next turn"
        # The opponent-turn plan never formed (failed, deferred or skipped).
        if our_turn and turn_num > self._last_reform_turn + 1:
            return "no plan for this turn"
        if key is not None and self._last_key is not None:
            reason = self._key_change_reason(self._last_key, key)
            if reason:
                return reason
        if board and board_changes(self._plan.basis, board, threats_only=True):
            return "new opposing threat"
        return ""

    def _key_change_reason(self, old: tuple, new: tuple) -> str:
        """Why the strategic key moved enough to re-form the plan ("" = it didn't)."""
        old_facts, new_facts = old[0], new[0]
        if old_facts is not None and new_facts is not None and old_facts != new_facts:
            if tuple(old_facts[1:]) != tuple(new_facts[1:]):
                return "role/lethal flip"
            if not {old_facts[0], new_facts[0]} <= self._ROLE_NOISE:
                return "role/lethal flip"
        if tuple(old[1:3]) != tuple(new[1:3]):
            return "commander change"
        old_engines = old[3] if len(old) > 3 else frozenset()
        new_engines = new[3] if len(new) > 3 else frozenset()
        if set(new_engines) - set(old_engines):
            return "new opposing permanent"
        return ""

    def _strategic_key(self, game_state: dict[str, Any], sig: tuple | None = None) -> tuple:
        """What must change for a mid-turn reform, whoever's turn and phase it is.

        * The board-math role and lethal/dead-soon flags, assessed on a copy
          set to the start of our own (next) turn (:meth:`_normalized_for_key`).
          On the raw snapshot, ``lethal_now`` needs our attack still pending
          and the role moves with the phase, so a key taken on their turn
          never matched one taken on ours: a "role/lethal flip" reform at
          nearly every turn change (2026-10-07 review: 6-8 plan calls over 6
          unchanged turns where 4 were due).
        * The commander zone and tax (rare, plan-changing).
        * The opponent's noncreature, nonland, nontoken permanents: a new
          engine or answer on their side can change the whole line (this used
          to be caught by the card-identity trigger).
        """
        sig = sig if sig is not None else self._signature(game_state)
        try:
            from arenamcp.board_assessment import assess

            assessment = assess(self._normalized_for_key(game_state))
        except Exception as error:  # a strategic-layer failure must not block planning
            logger.debug("board assessment unavailable for the plan cadence: %s", error)
            assessment = None
        facts = None
        if assessment is not None:
            dead_in = getattr(assessment, "dead_in", None)
            facts = (
                getattr(assessment, "role", ""),
                bool(
                    getattr(assessment, "lethal_now", False) or getattr(assessment, "lethal_next_turn", False)
                ),
                bool(getattr(assessment, "opp_lethal_on_board", False)),
                dead_in is not None and dead_in <= 2,
            )
        return (facts, sig[12], sig[13], self._opposing_engines(game_state))

    def _normalized_for_key(self, game_state: dict[str, Any]) -> dict[str, Any]:
        """A shallow copy at the start of our own turn: this one if ours, else our next.

        Our side is active in Main1 with nothing attacking or blocking and our
        permanents untapped, as after our untap step.
        """
        local = game_state.get("local_seat_id") or self._local_seat(game_state)
        if local is None:
            return game_state
        turn = dict(game_state.get("turn") or {})
        turn_number = _round(turn.get("turn_number"))
        if turn.get("active_player") != local and turn_number:
            turn_number += 1
        turn.update(
            turn_number=turn_number, active_player=local, priority_player=local, phase="Phase_Main1", step=""
        )
        battlefield = []
        for card in game_state.get("battlefield") or []:
            if isinstance(card, dict):
                ours = (card.get("controller_seat_id") or card.get("owner_seat_id")) == local
                card = dict(
                    card,
                    is_attacking=False,
                    is_blocking=False,
                    is_tapped=False if ours else card.get("is_tapped"),
                )
            battlefield.append(card)
        return dict(game_state, turn=turn, battlefield=battlefield, stack=[])

    def _opposing_engines(self, game_state: dict[str, Any]) -> frozenset:
        """Instance ids of the opponent's noncreature, nonland, nontoken permanents."""
        local = game_state.get("local_seat_id") or self._local_seat(game_state)
        if local is None:
            return frozenset()
        engines = set()
        for card in game_state.get("battlefield") or []:
            if not isinstance(card, dict):
                continue
            controller = card.get("controller_seat_id") or card.get("owner_seat_id")
            if controller is None or controller == local:
                continue
            if _is_engine(card):
                engines.add(str(card.get("instance_id") or card.get("name") or ""))
        return frozenset(engines)

    def _plan_turn(self, game_state: dict[str, Any]) -> int:
        """The turn a plan formed from ``game_state`` is for: this turn if ours, else our next."""
        turn = _round((game_state.get("turn") or {}).get("turn_number"))
        return turn if self._our_turn(game_state) else turn + 1

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

    def _reform(
        self, game_state: dict[str, Any], turn_num: int, *, cancel: threading.Event | None = None
    ) -> GamePlan | None:
        from arenamcp.board_assessment import assess
        from arenamcp.match_context import prepare_match_context, with_deck_reference

        # A plan spans turns: it lists the library's remaining cards (outs).
        game_state = prepare_match_context(game_state, library=True)
        context = self._build_context(game_state)
        assessment = assess(game_state)
        # Stable text first, for the server's prefix cache: system prompt,
        # deck reference (with_deck_reference) and the deck playbook, then the
        # volatile board.
        seed = self._seed_text(game_state)
        user_parts = [f"DECK PLAYBOOK / STRATEGY:\n{seed}\n"] if seed else []
        user_parts.append(context)
        if assessment is not None:
            logger.info(
                "Board facts (turn %d, %.1fms): %s | %s%s",
                turn_num,
                assessment.elapsed_ms,
                assessment.headline(),
                assessment.facts_line(),
                _line_telemetry(assessment),
            )
            when = "this turn" if assessment.our_turn else "our next turn"
            user_parts.append(
                "\nBOARD FACTS (deterministic; recomputed from the live board — treat as hard facts):\n"
                + assessment.planning_block()
                + f"\nT = turn {assessment.plan_turn} ({when}); T+1 = turn {assessment.plan_turn + 2}; "
                f"T+2 = turn {assessment.plan_turn + 4}. ASSESSED ROLE: {assessment.role}."
            )
        if self._plan:
            user_parts.append(self._plan.as_planner_block())
            stale = plan_board_note(self._plan, plan_board_changes(self._plan, game_state))
            if stale:
                user_parts.append(stale)
            losses = plan_card_losses(self._plan, game_state)
            if losses:
                user_parts.append(losses)
        recent = ""
        source = getattr(self, "_recent_actions_source", None)
        if callable(source):
            try:
                recent = str(source() or "")
            except Exception as error:  # the feed never blocks a reform
                logger.debug("recent actions unavailable: %s", error)
        if recent:
            user_parts.append(
                "\nWHAT WE DID SINCE THE LAST PLAN (our own decisions, oldest first; "
                "the plan must account for cards these spent):\n" + recent
            )
        ledger = self._ledger.prompt_block()
        if ledger:
            user_parts.append("\n" + ledger)
        if self._stall_count >= self._STALL_REFORM_THRESHOLD and self._stall_hint:
            user_parts.append(
                f"\nPRIOR PLAN STALLED: the previous plan-advancing play "
                f'"{self._stall_hint}" could NOT be executed across several '
                f"attempts. Do not rely on that line again — choose a DIFFERENT, "
                f"executable win condition / next play this time."
            )
        user_parts.append("\nForm the GAME PLAN as JSON now.")
        user_message = with_deck_reference("\n".join(user_parts), game_state)

        self._reform_failure = ""
        try:
            # 2026-10-10 Brawl: the first plan after a mid-game restart took 35 s
            # at high effort on a 21k-token prompt. With no plan at all, a quick
            # low-effort plan comes first; the next reform (their turn, a role
            # flip, staleness) runs at high effort as usual.
            effort = (
                "low" if self._low_effort_reform(game_state, turn_num) else self._PLAN_REASONING_EFFORT
            )
            if effort == "low":
                logger.info("Game plan: reform at low effort for speed")
            response = self._complete(GAME_PLAN_PROMPT, user_message, cancel=cancel, effort=effort)
        except Exception as e:
            logger.warning("game-plan LLM call failed (keeping prior plan): %s", e)
            self._reform_failure = "call failed"
            return None

        plan = self._usable_plan(response, turn_num, cancel=cancel, effort=effort)
        if plan is None and self._reform_failure == "no usable plan from the model":
            plan = self._retry_for_json(user_message, turn_num, cancel=cancel)
            if plan is not None:
                effort = "low"
        if plan is None:
            return None
        plan.effort = effort
        try:
            plan.basis = board_basis(game_state)
        except Exception as error:  # the board comparison is advisory
            logger.debug("board basis unavailable: %s", error)
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

    # 2026-10-19 ladder: "game-plan answer had no usable plan (keeping prior
    # plan)" 15 times in one session, with the answer never logged (10:56:57
    # match 15e0eb20: a 30.7 s high-effort call, 4233 reasoning tokens;
    # 11:11:47 match 115944ce). The answer's head goes to the log, and one
    # bounded retry at low effort (a few seconds) asks for just the JSON.
    _UNUSABLE_LOG_CHARS = 300
    _RETRY_NUDGE = (
        "\n\nYOUR PREVIOUS ANSWER WAS NOT A USABLE GAME PLAN (no JSON object with role, turns, "
        "win_conditions and path). Answer again with ONLY that JSON object: no prose, no markdown."
    )

    def _usable_plan(
        self, response: Any, turn_num: int, *, cancel: threading.Event | None, effort: str
    ) -> GamePlan | None:
        """The parsed plan in ``response``, or None with ``_reform_failure`` saying why (never raises)."""
        if cancel is not None and cancel.is_set():
            logger.info("Discarded a superseded game plan (turn %d); keeping the prior plan", turn_num)
            self._reform_failure = ""
            return None
        if is_skipped_call_text(response):
            # Skipped while the model server is down, or cancelled/dropped by
            # the background lane: nothing failed, the prior plan stays.
            logger.info("game-plan call not made (keeping prior plan): %s", str(response)[:160])
            self._reform_failure = "call not made (model server unavailable)"
            return None
        if is_backend_error_text(response):
            # The proxy returns an error sentinel instead of raising; on
            # 2026-10-06 five of seven plan calls timed out this way unlogged.
            logger.warning("game-plan LLM call failed (keeping prior plan): %s", str(response)[:160])
            self._reform_failure = "call failed"
            return None
        plan = self._parse(response, turn_num)
        if plan is None or plan.is_empty():
            text = response if isinstance(response, str) else repr(response)
            logger.warning(
                "game-plan answer had no usable plan (%s effort, %d chars; keeping prior plan): %r",
                effort,
                len(text),
                text[: self._UNUSABLE_LOG_CHARS],
            )
            saved = save_unusable_answer(text, turn_num=turn_num, effort=effort)
            if saved is not None:
                logger.info("game-plan: the full unusable answer is in %s", saved)
            self._reform_failure = "no usable plan from the model"
            return None
        self._reform_failure = ""
        return plan

    def _retry_for_json(
        self, user_message: str, turn_num: int, *, cancel: threading.Event | None
    ) -> GamePlan | None:
        """One low-effort retry after an unusable answer (none once cancelled or with the server down)."""
        if (cancel is not None and cancel.is_set()) or not background_llm_allowed(self._backend):
            return None
        logger.info("game-plan: asking once more, at low effort, for just the JSON plan (turn %d)", turn_num)
        try:
            response = self._complete(
                GAME_PLAN_PROMPT, user_message + self._RETRY_NUDGE, cancel=cancel, effort="low"
            )
        except Exception as e:
            logger.warning("game-plan retry call failed (keeping prior plan): %s", e)
            self._reform_failure = "call failed"
            return None
        plan = self._usable_plan(response, turn_num, cancel=cancel, effort="low")
        if plan is not None:
            logger.info("game-plan retry produced a usable plan (turn %d)", turn_num)
        return plan

    def _complete(
        self,
        system_prompt: str,
        user_message: str,
        *,
        cancel: threading.Event | None = None,
        effort: str | None = None,
    ) -> str:
        """Call the backend for a background strategy plan.

        Background strategy: high reasoning effort (the per-decision calls
        use low) with a larger token/time budget (_PLAN_MAX_TOKENS, 75 s).
        Labelled as background work at low scheduling priority, and
        cancellable once its turn has passed, where the backend supports
        those keywords.
        """
        labels = accepted_call_kwargs(
            self._backend, call_class=self._CALL_CLASS, priority=BACKGROUND_PRIORITY, cancel_event=cancel
        )
        return self._backend.complete(
            system_prompt,
            user_message,
            self._PLAN_MAX_TOKENS,
            temperature=0.0,
            request_timeout_s=self._timeout,
            background=True,
            reasoning_effort=effort or self._PLAN_REASONING_EFFORT,
            **labels,
        )

    @staticmethod
    def _parse(response: str, turn_num: int) -> GamePlan | None:
        if not response or not isinstance(response, str):
            return None
        text = response.strip()
        if is_backend_error_text(text):
            return None
        # The whole answer, fences and all: the plan is not always in the first
        # fenced block (a broken draft, then the plan again below it; #114).
        found = _plan_object(text)
        if found is None:
            return None
        data, blob = found

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
            lessons=items("lessons")[:4],
        )
