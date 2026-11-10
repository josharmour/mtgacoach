"""Validate a card-facts record against the printed card.

``validate(record, card)`` compares what the record claims with what the card
actually prints (name, mana cost, type line, power/toughness/loyalty and every
keyword ability its type line or rules text states). It returns a list of
human-readable problems; an empty list means the record agrees with the card.
It never raises — anything unexpected inside becomes a problem entry ending
with ``validator error`` so a broken record can never crash a caller.

The validator keys on mechanics and printed text only. It has no knowledge of
individual cards and no card-name-specific logic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arenamcp.card_facts.hashing import normalise
from arenamcp.card_facts.schema import CardFacts


@dataclass
class PrintedCard:
    """The printed card as the card source states it (e.g. Scryfall fields).

    ``oracle_text`` is all faces' rules text concatenated with newlines.
    """

    name: str
    mana_cost: str = ""
    type_line: str = ""
    oracle_text: str = ""
    power: str | None = None
    toughness: str | None = None
    loyalty: str | None = None


# Keyword abilities this validator recognises when stated by a type line or
# rules text. Evergreen keyword abilities only (Comprehensive Rules ch. 702);
# anything else simply is not checked rather than wrongly demanded.
KEYWORD_PHRASES: tuple[str, ...] = (
    "double strike",
    "first strike",
    "deathtouch",
    "hexproof",
    "indestructible",
    "lifelink",
    "menace",
    "protection",
    "vigilance",
    "ward",
    "flying",
    "haste",
    "prowess",
    "reach",
    "trample",
    "defender",
    "flash",
)

# A keyword occurrence is *granted* rather than intrinsic when one of these
# words sits between the start of its sentence/line and the keyword ("...a
# token with trample", "...gains vigilance"). Granted keywords belong to other
# permanents or to future states; they are not required in ``body.keywords``.
_GRANT_CUE = re.compile(r"\b(with|gains?|has|have|gets?)\b[^.;:\n]*$")

# Cast/use modes whose Comprehensive-Rules behaviour removes the permanent from
# the battlefield as part of resolving that mode. Keyed on mechanic name so it
# applies to every card with that mechanic.
_LEAVES_BATTLEFIELD_MODES = frozenset({"evoke", "flashback", "aftermath"})


def _stated_keywords(printed_text: str) -> set[str]:
    """Keywords intrinsically stated by ``printed_text`` (type line + rules)."""
    stated: set[str] = set()
    lowered = printed_text.casefold()
    for phrase in KEYWORD_PHRASES:
        for match in re.finditer(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered):
            # Text since the last sentence/line boundary before this match.
            prefix = lowered[: match.start()]
            prefix = prefix[max(prefix.rfind("\n"), prefix.rfind("."), prefix.rfind(";")) + 1 :]
            if _GRANT_CUE.search(prefix):
                continue  # granted to something else / later state
            stated.add(phrase)
            break
    return stated


def _check_stats(record_body, card: PrintedCard) -> list[str]:
    problems: list[str] = []
    pairs = (
        ("power", card.power),
        ("toughness", card.toughness),
        ("loyalty", card.loyalty),
    )
    for attr, printed in pairs:
        if printed is None:
            continue
        claimed = getattr(record_body, attr)
        if claimed is None:
            problems.append(
                f"{attr}: printed '{printed}' but record states none"
            )
        elif normalise(str(claimed)) != normalise(str(printed)):
            problems.append(
                f"{attr}: printed '{printed}' but record states '{claimed}'"
            )
    return problems


def _check_modes(record: CardFacts, card: PrintedCard) -> list[str]:
    problems: list[str] = []
    for mode in record.modes:
        mode_name = normalise(mode.name)
        if not mode_name:
            continue
        if (
            mode_name == "cast"
            and mode.cost.mana
            and normalise(mode.cost.mana) != normalise(card.mana_cost)
        ):
            problems.append(
                f"mode '{mode.name}': cast cost {mode.cost.mana}"
                f" but printed mana cost is {card.mana_cost}"
            )
        if (
            any(word in mode_name for word in _LEAVES_BATTLEFIELD_MODES)
            and mode.stays_on_battlefield
        ):
            problems.append(
                f"mode '{mode.name}': marked as staying on the battlefield,"
                " but that mechanic removes the permanent"
            )
    return problems


def validate(record: CardFacts, card: PrintedCard) -> list[str]:
    """Compare ``record`` with the printed ``card``; return problems ([] if ok)."""
    try:
        return _validate(record, card)
    except Exception as exc:  # never raise — surface as a problem instead
        return [f"validator error while checking {record.name!r}: {exc!r}"]


def _validate(record: CardFacts, card: PrintedCard) -> list[str]:
    problems: list[str] = []

    if normalise(record.name) != normalise(card.name):
        problems.append(
            f"name: printed '{card.name}' but record states '{record.name}'"
        )

    if normalise(record.mana_cost) != normalise(card.mana_cost):
        problems.append(
            f"mana_cost: printed '{card.mana_cost}' but record states"
            f" '{record.mana_cost}'"
        )

    problems.extend(_check_stats(record.body, card))

    printed_text = f"{card.type_line}\n{card.oracle_text}"
    stated = _stated_keywords(printed_text)
    claimed = {normalise(k) for k in record.body.keywords}
    for phrase in sorted(stated - claimed):
        problems.append(f"keyword '{phrase}' stated by the card but missing")
    for phrase in sorted(claimed - stated):
        if phrase not in printed_text.casefold():
            problems.append(
                f"keyword '{phrase}' claimed by the record but not stated"
                " by the type line or rules text"
            )

    problems.extend(_check_modes(record, card))

    for index, entry in enumerate(record.effects):
        effect = entry.effect
        effect_type = getattr(effect, "effect_type", "")
        if effect_type == "Unknown" and not getattr(effect, "text", ""):
            problems.append(
                f"effects[{index}]: unknown effect carries no original sentence"
            )

    return problems


__all__ = ["PrintedCard", "KEYWORD_PHRASES", "validate"]
