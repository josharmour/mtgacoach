"""Colour choices Arena asks as a SelectN over a static colour list.

"As it enters, choose a color" (Room of Refuge) reaches the client as a
``SelectNReq`` with ``listType SelectionListType_Static`` /
``staticList StaticList_Colors`` and **no ids** (Player.log 2026-10-07
18:39:38, msgId 27). The client's ``SelectColorWorkflow`` offers White, Blue,
Black, Red, Green for an empty id list (a ``StaticSubset`` carries the
allowed ids) and answers ``SubmitSelection((uint)CardColor)``: White=1,
Blue=2, Black=3, Red=4, Green=5 — the user's manual answer that night was
``selectNResp.ids [3]`` (Black). Mana-colour requests (``StaticList_ManaColors``
or ``SelectionContext_ManaFromAbility``) use the same 1..5 numbering
(``ManaColor`` enum).

Everything here is log-derivable, so the coach can name the colour without a
bridge; the bridge only submits the id.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

CARD_COLORS: dict[int, str] = {0: "Colorless", 1: "White", 2: "Blue", 3: "Black", 4: "Red", 5: "Green"}
COLOR_LETTERS: dict[str, str] = {"White": "W", "Blue": "U", "Black": "B", "Red": "R", "Green": "G"}
_NAME_TO_ID = {name.lower(): color_id for color_id, name in CARD_COLORS.items()}
_LETTER_TO_ID = {letter: _NAME_TO_ID[name.lower()] for name, letter in COLOR_LETTERS.items()}
_DEFAULT_IDS = [1, 2, 3, 4, 5]  # SelectColorWorkflow._staticDefaultWithoutColorless
_COLOR_STATIC_LISTS = {"Colors", "CardColors", "ManaColors"}
_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def enum_short(value: Any) -> str:
    """``"StaticList_Colors"`` / ``{"e": "Colors"}`` / ``"Colors"`` -> ``"Colors"``."""
    if isinstance(value, dict):
        value = value.get("e", "")
    text = "" if value is None else str(value)
    return text.rsplit("_", 1)[-1] if "_" in text else text


def color_selection_ids(
    *,
    list_type: Any = None,
    static_list: Any = None,
    context: Any = None,
    ids: Iterable[Any] = (),
    is_color: bool | None = None,
) -> list[int] | None:
    """The colour ids a SelectN offers, or None when it is not a colour choice.

    ``is_color`` is the client's own ``IsCardColorSelection`` /
    ``IsManaColorSelection`` verdict when a bridge read it; the raw enum
    fields decide otherwise (the log has only those).
    """
    list_kind = enum_short(list_type)
    static_kind = enum_short(static_list)
    is_static_colors = list_kind in {"Static", "StaticSubset"} and static_kind in _COLOR_STATIC_LISTS
    if not (is_color is True or is_static_colors or enum_short(context) == "ManaFromAbility"):
        return None
    offered: list[int] = []
    for raw in ids:
        try:
            color_id = int(raw)
        except (TypeError, ValueError):
            continue
        if color_id in CARD_COLORS and color_id not in offered:
            offered.append(color_id)
    return sorted(offered) if offered else list(_DEFAULT_IDS)


def color_options(color_ids: Iterable[int]) -> list[dict[str, Any]]:
    """Bridge/prompt payload entries: ``{"id": 2, "name": "Blue", "symbol": "U"}``."""
    return [
        {
            "id": color_id,
            "name": CARD_COLORS[color_id],
            "symbol": COLOR_LETTERS.get(CARD_COLORS[color_id], "C"),
        }
        for color_id in color_ids
        if color_id in CARD_COLORS
    ]


def color_ids_for_names(names: Iterable[str], offered: Iterable[int]) -> list[int]:
    """Map colour words or letters ("Blue", "blue", "U") to offered colour ids."""
    allowed = {int(i) for i in offered}
    picked: list[int] = []
    for name in names:
        key = str(name or "").strip()
        color_id = _NAME_TO_ID.get(key.lower())
        if color_id is None and len(key) == 1:
            color_id = _LETTER_TO_ID.get(key.upper())
        if color_id is not None and color_id in allowed and color_id not in picked:
            picked.append(color_id)
    return picked


def is_color_choice(decision: Any) -> bool:
    options = getattr(decision, "options", ()) or ()
    return bool(options) and all((o.meta or {}).get("choice") == "color" for o in options)


@dataclass(frozen=True)
class ColorPick:
    option_id: str
    name: str
    reason: str
    obvious: bool  # deterministic enough to skip the model


def _pips(mana_cost: str) -> dict[str, float]:
    """Coloured pips in a cost; hybrid halves split across their colours."""
    pips: dict[str, float] = {}
    for symbol in _SYMBOL_RE.findall(mana_cost or ""):
        parts = [part for part in symbol.upper().split("/") if part in _LETTER_TO_ID]
        for part in parts:
            pips[part] = pips.get(part, 0.0) + 1.0 / len(parts)
    return pips


def _produced(card: dict[str, Any]) -> set[str]:
    """Colours a land makes: ``color_production`` names or ``Add {B}`` in its text."""
    made = {COLOR_LETTERS.get(str(name), "") for name in card.get("color_production") or []}
    text = str(card.get("oracle_text") or "")
    for match in re.finditer(r"Add (\{[^.]*?\})", text):
        made.update(_pips(match.group(1)))
    return {letter for letter in made if letter}


def _is_land(card: dict[str, Any]) -> bool:
    types = [str(t).lower() for t in card.get("card_types") or []]
    return "land" in types or "land" in str(card.get("type_line") or "").lower()


def pick_color(decision: Any, game_state: dict[str, Any]) -> ColorPick | None:
    """Choose the colour our hand needs most and our lands make least.

    Score per offered colour: 3 x pips in hand + 1 x pips on our other
    nonland cards, minus one per land of ours already producing it (on the
    battlefield or in hand). Obvious when one colour leads by a full pip or
    every other colour scores nothing — then no model call is needed.
    """
    if not is_color_choice(decision):
        return None
    seat = game_state.get("local_seat_id")
    if seat is None:
        for player in game_state.get("players") or []:
            if player.get("is_local"):
                seat = player.get("seat_id")
    hand = [c for c in game_state.get("hand") or [] if isinstance(c, dict)]
    others = [
        c
        for zone in ("battlefield", "graveyard", "stack", "exile")
        for c in game_state.get(zone) or []
        if isinstance(c, dict)
        and (seat is None or c.get("controller_seat_id", c.get("owner_seat_id")) == seat)
    ]
    need: dict[str, float] = {}
    wanted_by: dict[str, list[str]] = {}
    for weight, cards in ((3.0, hand), (1.0, others)):
        for card in cards:
            if _is_land(card):
                continue
            for letter, count in _pips(str(card.get("mana_cost") or "")).items():
                need[letter] = need.get(letter, 0.0) + weight * count
                if weight == 3.0:
                    wanted_by.setdefault(letter, []).append(str(card.get("name") or "a card"))
    covered: dict[str, int] = {}
    for card in hand + others:
        if _is_land(card):
            for letter in _produced(card):
                covered[letter] = covered.get(letter, 0) + 1
    scored: list[tuple[float, int, Any]] = []
    for option in decision.options:
        letter = str(option.meta.get("color") or "")
        score = need.get(letter, 0.0) - covered.get(letter, 0)
        scored.append((score, int(option.meta.get("color_id") or 0), option))
    if not scored:
        return None
    scored.sort(key=lambda row: (-row[0], row[1]))
    best_score, _, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    letter = str(best.meta.get("color") or "")
    pips_in_hand = need.get(letter, 0.0) / 3.0 if letter in wanted_by else 0.0
    obvious = best_score > 0 and (runner_up <= 0 or best_score - runner_up >= 1.0)
    if best_score <= 0:
        reason = f"{best.label}: no colour is needed yet; defaulting to {best.label}"
    else:
        names = ", ".join(dict.fromkeys(wanted_by.get(letter, [])))
        reason = f"{best.label}: {pips_in_hand:g} {best.label.lower()} pip(s) in hand ({names})"
        if covered.get(letter):
            reason += f", {covered[letter]} land(s) already make it"
        else:
            reason += ", no land of ours makes it"
    return ColorPick(best.option_id, best.label, reason, obvious)
