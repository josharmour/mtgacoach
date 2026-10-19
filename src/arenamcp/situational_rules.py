"""Prompt rules that apply only when their subject is in the game.

The decision, planner and coach system prompts grew special cases from past
games: Vehicles and crew, animated lands and artifacts, haste-granting
Equipment, commanders, library searches. Every call carried all of them: in a
Limited game with none of those cards, ~800 of a typed decision's ~1,950
system-prompt tokens (2026-10-18 prompt audit). A rule now names its subject
(:data:`SUBJECTS`); a prompt leaves out a rule whose subject is absent from our
visible cards and the pending decision. With every subject present the text is
the full prompt, unchanged.

The check is deliberately generous: any of our cards in any visible zone, or
an option label, that mentions the subject keeps its rules. The coach's
Secrets of Strixhaven keyword primer ("set_mechanics") counts either side's
cards.
``ARENAMCP_SITUATIONAL_RULES=0`` sends every rule.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable, Sequence
from typing import Any

logger = logging.getLogger(__name__)

SUBJECTS = frozenset({"commander", "vehicle", "animation", "equipment", "library", "set_mechanics"})
_ZONES = ("hand", "battlefield", "stack", "command", "graveyard", "exile")
# The Secrets of Strixhaven keywords the coach prompt explains (coach_prompts):
# present on any visible card, either side.
_SET_MECHANICS = re.compile(
    r"\b(?:prepared?|increment|paradigm|infusion|opus|repartee|flashback|converge)\b", re.I
)
_VEHICLE = re.compile(r"\bvehicle\b|\bcrew(?:s|ed)?\b", re.I)
_ANIMATION = re.compile(
    r"\bbecomes? (?:a|an)\b[^.]*\bcreature\b|\bit'?s an? [^.]*\bcreature\b|\bis (?:also )?an? [^.]*\bcreature\b",
    re.I,
)
_EQUIPMENT = re.compile(r"\bequip", re.I)


def compose(parts: Sequence[tuple[str | None, str]], subjects: Iterable[str] = SUBJECTS) -> str:
    """The text of ``parts`` (subject, text) whose subject is None (always) or in ``subjects``."""
    wanted = set(subjects)
    return "".join(text for subject, text in parts if subject is None or subject in wanted)


def subjects_in_play(
    state: dict | None, decision: Any = None, extra_text: Iterable[str] = ()
) -> frozenset[str]:
    """The :data:`SUBJECTS` our visible cards, the pending decision or ``extra_text`` (menu lines) touch.

    A snapshot it cannot read gets every subject: a rule is never dropped by an error.
    """
    if os.environ.get("ARENAMCP_SITUATIONAL_RULES") == "0":
        return SUBJECTS
    try:
        return _subjects_in_play(state if isinstance(state, dict) else {}, decision, extra_text)
    except Exception as error:
        logger.debug("situational rules: sending every rule (%s)", error)
        return SUBJECTS


def _subjects_in_play(state: dict, decision: Any, extra_text: Iterable[str]) -> frozenset[str]:
    local = state.get("local_seat_id") or next(
        (p.get("seat_id") for p in state.get("players") or [] if isinstance(p, dict) and p.get("is_local")),
        None,
    )
    texts: list[str] = [str(text) for text in extra_text]
    for option in getattr(decision, "options", ()) or ():
        texts.append(str(getattr(option, "label", "") or ""))
    found: set[str] = set()
    for zone in _ZONES:
        for card in state.get(zone) or []:
            if not isinstance(card, dict):
                continue
            card_text = f"{card.get('type_line') or ''}\n{card.get('oracle_text') or ''}"
            if _SET_MECHANICS.search(card_text):
                found.add("set_mechanics")
            side = card.get("controller_seat_id") or card.get("owner_seat_id")
            if local is not None and side not in (None, local):
                continue
            if zone == "command":
                found.add("commander")
            texts.append(card_text)
    text = "\n".join(texts)
    if _SET_MECHANICS.search(text):
        found.add("set_mechanics")
    if getattr(decision, "min_weight", None) is not None or _VEHICLE.search(text):
        found.update(("vehicle", "animation"))  # crewing animates the Vehicle
    if _ANIMATION.search(text):
        found.add("animation")
    if _EQUIPMENT.search(text):
        found.add("equipment")
    from arenamcp.deck_strategy import commander_ids
    from arenamcp.match_context import library_in_play

    if commander_ids(state):
        found.add("commander")

    if (
        getattr(decision, "request_type", None) == "Search"
        or state.get("library_listed")
        or library_in_play(state)
    ):
        found.add("library")
    return frozenset(found)
