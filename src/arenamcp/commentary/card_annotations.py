"""Opt-in N2 fact projection; not wired into the running announcer.

Only caller-supplied independently reviewed exact record digests are eligible.
A record cannot self-authorize by setting confidence/provenance fields. Printed
hash and narrative evidence are rechecked. Facts describe threats/possibilities,
never assert that a multi-card recipe is active or executable.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.narrative import validate_layers
from arenamcp.card_facts.spellbook import project_relationships, validate_recipe
from arenamcp.card_facts.validate import PrintedCard


@dataclass(frozen=True)
class AnnotationEvent:
    event_id: str
    kind: str  # Explicitly observed spell_cast or permanent_entered only.
    card: str
    controller: int
    visibility: str = "public"
    face_down: bool = False


@dataclass(frozen=True)
class KnownCard:
    name: str
    zone: str
    controller: int
    visibility: str = "public"
    face_down: bool = False


def annotation_digest(record: dict) -> str:
    """Content identity, NOT approval; caller owns the independent-review list."""
    return hashlib.sha256(
        json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _visible(visibility, controller, face_down, local_seat, allow_private):
    return face_down is False and (
        visibility == "public"
        or (visibility == "private" and controller == local_seat and allow_private is True)
    )


def project_annotation_facts(
    event: AnnotationEvent,
    *,
    local_seat: int,
    allow_private: bool,
    known_cards: tuple[KnownCard, ...],
    records: Mapping[str, dict],
    printed: Mapping[str, PrintedCard],
    reviewed_digests: Mapping[str, str],
) -> dict:
    """Pure bounded projection using explicit public/private identity, no I/O.

    Reviewed digests must originate outside untrusted record contents. No default
    approval, file lookup, network, inference, global state or runtime activation.
    Private identity is allowed only for local-player cards with explicit opt-in.
    """
    if type(event) is not AnnotationEvent or type(local_seat) is not int or type(allow_private) is not bool:
        return {}
    if (
        event.kind not in {"spell_cast", "permanent_entered"}
        or not isinstance(event.event_id, str)
        or not event.event_id
        or type(event.controller) is not int
        or not isinstance(event.card, str)
        or not _visible(event.visibility, event.controller, event.face_down, local_seat, allow_private)
    ):
        return {}
    if (
        not all(isinstance(value, Mapping) for value in (records, printed, reviewed_digests))
        or type(known_cards) is not tuple
        or len(printed) > 100
        or len(known_cards) > 256
        or not local_seat > 0
        or event.controller < 1
        or len(event.event_id) > 200
        or len(event.card) > 200
    ):
        return {}
    record = records.get(event.card)
    source = printed.get(event.card)
    if not isinstance(record, dict) or type(source) is not PrintedCard:
        return {}
    try:
        if len(json.dumps(record, allow_nan=False).encode()) > 200_000:
            return {}
        digest = annotation_digest(record)
        if reviewed_digests.get(event.card) != digest or record.get("name") != event.card:
            return {}
        if record.get("rules_hash") != rules_hash(
            source.name, source.mana_cost, source.type_line, source.oracle_text
        ):
            return {}
        if source.name != event.card:
            return {}
        sources = {
            name: p.oracle_text
            for name, p in printed.items()
            if type(p) is PrintedCard and p.name == name and type(p.oracle_text) is str
        }
        threat, relationships = record.get("threat", {}), record.get("relationships", {})
        if validate_layers(threat, relationships, sources, card_name=event.card):
            return {}
        provenance = record.get("narrative_provenance", {})
        source_pins = provenance.get("source_text_sha256", {}) if isinstance(provenance, dict) else {}
        claims = [claim for field in ("plan", "clock", "answers") for claim in threat.get(field, [])]
        claims += [link["claim"] for link in relationships.get("links", [])]
        cited = {event.card} | {item["card"] for claim in claims for item in claim["evidence"]}
        if not isinstance(source_pins, dict) or any(
            source_pins.get(name) != hashlib.sha256(sources[name].encode()).hexdigest() for name in cited
        ):
            return {}
        catalog = relationships.get("catalog")
        if catalog is not None:
            if not isinstance(catalog, dict) or not isinstance(catalog.get("recipes"), list):
                return {}
            if any(validate_recipe(recipe) for recipe in catalog["recipes"]):
                return {}
            focal_ids = {
                piece["oracle_id"]
                for recipe in catalog["recipes"]
                for piece in recipe["pieces"]
                if piece["name"] == event.card
            }
            if len(focal_ids) != 1:
                return {}
            rebuilt = project_relationships(
                event.card, next(iter(focal_ids)), catalog["recipes"], catalog.get("source")
            )
            if annotation_digest(rebuilt) != annotation_digest(catalog):
                return {}
    except (ValueError, TypeError, KeyError, RecursionError):
        return {}
    visible = {}
    for card in known_cards[:256]:
        if (
            type(card) is KnownCard
            and card.zone in {"battlefield", "graveyard", "played"}
            and isinstance(card.name, str)
            and type(card.controller) is int
            and _visible(card.visibility, card.controller, card.face_down, local_seat, allow_private)
        ):
            visible.setdefault(card.name, []).append(card)
    result = {}
    private = event.visibility == "private"

    def add(kind, text, evidence, scope, other=None):
        if len(result) >= 24 or len(text) > 1000:
            return
        key = f"card_annotation:{event.event_id}:{len(result)}"
        result[key] = {
            "kind": kind,
            "text": text,
            "source": "reviewed_card_facts",
            "record_sha256": digest,
            "rules_hash": record["rules_hash"],
            "event_id": event.event_id,
            "event_kind": event.kind,
            "controller": event.controller,
            "card": event.card,
            "scope": scope,
            "visibility": "private" if private or (other and other.visibility == "private") else "public",
            "evidence": copy.deepcopy(evidence),
        }
        if other is not None:
            result[key]["observed_partner"] = {
                "name": other.name,
                "zone": other.zone,
                "controller": other.controller,
            }
            result[key]["activation"] = "not_established; all source conditions remain required"

    if event.controller != local_seat:
        for field in ("plan", "clock", "answers"):
            for claim in threat.get(field, [])[:8]:
                add("card_threat", claim["text"], claim["evidence"], field)
    if event.kind != "permanent_entered":
        return result
    for link in relationships.get("links", [])[:64]:
        partners = visible.get(link["card"], [])
        if not partners:
            continue
        # Prefer public evidence when an identical name has both scopes.
        partner = next((p for p in partners if p.visibility == "public"), partners[0])
        add(
            "card_relationship",
            "Potential interaction: " + link["claim"]["text"],
            link["claim"]["evidence"],
            "possibility",
            partner,
        )
    if catalog is not None:
        recipes = {recipe["id"]: recipe for recipe in catalog["recipes"]}
        for link in catalog["recipe_links"][:64]:
            partners = visible.get(link["card"], [])
            recipe = recipes.get(link["recipe_id"])
            if not partners or recipe is None or len(json.dumps(recipe).encode()) > 4000:
                continue
            partner = next((p for p in partners if p.visibility == "public"), partners[0])
            add(
                "catalogue_relationship",
                "Catalogue possibility: " + link["text"],
                [{"catalogue_source": catalog["source"], "recipe": recipe}],
                "possibility",
                partner,
            )
    return result
