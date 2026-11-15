"""Offline coverage admission for a deck study attached to a playbook.

Printed references and structural coverage are checkable. Strategic prose and
declared mana/timing are hypotheses for review, not executable planner rules.
Relationship coverage is relative to the independently installed public records.
"""

import hashlib
import json
import re

from arenamcp.card_db import is_bogus_type_line, is_unknown_card_name
from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print

STUDY_VERSION = 1
_CARD_TYPES = {
    "Artifact",
    "Battle",
    "Creature",
    "Enchantment",
    "Instant",
    "Kindred",
    "Land",
    "Planeswalker",
    "Sorcery",
    "Tribal",
}
_ZONES = {"hand", "battlefield", "graveyard", "exile", "command", "library", "stack"}


def study_source(card):
    """Bind all single- and multiface print data used by the study, including stats."""
    keys = (
        "name",
        "mana_cost",
        "type_line",
        "oracle_text",
        "power",
        "toughness",
        "loyalty",
        "related_faces",
        "linked_faces",
        "linked_face_ids",
        "card_faces",
    )
    faces = _faces(card)
    if faces is None or any(
        not isinstance(face.get("name"), str)
        or is_unknown_card_name(face["name"])
        or not isinstance(face.get("oracle_text"), str)
        or not isinstance(face.get("mana_cost"), str)
        or _type_parts(face.get("type_line")) is None
        for face in faces
    ):
        raise ValueError("Deck study needs resolved printed facts for cited pieces")
    facts = {key: card[key] for key in keys if key in card}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _type_parts(line):
    if not isinstance(line, str) or not line.strip() or is_bogus_type_line(line):
        return None
    parts = [set(re.split(r"\s+[—-]\s+", part, maxsplit=1)[0].split()) for part in line.split(" // ")]
    return parts if all(types & _CARD_TYPES for types in parts) else None


def _faces(card):
    if (
        not isinstance(card, dict)
        or not isinstance(card.get("name"), str)
        or is_unknown_card_name(card["name"])
    ):
        return None
    faces = [card]
    for key in ("related_faces", "card_faces"):
        extra = card.get(key, [])
        if type(extra) is not list or any(type(face) is not dict for face in extra):
            return None
        faces.extend(extra)
    if (card.get("linked_face_ids") or card.get("linked_faces")) and len(faces) == 1:
        return None
    return faces


def _land(card):
    faces = _faces(card)
    if faces is None:
        return None
    types = [_type_parts(face.get("type_line")) for face in faces]
    if any(parts is None for parts in types):
        return None
    return all("Land" in part for parts in types for part in parts)


def _reviewed_relationships(catalog):
    """Each edge has an exact, public, independently reviewed print at both ends."""
    _, records, _, _ = corpus()
    by_name = {}
    for gid, card in catalog.items():
        if isinstance(card, dict) and (source := reviewed_print({**card, "grp_id": gid})):
            by_name.setdefault(source.name, []).append(gid)
    links = set()
    for name, sources in by_name.items():
        for link in records[name]["relationships"].get("links", []):
            for source in sources:
                for target in by_name.get(link["card"], []):
                    links.add((source, target, link["role"]))
    return links, sum(map(len, by_name.values()))


def _object(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError(f"Deck study needs exactly {', '.join(sorted(fields))}")


def _list(value, label):
    if type(value) is not list:
        raise ValueError(f"Deck study needs a list for {label}")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Deck study needs a nonempty explanation")


def _card(value, catalog):
    if type(value) is not int or value not in catalog:
        raise ValueError("Deck study names an unknown card")
    return value


def _conditions(value, catalog):
    _object(value, {"pieces", "mana", "first_turn"})
    if type(value["first_turn"]) is not int or value["first_turn"] < 1:
        raise ValueError("Deck study needs a positive earliest turn")
    mana = value["mana"]
    if type(mana) is not dict or "generic" not in mana or set(mana) - {"generic", *"WUBRGC"}:
        raise ValueError("Deck study needs an explicit mana requirement")
    if any(type(n) is not int or n < 0 for n in mana.values()):
        raise ValueError("Deck study has invalid mana quantities")
    ids = set()
    for piece in _list(value["pieces"], "pieces"):
        _object(piece, {"card", "zone"})
        gid = _card(piece["card"], catalog)
        if gid in ids or type(piece["zone"]) is not str or piece["zone"] not in _ZONES:
            raise ValueError("Deck study needs distinct pieces and explicit local zones")
        ids.add(gid)
    if not ids:
        raise ValueError("Deck study needs at least one piece")
    return ids


def _evidence(rows, required, catalog):
    from arenamcp.deck_strategy import oracle_rules

    cited = set()
    references = set()
    for ref in _list(rows, "printed evidence"):
        _object(ref, {"card", "source", "rule", "quote"})
        gid = _card(ref["card"], catalog)
        digest = study_source(catalog[gid])
        rules = oracle_rules(catalog[gid])
        n = ref["rule"]
        if (
            gid not in required
            or type(n) is not int
            or not 1 <= n <= len(rules)
            or (gid, n) in references
            or ref["source"] != digest
            or ref["quote"] != rules[n - 1]
        ):
            raise ValueError("Deck study evidence differs from its printed source")
        cited.add(gid)
        references.add((gid, n))
    if cited != required:
        raise ValueError("Deck study lacks printed evidence for one or more pieces")


def _link(edge, catalog):
    _object(edge, {"source", "target", "role"})
    source, target = (_card(edge[k], catalog) for k in ("source", "target"))
    if type(edge["role"]) is not str or source == target:
        raise ValueError("Invalid deck study relationship")
    return source, target, edge["role"]


def validate_study(study, catalog):
    """Reject a partial typed study; legacy playbooks remain valid in their own format."""
    from arenamcp.loyalty_timing import additional_activation, conflicting_loyalty_sequence, walkers_in

    _object(study, {"version", "cards", "interactions", "minor_links"})
    if type(study["version"]) is not int or study["version"] != STUDY_VERSION:
        raise ValueError("Unsupported deck study schema")
    kinds = {gid: _land(card) for gid, card in catalog.items()}
    if any(type(gid) is not int or gid <= 0 for gid in catalog):
        raise ValueError("Deck study needs positive Arena card identifiers")
    if not kinds or None in kinds.values():
        raise ValueError("Cannot certify nonland coverage with unresolved printed types")
    nonlands = {gid for gid, land in kinds.items() if not land}
    expected, reviewed = _reviewed_relationships(catalog)
    covered, accounted = set(), set()
    prose = []
    for row in _list(study["cards"], "per-card rules"):
        _object(row, {"card", "role", "play", "hold", "avoid", "evidence"})
        gid = _card(row["card"], catalog)
        if gid in covered or gid not in nonlands:
            raise ValueError("Deck study role is duplicated or not a nonland")
        covered.add(gid)
        _text(row["role"])
        prose.append(row["role"])
        relevant = {gid}
        for key in ("play", "hold", "avoid"):
            action = row[key]
            _object(action, {"advice", "conditions"})
            _text(action["advice"])
            prose.append(action["advice"])
            pieces = _conditions(action["conditions"], catalog)
            if gid not in pieces:
                raise ValueError("A per-card rule lacks its focal card")
            relevant.update(pieces)
        _evidence(row["evidence"], relevant, catalog)
    if nonlands != covered:
        raise ValueError(f"Deck study is missing {len(nonlands - covered)} nonland card rule(s)")

    ids = set()
    counts = {"synergy": 0, "combo": 0}
    for entry in _list(study["interactions"], "interactions"):
        _object(entry, {"id", "kind", "conditions", "outcome", "stops", "evidence", "links"})
        _text(entry["id"])
        kind = entry["kind"]
        if entry["id"] in ids or type(kind) is not str or kind not in counts:
            raise ValueError("Invalid or duplicate deck study interaction")
        ids.add(entry["id"])
        counts[kind] += 1
        pieces = _conditions(entry["conditions"], catalog)
        if len(pieces) < 2:
            raise ValueError("A deck study interaction needs multiple distinct pieces")
        _text(entry["outcome"])
        _text(entry["stops"])
        prose.extend((entry["outcome"], entry["stops"]))
        _evidence(entry["evidence"], pieces, catalog)
        for item in _list(entry["links"], "covered relationships"):
            edge = _link(item, catalog)
            if edge not in expected or {edge[0], edge[1]} - pieces:
                raise ValueError("Deck study interaction claims an unsupported relationship")
            accounted.add(edge)

    in_entries = len(accounted)
    for item in _list(study["minor_links"], "minor relationships"):
        _object(item, {"link", "reason"})
        edge = _link(item["link"], catalog)
        _text(item["reason"])
        if edge in accounted or edge not in expected:
            raise ValueError("Minor deck relationship is duplicate, covered, or unsupported")
        accounted.add(edge)
        prose.append(item["reason"])
    if expected != accounted:
        raise ValueError(
            f"Deck study is missing {len(expected - accounted)} reviewed in-deck relationship(s)"
        )
    walkers = walkers_in(catalog.values())
    if (
        walkers
        and not additional_activation(catalog.values())
        and any(conflicting_loyalty_sequence(text, walkers) for text in prose)
    ):
        raise ValueError("Deck study spends loyalty twice in one turn")
    return {
        "structured_cards": len(covered),
        "synergies": counts["synergy"],
        "combos": counts["combo"],
        "links_in_entries": in_entries,
        "minor_links": len(accounted) - in_entries,
        "reviewed_links": len(expected),
        "reviewed_prints": reviewed,
    }


def coverage(data, catalog):
    kinds = {gid: _land(card) for gid, card in catalog.items()}
    nonlands = {gid for gid, land in kinds.items() if land is False}
    links, records = _reviewed_relationships(catalog)
    result = {
        "catalog_prints": len(catalog),
        "nonland_cards": len(nonlands),
        "unresolved_types": sum(v is None for v in kinds.values()),
        "legacy_nonland_roles": sum(str(gid) in (data.get("card_roles") or {}) for gid in nonlands),
        "legacy_mechanisms": len(data.get("mechanisms") or []),
        "structured_cards": 0,
        "synergies": 0,
        "combos": 0,
        "links_in_entries": 0,
        "minor_links": 0,
        "reviewed_links": len(links),
        "reviewed_prints": records,
    }
    if "study" in data:
        result.update(validate_study(data["study"], catalog))
    return result
