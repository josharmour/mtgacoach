"""Known color protection in current combat; no inference from grant spells."""

import re
from functools import lru_cache

from arenamcp.player_protection import protected_from_player_source

COLORS = {"white": "W", "blue": "U", "black": "B", "red": "R", "green": "G"}
# Verified in Arena's Abilities/Localizations_enUS, 2026-11-01.
COLOR_PROTECTION_ABILITIES = {185: "W", 186: "U", 187: "B", 188: "R", 189: "G"}


def _raw(card):
    return card.get("_card") or card


def source_colors(card):
    """Current source colors, or None when unknown. Explicit [] is colorless."""
    raw = _raw(card)
    values = None
    for field in ("_source_colors", "modified_colors", "colors"):
        for obj in (card, raw):
            if obj.get(field) is not None:
                values = obj[field]
                break
        if values is not None:
            break
    if values is None:
        return None
    found = set()
    for value in values:
        value = str(value).lower().removeprefix("color_").removeprefix("colortype_")
        if value in COLORS:
            found.add(COLORS[value])
        elif value.upper() in COLORS.values():
            found.add(value.upper())
        elif value not in ("c", "colorless"):
            return None  # unknown enum must not be mistaken for colorless
    return frozenset(found)


@lru_cache(maxsize=2048)
def _printed(oracle):
    text = re.sub(r"<[^>]*>", "", oracle).lower()
    while re.search(r"\([^()]*\)", text):
        text = re.sub(r"\([^()]*\)", "", text)
    found = set()
    for phrase in re.split(r"[\n,.;]", text):
        match = re.fullmatch(r"\s*protection from ([a-z ]+)\s*", phrase)
        if not match:
            continue
        words = match[1].replace(" and from ", " and ").split(" and ")
        if all(w in COLORS or w in ("everything", "colorless", "monocolored", "multicolored") for w in words):
            found.update(COLORS.get(w, w) for w in words)
    return frozenset(found)


def protection_qualities(card):
    raw = _raw(card)
    printed = _printed(str(raw.get("oracle_text") or ""))
    # Future forecasts retain printed protection. Current-only grants have no
    # established duration beyond this turn; don't turn Brave into a shield
    # against the opponent's later attack (#273).
    found = set(printed)
    if not card.get("_protection_future"):
        for obj in (raw, card):
            for ability in obj.get("ability_grp_ids") or []:
                if ability in COLOR_PROTECTION_ABILITIES:
                    found.add(COLOR_PROTECTION_ABILITIES[ability])
            for keyword in obj.get("keywords") or []:
                found.update(_printed(str(keyword)))
            for granted in obj.get("granted_abilities") or []:
                found.update(_printed(str(granted)))
    removed = [str(v).lower() for v in raw.get("removed_abilities") or []]
    if any(v in ("all abilities", "protection") for v in removed):
        return frozenset()
    for value in removed:
        found.difference_update(_printed(value))
    return frozenset(found)


def protected_from_source(card, source):
    if protected_from_player_source(card, source):
        return True
    qualities = protection_qualities(card)
    if not qualities:
        return False
    if "everything" in qualities:
        return True
    colors = source_colors(source)
    if colors is None:
        return False
    return bool(
        qualities & colors
        or ("colorless" in qualities and not colors)
        or ("monocolored" in qualities and len(colors) == 1)
        or ("multicolored" in qualities and len(colors) > 1)
    )


def damage_is_unpreventable(source):
    raw = _raw(source)
    if source.get("damage_cant_be_prevented") or raw.get("damage_cant_be_prevented"):
        return True
    text = str(raw.get("oracle_text") or "").lower().replace("’", "'")
    # Only unconditional statements that apply to this source too. A spell
    # granting this later or a conditional ability is not active evidence.
    return any(
        line.strip()
        in (
            "damage can't be prevented.",
            "combat damage that would be dealt by creatures you control can't be prevented.",
        )
        for line in text.splitlines()
    )


def prevents_damage(card, source):
    return protected_from_source(card, source) and not damage_is_unpreventable(source)


def protection_key(card):
    colors = source_colors(card)
    return (
        None if colors is None else tuple(sorted(colors)),
        tuple(sorted(protection_qualities(card))),
        damage_is_unpreventable(card),
    )


def future_protection(card):
    """Copy a projected body; never mutate the live snapshot or its _card."""
    if protection_qualities(card) <= _printed(str(_raw(card).get("oracle_text") or "")):
        return card
    return {**card, "_protection_future": True}
