"""Creature-type choices Arena asks as a SelectN over its subtype list (ledger #186).

Herald's Horn ("As this artifact enters, choose a creature type.") reaches the
client as a ``SelectNReq`` with ``listType SelectionListType_StaticSubset`` /
``staticList StaticList_SubTypes`` and 353 ids, each a ``SubType`` enum value
of the card database (``Enums`` table: 1 Angel, 2 Archer, ..., 232 Rabbit;
Player.log 2026-10-19 16:08:20, msgId 469, prompt CardId 561 = the Horn). The
typed decision showed "Option 1" ... "Option 133": the model meant Rabbit,
picked "Option 1" and named Angel (``selectNResp.ids [1]``), and the Mac
bridge's request snapshot had cut the list at 100 ids, so Rabbit (232) was not
even offered.

The ids are labelled from the card database, and our deck's most common
creature type is the deterministic answer when the asking card rewards our own
creatures of that type (the Horn's discount and card selection). The ids are a
log fact; the bridge only submits one.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from arenamcp.color_choice import enum_short

_SUBTYPE_STATIC_LISTS = {"SubTypes", "CreatureTypes", "CreatureSubTypes"}
# The asking card's text rewards our own creatures of the chosen type: a discount on our creature
# spells, a lord, card selection among our creature cards.
_FOR_US = re.compile(
    r"\bcreature spells you cast of the chosen type\b|\bcreatures? you control of the chosen type\b"
    r"|\bof the chosen type you control\b|\bcreature cards? of the chosen type\b"
    r"|\bchosen type get \+|\bspells of the chosen type\b"
)
# ... or works against creatures of that type: never our type by default.
_AGAINST = re.compile(r"\b(?:destroy|exile|sacrifice|can't block|can't attack)\b|\bgets? -")
# The type of the creature tokens a card makes: "create ... 1/1 white Rabbit creature tokens".
_TOKEN_TYPE = re.compile(r"\b([a-z][a-z'-]+) creature tokens?\b")


def subtype_selection_ids(
    *, list_type: Any = None, static_list: Any = None, ids: Iterable[Any] = ()
) -> list[int] | None:
    """The subtype ids a SelectN offers, or None when it is not a creature-type choice."""
    if enum_short(list_type) not in {"Static", "StaticSubset"}:
        return None
    if enum_short(static_list) not in _SUBTYPE_STATIC_LISTS:
        return None
    offered: list[int] = []
    for raw in ids:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in offered:
            offered.append(value)
    return offered


@lru_cache(maxsize=1)
def _db_subtype_names() -> tuple[tuple[int, str], ...]:
    try:
        from arenamcp.card_db import get_card_database

        raw = get_card_database().get_raw_mtgadb()
        names = raw.get_subtype_names() if raw is not None else {}
    except Exception:
        names = {}
    return tuple(sorted(names.items()))


def subtype_names() -> dict[int, str]:
    """{SubType enum value: name} from the MTGA card database; {} without it."""
    return dict(_db_subtype_names())


def is_creature_type_choice(decision: Any) -> bool:
    options = getattr(decision, "options", ()) or ()
    return bool(options) and all((o.meta or {}).get("choice") == "creature_type" for o in options)


def _creature_subtypes(type_line: str) -> list[str]:
    """'Creature — Rabbit Noble' -> ['Rabbit', 'Noble']; [] for a noncreature."""
    head, _dash, tail = str(type_line or "").partition("—")
    if "creature" not in head.lower() and "kindred" not in head.lower() and "tribal" not in head.lower():
        return []
    return [word for word in re.split(r"\s+", tail.strip()) if word]


def _local_seat(state: dict[str, Any]) -> Any:
    seat = state.get("local_seat_id")
    if seat is None:
        seat = next((p.get("seat_id") for p in state.get("players") or [] if p.get("is_local")), None)
    return seat


def creature_type_counts(
    state: dict[str, Any], resolve_card: Callable[[int], dict[str, Any]] | None = None
) -> Counter:
    """How much each creature type is ours: a point per creature card of the deck list
    (``deck_cards``, each copy), a point per creature of ours on the battlefield (tokens too), and
    half a point per copy whose rules text names the type (Hare Apparent's Rabbit tokens)."""
    seat = _local_seat(state)
    names = {name.lower(): name for name in subtype_names().values()}
    counts: Counter = Counter()
    resolve = resolve_card or _resolve_card
    deck = Counter(gid for gid in state.get("deck_cards") or [] if isinstance(gid, int) and gid > 4)
    cards: dict[int, dict[str, Any]] = {}
    for gid in deck:
        try:
            cards[gid] = resolve(gid) or {}
        except Exception:
            cards[gid] = {}
    for gid, copies in deck.items():
        card = cards[gid]
        for subtype in _creature_subtypes(str(card.get("type_line") or "")):
            counts[subtype] += copies
        text = str(card.get("oracle_text") or "").lower()
        for word in set(_TOKEN_TYPE.findall(text)):
            if word in names:
                counts[names[word]] += 0.5 * copies
    for card in state.get("battlefield") or []:
        if not isinstance(card, dict):
            continue
        if card.get("controller_seat_id", card.get("owner_seat_id")) != seat:
            continue
        for subtype in _creature_subtypes(str(card.get("type_line") or "")):
            counts[subtype] += 1
    return counts


def _resolve_card(grp_id: int) -> dict[str, Any]:
    try:
        from arenamcp import server

        return server.get_card_info(grp_id) or {}
    except Exception:
        return {}


@dataclass(frozen=True)
class TypePick:
    option_id: str
    name: str
    reason: str
    obvious: bool  # deterministic enough to skip the model


def pick_creature_type(
    decision: Any,
    state: dict[str, Any],
    *,
    source_text: str = "",
    resolve_card: Callable[[int], dict[str, Any]] | None = None,
) -> TypePick | None:
    """Our deck's most common creature type among the offered ones.

    Obvious (no model call) when it leads the next type and the asking card's
    text rewards our own creatures of the chosen type (``_FOR_US``) without
    working against them. None when this is not a creature-type choice or no
    offered type is ours.
    """
    if not is_creature_type_choice(decision):
        return None
    counts = creature_type_counts(state, resolve_card)
    by_name = {str(option.label).lower(): option for option in decision.options}
    scored = sorted(
        ((score, name) for name, score in counts.items() if name.lower() in by_name and score > 0),
        key=lambda row: (-row[0], row[1]),
    )
    if not scored:
        return None
    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    option = by_name[best.lower()]
    text = str(source_text or "").lower()
    for_us = bool(_FOR_US.search(text)) and not _AGAINST.search(text)
    top = ", ".join(f"{name} {score:g}" for score, name in scored[:3])
    reason = f"{option.label}: our deck's main creature type ({top})"
    return TypePick(option.option_id, option.label, reason, obvious=for_us and best_score > runner_up)


def type_choice_note(decision: Any, state: dict[str, Any], source: str = "") -> str:
    """The prompt line for a creature-type choice: what the ids are and our types."""
    if not is_creature_type_choice(decision):
        return ""
    counts = creature_type_counts(state)
    offered = {str(option.label).lower() for option in decision.options}
    ours = [(name, score) for name, score in counts.most_common() if name.lower() in offered][:4]
    asked = f" for {source}" if source else ""
    line = f"CREATURE TYPE CHOICE{asked}: these options are creature types (by name), not cards."
    if ours:
        line += (
            " Your deck's creature types: " + ", ".join(f"{name} ({score:g})" for name, score in ours) + "."
        )
    return line
