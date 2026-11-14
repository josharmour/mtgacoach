"""Bounded consumer projection; original records and Unknown remain unchanged."""

import dataclasses
import re

from arenamcp.ability_synthesizer import AddMana, Exile, UnknownEffect
from arenamcp.card_facts.schema import EffectEntry

_TARGET = "a nonartifact, nonland card from your hand"
_COLORS = "{any of the exiled card's colors}"
_ORACLE = re.compile(
    r"Imprint — When this artifact enters, you may exile a nonartifact, nonland card from your hand\. "
    r"\{T\}: Add one mana of any of the exiled card's colors\.",
    re.I,
)


def search_record(record, card):
    """Project only an exact optional imprint with a tap-only activation."""
    objects = [record, record.body, *record.effects, *record.modes, *record.mana]
    objects += [e.effect for e in record.effects] + [m.cost for m in record.modes]
    if any(getattr(obj, "_unknown_fields", {}) for obj in objects):
        return record  # future schema obligations are not silently projected away
    if record.confidence != "high" or any(dataclasses.asdict(record.body).values()):
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
    if len(record.effects) != 2 or len(record.modes) != 1 or len(record.mana) != 1:
        return record
    exile, mana = record.effects
    mode, production = record.modes[0], record.mana[0]
    if not (
        exile.trigger == "ETB"
        and isinstance(exile.effect, Exile)
        and exile.effect.target == _TARGET
        and tuple(exile.conditions) == ("you may exile " + _TARGET,)
        and mana.trigger == "activated"
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
