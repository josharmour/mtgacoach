"""Kill claims on a 'choose one' pick, and which mode a sentence names (ledger #142, #144).

2026-10-19 match 970d9b41 T12 (standalone.log 13:33:50-13:33:56): Iroh's
Demonstration offered "1 damage to each creature your opponents control" and
"4 damage to target creature" against a lone 1/2 Ravenhill Flock, and the
searched best line cast mode 2 on the Flock. At the cast, the hedge took the
low answer "Iroh's mode 1 sweeps Ravenhill Flock" as that line's play because
its target matched (#144). At the mode menu, MODES tagged mode 2 'LINE best'
and the low-effort answer chose mode 1: "Mode 1 kills Ravenhill Flock, the only
blocker ...; mode 2's 4 damage is wasted on a 1-toughness bird" (#142). The
Flock survived, grew with every card they drew and dealt 23 of the 29 damage we
took. Mode picks returned right after the shadow-only mode guard, and the
false-kill check ran only on ActionsAvailable.

``mode_kill_check`` is a correctness check, not a judgment: it names another
mode only when the reasoning's own kill is impossible for the chosen mode and
that mode makes every kill the reasoning claims for the pick.
``other_modes_named`` tells the hedge a low answer speaks of another mode than
the line's.

Pure: no I/O, no LLM. An unreadable menu, card or board gives None or [].
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from arenamcp.board_assessment import _body_colors, _int, _kills, _name, _types
from arenamcp.board_model import build_board_model
from arenamcp.line_search import _mode_source, _plain
from arenamcp.line_search_moves import bullets, classify, mode_label, mode_sweep, swept_now
from arenamcp.play_safety import _kill_claim_spans, _opposing_creatures, claimed_kills


@dataclass(frozen=True)
class ModeKills:
    """What one mode kills as it resolves (instance ids of their creatures): ``each`` whatever it
    targets (a sweep), plus one of ``target`` by the choice of target."""

    each: frozenset[int] = frozenset()
    target: frozenset[int] = frozenset()

    def kills(self, iid: int) -> bool:
        return iid in self.each or iid in self.target

    def kills_all(self, iids: Collection[int]) -> bool:
        """One cast kills every one of ``iids``: the sweep's victims and at most one target."""
        rest = set(iids) - self.each
        return len(rest) <= 1 and rest <= self.target


@dataclass(frozen=True)
class ModeMenu:
    """A modal CastingTimeOptions menu read against the board."""

    name: str  # the card
    texts: tuple[str, ...]  # its 'choose one' bullets
    index: dict[str, int]  # option id -> bullet, in menu order
    kills: dict[str, ModeKills]  # option id -> what that mode kills
    bodies: dict[int, dict]  # their creatures whose power and toughness are known

    def mode(self, option_id: str) -> str:
        """'mode 2 (deals 4 damage to target creature)'."""
        i = self.index[option_id]
        return f"mode {i + 1} ({describe_mode(self.name, self.texts[i])})"


@dataclass(frozen=True)
class ModeKillCheck:
    """A mode pick whose reasoning claims a kill that mode cannot make (ledger #142)."""

    chosen: str  # the model's option id
    chosen_mode: str  # 'mode 1 (deals 1 damage to each creature…)'
    claimed: tuple[str, ...]  # what the reasoning says the pick kills: 'Ravenhill Flock (1/2)'
    missed: tuple[str, ...]  # the claimed ones the chosen mode cannot kill
    missed_ids: tuple[int, ...]
    replacement: str = ""  # the first other mode that kills every claimed creature ('' = none)
    replacement_mode: str = ""
    # Their creatures the chosen mode kills whatever it targets and the replacement does not:
    # the pick does something real the claim left out, so it is kept (as ledger #86's casts).
    extra: tuple[str, ...] = ()

    @property
    def applies(self) -> bool:
        return bool(self.replacement) and not self.extra

    def as_trace(self) -> dict[str, Any]:
        return {
            "chosen": self.chosen,
            "chosen_mode": self.chosen_mode,
            "claimed": list(self.claimed),
            "missed": list(self.missed),
            "replacement": self.replacement,
            "replacement_mode": self.replacement_mode,
            "extra": list(self.extra),
        }


def modal_options(decision: Any) -> list:
    """The options of a CastingTimeOptions decision's first modal child ('choose one')."""
    if getattr(decision, "request_type", "") != "CastingTimeOptions":
        return []
    modal = [o for o in decision.options if (o.meta or {}).get("choiceKind") == "modal"]
    child = modal[0].meta.get("childIndex") if modal else None
    return [o for o in modal if o.meta.get("childIndex") == child]


def describe_mode(name: str, text: str) -> str:
    """The line search's short name for one bullet: 'deals 4 damage to target creature'."""
    kind, gain, _reach, _bounce = classify(name, text)
    return mode_label(kind, gain, text, name)


def read_menu(state: dict, decision: Any) -> ModeMenu | None:
    """The modal menu, its card's bullets and what each mode kills on this board; None if unread.

    Each option maps to its bullet as ``line_search.compare_modes`` maps it
    (its label's text, else Arena's optionIndex). A sweep kills every creature
    of theirs ``swept_now`` says it does; a removal mode can kill each one
    ``board_assessment._kills`` allows (marked damage counted when the text
    sets no power or toughness limit). A bounce kills nothing.
    """
    modal = modal_options(decision)
    if len(modal) < 2:
        return None
    source, holder, _on_battlefield = _mode_source(state, modal)
    texts = bullets(str((holder or {}).get("oracle_text") or "")) or bullets(
        str((source or {}).get("oracle_text") or "")
    )
    if source is None or not texts:
        return None
    model = build_board_model({**state, "stack": [c for c in state.get("stack") or [] if c is not holder]})
    if model is None:
        return None
    name = _name(source)
    index: dict[str, int] = {}
    kills: dict[str, ModeKills] = {}
    for option in modal:
        label = _plain(re.sub(r"^mode \d+:\s*", "", str(option.label), flags=re.I))
        found = next(
            (i for i, b in enumerate(texts) if _plain(b) == label),
            _int((option.meta or {}).get("optionIndex")),
        )
        if found is None or not 0 <= found < len(texts):
            return None
        index[option.option_id] = found
        kills[option.option_id] = _mode_kills(name, texts[found], source, model)
    return ModeMenu(name, tuple(texts), index, kills, {b["instance_id"]: b for b in model.theirs})


def _mode_kills(name: str, text: str, source: dict, model: Any) -> ModeKills:
    _kind, _gain, reach, bounce = classify(name, text)
    amount = mode_sweep(name, text)
    each = frozenset(b["instance_id"] for b in model.theirs if amount and swept_now(b, amount, source))
    if reach is None or bounce:
        return ModeKills(each)
    # One bullet's text: its card's type and colours decide protection, as for the line search's modes.
    reach = {**reach, "_source_types": _types(source), "_source_colors": _body_colors(source)}
    limited = bool(re.search(r"\b(?:power|toughness)\b", text.lower()))
    target = set()
    for body in model.theirs:
        marked = _int((body.get("_card") or {}).get("damage")) or 0
        bodies = [body]
        if marked > 0 and not limited:
            bodies.append({**body, "toughness": max(1, body["toughness"] - marked)})
        if any(
            _kills(
                reach,
                b,
                ours=model.ours,
                attacking=model.in_combat_before_damage,
                ward_mana=len(model.sources_now),
            )
            for b in bodies
        ):
            target.add(body["instance_id"])
    return ModeKills(each, frozenset(target))


# --- which mode a sentence names -------------------------------------------------------------

_NUMBERS = ("one", "two", "three", "four", "five")
_ORDINALS = ("first", "second", "third", "fourth", "fifth")
_DAMAGE = re.compile(r"\bdeals (\d+) damage\b")
_SWEEP_WORDS = (
    r"sweep\w*",
    r"(?:board ?)?wipe\w*",
    r"each (?:of (?:their|the) )?(?:opposing |enemy )?creatures?\b",
    r"all (?:of )?(?:their |the |opposing |enemy )?creatures\b",
    r"to each\b",
)
# A claim's clause: a sentence, or one side of "but" / "while" / "whereas" / "(al)though".
_CLAUSE_BREAK = re.compile(r"[.;!?]|\b(?:but|while|whereas|although|though)\b")
# "mode 2 over the sweep", "instead of mode 1": the rejected mode is not the one meant.
_CONTRAST = re.compile(r"\b(?:instead of|rather than|over|not)\b[^.;,:—–]*")


def mode_patterns(texts: Collection[str], name: str) -> list[re.Pattern]:
    """One regex per bullet for the words that name it in lowercased, space-normalised text.

    Its number ("mode 2", "second mode"); a sweep's words ("sweeps", "each
    creature", "1 damage to each") when another bullet is no sweep; its damage
    ("4 damage", "4-damage") when no other bullet deals the same; "single
    target" for the one targeted bullet beside a sweep.
    """
    texts = [str(t) for t in texts]
    lower = [t.lower() for t in texts]
    sweeps = [bool(mode_sweep(name, t)) for t in texts]
    amounts = [int(m.group(1)) if (m := _DAMAGE.search(t)) else None for t in lower]
    targeted = ["target" in t for t in lower]
    patterns = []
    for i in range(len(texts)):
        words = [rf"mode\s*#?\s*{i + 1}\b", rf"{i + 1}(?:st|nd|rd|th) mode\b"]
        if i < len(_NUMBERS):
            words += [rf"mode {_NUMBERS[i]}\b", rf"{_ORDINALS[i]} mode\b"]
        if sweeps[i] and not all(sweeps):
            words += list(_SWEEP_WORDS)
        if amounts[i] is not None and amounts.count(amounts[i]) == 1:
            words.append(rf"{amounts[i]}(?:\s|-)?(?:damage|dmg)\b")
        if targeted[i] and not sweeps[i] and not all(targeted):
            words.append(r"single[- ]target\b")
        patterns.append(re.compile(r"\b(?:" + "|".join(words) + ")"))
    return patterns


def mode_mentions(text: str, patterns: list[re.Pattern]) -> list[tuple[int, int, int]]:
    """(start, end, bullet) of every place ``text`` names a mode, in order."""
    return sorted(
        (m.start(), m.end(), i) for i, pattern in enumerate(patterns) for m in pattern.finditer(text)
    )


def _claim_mode(text: str, start: int, end: int, mentions: list[tuple[int, int, int]], default: int) -> int:
    """The bullet a claim at ``text[start:end]`` speaks of: the nearest mode named before it in its
    clause, else the first named after it there, else ``default`` (the pick)."""
    low = max((m.end() for m in _CLAUSE_BREAK.finditer(text, 0, start)), default=0)
    stop = _CLAUSE_BREAK.search(text, end)
    high = stop.start() if stop else len(text)
    before = [m for m in mentions if low <= m[0] and m[1] <= start]
    if before:
        return before[-1][2]
    after = [m for m in mentions if end <= m[0] and m[1] <= high]
    return after[0][2] if after else default


def other_modes_named(reasoning: str, card: dict, mode_text: str) -> list[str]:
    """The modes of ``card`` other than the line's ``mode_text`` that ``reasoning`` names (ledger #144).

    ``mode_text`` is the line search's name for the line's mode
    (``line_search_moves.Step.modes``: 'deals 4 damage to target creature');
    [] when it matches no single bullet. A mode named only as the rejected
    choice ("mode 2 over the sweep", "instead of mode 1") does not count.
    Returns 'mode 1 (deals 1 damage to each creature…)' strings.
    """
    name = _name(card)
    texts = bullets(str(card.get("oracle_text") or ""))
    wanted = str(mode_text or "").strip()
    if len(texts) < 2 or not wanted:
        return []
    line = [i for i, text in enumerate(texts) if describe_mode(name, text) == wanted]
    if len(line) != 1:
        return []
    said = _CONTRAST.sub(" ", " ".join(str(reasoning or "").lower().split()))
    named = {i for _start, _end, i in mode_mentions(said, mode_patterns(texts, name)) if i != line[0]}
    return [f"mode {i + 1} ({describe_mode(name, texts[i])})" for i in sorted(named)]


# --- the kill claim on a mode pick ------------------------------------------------------------


def _label(body: dict) -> str:
    return f"{body.get('name')} ({body.get('power')}/{body.get('toughness')})"


def mode_kill_check(state: dict, decision: Any, chosen: list[str], reasoning: str) -> ModeKillCheck | None:
    """The pick's reasoning claims a kill its mode cannot make: the facts, and the mode that makes it.

    The claims are ``play_safety.claimed_kills`` (negation-aware) about the
    chosen mode: a claim in a clause that names another mode ("mode 2 would
    kill the Flock, but the sweep gets both tokens") is about that mode. None
    when there is no such claim, the claimed creature's power and toughness are
    unknown, the chosen mode does kill everything claimed, or nothing can be
    read. ``replacement`` is the first other mode that kills every claimed
    creature in one cast and ``decision.selection_is_valid`` accepts; ``extra``
    names what the chosen mode kills (whatever it targets) that it would not.
    """
    if len(chosen or []) != 1 or not modal_options(decision):
        return None
    text = " ".join(str(reasoning or "").lower().split())
    victims = claimed_kills(text, _opposing_creatures(state))
    if not victims:
        return None  # no claim: the board is not read
    menu = read_menu(state, decision)
    if menu is None or chosen[0] not in menu.index:
        return None
    picked = menu.index[chosen[0]]
    mentions = mode_mentions(text, mode_patterns(menu.texts, menu.name))
    claimed = [
        victim
        for victim in victims
        if any(
            _claim_mode(text, s, e, mentions, picked) == picked for s, e in _kill_claim_spans(text, victim)
        )
    ]
    ids = [_int(victim.get("instance_id")) for victim in claimed]
    if not claimed or any(iid is None or iid not in menu.bodies for iid in ids):
        return None
    mine = menu.kills[chosen[0]]
    missed = [iid for iid in ids if not mine.kills(iid)]
    if not missed:
        return None
    replacement = next(
        (
            option_id
            for option_id in menu.index
            if option_id != chosen[0]
            and menu.kills[option_id].kills_all(ids)
            and decision.selection_is_valid([option_id])
        ),
        "",
    )
    extra = (
        tuple(_label(menu.bodies[iid]) for iid in sorted(mine.each - menu.kills[replacement].each - set(ids)))
        if replacement
        else ()
    )
    return ModeKillCheck(
        chosen=chosen[0],
        chosen_mode=menu.mode(chosen[0]),
        claimed=tuple(_label(menu.bodies[iid]) for iid in ids),
        missed=tuple(_label(menu.bodies[iid]) for iid in missed),
        missed_ids=tuple(missed),
        replacement=replacement,
        replacement_mode=menu.mode(replacement) if replacement else "",
        extra=extra,
    )


__all__ = [
    "ModeKillCheck",
    "ModeKills",
    "ModeMenu",
    "describe_mode",
    "modal_options",
    "mode_kill_check",
    "mode_mentions",
    "mode_patterns",
    "other_modes_named",
    "read_menu",
]
