"""Bounded consumer projection; original records and Unknown remain unchanged."""

import dataclasses
import re
from typing import Any

from arenamcp.ability_synthesizer import AddMana, Exile, UnknownEffect, _canonicalize_trigger
from arenamcp.card_facts.schema import EffectEntry

_TARGET = "a nonartifact, nonland card from your hand"
_COLORS = "{any of the exiled card's colors}"
_ORACLE = re.compile(
    r"Imprint — When this artifact enters, you may exile a nonartifact, nonland card from your hand\. "
    r"\{T\}: Add one mana of any of the exiled card's colors\.",
    re.I,
)


def canonicalize_trigger(trigger: str) -> str | None:
    """Map exact case-insensitive card-facts trigger tokens to canonical names, else None."""
    return _canonicalize_trigger(trigger)


def _has_unknown_fields(value: Any) -> bool:
    if getattr(value, "_unknown_fields", None):
        return True
    if dataclasses.is_dataclass(value):
        return any(_has_unknown_fields(getattr(value, field.name)) for field in dataclasses.fields(value))
    if isinstance(value, (tuple, list)):
        return any(_has_unknown_fields(item) for item in value)
    if isinstance(value, dict):
        return any(_has_unknown_fields(item) for item in value.values())
    return False


def _no_extra_cost(cost: Any) -> bool:
    return (
        not cost.tap
        and cost.sacrifice is None
        and not cost.life
        and not cost.discard
        and not cost.exile_from_graveyard
    )


def search_record(record, card):
    """Project only an exact optional imprint with a tap-only activation."""
    if _has_unknown_fields(record):
        return record  # future schema obligations are not silently projected away
    if record.confidence != "high" or record.threat or record.relationships or record.limited or record.notes:
        return record
    if (
        record.body.power is not None
        or record.body.toughness is not None
        or record.body.loyalty is not None
        or any(k.casefold() != "imprint" for k in record.body.keywords)
    ):
        return record
    if any(isinstance(e.effect, UnknownEffect) for e in record.effects):
        return record
    if (record.name, record.mana_cost, record.type_line) != (
        card.get("name"),
        card.get("mana_cost"),
        card.get("type_line"),
    ):
        return record
    oracle = re.sub(r"<[^>]*>", "", str(card.get("oracle_text") or "")).replace("{oT}", "{T}")
    if not _ORACLE.fullmatch(" ".join(oracle.split())):
        return record
    if len(record.effects) != 2 or len(record.mana) != 1:
        return record
    if len(record.modes) == 1:
        mode = record.modes[0]
    elif len(record.modes) == 2:
        cast_mode, mode = record.modes
        if not (
            cast_mode.name == "cast"
            and cast_mode.cost.mana == record.mana_cost
            and cast_mode.cost_text == record.mana_cost
            and cast_mode.stays_on_battlefield
            and _no_extra_cost(cast_mode.cost)
        ):
            return record
    else:
        return record
    exile, mana = record.effects
    production = record.mana[0]
    if not (
        canonicalize_trigger(exile.trigger) == "ENTERS_THE_BATTLEFIELD"
        and isinstance(exile.effect, Exile)
        and exile.effect.target == _TARGET
        and tuple(exile.conditions) == ("you may exile " + _TARGET,)
        and canonicalize_trigger(mana.trigger) == "ACTIVATED"
        and not mana.conditions
        and isinstance(mana.effect, AddMana)
        and mana.effect.amount == 1
        and mana.effect.mana == _COLORS
        and production.mana == _COLORS
        and production.condition == "requires an exiled card via Imprint"
        and mode.name == "activate"
        and mode.cost_text == "{T}"
        and mode.stays_on_battlefield
        and mode.cost.tap
        and not mode.cost.mana
        and mode.cost.sacrifice is None
        and not mode.cost.life
        and not mode.cost.discard
        and not mode.cost.exile_from_graveyard
    ):
        return record
    # The optional condition maps to existing decline/exact-payment variants.
    # Tap-only activation maps to one consumed mana source, not a cast mode.
    # dataclasses.replace creates a consumer view; original JSON is untouched.
    return dataclasses.replace(
        record,
        modes=(),
        effects=(
            EffectEntry(trigger="", effect=AddMana(mana="any color", amount=1)),
            EffectEntry(trigger="ENTERS_THE_BATTLEFIELD", effect=exile.effect),
        ),
    )
