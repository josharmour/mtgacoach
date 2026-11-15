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
    match_id: str = ""
    game_number: int = 0


@dataclass(frozen=True)
class KnownCard:
    name: str
    zone: str
    controller: int
    visibility: str = "public"
    face_down: bool = False
    match_id: str = ""
    game_number: int = 0


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
        type(event.kind) is not str
        or event.kind not in {"spell_cast", "permanent_entered"}
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
    if (
        not isinstance(event.match_id, str)
        or not event.match_id
        or type(event.game_number) is not int
        or event.game_number < 1
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
            if catalog["recipes"] and len(focal_ids) != 1:
                return {}
            # Empty exports have no focal identity to infer, and no links.
            rebuilt = project_relationships(
                event.card,
                next(iter(focal_ids)) if focal_ids else "empty-projection",
                catalog["recipes"],
                catalog.get("source"),
            )
            if annotation_digest(rebuilt) != annotation_digest(catalog):
                return {}
    except (ValueError, TypeError, KeyError, RecursionError):
        return {}
    visible = {}
    for card in known_cards[:256]:
        if (
            type(card) is KnownCard
            and card.match_id == event.match_id
            and type(card.game_number) is int
            and card.game_number == event.game_number
            and type(card.zone) is str
            and card.zone in {"battlefield", "graveyard", "played"}
            and isinstance(card.name, str)
            and type(card.controller) is int
            and card.controller > 0
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
            "match_id": event.match_id,
            "game_number": event.game_number,
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


def public_event_print(observation, event):
    from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print

    local = observation.public_state.get("local_seat_id")
    if (
        type(local) is not int
        or local not in (1, 2)
        or type(event.seat) is not int
        or event.seat not in (1, 2)
        or event.visibility != "public"
        or event.kind not in {"action_observed", "spell_resolved", "permanent_entered", "land_drop"}
        or type(event.payload.get("grp_id")) is not int
        or event.payload["grp_id"] not in corpus()[0]
    ):
        return None
    entry = event.kind != "action_observed"
    if entry and event.payload.get("to_zone") != "battlefield":
        return None
    zones = observation.public_state.get("zones", {})
    candidates = [
        card
        for zone in (("battlefield",) if entry else ("stack", "battlefield", "graveyard", "exile"))
        for card in zones.get(zone, ())
        if card.get("instance_id") == event.payload.get("instance_id")
    ]
    if len(candidates) != 1 or type(candidates[0].get("instance_id")) is not int:
        return None
    focal = candidates[0]
    if (
        not reviewed_print(focal)
        or focal["name"] != event.payload.get("name")
        or focal["grp_id"] != event.payload["grp_id"]
        or focal.get("controller_seat_id") != event.seat
    ):
        return None
    return focal


def observed_annotations(observation, event, played=()):
    """Project public print evidence with an epoch-scoped public cast history."""
    from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print

    local = observation.public_state.get("local_seat_id")
    focal = public_event_print(observation, event)
    if focal is None:
        return {}
    entry = event.kind != "action_observed"
    zones = observation.public_state["zones"]
    epoch = observation.epoch
    partners = (
        tuple(
            KnownCard(card["name"], zone, event.seat, match_id=epoch.match_id, game_number=epoch.game_number)
            for zone in ("battlefield", "graveyard")
            for card in zones.get(zone, ())
            if card.get("controller_seat_id") == event.seat
            and reviewed_print(card)
            and card.get("instance_id") != focal["instance_id"]
        )
        if entry
        else ()
    )
    _, records, sources, pins = corpus()
    return project_annotation_facts(
        AnnotationEvent(
            event.event_id,
            "permanent_entered" if entry else "spell_cast",
            focal["name"],
            event.seat,
            match_id=epoch.match_id,
            game_number=epoch.game_number,
        ),
        local_seat=local,
        allow_private=False,
        known_cards=partners + tuple(c for c in played if c.controller == event.seat),
        records=records,
        printed=sources,
        reviewed_digests=pins,
    )
