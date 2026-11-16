"""Public-board conditions for land mana and entry forecasts (#303).

Printed colour production is potential, not permission to activate a Verge.
Unknown entry conditions stay tapped; this does not promise future draws/types.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from arenamcp.mulligan_policy import _land_colors, _symbols


def _text(card: dict) -> str:
    return re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).lower()


def basic_types(card: dict) -> set[str]:
    # Never infer basic land types from a card's name (e.g. Sunbillow Verge).
    text = str(card.get("type_line") or "").lower()
    types = [str(t).lower().removeprefix("subtype_") for t in card.get("subtypes") or []]
    return set(re.findall(r"\b(?:plains|island|swamp|mountain|forest)\b", text)) | set(types)


def land_colors(card: dict, permanents=()) -> frozenset[str]:
    text = _text(card)
    if "activate only if you control" not in text:
        return _land_colors(card)
    board_types = set().union(*(basic_types(c) for c in permanents)) if permanents else set()
    colors = set()
    # Cached Arena rules can flatten newlines. Attach an activation condition
    # to its immediately preceding mana ability, not to every ability on the land.
    abilities = re.finditer(r"\{o?t\}:\s*add\s+([^.]+)\.\s*(activate only if you control[^.]*\.)?", text)
    for ability in abilities:
        condition = ability.group(2)
        if condition:
            match = re.fullmatch(
                r"activate only if you control (?:a|an) (plains|island|swamp|mountain|forest)"
                r" or (?:a|an) (plains|island|swamp|mountain|forest)\.",
                condition,
            )
            if not match or not (set(match.groups()) & board_types):
                continue
        for symbol in _symbols(ability.group(1).upper().replace("O", "o")):
            colors.update(part for part in symbol.split("/") if part in "WUBRGC")
    # Intrinsic basic-type mana abilities survive conditional printed abilities.
    colors.update(
        {"plains": "W", "island": "U", "swamp": "B", "mountain": "R", "forest": "G"}[t]
        for t in basic_types(card)
        if t in {"plains", "island", "swamp", "mountain", "forest"}
    )
    return frozenset(colors)


_LAND_COUNT_ENTRY = re.compile(
    r"enters (?:the battlefield )?tapped unless you control two or (more|fewer) other lands\."
)


def _land_count_entry(card, permanents, direction):
    """Current types bound the count even if a face or a permanent is unidentified."""
    if permanents is None:
        return None
    minimum = maximum = 0
    for obj in permanents:
        if obj.get("is_phased_out") or (
            card.get("instance_id") and card["instance_id"] == obj.get("instance_id")
        ):
            continue
        types = obj.get("card_types")
        if types:
            is_land = any(str(t).lower().removeprefix("cardtype_") == "land" for t in types)
        else:
            line = str(obj.get("type_line") or "")
            is_land = (
                bool(re.search(r"\bland\b", line.split("—", 1)[0], re.I))
                if line and "//" not in line
                else None
            )
        minimum += is_land is True
        maximum += is_land is not False
    if direction == "fewer":
        return True if minimum > 2 else False if maximum <= 2 else None
    return True if maximum < 2 else False if minimum >= 2 else None


def enters_tapped(card: dict, permanents=None) -> bool:
    text = _text(card)
    if not re.search(r"enters (?:the battlefield )?tapped", text):
        return False
    if permanents is None:
        return True
    count_condition = _LAND_COUNT_ENTRY.search(text)
    if count_condition:
        return _land_count_entry(card, permanents, count_condition[1]) is not False
    check = re.search(
        r"enters (?:the battlefield )?tapped unless you control (?:a|an) (plains|island|swamp|mountain|forest)\b(?: or (?:a|an) (plains|island|swamp|mountain|forest)\b)?\.",
        text,
    )
    if check:
        return not any(basic_types(c) & set(check.groups()) for c in permanents if not c.get("is_phased_out"))
    return True


def mana_source(card: dict, permanents=()) -> SimpleNamespace:
    colors = land_colors(card, permanents)
    # Carry the public land identity so later simulated drops can enable an
    # existing Verge and later slow lands can count prior drops correctly.
    return SimpleNamespace(produces=colors or frozenset({"C"}), name=card.get("name") or "?", land_card=card)


def refresh_sources(sources, permanents):
    return [mana_source(s.land_card, permanents) if getattr(s, "land_card", None) else s for s in sources]
