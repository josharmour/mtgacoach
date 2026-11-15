"""Narrow, reproducible N1 threat annotations from typed and printed facts.

No keyword/strategy guessing. Only an entire unconditional Draw/GainLife spell
sentence with one matching untriggered typed effect is supported. Everything
else emits no claims. Derived here means reproducible, not semantically reviewed.
These annotations never become executable effects or a clock estimate.
"""

from __future__ import annotations

import json
import re

from arenamcp.ability_synthesizer import Draw, GainLife
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.schema import CardFacts, EffectEntry
from arenamcp.card_facts.validate import PrintedCard, validate

_NUMBERS = {
    "a": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def derive_threat(record: CardFacts, printed: PrintedCard) -> dict:
    """Produce only supported exact claims; no implicit promotion or mutation."""
    empty = {"version": 1, "plan": [], "clock": [], "answers": []}
    if type(record) is not CardFacts or type(printed) is not PrintedCard:
        return empty
    if any(
        type(getattr(printed, field)) is not str
        for field in ("name", "mana_cost", "type_line", "oracle_text")
    ):
        return empty
    if record.confidence != "high" or validate(record, printed):
        return empty
    if record.rules_hash != rules_hash(
        printed.name, printed.mana_cost, printed.type_line, printed.oracle_text
    ):
        return empty
    # No permanents, modal/DFC representations, secondary or reminder text.
    if (
        printed.type_line not in {"Instant", "Sorcery"}
        or type(record.effects) is not tuple
        or len(record.effects) != 1
    ):
        return empty
    entry = record.effects[0]
    if type(entry) is not EffectEntry or entry.trigger or entry.conditions:
        return empty
    text = printed.oracle_text
    if type(text) is not str:
        return empty
    draw = re.fullmatch(r"Draw (a|one|two|three|four|five|six|seven|eight|nine|ten) cards?\.", text)
    life = re.fullmatch(r"You gain ([1-9][0-9]?) life\.", text)
    effect = entry.effect
    if draw and type(effect) is Draw and type(effect.count) is int and effect.count == _NUMBERS[draw[1]]:
        statement = f"Its printed spell effect instructs you to draw {effect.count} card(s)."
    elif life and type(effect) is GainLife and type(effect.amount) is int and effect.amount == int(life[1]):
        statement = f"Its printed spell effect instructs you to gain {effect.amount} life."
    else:
        return empty
    empty["plan"].append(
        {
            "text": statement,
            "origin": "derived",
            "rule": "exact_single_spell_effect_v1",
            "effect_index": 0,
            "evidence": [{"card": printed.name, "quote": text}],
        }
    )
    return empty


def validate_derived_threat(value: object, record: CardFacts, printed: PrintedCard) -> list[str]:
    """Do not accept a model's claim of derivation: regenerate and compare all fields."""
    try:
        same = json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(
            derive_threat(record, printed), sort_keys=True, allow_nan=False
        )
    except (ValueError, TypeError, RecursionError):
        same = False
    return [] if same else ["derived threat differs from deterministic source projection"]
