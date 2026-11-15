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
from dataclasses import dataclass, field

from arenamcp.ability_synthesizer import Draw, Pump, UnknownEffect
from arenamcp.card_facts.hashing import normalise
from arenamcp.card_facts.schema import CardFacts


@dataclass
class PrintedCard:
    """The printed card as the card source states it (e.g. Scryfall fields).

    ``oracle_text`` is the source projection. Full raw ``card_faces`` are
    retained when present; executable compilation currently refuses them.
    """

    name: str
    mana_cost: str = ""
    type_line: str = ""
    oracle_text: str = ""
    power: str | None = None
    toughness: str | None = None
    loyalty: str | None = None
    card_faces: list[dict] = field(default_factory=list)
    layout: str | None = None
    linked_face_ids: list[int] = field(default_factory=list)
    source_print_id: str | None = None
    single_face_source: bool = True


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


def _aura_keywords(line):
    # A granted or referenced keyword inside a sentence does not describe the
    # Aura itself. Unrecognised mechanic lines retain their full printed text.
    parts = [v.strip().casefold() for v in line.split(",")]
    if all(v in KEYWORD_PHRASES for v in parts):
        return set(parts)
    if re.fullmatch(r"ward (?:\{[^{}]+\})+", line, re.I):
        return {"ward"}
    if re.fullmatch(r"protection from [\w -]+", line, re.I):
        return {"protection"}
    return set()


def _aura_line_matches(entry, line):
    if entry.conditions:
        return False
    lowered = line.casefold()
    if lowered.startswith(("when this aura enters,", "when this aura enters the battlefield,")):
        if entry.trigger != "ETB":
            return False
        if lowered in {
            "when this aura enters, draw a card.",
            "when this aura enters the battlefield, draw a card.",
        } and entry.effect == Draw(count=1):
            return True
    elif lowered.startswith(("when ", "whenever ", "at ")):
        if entry.trigger != line.partition(",")[0]:
            return False
    elif ":" in line.split(".")[0]:
        if entry.trigger not in ("", "activate"):
            return False
    else:
        if entry.trigger not in ("", "static"):
            return False
        pump = re.fullmatch(r"Enchanted creature gets ([+-]\d+)/([+-]\d+)\.", line, re.I)
        if pump and entry.effect == Pump(int(pump[1]), int(pump[2]), "enchanted creature"):
            return True
    return isinstance(entry.effect, UnknownEffect) and entry.effect.text == line


def _aura_problems(record, card):
    lines = [line.strip() for line in card.oracle_text.splitlines() if line.strip()]
    problems = []
    if card.card_faces:
        return ["Whole-Aura validation requires an isolated single-face print"]
    if not any(line.startswith("Enchant ") for line in lines):
        problems.append("Aura has no verified printed attachment restriction")
    expected = set().union(*(_aura_keywords(line) for line in lines))
    claimed = {normalise(k) for k in record.body.keywords}
    if len(claimed) != len(record.body.keywords):
        problems.append("Aura body claims duplicate keyword abilities")
    for k in sorted(claimed - expected):
        problems.append(f"keyword '{k}' is not intrinsic to the printed Aura")
    for k in sorted(expected - claimed):
        problems.append(f"keyword '{k}' stated by the Aura but missing")
    for key in ("power", "toughness", "loyalty"):
        if getattr(card, key) is None and getattr(record.body, key) is not None:
            problems.append(f"Aura body has unprinted {key}")
    remaining = list(record.effects)
    for line in lines:
        if all(word.strip().casefold() in KEYWORD_PHRASES for word in line.split(",")):
            continue
        for i, entry in enumerate(remaining):
            if _aura_line_matches(entry, line):
                del remaining[i]
                break
        else:
            kind = "attachment restriction" if line.startswith("Enchant ") else "ability"
            problems.append(f"Aura {kind} missing or changed: {line}")
    if remaining:
        problems.append("Aura has extra, duplicate or partial effects without a complete printed ability")
    return problems


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
            problems.append(f"{attr}: printed '{printed}' but record states none")
        elif normalise(str(claimed)) != normalise(str(printed)):
            problems.append(f"{attr}: printed '{printed}' but record states '{claimed}'")
    return problems


def _check_modes(record: CardFacts, card: PrintedCard) -> list[str]:
    problems: list[str] = []
    for mode in record.modes:
        mode_name = normalise(mode.name)
        if not mode_name:
            continue
        if mode_name == "cast" and normalise(mode.cost.mana) != normalise(card.mana_cost):
            problems.append(
                f"mode '{mode.name}': cast cost {mode.cost.mana} but printed mana cost is {card.mana_cost}"
            )
        if any(word in mode_name for word in _LEAVES_BATTLEFIELD_MODES) and mode.stays_on_battlefield:
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
        problems.append(f"name: printed '{card.name}' but record states '{record.name}'")

    if normalise(record.mana_cost) != normalise(card.mana_cost):
        problems.append(f"mana_cost: printed '{card.mana_cost}' but record states '{record.mana_cost}'")

    if normalise(record.type_line) != normalise(card.type_line):
        problems.append(f"type_line: printed '{card.type_line}' but record states '{record.type_line}'")

    problems.extend(_check_stats(record.body, card))

    printed_text = f"{card.type_line}\n{card.oracle_text}"
    if "Aura" in card.type_line.split():
        problems.extend(_aura_problems(record, card))
    else:
        stated = _stated_keywords(printed_text)
        claimed = {normalise(k) for k in record.body.keywords}
        for phrase in sorted(stated - claimed):
            problems.append(f"keyword '{phrase}' stated by the card but missing")
        for phrase in sorted(claimed - stated):
            if phrase not in printed_text.casefold():
                problems.append(
                    f"keyword '{phrase}' claimed by the record but not stated by the type line or rules text"
                )

    problems.extend(_check_modes(record, card))

    for index, entry in enumerate(record.effects):
        effect = entry.effect
        effect_type = getattr(effect, "effect_type", "")
        if effect_type == "Unknown" and not getattr(effect, "text", ""):
            problems.append(f"effects[{index}]: unknown effect carries no original sentence")

    return problems


__all__ = ["PrintedCard", "KEYWORD_PHRASES", "validate"]
