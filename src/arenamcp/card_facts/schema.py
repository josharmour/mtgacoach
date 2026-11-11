"""Card-facts record schema.

One record per card, read by the planner, the announcer and the draft code.
The record is plain dataclasses with JSON round-tripping; it reuses the effect
types from :mod:`arenamcp.ability_synthesizer` so that consumers of parsed
abilities can read card facts without learning a second vocabulary.

Standard library only — no new dependencies.

Forward compatibility: ``from_dict`` preserves every key it does not recognise
(at any nesting level) on the instance, and ``to_dict`` writes them back
unchanged, so an older client can load a record written by a newer schema
version without losing data.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from arenamcp.ability_synthesizer import (
    AddMana,
    Bounce,
    Cost,
    CounterSpell,
    CreateToken,
    Damage,
    Destroy,
    Draw,
    Effect,
    Exile,
    Filter,
    GainLife,
    Pump,
    PutCounters,
    ReturnFromGraveyard,
    TokenSpec,
    UnknownEffect,
)

SCHEMA_VERSION = 1


# ── Effect-type registry ─────────────────────────────────────────────────────
# Keyed by each class's declared ``effect_type`` default, so JSON keeps the same
# tag the ability synthesizer already uses ("Counter" for CounterSpell, ...).


def _declared_effect_type(cls: type) -> str:
    for f in dataclasses.fields(cls):
        if f.name == "effect_type":
            return str(f.default)
    return cls.__name__


_EFFECT_CLASSES: tuple[type, ...] = (
    CreateToken,
    GainLife,
    Draw,
    Damage,
    Destroy,
    Exile,
    CounterSpell,
    Pump,
    AddMana,
    PutCounters,
    ReturnFromGraveyard,
    Bounce,
)

EFFECT_TYPES: dict[str, type] = {_declared_effect_type(c): c for c in _EFFECT_CLASSES}
EFFECT_TYPES[_declared_effect_type(UnknownEffect)] = UnknownEffect


# ── Record sections ──────────────────────────────────────────────────────────


@dataclass
class CardBody:
    """Printed body of the permanent: stats and keyword abilities."""

    power: str | None = None
    toughness: str | None = None
    loyalty: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass
class Mode:
    """One way to cast or use the card.

    ``name`` is the mechanic or action ("cast", "evoke", "flashback",
    "activate", "+1", ...). ``cost`` is the structured cost where the shared
    ``Cost`` shape fits; ``cost_text`` carries the printed cost sentence
    verbatim when it does not (for example "Exile a red card from your hand").
    """

    name: str = ""
    cost: Cost = field(default_factory=Cost)
    cost_text: str = ""
    stays_on_battlefield: bool = True


@dataclass
class EffectEntry:
    """One effect of the card.

    ``trigger`` is the event that causes it ("" for an effect that happens as
    part of casting or activating, or for a static ability). ``effect`` is one
    of the ability-synthesizer effect types; a sentence the compiler could not
    model goes in as ``UnknownEffect`` with the original sentence in ``text``.
    """

    trigger: str = ""
    effect: Effect = field(default_factory=lambda: UnknownEffect(text=""))
    conditions: tuple[str, ...] = ()


@dataclass
class ManaProduction:
    """Mana the card can produce, and under what condition."""

    mana: str = ""
    condition: str = ""


@dataclass
class CardFacts:
    """The full card-facts record for one card."""

    schema_version: int = SCHEMA_VERSION
    name: str = ""
    rules_hash: str = ""
    type_line: str = ""
    mana_cost: str = ""
    body: CardBody = field(default_factory=CardBody)
    modes: tuple[Mode, ...] = ()
    effects: tuple[EffectEntry, ...] = ()
    mana: tuple[ManaProduction, ...] = ()
    # Reserved layers, empty until later chunks fill them in.
    threat: dict[str, Any] = field(default_factory=dict)
    relationships: dict[str, Any] = field(default_factory=dict)
    limited: dict[str, Any] = field(default_factory=dict)
    compiled_by: str = ""
    compiled_at: str = ""
    confidence: str = ""
    notes: str = ""


# ── Encoding ─────────────────────────────────────────────────────────────────


def _encode(obj: Any) -> Any:
    """Recursively turn dataclasses/tuples into JSON-compatible structures."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_encode(v) for v in obj]
    if isinstance(obj, dict):
        return {str(k): _encode(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            out[f.name] = _encode(getattr(obj, f.name))
        # Unknown fields preserved on load are written back unchanged.
        for key, value in getattr(obj, "_unknown_fields", {}).items():
            out.setdefault(key, value)
        return out
    return str(obj)


def _merge_unknown(instance: Any, data: dict[str, Any], known: set[str]) -> None:
    extras = {k: v for k, v in data.items() if k not in known}
    if extras:
        # object.__setattr__ works on frozen dataclasses too.
        object.__setattr__(instance, "_unknown_fields", extras)


def _known_fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


# ── Decoding ─────────────────────────────────────────────────────────────────


def _decode_filter(value: Any) -> Filter | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return Filter(target=str(value))
    filt = Filter(
        target=value.get("target", ""),
        count=int(value.get("count", 1)),
        condition=value.get("condition", ""),
    )
    _merge_unknown(filt, value, _known_fields(Filter))
    return filt


def _decode_cost(value: Any) -> Cost:
    if not isinstance(value, dict):
        return Cost()
    cost = Cost(
        mana=value.get("mana", ""),
        tap=bool(value.get("tap", False)),
        sacrifice=_decode_filter(value.get("sacrifice")),
        life=int(value.get("life", 0)),
        discard=int(value.get("discard", 0)),
        exile_from_graveyard=int(value.get("exile_from_graveyard", 0)),
    )
    _merge_unknown(cost, value, _known_fields(Cost))
    return cost


def _decode_token_spec(value: Any) -> TokenSpec:
    if not isinstance(value, dict):
        return TokenSpec(name=str(value))

    def tup(key: str) -> tuple[str, ...]:
        raw = value.get(key)
        if isinstance(raw, list):
            return tuple(str(x) for x in raw)
        return (str(raw),) if raw else ()

    spec = TokenSpec(
        name=value.get("name", ""),
        count=int(value.get("count", 1)),
        power=value.get("power"),
        toughness=value.get("toughness"),
        types=tup("types") or ("Creature",),
        subtypes=tup("subtypes"),
        keywords=tup("keywords"),
        tapped=bool(value.get("tapped", False)),
        attacking=bool(value.get("attacking", False)),
        copy_of_self=bool(value.get("copy_of_self", False)),
        not_legendary=bool(value.get("not_legendary", False)),
        per_opponent=bool(value.get("per_opponent", False)),
    )
    _merge_unknown(spec, value, _known_fields(TokenSpec))
    return spec


def _decode_effect_payload(value: Any) -> Effect:
    """Decode one tagged effect object into its ability-synthesizer class."""
    if not isinstance(value, dict):
        return UnknownEffect(text=str(value))
    tag = value.get("effect_type")
    cls = EFFECT_TYPES.get(tag if isinstance(tag, str) else "")
    if cls is None:
        unknown = UnknownEffect(
            text=str(value.get("text", "")),
            effect_type=str(tag or "Unknown"),
        )
        _merge_unknown(unknown, value, {"text", "effect_type"})
        return unknown

    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name == "effect_type" or f.name not in value:
            continue
        raw = value[f.name]
        if cls is CreateToken and f.name == "spec":
            kwargs[f.name] = _decode_token_spec(raw)
        elif isinstance(raw, list) and f.name in ("types", "subtypes", "keywords"):
            kwargs[f.name] = tuple(str(x) for x in raw)
        else:
            kwargs[f.name] = raw
    effect = cls(**kwargs)
    _merge_unknown(effect, value, _known_fields(cls))
    return effect


def _decode_body(value: Any) -> CardBody:
    if not isinstance(value, dict):
        return CardBody()
    keywords_raw = value.get("keywords")
    body = CardBody(
        power=value.get("power"),
        toughness=value.get("toughness"),
        loyalty=value.get("loyalty"),
        keywords=tuple(str(k) for k in keywords_raw) if isinstance(keywords_raw, list) else (),
    )
    _merge_unknown(body, value, _known_fields(CardBody))
    return body


def _decode_mode(value: Any) -> Mode:
    if not isinstance(value, dict):
        return Mode(name=str(value))
    mode = Mode(
        name=value.get("name", ""),
        cost=_decode_cost(value.get("cost")),
        cost_text=value.get("cost_text", ""),
        stays_on_battlefield=bool(value.get("stays_on_battlefield", True)),
    )
    _merge_unknown(mode, value, _known_fields(Mode))
    return mode


def _decode_effect_entry(value: Any) -> EffectEntry:
    if not isinstance(value, dict):
        return EffectEntry()
    conditions_raw = value.get("conditions")
    entry = EffectEntry(
        trigger=value.get("trigger", ""),
        effect=_decode_effect_payload(value.get("effect")),
        conditions=(tuple(str(c) for c in conditions_raw) if isinstance(conditions_raw, list) else ()),
    )
    _merge_unknown(entry, value, _known_fields(EffectEntry))
    return entry


def _decode_mana_production(value: Any) -> ManaProduction:
    if not isinstance(value, dict):
        return ManaProduction(mana=str(value))
    production = ManaProduction(
        mana=value.get("mana", ""),
        condition=value.get("condition", ""),
    )
    _merge_unknown(production, value, _known_fields(ManaProduction))
    return production


# ── CardFacts JSON interface ─────────────────────────────────────────────────


def card_facts_to_dict(record: CardFacts) -> dict[str, Any]:
    """Serialise a record to a JSON-compatible dict (unknown fields included)."""
    return _encode(record)


def card_facts_from_dict(data: dict[str, Any]) -> CardFacts:
    """Build a record from a dict, preserving unknown keys for re-export."""
    if not isinstance(data, dict):
        raise TypeError("card-facts record must be a JSON object")
    modes_raw = data.get("modes")
    effects_raw = data.get("effects")
    mana_raw = data.get("mana")
    record = CardFacts(
        schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        name=data.get("name", ""),
        rules_hash=data.get("rules_hash", ""),
        type_line=data.get("type_line", ""),
        mana_cost=data.get("mana_cost", ""),
        body=_decode_body(data.get("body")),
        modes=tuple(_decode_mode(m) for m in modes_raw) if isinstance(modes_raw, list) else (),
        effects=(
            tuple(_decode_effect_entry(e) for e in effects_raw) if isinstance(effects_raw, list) else ()
        ),
        mana=(tuple(_decode_mana_production(m) for m in mana_raw) if isinstance(mana_raw, list) else ()),
        threat=data.get("threat") or {},
        relationships=data.get("relationships") or {},
        limited=data.get("limited") or {},
        compiled_by=data.get("compiled_by", ""),
        compiled_at=data.get("compiled_at", ""),
        confidence=data.get("confidence", ""),
        notes=data.get("notes", ""),
    )
    _merge_unknown(record, data, _known_fields(CardFacts))
    return record


def to_json(record: CardFacts, *, indent: int | None = 2) -> str:
    """Serialise a record to a JSON string."""
    return json.dumps(card_facts_to_dict(record), indent=indent, sort_keys=True)


def from_json(text: str) -> CardFacts:
    """Parse a JSON string back into a record."""
    return card_facts_from_dict(json.loads(text))


# Convenience methods on the record itself.
CardFacts.to_dict = card_facts_to_dict  # type: ignore[attr-defined]
CardFacts.from_dict = classmethod(  # type: ignore[method-assign]
    lambda cls, data: card_facts_from_dict(data)
)
CardFacts.to_json = lambda self, indent=2: to_json(self, indent=indent)  # type: ignore[attr-defined]
CardFacts.from_json = staticmethod(from_json)  # type: ignore[attr-defined]


__all__ = [
    "SCHEMA_VERSION",
    "EFFECT_TYPES",
    "CardBody",
    "CardFacts",
    "EffectEntry",
    "ManaProduction",
    "Mode",
    "card_facts_from_dict",
    "card_facts_to_dict",
    "from_json",
    "to_json",
]
