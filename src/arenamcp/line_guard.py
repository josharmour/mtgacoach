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

Both guards are shadow-only by default: they return a verdict whose
``applies`` is False, and the caller logs what it would have done. The env vars
``ARENAMCP_LINE_GUARD`` / ``ARENAMCP_MODE_GUARD`` ('off', 'shadow', 'on') switch
them; settings.py is not used. A contingent verdict (a modal card's other mode
does something the search can't model, e.g. answers a token/copy engine) never
applies, even in 'on' mode.

Conservative rules: no guard fires with lethal on board for us, when the best
line wins this turn (posture 'lethal'), on a choice inside a winning line, on
a pass, on a cast whose effect the search does not model
(``line_search_moves.unmodelled_effect``: tokens it can't read, an aura, a
pump, an attack trigger, player damage it has no face variant for; such a
cast gets no '[LINE ...]' tag either), on the opponent's turn, with a
non-empty stack (the modal source itself excepted), with an unknown-P/T
creature, or on a truncated search; an override also has to be no worse under
the +2/+0 trick proxy (``line_search.proxy_outcome``). ARENAMCP_LINE_SEARCH=0
also turns off the per-mode comparisons, their tags and the mode guard. Pure:
no I/O, no LLM; any error returns None or '' so today's choice stands.
"""

from __future__ import annotations

import logging
import os
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
from arenamcp.line_search import (
    ALIVE,
    DEAD,
    LINE_TOL,
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
GUARD_MODES = ("off", "shadow", "on")
_ENV = {"line": "ARENAMCP_LINE_GUARD", "mode": "ARENAMCP_MODE_GUARD"}
_NOTE_MAX = 60
_SUMMARY_MAX = 200


def guard_mode(kind: str) -> str:
    """'off', 'shadow' or 'on' for the 'line' or 'mode' guard; anything else is 'shadow'."""
    value = str(os.environ.get(_ENV.get(kind, ""), "") or "").strip().lower()
    return value if value in GUARD_MODES else "shadow"


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
    source, _zone = _source_card(state, meta)
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
    'pass' is tagged only in our Main2. A modal option needs the decision's
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
    "GUARD_MODES",
    "LINE_GUARD_MARGIN",
    "LINE_TOL",
    "LineVerdict",
    "guard_mode",
    "line_fallback_pick",
    "line_guard",
    "lines_summary",
    "mode_comparison",
    "mode_guard",
    "option_note",
    "unmodelled_cast",
    "unmodelled_effect",
]
