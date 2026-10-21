"""Shadow-first guards, option tags and a fallback pick from the line search.

``line_search`` compares our candidate lines; this module turns its result into
four narrow, deterministic uses at decision time:

- ``line_guard``: an ActionsAvailable pick whose best line is strictly worse
  (outcome class or death timing, or by ``LINE_GUARD_MARGIN`` in value when we
  are surviving) than the search's best line gets a replacement from that line:
  removal on an attacker, then a creature, then the land that makes the best
  turn castable.
- ``mode_guard``: a modal CastingTimeOptions pick ('choose one') whose mode
  dies sooner than another mode (``line_search.compare_modes``).
- ``option_note`` / ``lines_summary``: short '[LINE ...]' option tags and the
  'LINES ...' prompt line.
- ``line_fallback_pick``: the best line's first action when the model gave no
  usable answer.
- ``attack_line_guard`` / ``attack_line_note`` (ledger #152): declared
  attackers the best surviving line keeps home, when attacking with them dies
  sooner (``ARENAMCP_ATTACK_LINE_GUARD``, shadow by default), and the
  attack-vs-hold verdict for the declare-attackers prompt.
- ``attack_push`` (ledger #178): the best line's attackers instead of a
  declaration (an empty one included) that dies sooner or, with an 'attack'
  posture, ranks below it (``ARENAMCP_ATTACK_PUSH``, on by default).

Every guard has a mode, 'off', 'shadow' or 'on' (``guard_mode``): its
environment variable (``GUARD_ENV``) wins, then its ``guard_<kind>`` setting
(Tools > Autopilot Guards in the desktop app), then ``GUARD_DEFAULTS`` (line,
line gap and attack push on; mode and attack line in shadow). In 'shadow' a
verdict's ``applies`` is False and the caller logs what it would have done. A
contingent verdict (a modal card's other mode does something the search can't
model, e.g. answers a token/copy engine) never applies, even in 'on' mode.

Conservative rules: no guard fires with lethal on board for us, when the best
line wins this turn (posture 'lethal'), on a choice inside a winning line, on
a pass, on a cast whose effect the search does not model
(``line_search_moves.unmodelled_effect``: tokens it can't read, an aura, a
pump, an attack trigger other than a fixed ping to players, player damage it
has no face variant for; such a cast gets no '[LINE ...]' tag either), on the
opponent's turn, with a
non-empty stack (the modal source itself excepted), with an unknown-P/T
creature, or on a truncated search; an override also has to be no worse under
the +2/+0 trick proxy (``line_search.proxy_outcome``). ARENAMCP_LINE_SEARCH=0
also turns off the per-mode comparisons, their tags and the mode guard. Pure:
no I/O, no LLM; any error returns None or '' so today's choice stands.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from arenamcp.board_assessment import (
    _int,
    _is_creature,
    _line_search_enabled,
    _name,
    _seats,
    _signature,
    _source_card,
)
from arenamcp.guard_settings import (  # noqa: F401 (re-exported)
    GUARD_DEFAULTS,
    GUARD_ENV,
    GUARD_MODES,
    GUARD_SETTINGS,
    describe_guard_modes,
    guard_mode,
    guard_source,
)
from arenamcp.line_search import (
    ALIVE,
    DEAD,
    LINE_TOL,
    POSTURE_MARGIN,
    WIN,
    Line,
    LineSearchResult,
    ModeComparison,
    action_key,
    compare_modes,
    proxy_outcome,
    unmodelled_effect,
)
from arenamcp.mulligan_policy import _pip_matching

logger = logging.getLogger(__name__)

# Survival mode, both lines alive: a value gap this large is an improvement too.
LINE_GUARD_MARGIN = 5.0
_NOTE_MAX = 60
_SUMMARY_MAX = 200


def line_gap_mode() -> str:
    """'off', 'shadow' or 'on' for the planner's line-gap second look (ledger #116); default 'on'.

    ``ARENAMCP_LINE_GAP`` / setting ``guard_line_gap``: 'on' doubts a cast tagged
    ``LINE_GUARD_MARGIN`` or more below the best line (the low answer waits for high effort,
    else the line's pick decides); 'shadow' only logs what it would replace.
    """
    return guard_mode("line_gap")


@dataclass
class LineVerdict:
    """A guard's replacement, shaped like ``board_assessment.GuardVerdict``.

    ``applies`` is False in shadow mode and for a contingent verdict: the
    caller only logs it. ``robust``: the improvement also holds when the
    opponent has a +2/+0 trick (the firing rule only needs it to be no worse).
    """

    option_id: str
    reason: str  # full reason for the log
    summary: str = ""  # short reasoning for narration
    chosen_line: Line | None = None
    best_line: Line | None = None
    robust: bool = False
    contingent: list[str] = field(default_factory=list)
    applies: bool = False
    replaced: str = ""  # the chosen option id
    kind: str = "line"  # 'line' or 'mode'
    setting: str = "shadow"  # the guard mode it was produced under

    def as_trace(self) -> dict[str, Any]:
        """JSON-safe facts for the decision trace."""
        return {
            "replaced": self.replaced,
            "with": self.option_id,
            "reason": self.reason,
            "applied": self.applies,
            "robust": self.robust,
            "contingent": list(self.contingent),
            "kind": self.kind,
            "mode": self.setting,
        }


# --- shared helpers ---------------------------------------------------------------------


def _ct(line: Line) -> tuple[int, int]:
    return (line.cls, line.timing)


def _improves(better: Line, worse: Line, *, by_value: bool) -> bool:
    """Strictly better in (class, timing); or, ``by_value``, both alive and LINE_GUARD_MARGIN apart."""
    if _ct(better) != _ct(worse):
        return _ct(better) > _ct(worse)
    return by_value and better.cls == ALIVE and better.v - worse.v >= LINE_GUARD_MARGIN


def _proxy_check(
    best: tuple[LineSearchResult, Line], chosen: tuple[LineSearchResult, Line], *, by_value: bool
) -> tuple[bool, bool, str] | None:
    """(not worse, robust, text) under the +2/+0 trick proxy; None when it can't be replayed."""
    trick_best, trick_chosen = proxy_outcome(*best), proxy_outcome(*chosen)
    if trick_best is None or trick_chosen is None:
        return None
    text = f"+2/+0 trick: {trick_best.outcome_text()} vs {trick_chosen.outcome_text()}"
    return (
        _ct(trick_best) >= _ct(trick_chosen),
        _improves(trick_best, trick_chosen, by_value=by_value),
        text,
    )


def _short_label(option: Any) -> str:
    """The option's label without the ability text in brackets and a trailing period."""
    label = str(getattr(option, "label", "") or getattr(option, "option_id", "") or "")
    return label.split(" [", 1)[0].strip().rstrip(".")[:80]


def _brief(line: Line) -> str:
    """'lethal on T15' / 'dead on T15' / 'life 7, 5, 8'."""
    if line.cls in (WIN, DEAD):
        return line.outcome_text()
    lives = line.lives()
    return "life " + ", ".join(str(life) for life in lives) if lives else "no attack back"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip(" ,;:") + "…"


def _is_pass(option: Any) -> bool:
    action = str((getattr(option, "meta", None) or {}).get("actionType") or "").lower()
    return getattr(option, "option_id", "") == "pass" or action.removeprefix("actiontype_") == "pass"


def _our_turn(state: dict) -> bool:
    local, _opponent = _seats(state)
    active = _int((state.get("turn") or {}).get("active_player"))
    return local is not None and active == _int(local)


# --- replacement preference ---------------------------------------------------------------


def _land_enables(result: LineSearchResult, line: Line) -> bool:
    """The best T step's land drop is what pays for its casts this turn."""
    step = line.steps[0]
    land, casts, _attack = step.plays if len(step.plays) == 3 else (None, (), ())
    if land is None or land.tapped or not casts:
        return False
    sources = list(result.model.sources_now)  # our sources at T before the drop
    pips = tuple(p for var, _target in casts for p in var.pips)
    return sum(var.mana_value for var, _target in casts) > len(sources) or (
        bool(pips) and not _pip_matching(pips, sources)
    )


def _candidates(
    result: LineSearchResult, decision: Any, state: dict, exclude: str = ""
) -> list[tuple[int, int, Any]]:
    """Payable options that start the best line, ranked: removal on an attacker, creature, land.

    Only the best T step's own plays qualify: removal (or a bounce) it aims at
    a creature of theirs that can attack, a creature (or a spell making
    creature tokens) it casts, and its land drop when that land pays for the
    step's casts.
    """
    best = result.best
    if not best.steps:
        return []
    step = best.steps[0]
    aimed = dict(step.targets)  # card -> their creature
    attackers = {b["name"] for b in result.model.theirs if b.get("_can_attack", True)}
    land = step.plays[0] if len(step.plays) == 3 else None
    makers = {var.spell for var, _target in (step.plays[1] if len(step.plays) == 3 else ()) if var.tokens}
    bodies = {result._search.spells[i].name for i in makers} if result._search is not None else set()
    found = []
    for index, option in enumerate(decision.options):
        if option.option_id == exclude or option.payable is False or _is_pass(option):
            continue
        key = action_key(option, state)
        if key is None or key not in best.first_actions:
            continue
        source, _zone = _source_card(state, option.meta or {})
        name = _name(source) if source else ""
        if key[0] == "cast" and aimed.get(name) == "opponent" and best.cls == WIN and best.win_at == 1:
            found.append((-1, index, option))  # the burn that wins now
        elif key[0] == "cast" and aimed.get(name) in attackers:
            found.append((0, index, option))
        elif key[0] == "cast" and name in step.casts and (_is_creature(source) or name in bodies):
            found.append((1, index, option))
        elif (
            key[0] == "land"
            and land is not None
            and key == ("land", land.key)
            and _land_enables(result, best)
        ):
            found.append((2, index, option))
    return sorted(found, key=lambda item: item[:2])


def _usable(result: LineSearchResult | None) -> bool:
    return result is not None and not result.truncated and not result.dead_now


def _unmodelled_finisher(result: LineSearchResult, option: Any, state: dict) -> bool:
    """The option casts a card whose effect the search does not model (``unmodelled_effect``):
    tokens it can't read, an aura, a pump, an attack trigger, player damage it has no face
    variant for... Its line undervalues it, so it is no evidence for or against the cast."""
    return bool(unmodelled_cast(result, option, state))


def _searched_commander(result: LineSearchResult | None, source: dict) -> dict | None:
    """The search's own card for this command-zone card (its commander spell), or None."""
    spells = getattr(getattr(result, "_search", None), "spells", None) or []
    iid = _int(source.get("instance_id"))
    return next((hs.spell.card for hs in spells if hs.spell.zone == "command" and hs.iid == iid), None)


def unmodelled_cast(result: LineSearchResult | None, option: Any, state: dict) -> str:
    """'<card> (<why>)' when ``option`` casts a card the search can't value, else ''.

    Only casts: a land drop, landcycling and abilities are moves the search
    models (or does not offer at all). ``result`` (may be None) lets the check
    see whether the card's player damage has a face variant.
    """
    meta = getattr(option, "meta", None) or {}
    action = str(meta.get("actionType") or "").removeprefix("ActionType_").lower()
    if _is_pass(option) or action in ("play", "playland", "activate"):
        return ""
    source, zone = _source_card(state, meta)
    if zone == "command":
        # Our commander as the search read it (``board_model.our_commanders``: a copy marked
        # when its card data is unknown).
        source = _searched_commander(result, source) or source
    why = unmodelled_effect(source, result) if source else ""
    return f"{_name(source)} ({why})" if why else ""


# --- the line guard -----------------------------------------------------------------------


def line_guard(
    result: LineSearchResult | None,
    decision: Any,
    chosen_id: str,
    state: dict,
    *,
    survival_mode: bool,
    lethal_now: bool,
    our_turn: bool,
    unknown_bodies: list[str] | tuple[str, ...] = (),
) -> LineVerdict | None:
    """Replace an ActionsAvailable pick the line search shows is strictly worse; None to keep it.

    None when: the guard is off; there is no usable search (none, truncated,
    their attack under way kills us); we have lethal; the request is not
    ActionsAvailable; it is not our turn; the stack is not empty; a creature
    has unknown power/toughness; the pick is missing, a pass, or not a move the
    search models; the pick already starts the best line; its own best line
    wins; or it is a land and the gain is only in value. The best line must be
    strictly better in (class, timing) or, in survival mode with both lines
    alive, by ``LINE_GUARD_MARGIN`` in value (class/timing only when the
    search was bounded), and no worse under the +2/+0 trick proxy.
    """
    try:
        return _line_guard(
            result, decision, chosen_id, state, survival_mode=survival_mode, lethal_now=lethal_now,
            our_turn=our_turn, unknown_bodies=unknown_bodies,
        )  # fmt: skip
    except Exception:  # never let the guard break a decision
        logger.debug("line guard failed", exc_info=True)
        return None


def _line_guard(
    result, decision, chosen_id, state, *, survival_mode, lethal_now, our_turn, unknown_bodies
) -> LineVerdict | None:
    setting = guard_mode("line")
    if setting == "off" or not _usable(result) or lethal_now or not our_turn:
        return None
    if getattr(decision, "request_type", "") != "ActionsAvailable" or not result.model.our_turn:
        return None
    if state.get("stack") or result.model.stack_nonempty or unknown_bodies or result.model.unknown_bodies:
        return None
    if result.posture == "lethal":
        return None  # a winning line is on the table: never steer away from it
    chosen = decision.find(chosen_id)
    if chosen is None or _is_pass(chosen) or _unmodelled_finisher(result, chosen, state):
        return None
    key = action_key(chosen, state)
    best = result.best
    if key is None or key in best.first_actions:
        return None
    mine = result.first_action.get(key)
    if mine is None or mine.cls == WIN:
        return None
    by_value = survival_mode and not result.bounded and key[0] != "land"
    if not _improves(best, mine, by_value=by_value):
        return None
    candidates = _candidates(result, decision, state, exclude=chosen_id)
    if not candidates:
        return None
    proxy = _proxy_check((result, best), (result, mine), by_value=by_value)
    if proxy is None or not proxy[0]:
        return None
    _rank, _index, replacement = candidates[0]
    label = _short_label(chosen)
    reason = f"Line guard: {best.summary()} instead of {label} ({mine.summary()}) [{proxy[2]}]"
    summary = f"{best.steps[0].text()} instead of {label}: {_brief(best)} rather than {_brief(mine)}."
    return LineVerdict(
        option_id=replacement.option_id,
        reason=reason,
        summary=_clip(summary, _SUMMARY_MAX),
        chosen_line=mine,
        best_line=best,
        robust=proxy[1],
        contingent=[],
        applies=setting == "on",
        replaced=chosen_id,
        kind="line",
        setting=setting,
    )


# --- the mode guard -----------------------------------------------------------------------

_MODE_CACHE: OrderedDict[tuple, ModeComparison | None] = OrderedDict()
_MODE_CACHE_LOCK = threading.Lock()
_MODE_CACHE_SIZE = 8
_MODE_INFLIGHT: dict[tuple, threading.Event] = {}
_MODE_WAIT_S = 0.5  # past compare_modes' 200 ms budget


def _modal(decision: Any) -> list:
    """The modal options of a CastingTimeOptions decision's first modal child."""
    if getattr(decision, "request_type", "") != "CastingTimeOptions":
        return []
    modal = [o for o in decision.options if (o.meta or {}).get("choiceKind") == "modal"]
    if not modal:
        return []
    child = modal[0].meta.get("childIndex")
    return [o for o in modal if o.meta.get("childIndex") == child]


def mode_comparison(state: dict, decision: Any) -> ModeComparison | None:
    """``compare_modes`` memoised per board, stack and menu (option tags and the guard share it)."""
    try:
        modal = _modal(decision)
        if not modal:
            return None
        stack = tuple(
            (c.get("instance_id"), c.get("grp_id"), c.get("parent_instance_id"))
            for c in state.get("stack") or []
            if isinstance(c, dict)
        )
        key = (
            _signature(state),
            stack,
            tuple(getattr(decision, "request_id", ()) or ()),
            tuple((o.option_id, o.label, repr(sorted(o.meta.items()))) for o in modal),
            _line_search_enabled(),
        )
        hash(key)
    except Exception:
        logger.debug("mode comparison key failed", exc_info=True)
        return None
    if not key[-1]:
        return None  # ARENAMCP_LINE_SEARCH=0: no per-mode searches, tags or guard
    with _MODE_CACHE_LOCK:
        if key in _MODE_CACHE:
            _MODE_CACHE.move_to_end(key)
            return _MODE_CACHE[key]
        event = _MODE_INFLIGHT.get(key)
        owner = event is None
        if owner:
            event = _MODE_INFLIGHT[key] = threading.Event()
    if not owner:  # the same menu is being compared: share that result
        event.wait(_MODE_WAIT_S)
        with _MODE_CACHE_LOCK:
            if key in _MODE_CACHE:
                return _MODE_CACHE[key]
    try:
        found = compare_modes(state, decision)
    except Exception:  # compare_modes catches its own errors; never leave waiters hanging
        logger.debug("mode comparison failed", exc_info=True)
        found = None
    with _MODE_CACHE_LOCK:
        _MODE_CACHE[key] = found
        while len(_MODE_CACHE) > _MODE_CACHE_SIZE:
            _MODE_CACHE.popitem(last=False)
        if owner:
            _MODE_INFLIGHT.pop(key, None)
    if owner:
        event.set()
    return found


def mode_guard(
    result_fn: Callable[[dict, Any], ModeComparison | None] | ModeComparison | None,
    decision: Any,
    chosen_ids: list[str],
    state: dict,
    *,
    lethal_now: bool,
) -> LineVerdict | None:
    """Replace a modal mode that dies sooner (or loses) than another; None to keep the pick.

    ``result_fn(state, decision)`` gives the per-mode lines (default: the
    memoised ``compare_modes``); a ``ModeComparison`` may be passed directly.
    Only for one chosen mode of a 'choose one' CastingTimeOptions child, on
    our turn, without lethal for us, with nothing else on the stack, every
    mode searched and no unknown-P/T creature. Fires only on a strict (class,
    timing) improvement that the +2/+0 trick proxy does not reverse, and only
    with an option set ``decision.selection_is_valid`` accepts. A verdict with
    ``contingent`` notes (the other mode's effect is not modelled) never applies.
    """
    try:
        return _mode_guard(result_fn, decision, chosen_ids, state, lethal_now=lethal_now)
    except Exception:  # never let the guard break a decision
        logger.debug("mode guard failed", exc_info=True)
        return None


def _mode_guard(result_fn, decision, chosen_ids, state, *, lethal_now) -> LineVerdict | None:
    setting = guard_mode("mode")
    if setting == "off" or lethal_now or not _our_turn(state):
        return None
    modal = _modal(decision)
    ids = {o.option_id for o in modal}
    if len(chosen_ids or []) != 1 or chosen_ids[0] not in ids:
        return None
    chosen_id = chosen_ids[0]
    source_id = _int((modal[0].meta or {}).get("sourceId"))
    stack = [c for c in state.get("stack") or [] if isinstance(c, dict)]
    others = [c for c in stack if source_id is None or _int(c.get("instance_id")) != source_id]
    if len(others) > (1 if source_id is None else 0):
        return None  # something else is still to resolve: the board will change
    if isinstance(result_fn, ModeComparison):
        comparison = result_fn
    else:
        comparison = (result_fn or mode_comparison)(state, decision)
    if comparison is None or not comparison.complete:
        return None
    if any(r.model.unknown_bodies or not r.model.our_turn for r in comparison.results.values()):
        return None
    mine = comparison.lines.get(chosen_id)
    if mine is None or mine.cls == WIN:
        return None
    order = [o.option_id for o in modal]
    better = [
        oid
        for oid in order
        if oid != chosen_id
        and oid in comparison.lines
        and _improves(comparison.lines[oid], mine, by_value=False)
    ]
    if not better:
        return None
    best_id = max(better, key=lambda oid: (comparison.lines[oid].score, -order.index(oid)))
    best = comparison.lines[best_id]
    if not decision.selection_is_valid([best_id]):
        return None
    proxy = _proxy_check(
        (comparison.results[best_id], best), (comparison.results[chosen_id], mine), by_value=False
    )
    if proxy is None or not proxy[0]:
        return None
    chosen, replacement = decision.find(chosen_id), decision.find(best_id)
    mode_text = comparison.modes.get(best_id) or _short_label(replacement)
    contingent = list(comparison.contingent)
    reason = (
        f"Mode guard: {comparison.source}: {mode_text} ({best.outcome_text()}) instead of "
        f"{_short_label(chosen)} ({mine.outcome_text()}) [{proxy[2]}]"
    )
    if contingent:
        reason += f" [contingent: {'; '.join(contingent)}]"
    summary = (
        f"{comparison.source}: choose {mode_text} instead of {_short_label(chosen)}: "
        f"{_brief(best)} rather than {_brief(mine)}."
    )
    return LineVerdict(
        option_id=best_id,
        reason=reason,
        summary=_clip(summary, _SUMMARY_MAX),
        chosen_line=mine,
        best_line=best,
        robust=proxy[1],
        contingent=contingent,
        applies=setting == "on" and not contingent,
        replaced=chosen_id,
        kind="mode",
        setting=setting,
    )


# --- the attack line guard (ledger #152) ------------------------------------------------------

# The searched line that sends the attackers we would hold must die at least this many of their
# attacks sooner than the holding line (a holding line that survives its last searched attack
# counts as dying one attack past it).
ATTACK_LINE_MARGIN = 1


def attack_line_mode() -> str:
    """'off', 'shadow' or 'on' for the attack line guard (``ARENAMCP_ATTACK_LINE_GUARD`` / setting
    ``guard_attack_line``); default 'shadow' (``GUARD_DEFAULTS``)."""
    return guard_mode("attack_line")


@dataclass
class AttackLineVerdict:
    """Declared attackers the searched lines keep home, because attacking with them dies sooner."""

    hold_ids: list[int]
    hold_names: list[str]
    attack_line: Line
    hold_line: Line
    reason: str  # 'attack dead T16, hold survives (life 10, 6, 2); best line: ...' for the log
    summary: str  # short text for narration
    attack_text: str = ""  # 'dead on T16'
    hold_text: str = ""  # 'survives (life 10, 6, 2)'
    robust: bool = False
    applies: bool = False
    setting: str = "shadow"

    def as_trace(self) -> dict[str, Any]:
        return {
            "hold": list(self.hold_names),
            "hold_ids": list(self.hold_ids),
            "attack_line": self.attack_line.summary(),
            "hold_line": self.hold_line.summary(),
            "robust": self.robust,
            "applied": self.applies,
            "mode": self.setting,
        }


def _attack_ids(line: Line) -> frozenset[int] | None:
    """The attacker ids of the line's T step; None when they can't be told from its names."""
    if not line.steps:
        return None
    step = line.steps[0]
    ids = frozenset(_int(i) for i in (step.plays[2] if len(step.plays) == 3 else ()) or ())
    return ids if len(ids) == len(step.attack) else None


def _dies_at(line: Line) -> int:
    """Their attack that kills us in this line; a line that lives dies one past its last searched attack."""
    if line.cls == DEAD and line.dead_at is not None:
        return line.dead_at
    return sum(1 for step in line.steps if step.life_after is not None) + 1


def _outcome_brief(line: Line) -> str:
    """'dead T16' / 'survives (life 10, 6, 2)' / 'lethal on T15'."""
    if line.cls == DEAD and line.dead_turn is not None:
        return f"dead T{line.dead_turn}"
    return line.outcome_text()


def attack_line_guard(
    result: LineSearchResult | None,
    declared_ids: list[int] | tuple[int, ...],
    state: dict,
    *,
    lethal_now: bool,
    all_in: bool,
    unknown_bodies: list[str] | tuple[str, ...] = (),
) -> AttackLineVerdict | None:
    """Declared attackers to hold because the searched lines keep them home; None to keep the attack.

    Ledger #152 (2026-10-19 match 633f56bd T11, 13:59:54): both Slickshot
    Show-Offs attacked into a tapped-out board ("free 4 flying damage"; it was
    2) while the line search said holding survives (life 10, 6, 2) and
    attacking is dead on T16. Untapped, they would have blocked; their next
    attack took 8. Only the one-turn crackback guard judged attacks.

    Holds the declared attackers the best line's T step leaves home, when that
    best line survives (or wins later), the searched line that attacks as
    declared (else the best one sending all of them and no undeclared
    attacker) dies at least ``ATTACK_LINE_MARGIN`` of their attacks sooner, and
    the +2/+0 trick proxy does not reverse that. None when: off; no usable search;
    lethal now or a winning posture; all in; not our turn or not before our
    attack; an unknown-P/T creature; the best line's attackers can't be told;
    or no searched line sends the attackers it would hold. Shadow by default
    (``ARENAMCP_ATTACK_LINE_GUARD``): ``applies`` is then False and the caller
    only logs the verdict.
    """
    try:
        return _attack_line_guard(
            result, declared_ids, state, lethal_now=lethal_now, all_in=all_in, unknown_bodies=unknown_bodies
        )
    except Exception:  # never let the guard break a declaration
        logger.debug("attack line guard failed", exc_info=True)
        return None


def _attack_line_guard(result, declared_ids, state, *, lethal_now, all_in, unknown_bodies):
    setting = attack_line_mode()
    if setting == "off" or not _usable(result) or lethal_now or all_in or not _our_turn(state):
        return None
    model = result.model
    if not model.our_turn or not model.our_attack_pending or unknown_bodies or model.unknown_bodies:
        return None
    best = result.best
    if result.posture == "lethal" or best.cls == DEAD or (best.cls == WIN and best.win_at == 1):
        return None
    declared = frozenset(_int(i) for i in declared_ids or () if _int(i) is not None)
    best_attack = _attack_ids(best)
    if not declared or best_attack is None:
        return None
    home = declared - best_attack
    if not home:
        return None
    # The line that attacks as declared, else the best searched one that sends every attacker we'd
    # hold and none we did not declare (an extra attacker would be a blocker it gave up).
    attacking = result.first_action.get(("attack", declared))
    if attacking is None:
        sending = [
            line
            for key, line in result.first_action.items()
            if len(key) == 2 and key[0] == "attack" and home <= set(key[1]) <= declared
        ]
        attacking = max(sending, key=lambda line: line.score) if sending else None
    if attacking is None or attacking.cls != DEAD:
        return None
    if _dies_at(best) - _dies_at(attacking) < ATTACK_LINE_MARGIN:
        return None
    proxy = _proxy_check((result, best), (result, attacking), by_value=False)
    if proxy is None or not proxy[0]:
        return None
    names = {
        _int(card.get("instance_id")): _name(card)
        for card in state.get("battlefield") or []
        if isinstance(card, dict)
    }
    hold_ids = sorted(home)
    hold_names = [names.get(i) or f"#{i}" for i in hold_ids]
    attack_text, hold_text = attacking.outcome_text(), best.outcome_text()
    reason = (
        f"attack {_outcome_brief(attacking)}, hold {_outcome_brief(best)}; best line: {best.summary()} "
        f"[{proxy[2]}]"
    )
    summary = (
        f"Holding {', '.join(hold_names)} back: by the line search, attacking is {attack_text}, "
        f"holding {hold_text}."
    )
    return AttackLineVerdict(
        hold_ids=hold_ids,
        hold_names=hold_names,
        attack_line=attacking,
        hold_line=best,
        reason=reason,
        summary=_clip(summary, _SUMMARY_MAX),
        attack_text=attack_text,
        hold_text=hold_text,
        robust=proxy[1],
        applies=setting == "on",
        setting=setting,
    )


# A push on value alone (both lines alive, the same timing) needs twice the posture's margin.
ATTACK_PUSH_MARGIN = 2 * POSTURE_MARGIN


def attack_push_mode() -> str:
    """'off', 'shadow' or 'on' for the attack push (``ARENAMCP_ATTACK_PUSH`` / setting
    ``guard_attack_push``); default 'on' (``GUARD_DEFAULTS``)."""
    return guard_mode("attack_push")


@dataclass
class AttackPushVerdict:
    """The searched line's attack, to declare instead of a declaration that ends worse."""

    attack_ids: list[int]
    attack_names: list[str]
    at_walkers: dict[str, str]  # attacker name -> planeswalker name, the line's split
    declared_line: Line
    attack_line: Line
    reason: str  # 'hold dead T15, attack survives (life 20, 15, 13; ...); best line: ... [+2/+0 ...]'
    summary: str  # short text for narration
    declared_text: str = ""  # 'dead on T15'
    attack_text: str = ""  # 'survives (life 20, 15, 13; opponent at 26)'
    robust: bool = False
    applies: bool = False
    setting: str = "shadow"

    def as_trace(self) -> dict[str, Any]:
        return {
            "attack": list(self.attack_names),
            "attack_ids": list(self.attack_ids),
            "declared_line": self.declared_line.summary(),
            "attack_line": self.attack_line.summary(),
            "robust": self.robust,
            "applied": self.applies,
            "mode": self.setting,
        }


def attack_push(
    result: LineSearchResult | None,
    declared_ids: list[int] | tuple[int, ...],
    state: dict,
    *,
    eligible: list[int] | tuple[int, ...] = (),
    lethal_now: bool,
    all_in: bool,
    unknown_bodies: list[str] | tuple[str, ...] = (),
) -> AttackPushVerdict | None:
    """The best line's attackers to declare instead of ``declared_ids`` (empty: no attack), or None.

    Ledger #178 (2026-10-19 match c0af235f T12, 15:21:41): twenty attackers
    (Leonardo 27/3) against seven blockers; the board facts said holding is
    dead on T15 and attacking the only surviving line, and the empty
    declaration ("Stabilize behind the token wall") stood: the attack line
    guard can only hold attackers back. On master the same board goes to the
    combat solver (20 attackers), which holds all of them.

    Pushes when the best line attacks with creatures the declaration keeps
    home and either the searched line that attacks as declared (no attack:
    the line that holds) is dead at least ``ATTACK_LINE_MARGIN`` of their
    attacks sooner than the best line (which may die later), or the search's posture
    is 'attack' and the declared line ranks below the best one (a worse
    outcome or timing, or ``ATTACK_PUSH_MARGIN`` less value); and the +2/+0
    trick proxy does not reverse it. Every attacker of the line must be in
    ``eligible`` (the request's legal attackers) when that is given. None when:
    off; no usable search; lethal now or a winning posture (their own rules);
    all in; not our turn or not before our attack; an unknown-P/T creature;
    the declared attack was not searched; or the push would only hold
    attackers back (``attack_line_guard``). Shadow by default
    (``ARENAMCP_ATTACK_PUSH``): ``applies`` is then False and the caller only
    logs the verdict.
    """
    try:
        return _attack_push(
            result, declared_ids, state, eligible=eligible, lethal_now=lethal_now, all_in=all_in,
            unknown_bodies=unknown_bodies,
        )  # fmt: skip
    except Exception:  # never let the push break a declaration
        logger.debug("attack push failed", exc_info=True)
        return None


def _ranked_proxy_check(result: LineSearchResult, best: Line, chosen: Line) -> tuple[bool, bool, str] | None:
    """(not worse, robust, text) under the +2/+0 trick proxy, by ``Line.ranked`` (a distant win
    ranks as alive, ledger #158, as it does in the posture); None when it can't be replayed."""
    trick_best, trick_chosen = proxy_outcome(result, best), proxy_outcome(result, chosen)
    if trick_best is None or trick_chosen is None:
        return None
    text = f"+2/+0 trick: {trick_best.outcome_text()} vs {trick_chosen.outcome_text()}"
    better, worse = trick_best.ranked, trick_chosen.ranked
    robust = better[:2] > worse[:2] or (
        better[:2] == worse[:2] and better[2] - worse[2] >= ATTACK_PUSH_MARGIN
    )
    return better >= worse, robust, text


def _attack_push(result, declared_ids, state, *, eligible, lethal_now, all_in, unknown_bodies):
    setting = attack_push_mode()
    if setting == "off" or not _usable(result) or lethal_now or all_in or not _our_turn(state):
        return None
    model = result.model
    if not model.our_turn or not model.our_attack_pending or unknown_bodies or model.unknown_bodies:
        return None
    best = result.best
    if result.posture == "lethal" or (best.cls == WIN and best.win_at == 1):
        return None
    best_attack = _attack_ids(best)
    declared = frozenset(_int(i) for i in declared_ids or () if _int(i) is not None)
    if not best_attack or not (best_attack - declared):
        return None
    allowed = {_int(i) for i in eligible or ()}
    if allowed and not best_attack <= allowed:
        return None
    declared_line = result.first_action.get(("attack", declared) if declared else ("noattack",))
    if declared_line is None:
        return None
    dead_sooner = declared_line.cls == DEAD and _dies_at(best) - _dies_at(declared_line) >= ATTACK_LINE_MARGIN
    ranks_below = result.posture == "attack" and (
        best.ranked[:2] > declared_line.ranked[:2]
        or (
            best.ranked[:2] == declared_line.ranked[:2]
            and best.ranked[2] - declared_line.ranked[2] >= ATTACK_PUSH_MARGIN
        )
    )
    if not (dead_sooner or ranks_below):
        return None
    proxy = _ranked_proxy_check(result, best, declared_line)
    if proxy is None or not proxy[0]:
        return None
    names = {
        _int(card.get("instance_id")): _name(card)
        for card in state.get("battlefield") or []
        if isinstance(card, dict)
    }
    attack_ids = sorted(best_attack)
    attack_names = [names.get(i) or f"#{i}" for i in attack_ids]
    declared_text, attack_text = declared_line.outcome_text(), best.outcome_text()
    if not dead_sooner:  # same class: the value gap is the reason
        declared_text += f" (ranks below: value {declared_line.ranked[2]:.1f} vs {best.ranked[2]:.1f})"
    what = "hold" if not declared else "attack as declared"
    reason = (
        f"{what} {_outcome_brief(declared_line) if dead_sooner else declared_text}, attack "
        f"{_outcome_brief(best)}; best line: {best.summary()} [{proxy[2]}]"
    )
    summary = (
        f"Attacking with {len(attack_ids)}: by the line search, "
        f"{'holding' if not declared else 'the declared attack'} ends {declared_text}; this attack "
        f"{attack_text}."
    )
    return AttackPushVerdict(
        attack_ids=attack_ids,
        attack_names=attack_names,
        at_walkers=dict(best.steps[0].at_walkers),
        declared_line=declared_line,
        attack_line=best,
        reason=reason,
        summary=_clip(summary, _SUMMARY_MAX),
        declared_text=declared_text,
        attack_text=attack_text,
        robust=proxy[1],
        applies=setting == "on",
        setting=setting,
    )


def attack_line_note(result: LineSearchResult | None, state: dict) -> str:
    """The line search's attack-vs-hold verdict for a declare-attackers prompt, or '' (ledger #152).

    Only on our turn before our attack, with a usable search whose posture is
    'attack' or 'hold' (a lethal posture has its own facts; 'either' says
    nothing).
    """
    try:
        if not _usable(result) or not _our_turn(state):
            return ""
        model = result.model
        if not model.our_turn or not model.our_attack_pending:
            return ""
        if result.posture not in ("attack", "hold") or not result.posture_reason:
            return ""
        head = (
            "ATTACK vs HOLD (board-math line search, 2 turns + greedy 3rd; their new cards and tricks "
            f"not modelled): {result.posture_reason}. Verdict: {result.posture.upper()}"
        )
        if result.posture == "hold":
            return (
                f"{head}. Not lethal this turn. Their creatures being tapped now does not make an attack "
                "free: an attacker without vigilance stays tapped through their next turn and cannot "
                "block. Keep the attackers the search keeps home unless you can name what it is missing."
            )
        return f"{head}."
    except Exception:
        logger.debug("attack line note failed", exc_info=True)
        return ""


# --- prompt text --------------------------------------------------------------------------


def _life_parts(line: Line, *, modal: bool = False) -> list[str]:
    """'7 after T13', ... ending in 'dead T15' / 'lethal T15' (modal: 'survives T15 at 2' first)."""
    if line.cls == WIN:
        return [f"lethal T{line.win_turn}"]
    parts = []
    for step in line.steps:
        if step.life_after is None:
            continue
        if step.life_after <= 0:
            break
        their_turn = step.turn + 1
        if modal and not parts:
            parts.append(f"survives T{their_turn} at {step.life_after}")
        else:
            parts.append(f"{step.life_after} after T{their_turn}")
    if line.cls == DEAD and line.dead_turn is not None:
        parts.append(f"dead T{line.dead_turn}")
    return parts


def _fit(head: str, parts: list[str], tail: str = "") -> str:
    """'[<head><parts><tail>]' within 60 characters: the first part and a final outcome are kept."""
    keep = list(parts)
    outcome = len(keep) > 1 and keep[-1].startswith(("dead", "lethal"))

    def render(items: list[str], end: str) -> str:
        return f"[{head}{', '.join(items)}{end}]"

    while len(render(keep, tail)) > _NOTE_MAX and len(keep) > 1:
        del keep[-2 if outcome and len(keep) > 2 else -1]
    text = render(keep, tail)
    if len(text) > _NOTE_MAX:
        text = render(keep, "")
    return text if len(text) <= _NOTE_MAX else text[: _NOTE_MAX - 2] + "…]"


def _note_for(line: Line, best: Line, *, is_best: bool) -> str:
    parts = _life_parts(line)
    if not parts:
        return ""
    if is_best:
        return _fit("LINE best: ", parts)
    if _ct(line) == _ct(best):
        gap = best.v - line.v
        head = "~best; " if gap < LINE_TOL else f"-{round(gap)} vs best; "
        return _fit(f"LINE: {head}", parts)
    return _fit("LINE: ", parts)


def option_note(result: LineSearchResult | ModeComparison | None, option: Any, state: dict) -> str:
    """A '[LINE ...]' tag of at most 60 characters for one option, or ''.

    ActionsAvailable options on our turn (nothing on the stack): the best line
    that starts with this play, e.g. '[LINE best: 7 after T13, 5 after T15]',
    '[LINE: -17 vs best; 4 after T13, 4 after T15]', '[LINE: dead T15]'.
    'pass' is tagged only in our Main2, or as the best line while only our own
    spell is on the stack and the line's next casts wait for it
    (``burn_timing.own_stack_line_waits``). A modal option needs the decision's
    ``ModeComparison`` (``mode_comparison``) as ``result``:
    '[LINE: survives T15 at 2, dead T17]'. Never names an opponent's card.
    """
    try:
        return _option_note(result, option, state)
    except Exception:
        logger.debug("option note failed", exc_info=True)
        return ""


def _option_note(result, option, state) -> str:
    meta = getattr(option, "meta", None) or {}
    if isinstance(result, ModeComparison):
        line = result.lines.get(option.option_id)
        if meta.get("choiceKind") != "modal" or line is None or not result.complete:
            return ""
        top = max(result.lines.values(), key=lambda x: x.score)
        unique = sum(1 for x in result.lines.values() if x.score == top.score) == 1
        parts = _life_parts(line, modal=True)
        if not parts:
            return ""
        tail = "; effect unmodelled" if result.modes.get(option.option_id) == "other" else ""
        return _fit(
            "LINE best: " if line is top and unique and len(result.lines) > 1 else "LINE: ", parts, tail
        )
    if not _usable(result) or meta.get("choiceKind"):
        return ""
    model = result.model
    if _is_pass(option) and model.our_turn and state.get("stack") and result.posture != "lethal":
        # Ledger #139 (d9a33232 T4): with only our own spell on the stack and the best line's
        # next casts all sorcery-speed, passing (letting it resolve) is the line's own next move.
        from arenamcp.burn_timing import own_stack_line_waits

        if own_stack_line_waits(state, result) is not None:
            return _note_for(result.best, result.best, is_best=True)
        return ""
    if not model.our_turn or state.get("stack") or model.stack_nonempty or result.posture == "lethal":
        return ""
    if not _is_pass(option) and _unmodelled_finisher(result, option, state):
        return ""
    best = result.best
    if _is_pass(option):
        line = result.first_action.get(("nocast",))
        if model.phase != "Phase_Main2" or line is None or not line.steps or line.steps[0].land:
            return ""
        return _note_for(line, best, is_best=line.first_sig == best.first_sig)
    key = action_key(option, state)
    if key is None:
        return ""
    if key in best.first_actions:
        return _note_for(best, best, is_best=True)
    line = result.first_action.get(key)
    return _note_for(line, best, is_best=False) if line is not None else ""


def lines_summary(
    result: LineSearchResult | None, *, unmodelled: Any = (), pending: Any = (), max_chars: int = 320
) -> str:
    """The 'LINES ...' prompt line (at most ``max_chars``), or '' without a usable search.

    ``unmodelled`` / ``pending``: the board assessment's casts the search can't
    value and our pending stack objects (``LineSearchResult.prompt_line``).
    """
    try:
        if result is None or result.truncated:
            return ""
        return result.prompt_line(max_chars, unmodelled=unmodelled, pending=pending)
    except Exception:
        logger.debug("lines summary failed", exc_info=True)
        return ""


def line_fallback_pick(result: LineSearchResult | None, decision: Any, state: dict) -> list[str] | None:
    """The best line's payable first action (same preference as the line guard), or None.

    For the model-failure path of an ActionsAvailable decision on our turn
    with nothing on the stack; None (today's fallback) otherwise, or when the
    best turn starts with nothing the guard would pick.
    """
    try:
        if not _usable(result) or getattr(decision, "request_type", "") != "ActionsAvailable":
            return None
        model = result.model
        if not model.our_turn or state.get("stack") or model.unknown_bodies:
            return None
        found = _candidates(result, decision, state)
        return [found[0][2].option_id] if found else None
    except Exception:
        logger.debug("line fallback pick failed", exc_info=True)
        return None


__all__ = [
    "ATTACK_LINE_MARGIN",
    "ATTACK_PUSH_MARGIN",
    "AttackPushVerdict",
    "GUARD_MODES",
    "LINE_GUARD_MARGIN",
    "LINE_TOL",
    "AttackLineVerdict",
    "LineVerdict",
    "attack_line_guard",
    "attack_line_mode",
    "attack_line_note",
    "attack_push",
    "attack_push_mode",
    "guard_mode",
    "line_fallback_pick",
    "line_gap_mode",
    "line_guard",
    "lines_summary",
    "mode_comparison",
    "mode_guard",
    "option_note",
    "unmodelled_cast",
    "unmodelled_effect",
]
