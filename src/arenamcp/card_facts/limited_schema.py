"""Offline D1 source contract, deliberately not wired into CardFacts or drafting.

Validation establishes shape, identity and quoted-source provenance, NOT the
truth of an inferred role, removal assessment or archetype. A passing bundle
still needs semantic review. No ratings are accepted: 17lands remains the
external measure of card quality. Unsupported versions/fields fail closed.
"""

from __future__ import annotations

import hashlib
import json
import re

SCHEMA_VERSION = 1
ROLES = frozenset({"finisher", "enabler", "payoff", "filler"})
REMOVAL = frozenset({"none", "conditional", "unconditional", "unknown"})
COLOR_PAIRS = frozenset({"WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG"})
PRINTED_FIELDS = frozenset({"name", "mana_cost", "type_line", "oracle_text"})
SOURCE_FIELDS = PRINTED_FIELDS | {"card_id", "set_code"}


class LimitedValidationError(ValueError):
    """Fixed diagnostics never include untrusted record text."""


def _need(condition, reason):
    if not condition:
        raise LimitedValidationError(reason)


def _object(value, fields):
    _need(type(value) is dict and value.keys() == fields, "unsupported object fields")


def _text(value, *, empty=False, limit=12000):
    _need(type(value) is str and len(value) <= limit and (empty or bool(value.strip())), "invalid text")
    _need(not any(ord(c) < 32 and c not in "\n\t" for c in value), "invalid control character")


def _list(value, *, limit=5000):
    _need(type(value) is list and len(value) <= limit, "invalid list")


def _unique(values):
    _need(len(set(values)) == len(values), "duplicate reference")


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    ).hexdigest()


def _catalog(cards):
    _list(cards)
    _need(bool(cards), "empty source catalog")
    result = {}
    for card in cards:
        _object(card, SOURCE_FIELDS)
        for field in SOURCE_FIELDS:
            _text(card[field], empty=field in {"mana_cost", "oracle_text"})
        _need(re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", card["card_id"]), "invalid source identity")
        _need(re.fullmatch(r"[a-z0-9]{2,8}", card["set_code"]), "invalid set code")
        _need(card["card_id"] not in result, "duplicate source identity")
        result[card["card_id"]] = card
    _need(len({c["set_code"] for c in cards}) == 1, "mixed source sets")
    return result


def catalog_digest(cards: list[dict]) -> str:
    """Hash the exact supplied printed projection, sorted by exact card identity.

    This does not authenticate its origin; the caller must retain the actual
    source/version separately and provide it again to validate_bundle.
    """
    catalog = _catalog(cards)
    return _digest([catalog[key] for key in sorted(catalog)])


def card_digest(card: dict) -> str:
    _catalog([card])
    return _digest(card)


def _evidence(items, catalog, *, required=True, card_id=None):
    _list(items, limit=100)
    _need(not required or bool(items), "missing evidence")
    seen = set()
    for item in items:
        _object(item, {"card_id", "field", "quote"})
        _text(item["card_id"], limit=80)
        _text(item["field"], limit=32)
        _text(item["quote"])
        _need(item["card_id"] in catalog and item["field"] in PRINTED_FIELDS, "unknown evidence source")
        _need(card_id is None or item["card_id"] == card_id, "wrong card evidence")
        _need(item["quote"] in catalog[item["card_id"]][item["field"]], "quote not in source")
        key = (item["card_id"], item["field"], item["quote"])
        _need(key not in seen, "duplicate evidence")
        seen.add(key)


def _references(items, allowed):
    _list(items)
    for item in items:
        _text(item, limit=80)
        _need(item in allowed, "unknown reference")
    _unique(items)


def _validate(bundle, cards, provider, source_version):
    catalog = _catalog(cards)
    _text(provider, limit=100)
    _text(source_version, limit=100)
    set_code = cards[0]["set_code"]
    _object(bundle, {"schema_version", "set_code", "provenance", "set_record", "card_records"})
    _need(
        type(bundle["schema_version"]) is int and bundle["schema_version"] == SCHEMA_VERSION,
        "unsupported schema version",
    )
    _need(type(bundle["set_code"]) is str and bundle["set_code"] == set_code, "set identity mismatch")
    provenance = bundle["provenance"]
    _object(provenance, {"provider", "source_version", "source_sha256", "producer_version"})
    for value in provenance.values():
        _text(value, limit=100)
    _need(
        provenance["provider"] == provider and provenance["source_version"] == source_version,
        "source provenance mismatch",
    )
    _need(provenance["source_sha256"] == catalog_digest(cards), "source digest mismatch")
    record = bundle["set_record"]
    _object(record, {"status", "mechanics", "archetypes"})
    _need(type(record["status"]) is str and record["status"] in {"known", "unknown"}, "invalid status")
    _list(record["mechanics"], limit=100)
    _list(record["archetypes"], limit=10)
    pairs = {}
    mechanics = []
    for mechanic in record["mechanics"]:
        _object(mechanic, {"name", "evidence"})
        _text(mechanic["name"], limit=100)
        mechanics.append(mechanic["name"])
        _evidence(mechanic["evidence"], catalog)
    _unique(mechanics)
    for archetype in record["archetypes"]:
        _object(archetype, {"colors", "name", "enablers", "payoffs", "evidence"})
        _text(archetype["colors"], limit=2)
        _need(
            archetype["colors"] in COLOR_PAIRS and archetype["colors"] not in pairs, "invalid archetype pair"
        )
        _text(archetype["name"], limit=200)
        _references(archetype["enablers"], catalog)
        _references(archetype["payoffs"], catalog)
        _evidence(archetype["evidence"], catalog)
        evidenced = {item["card_id"] for item in archetype["evidence"]}
        _need(
            set(archetype["enablers"] + archetype["payoffs"]) <= evidenced,
            "archetype member lacks source evidence",
        )
        pairs[archetype["colors"]] = archetype
    if record["status"] == "unknown":
        _need(not mechanics and not pairs, "unknown set contains claims")
    else:
        _need(bool(mechanics) and pairs.keys() == COLOR_PAIRS, "incomplete set record")
    _list(bundle["card_records"])
    seen = set()
    for card in bundle["card_records"]:
        _object(
            card,
            {
                "card_id",
                "name",
                "set_code",
                "source_sha256",
                "status",
                "roles",
                "removal",
                "archetypes",
                "evidence",
            },
        )
        for key in ("card_id", "name", "set_code", "source_sha256", "status", "removal"):
            _text(card[key])
        identity = card["card_id"]
        _need(identity in catalog and identity not in seen, "unknown or duplicate card")
        seen.add(identity)
        source = catalog[identity]
        _need(card["name"] == source["name"] and card["set_code"] == set_code, "card identity mismatch")
        _need(card["source_sha256"] == card_digest(source), "card source digest mismatch")
        _need(
            card["status"] in {"known", "unknown"} and card["removal"] in REMOVAL,
            "invalid card classification",
        )
        _references(card["roles"], ROLES)
        _references(card["archetypes"], pairs)
        _evidence(card["evidence"], catalog, required=card["status"] == "known", card_id=identity)
        if card["status"] == "unknown":
            _need(
                not card["roles"]
                and not card["archetypes"]
                and not card["evidence"]
                and card["removal"] == "unknown",
                "unknown card contains claims",
            )
        else:
            _need(bool(card["roles"]) or card["removal"] not in {"none", "unknown"}, "empty known card")
        for pair in card["archetypes"]:
            for role, field in (("enabler", "enablers"), ("payoff", "payoffs")):
                if role in card["roles"]:
                    _need(identity in pairs[pair][field], "card/set role disagreement")

    detailed = {card["card_id"]: card for card in bundle["card_records"]}
    for pair, archetype in pairs.items():
        for role, field in (("enabler", "enablers"), ("payoff", "payoffs")):
            for identity in archetype[field]:
                if identity in detailed:
                    card = detailed[identity]
                    _need(
                        card["status"] == "known" and role in card["roles"] and pair in card["archetypes"],
                        "set/card role disagreement",
                    )


def validate_bundle(
    bundle: object, *, cards: list[dict], provider: str, source_version: str
) -> tuple[str, ...]:
    """Return fixed errors, or () for structural/source-binding validity only.

    No mutation, IO, model calls, semantic approval, or 17lands quality claim.
    Current-set coverage and ten actual records are separate acceptance gates;
    synthetic fixtures and a partial valid bundle do not satisfy those gates.
    """
    try:
        _validate(bundle, cards, provider, source_version)
    except LimitedValidationError as exc:
        return (str(exc),)
    except Exception:
        return ("malformed limited record",)
    return ()
