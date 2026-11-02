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


def _sig(value):
    """Hashable form of one field value (a list becomes a tuple)."""
    return tuple(value) if isinstance(value, list) else value


def _colors_signature(card, inner):
    """The first color field ``source_colors`` would read, as a hashable value."""
    for field in ("_source_colors", "modified_colors", "colors"):
        for obj in (card, inner):
            if obj is None:
                continue
            value = obj.get(field)
            if value is not None:
                return _sig(value)
    return None


def _may_have_protection(card, raw):
    """Whether any field other than colors can contribute a protection quality."""
    if card.get("_protection_future"):
        return True
    # Every English printing of the ability contains this stem; checking it
    # without ``lower()`` keeps the common no-protection body allocation-free.
    # ``damage_is_unpreventable`` also reads two printed lines, so a body that
    # says damage can't be prevented must take the slow path too.
    text = str(raw.get("oracle_text") or "")
    if "rotection" in text or "prevented" in text:
        return True
    objects = (card,) if raw is card else (card, raw)
    for obj in objects:
        for ability in obj.get("ability_grp_ids") or ():
            if ability in COLOR_PROTECTION_ABILITIES:
                return True
        for field in ("keywords", "granted_abilities"):
            for value in obj.get(field) or ():
                if "rotection" in str(value):
                    return True
    return False


def protection_matters(card) -> bool:
    """Whether ``protection_key`` of this card can differ from the plain default.

    A fight whose bodies all answer False cannot be changed by protection at
    all: every key would be ``(colors, (), False)`` and colors only matter to a
    body that has a protection quality.
    """
    inner = card.get("_card")
    raw = inner if isinstance(inner, dict) and inner and inner is not card else card
    if card.get("damage_cant_be_prevented") or raw.get("damage_cant_be_prevented"):
        return True
    return _may_have_protection(card, raw)


def protection_signature(card):
    """Every input ``protection_key`` reads, as a cheap hashable fingerprint.

    ``protection_key`` is memoised per search; this lets the memo notice an
    in-place refresh of the body or of its nested ``_card`` backing dict (a
    ``modified_colors`` change must not serve a stale key). It reads the same
    fields ``protection_key`` does, without the parsing and set work.

    Most bodies have no protection at all. For those the key's quality set is
    always empty, so only the color fields and the unpreventable flag can
    change it; skip the other ten field reads and tuple entries.
    """
    inner = card.get("_card")
    if not isinstance(inner, dict) or not inner or inner is card:
        inner = None
    raw = inner if inner is not None else card
    colors = _colors_signature(card, inner)
    if not _may_have_protection(card, raw):
        return ("plain", colors, bool(card.get("damage_cant_be_prevented")))
    parts = [
        colors,
        bool(card.get("_protection_future")),
        _sig(card.get("ability_grp_ids")),
        _sig(card.get("keywords")),
        _sig(card.get("granted_abilities")),
        bool(card.get("damage_cant_be_prevented")),
        raw.get("oracle_text"),
        _sig(raw.get("removed_abilities")),
    ]
    if inner is not None:
        parts.extend(
            (
                _sig(inner.get("ability_grp_ids")),
                _sig(inner.get("keywords")),
                _sig(inner.get("granted_abilities")),
                bool(inner.get("damage_cant_be_prevented")),
            )
        )
    return tuple(parts)


def future_protection(card):
    """Copy a projected body; never mutate the live snapshot or its _card."""
    if protection_qualities(card) <= _printed(str(_raw(card).get("oracle_text") or "")):
        return card
    return {**card, "_protection_future": True}
