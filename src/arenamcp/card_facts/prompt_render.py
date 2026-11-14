"""Preparatory E3 rendering, default off and not wired into live prompts.

Only verified printed keyword-only creature bodies are supported. All options,
permanents, modal/effect-bearing or ambiguous cards retain caller text exactly.
No warnings are removed; E2/F3 and real prompt-size acceptance remain open.
"""

from __future__ import annotations

import dataclasses
import re

from arenamcp.card_facts.hashing import normalise, rules_hash
from arenamcp.card_facts.schema import CardFacts

_KEYWORDS = frozenset(
    {
        "flying",
        "first strike",
        "double strike",
        "haste",
        "vigilance",
        "trample",
        "reach",
        "deathtouch",
        "lifelink",
        "menace",
        "defender",
    }
)


def source_text(card):
    """Explicit source template for future integration; not a prompt migration."""
    return (
        f"Name: {card['name']}\nMana cost: {card['mana_cost']}\n"
        f"Type: {card['type_line']}\nPower/toughness: {card['power']}/{card['toughness']}\n"
        f"Rules: {card['oracle_text']}"
    )


def _unknown(value):
    if getattr(value, "_unknown_fields", None):
        return True
    if dataclasses.is_dataclass(value):
        return any(_unknown(getattr(value, field.name)) for field in dataclasses.fields(value))
    if isinstance(value, (tuple, list)):
        return any(_unknown(item) for item in value)
    return False


def render(card, record, full_text, *, enabled=False, role="unknown"):
    """Return exact full_text unless a narrow complete source contract passes.

    Only role='stable_deck' is eligible. Callers must not substitute a compact
    descriptor for dynamic battlefield state or option text. The exact template
    match prevents discarding arbitrary caller annotations or other faces.
    """
    if enabled is not True or role != "stable_deck":
        return full_text
    if type(card) is not dict or type(record) is not CardFacts or type(full_text) is not str:
        return full_text
    try:
        if full_text != source_text(card):
            return full_text
        if card.get("related_faces") or card.get("card_faces") or card.get("loyalty") is not None:
            return full_text
        if (
            type(record.schema_version) is not int
            or record.schema_version != 1
            or record.confidence != "high"
            or _unknown(record)
        ):
            return full_text
        if (
            record.effects
            or record.mana
            or record.threat
            or record.relationships
            or record.limited
            or record.notes
        ):
            return full_text
        if (record.name, record.mana_cost, record.type_line) != (
            card["name"],
            card["mana_cost"],
            card["type_line"],
        ):
            return full_text
        if record.rules_hash != rules_hash(
            record.name, record.mana_cost, record.type_line, card["oracle_text"]
        ):
            return full_text
        if "Creature" not in record.type_line or "//" in record.name or record.body.loyalty is not None:
            return full_text
        if not all(re.fullmatch(r"\d+", str(card[key])) for key in ("power", "toughness")):
            return full_text
        if (str(record.body.power), str(record.body.toughness)) != (
            str(card["power"]),
            str(card["toughness"]),
        ):
            return full_text
        keywords = tuple(normalise(word) for word in record.body.keywords)
        if any(word not in _KEYWORDS for word in keywords) or len(set(keywords)) != len(keywords):
            return full_text
        if normalise(card["oracle_text"]) != ", ".join(keywords):
            return full_text
        if len(record.modes) != 1:
            return full_text
        mode = record.modes[0]
        cost = mode.cost
        if (
            mode.name != "cast"
            or mode.stays_on_battlefield is not True
            or mode.cost_text != record.mana_cost
            or cost.mana != record.mana_cost
            or not re.fullmatch(r"(?:\{[0-9WUBRGC]+\})+", record.mana_cost)
            or cost.tap
            or cost.sacrifice is not None
            or cost.life
            or cost.discard
            or cost.exile_from_graveyard
        ):
            return full_text
        compact = (
            f"{record.name} — {record.mana_cost}; {record.type_line}; "
            f"{record.body.power}/{record.body.toughness}"
            + (f"; {', '.join(record.body.keywords)}" if keywords else "")
        )
        return compact if len(compact) < len(full_text) else full_text
    except (AttributeError, KeyError, TypeError, ValueError):
        return full_text
