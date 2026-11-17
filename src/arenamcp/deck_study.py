"""Offline coverage admission for a deck study attached to a playbook.

Printed references and structural coverage are checkable. Strategic prose and
declared mana/timing are hypotheses for review, not executable planner rules.
Relationship coverage is relative to the independently installed public records.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from arenamcp.card_db import is_bogus_type_line, is_unknown_card_name
from arenamcp.card_facts.reviewed_notes import corpus, reviewed_print

logger = logging.getLogger(__name__)

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
        not isinstance(face.get("oracle_text"), str)
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
    if not isinstance(card, dict):
        return None
    faces = [card]
    for key in ("related_faces", "card_faces"):
        extra = card.get(key, [])
        if type(extra) is not list or any(type(face) is not dict for face in extra):
            return None
        faces.extend(extra)
    for key in ("linked_face_ids", "linked_faces"):
        linked = card.get(key, [])
        if not isinstance(linked, list):
            return None
        if linked:
            # The native adapter drops unresolved rows from related_faces, but
            # retains all declared IDs. An unrelated or partial face list
            # cannot establish that every face is a land.
            resolved = [face.get("grp_id") for face in card.get("related_faces", [])]
            if (
                any(type(gid) is not int or gid <= 4 for gid in (*linked, *resolved))
                or len(set(linked)) != len(linked)
                or sorted(linked) != sorted(resolved)
            ):
                return None
    if any(
        not isinstance(face.get("name"), str) or is_unknown_card_name(face["name"].strip()) for face in faces
    ):
        return None
    return faces


def current_study_sources_match(catalog, *, allow_legacy_face_projection=False):
    """Read exact prints from the initialized Arena database without starting a lookup service."""
    from arenamcp.card_db import MTGADatabaseAdapter, initialized_card_database

    database = initialized_card_database()
    native = database.get_raw_mtgadb() if database is not None else None
    if native is None or not native.available:
        return False
    adapter = MTGADatabaseAdapter(native)
    for gid, saved in catalog.items():
        current = adapter.get_card_by_arena_id(gid)
        if current is None or type(current.arena_id) is not int or current.arena_id != gid:
            return False
        # Older projections omit empty optional fields. Preserve substantive
        # native fields even when the saved study failed to supply them.
        printed = {
            key: value for key, value in vars(current).items() if key in saved or value not in ("", [], None)
        }
        if (
            allow_legacy_face_projection
            and "linked_face_ids" not in saved
            and "linked_faces" not in saved
            and printed.get("linked_face_ids")
        ):
            # Old playbooks stored the full ordered native faces, including
            # each Arena ID, before also storing the redundant ID vector.
            saved_faces = saved.get("related_faces")
            if not isinstance(saved_faces, list) or any(type(face) is not dict for face in saved_faces):
                return False
            ids = [face.get("grp_id") for face in saved_faces]
            if ids != printed["linked_face_ids"] or any(type(i) is not int for i in ids):
                return False
            saved = {**saved, "linked_face_ids": ids}
        if study_source(printed) != study_source(saved):
            return False
    return True


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


def validate_card_rule(row, catalog, prose):
    _object(row, {"card", "role", "play", "hold", "avoid", "evidence"})
    gid = _card(row["card"], catalog)
    if _land(catalog[gid]) is not False:
        raise ValueError("Card study requires a resolved nonland print")
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
    return gid


def validate_relationship_coverage(entries, minor, catalog, expected, prose):
    ids, accounted = set(), set()
    counts = {"synergy": 0, "combo": 0}
    for entry in _list(entries, "interactions"):
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
    for item in _list(minor, "minor relationships"):
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
    return counts, in_entries


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
    covered = set()
    prose = []
    for row in _list(study["cards"], "per-card rules"):
        gid = validate_card_rule(row, catalog, prose)
        if gid in covered:
            raise ValueError("Deck study contains a duplicate card rule")
        covered.add(gid)
    if nonlands != covered:
        raise ValueError(f"Deck study is missing {len(nonlands - covered)} nonland card rule(s)")

    counts, in_entries = validate_relationship_coverage(
        study["interactions"], study["minor_links"], catalog, expected, prose
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
        "minor_links": len(expected) - in_entries,
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
    if "study_chapters" in data:
        result["chapter_coverage"] = validate_study_chapters(data["study_chapters"], catalog)
    return result


# ---------------------------------------------------------------------------
# G2: Multi-pass deck study, chapters, shared client, and incremental edit reuse
# ---------------------------------------------------------------------------

CANONICAL_SWITCH_KEYS = ("fast_combo", "wide_board", "counterspells", "graveyard")
OPTIONAL_SWITCH_KEYS = ("flyers", "aggro_burn")
ALLOWED_SWITCH_KEYS = frozenset(CANONICAL_SWITCH_KEYS) | frozenset(OPTIONAL_SWITCH_KEYS)
_STUDY_UPLOAD_PATH = "/v1/deck-studies"


@dataclass(frozen=True)
class StudyBuildResult:
    """Result and telemetry for a completed validated multi-pass deck study."""

    deck_hash: str
    study: dict[str, Any]
    chapters: dict[str, Any]
    coverage: dict[str, Any]
    from_server: bool
    elapsed_s: float
    reasoning_tokens: int
    calls_made: int
    reused_cards: int = 0
    reused_interactions: int = 0
    model_id: str = ""


def deck_study_hash(
    catalog: dict[int, dict[str, Any]],
    commanders: Iterable[int] = (),
    *,
    counts: dict[int, int] | None = None,
) -> str:
    """Deterministic anonymous hash of the printed deck list and commanders."""
    counts = counts or {}
    cards_part = [
        [int(gid), max(1, int(counts.get(gid, 1))), study_source(catalog[gid])] for gid in sorted(catalog)
    ]
    payload = [STUDY_VERSION, sorted({int(gid) for gid in commanders}), cards_part]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _gid_list(values: Any, catalog: dict[int, dict[str, Any]], label: str) -> list[int]:
    if type(values) is not list:
        raise ValueError(f"Deck study chapters need a list for {label}")
    seen: set[int] = set()
    out: list[int] = []
    for gid in values:
        _card(gid, catalog)
        if gid not in seen:
            seen.add(gid)
            out.append(gid)
    return out


def validate_study_chapters(chapters: Any, catalog: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Validate machine-readable sequencing and opponent-switch chapters against catalog."""
    from arenamcp.loyalty_timing import additional_activation, conflicting_loyalty_sequence, walkers_in

    _object(chapters, {"sequencing", "switches"})
    seq = chapters["sequencing"]
    _object(seq, {"sequencing", "mulligan", "mana", "recovery"})
    prose: list[str] = []
    for field in ("mulligan", "mana", "recovery"):
        _text(seq[field])
        prose.append(seq[field])
    seq_rules = _list(seq["sequencing"], "sequencing rules")
    if not seq_rules:
        raise ValueError("Deck study sequencing chapter needs at least one sequencing rule")
    seq_ids: set[str] = set()
    for item in seq_rules:
        _object(item, {"id", "before", "after", "turn_window", "why"})
        _text(item["id"])
        if item["id"] in seq_ids:
            raise ValueError(f"Duplicate sequencing rule id: {item['id']}")
        seq_ids.add(item["id"])
        before = _gid_list(item["before"], catalog, "sequencing.before")
        after = _gid_list(item["after"], catalog, "sequencing.after")
        if not before and not after:
            raise ValueError("Sequencing rule must cite at least one catalog card ID")
        _text(item["turn_window"])
        _text(item["why"])
        prose.extend((item["turn_window"], item["why"]))

    switches = chapters["switches"]
    if type(switches) is not dict:
        raise ValueError("Deck study switches chapter must be a dict")
    if not set(CANONICAL_SWITCH_KEYS) <= set(switches):
        missing = sorted(set(CANONICAL_SWITCH_KEYS) - set(switches))
        raise ValueError(f"Deck study switches chapter missing required keys: {missing}")
    extra = sorted(set(switches) - ALLOWED_SWITCH_KEYS)
    if extra:
        raise ValueError(f"Deck study switches chapter contains unknown threat classes: {extra}")
    for key in sorted(switches):
        entry = switches[key]
        _object(entry, {"posture", "prioritize", "hold_up", "trim_or_delay", "rule"})
        _text(entry["posture"])
        _text(entry["rule"])
        _gid_list(entry["prioritize"], catalog, f"switches.{key}.prioritize")
        _gid_list(entry["hold_up"], catalog, f"switches.{key}.hold_up")
        _gid_list(entry["trim_or_delay"], catalog, f"switches.{key}.trim_or_delay")
        prose.extend((entry["posture"], entry["rule"]))

    walkers = walkers_in(catalog.values())
    if (
        walkers
        and not additional_activation(catalog.values())
        and any(conflicting_loyalty_sequence(line, walkers) for line in prose)
    ):
        raise ValueError("Deck study chapters spend loyalty twice in one turn")

    return {"sequencing_rules": len(seq_rules), "switches": sorted(switches)}


def _default_study_opener(
    method: str, url: str, body: bytes | None, headers: dict[str, str], timeout_s: float
) -> tuple[int, str]:
    req_headers = dict(headers)
    req_headers.setdefault("User-Agent", "mtgacoach/unknown (deck-study)")
    request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return int(response.status), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            text = error.read().decode("utf-8", "replace")
        except Exception:
            text = ""
        return int(error.code), text
    except Exception as error:
        logger.debug("deck study HTTP %s transport error: %s", method, error)
        return 0, f"{type(error).__name__}: {error}"


class DeckStudyClient:
    """HTTP client for shared server-side deck studies (anonymous by deck hash)."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        license_key: str = "",
        opener: Callable[[str, str, bytes | None, dict[str, str], float], tuple[int, str]] | None = None,
        timeout_s: float = 8.0,
    ) -> None:
        self._base_url = base_url
        self._license_key = license_key
        self._opener = opener or _default_study_opener
        self._timeout_s = float(timeout_s)
        self._logged_outage = False

    def _headers(self) -> dict[str, str]:
        from arenamcp.card_facts.client import _license_key

        headers = {"Content-Type": "application/json"}
        try:
            from arenamcp.client_metadata import get_client_headers

            headers.update(get_client_headers())
        except Exception:
            headers.setdefault("User-Agent", "mtgacoach/unknown (deck-study)")
        key = self._license_key or _license_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _endpoint(self, path: str) -> str:
        from arenamcp.card_facts.client import _website_base

        base = (self._base_url or _website_base()).rstrip("/")
        return base + path

    def fetch_study(self, deck_hash: str, *, version: int = STUDY_VERSION) -> dict[str, Any] | None:
        """Look up a shared study on the server; returns None on miss or error (fail-open)."""
        if not re.fullmatch(r"[0-9a-f]{64}", deck_hash or ""):
            return None
        url = f"{self._endpoint(_STUDY_UPLOAD_PATH)}/{deck_hash}?version={int(version)}"
        status, text = self._opener("GET", url, None, self._headers(), self._timeout_s)
        if status != 200:
            if not self._logged_outage:
                self._logged_outage = True
                logger.info(
                    "deck study lookup unavailable (status %s); building locally", status or "network"
                )
            return None
        self._logged_outage = False
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        if not isinstance(payload, dict) or payload.get("found") is not True:
            return None
        study_bundle = payload.get("study")
        return study_bundle if isinstance(study_bundle, dict) else None

    def upload_study(
        self,
        deck_hash: str,
        payload: dict[str, Any],
        *,
        version: int = STUDY_VERSION,
        model_id: str = "",
    ) -> bool:
        """Upload a validated anonymous study payload; never raises and sends zero PII."""
        if not re.fullmatch(r"[0-9a-f]{64}", deck_hash or "") or not isinstance(payload, dict):
            return False
        sanitized = {
            key: payload[key]
            for key in ("study", "chapters")
            if key in payload and isinstance(payload[key], dict)
        }
        if not sanitized:
            sanitized = dict(payload)
        body = json.dumps(
            {
                "deck_hash": deck_hash,
                "study_version": int(version),
                "model_id": str(model_id or "")[:120],
                "study": sanitized,
            },
            allow_nan=False,
        ).encode("utf-8")
        status, text = self._opener(
            "POST", self._endpoint(_STUDY_UPLOAD_PATH), body, self._headers(), self._timeout_s
        )
        if status != 200:
            logger.debug("deck study upload returned status %s: %s", status, str(text)[:160])
            return False
        try:
            parsed = json.loads(text)
        except ValueError:
            return False
        return isinstance(parsed, dict) and parsed.get("stored") is True


def _strip_json_fences(raw: Any) -> dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    for candidate in (
        text,
        text[text.find("{") : text.rfind("}") + 1] if "{" in text and "}" in text else "",
    ):
        if not candidate:
            continue
        for variant in (candidate, re.sub(r",(\s*[}\]])", r"\1", candidate)):
            try:
                parsed = json.loads(variant)
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                continue
    raise ValueError("Model response did not contain valid JSON object")


def _load_card_facts_map(
    catalog: dict[int, dict[str, Any]], explicit: dict[str, dict[str, Any]] | None
) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = dict(explicit or {})
    try:
        from arenamcp.card_facts.client import fresh_memory_facts, warm_memory_from_disk
        from arenamcp.card_facts.prompt_render import candidate_fact_hashes, normalized_scryfall_card

        cards = [c for c in catalog.values() if isinstance(c, dict)]
        norm_cards = [n for c in cards if (n := normalized_scryfall_card(c)) is not None]
        alt = [h for c in cards for h in candidate_fact_hashes(c)]
        warm_memory_from_disk(cards + norm_cards, alt_hashes=alt)
        loaded = fresh_memory_facts(cards + norm_cards)
        for k, v in loaded.items():
            merged.setdefault(k, v)
    except Exception:
        pass
    return merged


def _fact_for_card(card: dict[str, Any], card_facts_map: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not card_facts_map:
        return None
    try:
        from arenamcp.card_facts.prompt_render import candidate_fact_hashes

        for digest in candidate_fact_hashes(card):
            if digest in card_facts_map:
                return card_facts_map[digest]
    except Exception:
        pass
    return None


def _all_deck_relationships(
    catalog: dict[int, dict[str, Any]], card_facts_map: dict[str, dict[str, Any]]
) -> tuple[set[tuple[int, int, str]], set[tuple[int, int, str]]]:
    """Return (reviewed_edges, all_edges_including_card_facts)."""
    reviewed, _ = _reviewed_relationships(catalog)
    all_edges: set[tuple[int, int, str]] = set(reviewed)
    by_name: dict[str, list[int]] = {}
    for gid, card in catalog.items():
        if isinstance(card, dict) and isinstance(card.get("name"), str) and card["name"].strip():
            by_name.setdefault(card["name"].strip().casefold(), []).append(gid)
    for gid, card in catalog.items():
        fact = _fact_for_card(card, card_facts_map)
        if not fact:
            continue
        rels = fact.get("relationships")
        if isinstance(rels, dict):
            links = rels.get("links") or ()
        elif isinstance(rels, list):
            links = rels
        else:
            links = ()
        for link in links:
            if not isinstance(link, dict):
                continue
            target_name = str(link.get("card") or link.get("target") or "").strip().casefold()
            role = str(link.get("role") or "synergy").strip() or "synergy"
            for target_gid in by_name.get(target_name, ()):
                if target_gid != gid:
                    all_edges.add((gid, target_gid, role))
    return reviewed, all_edges


def _cluster_edges(
    edges: Iterable[tuple[int, int, str]],
) -> list[tuple[list[int], list[tuple[int, int, str]]]]:
    remaining = set(edges)
    clusters: list[tuple[list[int], list[tuple[int, int, str]]]] = []
    while remaining:
        seed = min(remaining)
        nodes = {seed[0], seed[1]}
        group_edges: set[tuple[int, int, str]] = set()
        while True:
            touched = {e for e in remaining if e[0] in nodes or e[1] in nodes}
            if not touched:
                break
            remaining -= touched
            group_edges |= touched
            for src, dst, _role in touched:
                nodes.add(src)
                nodes.add(dst)
        clusters.append((sorted(nodes), sorted(group_edges)))
    return clusters


def _format_card_context_line(
    gid: int, card: dict[str, Any], fact: dict[str, Any] | None, *, is_commander: bool = False
) -> str:
    from arenamcp.deck_strategy import oracle_rules

    role_tag = " [COMMANDER]" if is_commander else ""
    header = (
        f"CARD {gid}: {card.get('name', 'Unknown')}{role_tag} | "
        f"{card.get('mana_cost', '')} | {card.get('type_line', '')}"
    )
    if card.get("power") not in (None, "") and card.get("toughness") not in (None, ""):
        header += f" {card['power']}/{card['toughness']}"
    lines = [header]
    for num, rule_text in enumerate(oracle_rules(card), 1):
        lines.append(f"  RULE {gid}:{num}: {rule_text}")
    if fact:
        effects = [
            f"{e.get('trigger', '')}:{((e.get('effect') or {}).get('effect_type', ''))}"
            for e in (fact.get("effects") or ())
            if isinstance(e, dict)
        ]
        if effects:
            lines.append(f"  CARD_FACTS_EFFECTS: {', '.join(effects[:8])}")
        rels = fact.get("relationships")
        if isinstance(rels, dict) and rels.get("links"):
            link_str = ", ".join(
                f"{ln.get('card')} ({ln.get('role')})"
                for ln in rels["links"][:8]
                if isinstance(ln, dict) and ln.get("card")
            )
            if link_str:
                lines.append(f"  CARD_FACTS_LINKS: {link_str}")
    return "\n".join(lines)


def _coerce_nonneg_int(val: Any, label: str) -> int:
    if isinstance(val, str) and val.strip().isdigit():
        val = int(val.strip())
    if type(val) is not int or val < 0:
        raise ValueError(f"invalid mana count for {label}")
    return val


def _normalize_conditions(
    raw: Any, required_gid: int | None, catalog: dict[int, dict[str, Any]]
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        if required_gid is None:
            raise ValueError("conditions must be an object")
        raw = {}
    pieces_raw = raw.get("pieces")
    if not isinstance(pieces_raw, list) or not pieces_raw:
        if required_gid is None:
            raise ValueError("interaction conditions require at least 2 pieces")
        pieces_raw = [{"card": required_gid, "zone": "hand"}]
    seen_cards: set[int] = set()
    pieces: list[dict[str, Any]] = []
    for piece in pieces_raw:
        if not isinstance(piece, dict):
            if required_gid is not None:
                continue
            raise ValueError("condition piece must be an object")
        gid = piece.get("card")
        if isinstance(gid, str) and gid.strip().isdigit():
            gid = int(gid.strip())
        if type(gid) is not int or gid not in catalog:
            if required_gid is not None:
                # Pass 1 rules can reference placeholder IDs (e.g. 0 for any graveyard target).
                continue
            raise ValueError(f"condition names unknown card: {gid}")
        if gid in seen_cards:
            continue
        zone = str(piece.get("zone") or "battlefield").strip().lower()
        if zone not in _ZONES:
            zone = "battlefield"
        seen_cards.add(gid)
        pieces.append({"card": gid, "zone": zone})
    if required_gid is not None and required_gid not in seen_cards:
        pieces.insert(0, {"card": required_gid, "zone": "hand"})
        seen_cards.add(required_gid)
    raw_mana = raw.get("mana") if isinstance(raw.get("mana"), dict) else {}
    mana: dict[str, int] = {"generic": _coerce_nonneg_int(raw_mana.get("generic", 0), "generic")}
    for pip in "WUBRGC":
        if pip in raw_mana:
            mana[pip] = _coerce_nonneg_int(raw_mana[pip], pip)
    first_turn = raw.get("first_turn", 1)
    if isinstance(first_turn, str) and first_turn.strip().isdigit():
        first_turn = int(first_turn.strip())
    if type(first_turn) is not int or first_turn < 1:
        first_turn = 1
    return {"pieces": pieces, "mana": mana, "first_turn": first_turn}


def _build_evidence(
    raw_evidence: Any, required_gids: set[int], catalog: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    from arenamcp.deck_strategy import oracle_rules

    if not isinstance(raw_evidence, list):
        raw_evidence = []
    out: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, int]] = set()
    cited_gids: set[int] = set()
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        gid = item.get("card")
        rule = item.get("rule")
        if isinstance(gid, str) and gid.strip().isdigit():
            gid = int(gid.strip())
        if isinstance(rule, str) and rule.strip().isdigit():
            rule = int(rule.strip())
        if type(gid) is not int or gid not in catalog or gid not in required_gids:
            continue
        rules = oracle_rules(catalog[gid])
        if type(rule) is not int or not (1 <= rule <= len(rules)):
            continue
        if (gid, rule) in seen_pairs:
            continue
        seen_pairs.add((gid, rule))
        cited_gids.add(gid)
        out.append(
            {
                "card": gid,
                "source": study_source(catalog[gid]),
                "rule": rule,
                "quote": rules[rule - 1],
            }
        )
    if cited_gids != required_gids:
        # Ensure every cited piece supplies at least its printed characteristics rule if omitted
        for gid in sorted(required_gids - cited_gids):
            rules = oracle_rules(catalog[gid])
            if not rules:
                raise ValueError(f"No printed rules for card {gid}")
            rule_idx = 1
            out.append(
                {
                    "card": gid,
                    "source": study_source(catalog[gid]),
                    "rule": rule_idx,
                    "quote": rules[rule_idx - 1],
                }
            )
    return out


def _call_study_backend(
    backend: Any,
    system: str,
    user: str,
    *,
    response_format: dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    extra_kwargs: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    if cancel_check is not None and cancel_check():
        raise ValueError("Deck study cancelled")
    kwargs: dict[str, Any] = {
        "temperature": 0.0,
        "max_tokens": 12288,
        "background": True,
        "idle_playbook": True,
        "enable_thinking": True,
        "reasoning_effort": "high",
        "call_class": "background.deck_playbook",
        "response_format": response_format if response_format is not None else {"type": "json_object"},
    }
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    try:
        raw = backend.complete(system, user, **kwargs)
    except TypeError:
        try:
            raw = backend.complete(system, user, max_tokens=12288)
        except TypeError:
            raw = backend.complete(system, user)
    if isinstance(raw, str) and raw.startswith("[BACKEND ERROR]"):
        raise ValueError(raw)
    metrics = getattr(backend, "last_request_metrics", None)
    reasoning_tokens = 0
    if isinstance(metrics, dict):
        rt = metrics.get("reasoning_tokens") or metrics.get("thinking_tokens") or 0
        if type(rt) is int and rt >= 0:
            reasoning_tokens = rt
        else:
            details = metrics.get("completion_tokens_details")
            if isinstance(details, dict) and type(details.get("reasoning_tokens")) is int:
                reasoning_tokens = max(0, details["reasoning_tokens"])
    return _strip_json_fences(raw), reasoning_tokens


def _study_cards_pass(
    batch_gids: list[int],
    catalog: dict[int, dict[str, Any]],
    commanders: set[int],
    profile_text: str,
    card_facts_map: dict[str, dict[str, Any]],
    backend: Any,
    cancel_check: Callable[[], bool] | None,
    extra_kwargs: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], int, int]:
    system = (
        "DECK_STUDY_PASS: cards\n"
        "Analyze each nonland card for its exact role in this deck using ONLY supplied printed rules and CardFacts.\n"
        'Return JSON: {"cards": [{"card": int, "role": str, '
        '"play": {"advice": str, "conditions": {"pieces": [{"card": int, "zone": str}], "mana": {"generic": int}, "first_turn": int}}, '
        '"hold": {"advice": str, "conditions": ...}, "avoid": {"advice": str, "conditions": ...}, '
        '"evidence": [{"card": int, "rule": int}]}]}.\n'
        "Zones must be in {hand, battlefield, graveyard, exile, command, library, stack}. Cite only printed RULE numbers."
    )
    cards_block = "\n\n".join(
        _format_card_context_line(
            gid,
            catalog[gid],
            _fact_for_card(catalog[gid], card_facts_map),
            is_commander=(gid in commanders),
        )
        for gid in batch_gids
    )
    base_user = (
        f"DECK PROFILE:\n{profile_text}\n\n"
        f"TARGET CARD IDS ({len(batch_gids)}): {json.dumps(batch_gids)}\n\n"
        f"{cards_block}"
    )
    reasoning = 0
    calls = 0
    by_gid: dict[int, dict[str, Any]] = {}
    remaining = list(batch_gids)
    for attempt in range(2):
        target_ids = batch_gids if attempt == 0 else remaining
        rem_block = "\n\n".join(
            _format_card_context_line(
                gid,
                catalog[gid],
                _fact_for_card(catalog[gid], card_facts_map),
                is_commander=(gid in commanders),
            )
            for gid in target_ids
        )
        prompt_user = (
            base_user
            if attempt == 0
            else (
                f"Repair missing/invalid card entries for TARGET CARD IDS: {json.dumps(remaining)}.\n"
                f"Return STRICT valid JSON only.\n\n{rem_block}"
            )
        )
        try:
            batch_extra = {"reasoning_effort": "low", **(extra_kwargs or {})}
            parsed, rt = _call_study_backend(
                backend, system, prompt_user, cancel_check=cancel_check, extra_kwargs=batch_extra
            )
        except ValueError as exc:
            logger.debug("Card study attempt %d failed (%s)", attempt + 1, exc)
            if attempt == 1:
                raise
            continue
        reasoning += rt
        calls += 1
        raw_cards = parsed.get("cards") if isinstance(parsed.get("cards"), list) else []
        for row in raw_cards:
            if not isinstance(row, dict):
                continue
            gid = row.get("card")
            if isinstance(gid, str) and gid.strip().isdigit():
                gid = int(gid.strip())
            if type(gid) is not int or gid not in batch_gids or gid in by_gid:
                continue
            try:
                role = str(row.get("role") or "").strip()
                if not role:
                    raise ValueError("missing role")
                norm_actions: dict[str, dict[str, Any]] = {}
                needed_pieces: set[int] = {gid}
                for k in ("play", "hold", "avoid"):
                    act = row.get(k)
                    if not isinstance(act, dict):
                        raise ValueError(f"missing {k}")
                    advice = str(act.get("advice") or "").strip()
                    if not advice:
                        raise ValueError(f"empty {k}.advice")
                    conds = _normalize_conditions(act.get("conditions"), gid, catalog)
                    needed_pieces.update(p["card"] for p in conds["pieces"])
                    norm_actions[k] = {"advice": advice, "conditions": conds}
                evidence = _build_evidence(row.get("evidence"), needed_pieces, catalog)
                candidate = {
                    "card": gid,
                    "role": role,
                    **norm_actions,
                    "evidence": evidence,
                }
                validate_card_rule(candidate, catalog, [])
                by_gid[gid] = candidate
            except Exception as exc:
                logger.debug("Card rule validation failed for gid=%s: %s", gid, exc)
        remaining = [gid for gid in batch_gids if gid not in by_gid]
        if not remaining:
            break
    if remaining:
        raise ValueError(f"Deck study cards pass missing/invalid rules for card IDs: {remaining}")
    return [by_gid[gid] for gid in batch_gids], reasoning, calls


def _normalize_interactions_and_minor(
    parsed: dict[str, Any],
    *,
    allowed_ids: set[int],
    expected_edges: set[tuple[int, int, str]],
    catalog: dict[int, dict[str, Any]],
    next_idx_start: int = 1,
    skip_invalid: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    minors: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    idx = next_idx_start
    for item in parsed.get("interactions") or ():
        if not isinstance(item, dict):
            continue
        try:
            kind = str(item.get("kind") or "synergy").strip().lower()
            if kind not in ("synergy", "combo"):
                kind = "synergy"
            conds = _normalize_conditions(item.get("conditions"), None, catalog)
            piece_ids = {p["card"] for p in conds["pieces"]}
            if len(piece_ids) < 2 or not (piece_ids <= allowed_ids):
                raise ValueError("Deck study interaction must name >= 2 allowed pieces from catalog")
            outcome = str(item.get("outcome") or "").strip()
            stops = str(item.get("stops") or "").strip()
            if not outcome or not stops:
                raise ValueError("Deck study interaction needs nonempty outcome and stops")
            evidence = _build_evidence(item.get("evidence"), piece_ids, catalog)
            links: list[dict[str, Any]] = []
            for raw_link in item.get("links") or ():
                if not isinstance(raw_link, dict):
                    continue
                src, dst = raw_link.get("source"), raw_link.get("target")
                role = str(raw_link.get("role") or "")
                if (src, dst, role) in expected_edges and {src, dst} <= piece_ids:
                    links.append({"source": src, "target": dst, "role": role})
        except ValueError as exc:
            if skip_invalid and not any(
                isinstance(rl, dict)
                and (rl.get("source"), rl.get("target"), str(rl.get("role") or "")) in expected_edges
                for rl in (item.get("links") or ())
            ):
                logger.debug("Skipping invalid optional study interaction (%s)", exc)
                continue
            raise
        eid = str(item.get("id") or "").strip() or f"syn_{idx}"
        while eid in seen_ids:
            idx += 1
            eid = f"syn_{idx}"
        seen_ids.add(eid)
        idx += 1
        entries.append(
            {
                "id": eid,
                "kind": kind,
                "conditions": conds,
                "outcome": outcome,
                "stops": stops,
                "evidence": evidence,
                "links": links,
            }
        )
    for item in parsed.get("minor_links") or ():
        if not isinstance(item, dict):
            continue
        raw_link = item.get("link")
        if not isinstance(raw_link, dict):
            continue
        edge = (raw_link.get("source"), raw_link.get("target"), str(raw_link.get("role") or ""))
        if edge not in expected_edges:
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            continue
        minors.append(
            {
                "link": {"source": edge[0], "target": edge[1], "role": edge[2]},
                "reason": reason,
            }
        )
    return entries, minors


def _gid_int_list(raw: Any, catalog: dict[int, dict[str, Any]], *, drop_unknown: bool = False) -> list[int]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError("Deck study card ID list must be a list")
    out: list[int] = []
    for g in raw:
        if isinstance(g, str) and g.strip().isdigit():
            g = int(g.strip())
        if type(g) is int and g > 0:
            if not drop_unknown or g in catalog:
                if g not in out:
                    out.append(g)
        elif not drop_unknown:
            raise ValueError(f"Invalid card ID in chapter list: {g!r}")
    return out


def _normalize_seq_chapter(
    parsed_seq: dict[str, Any], catalog: dict[int, dict[str, Any]], *, drop_unknown: bool = False
) -> dict[str, Any]:
    raw_seq_rules = parsed_seq.get("sequencing")
    if raw_seq_rules is not None and not isinstance(raw_seq_rules, list):
        raise ValueError("Deck study sequencing must be a list")
    norm_seq_rules = []
    seen_seq_ids: set[str] = set()
    for ridx, r in enumerate(raw_seq_rules or (), 1):
        if not isinstance(r, dict):
            if not drop_unknown:
                raise ValueError("Deck study sequencing entry must be an object")
            continue
        before = _gid_int_list(r.get("before"), catalog, drop_unknown=drop_unknown)
        after = _gid_int_list(r.get("after"), catalog, drop_unknown=drop_unknown)
        if not before and not after:
            if not drop_unknown:
                raise ValueError("Deck study sequencing rule has no valid before/after card IDs")
            continue
        rid = str(r.get("id") or f"seq_{ridx}").strip() or f"seq_{ridx}"
        while rid in seen_seq_ids:
            ridx += 1
            rid = f"seq_{rid}_{ridx}"
        seen_seq_ids.add(rid)
        tw = str(r.get("turn_window") or r.get("turns") or "").strip()
        why = str(r.get("why") or r.get("reason") or "").strip()
        if not (tw and why):
            if not drop_unknown:
                raise ValueError("Deck study sequencing rule missing turn_window or why")
            continue
        norm_seq_rules.append({"id": rid, "before": before, "after": after, "turn_window": tw, "why": why})
    return {
        "sequencing": norm_seq_rules,
        "mulligan": str(parsed_seq.get("mulligan") or "").strip(),
        "mana": str(parsed_seq.get("mana") or "").strip(),
        "recovery": str(parsed_seq.get("recovery") or "").strip(),
    }


def _normalize_switches_chapter(
    parsed_sw: dict[str, Any],
    catalog: dict[int, dict[str, Any]],
    *,
    drop_unknown: bool = False,
    allow_defaults: bool = False,
) -> dict[str, Any]:
    src = parsed_sw["switches"] if isinstance(parsed_sw.get("switches"), dict) else parsed_sw
    unknown_keys = sorted(k for k in src if k not in ALLOWED_SWITCH_KEYS)
    if unknown_keys and not drop_unknown:
        raise ValueError(f"Unknown threat class key(s) in switches: {unknown_keys}")
    candidate_sw: dict[str, Any] = {}
    for k in ALLOWED_SWITCH_KEYS:
        raw_sw = src.get(k)
        if not isinstance(raw_sw, dict):
            continue
        posture = str(raw_sw.get("posture") or "").strip() or "balanced"
        rule = str(raw_sw.get("rule") or "").strip()
        if not rule:
            if not drop_unknown:
                raise ValueError(f"Switch {k!r} missing non-empty rule")
            continue
        candidate_sw[k] = {
            "posture": posture,
            "prioritize": _gid_int_list(raw_sw.get("prioritize"), catalog, drop_unknown=drop_unknown),
            "hold_up": _gid_int_list(raw_sw.get("hold_up"), catalog, drop_unknown=drop_unknown),
            "trim_or_delay": _gid_int_list(raw_sw.get("trim_or_delay"), catalog, drop_unknown=drop_unknown),
            "rule": rule,
        }
    if allow_defaults and candidate_sw:
        for k in CANONICAL_SWITCH_KEYS:
            candidate_sw.setdefault(
                k,
                {
                    "posture": "balanced",
                    "prioritize": [],
                    "hold_up": [],
                    "trim_or_delay": [],
                    "rule": f"Adjust tempo and interaction cleanly against {k.replace('_', ' ')} plans.",
                },
            )
    return candidate_sw


def _run_chapters_passes(
    *,
    catalog: dict[int, dict[str, Any]],
    cmd_list: list[int],
    seed_text: str,
    all_cards: list[dict[str, Any]],
    interactions: list[dict[str, Any]],
    format_name: str,
    backend: Any,
    cancel_check: Callable[[], bool] | None,
    extra_complete_kwargs: dict[str, Any] | None,
    changed_cards: list[int] | None = None,
) -> tuple[dict[str, Any], int, int]:
    """Run Pass 3 (sequencing) and Pass 4 (switches) with strict validation on attempt 0."""
    reasoning_delta = 0
    calls_delta = 0
    changed_header = f"CHANGED_CARDS: {json.dumps(changed_cards)}\n" if changed_cards is not None else ""

    seq_system = (
        "DECK_STUDY_PASS: sequencing\n"
        "Specify deck-specific sequencing, mulligan criteria, land/mana plan, and recovery after disruption.\n"
        'Return JSON: {"sequencing": [{"id": str, "before": [int], "after": [int], "turn_window": str, "why": str}], '
        '"mulligan": str, "mana": str, "recovery": str}. Only cite valid deck card IDs.'
    )
    seq_user = (
        f"FORMAT: {format_name or 'Constructed'}\nCOMMANDERS: {json.dumps(cmd_list)}\n"
        f"{changed_header}"
        f"DECK PROFILE:\n{seed_text}\n\n"
        "CARD ROLES:\n"
        + "\n".join(
            f"  {c['card']}: {catalog[c['card']].get('name')} — {c['role']}"
            for c in all_cards
            if c.get("card") in catalog
        )
        + "\n\nINTERACTIONS:\n"
        + "\n".join(
            f"  {i['id']} ({[p['card'] for p in i['conditions']['pieces']]}): {i['outcome']}"
            for i in interactions
        )
    )
    seq_chapter: dict[str, Any] | None = None
    for attempt in range(2):
        msg = (
            seq_user
            if attempt == 0
            else "Previous sequencing output failed validation; fix card IDs, required keys, and return STRICT valid JSON.\n"
            + seq_user
        )
        try:
            parsed_seq, rt = _call_study_backend(
                backend, seq_system, msg, cancel_check=cancel_check, extra_kwargs=extra_complete_kwargs
            )
            reasoning_delta += rt
            calls_delta += 1
        except ValueError as exc:
            logger.debug("Sequencing pass attempt %d failed (%s)", attempt + 1, exc)
            if attempt == 1:
                raise
            continue
        try:
            candidate_seq = _normalize_seq_chapter(parsed_seq, catalog, drop_unknown=(attempt == 1))
            validate_study_chapters(
                {
                    "sequencing": candidate_seq,
                    "switches": {
                        k: {
                            "posture": "placeholder",
                            "prioritize": [],
                            "hold_up": [],
                            "trim_or_delay": [],
                            "rule": "placeholder",
                        }
                        for k in CANONICAL_SWITCH_KEYS
                    },
                },
                catalog,
            )
            seq_chapter = candidate_seq
            break
        except ValueError as exc:
            logger.debug("Sequencing validation failed attempt %d: %s", attempt + 1, exc)
            if attempt == 1:
                raise

    sw_system = (
        "DECK_STUDY_PASS: switches\n"
        "Specify posture switches keyed by opposing threat class using ONLY supplied card IDs.\n"
        f"Required keys: {list(CANONICAL_SWITCH_KEYS)}. Optional keys: {list(OPTIONAL_SWITCH_KEYS)}.\n"
        'Return JSON: {"<threat_class>": {"posture": str, "prioritize": [int], "hold_up": [int], "trim_or_delay": [int], "rule": str}}.'
    )
    sw_user = (
        f"FORMAT: {format_name or 'Constructed'}\nCOMMANDERS: {json.dumps(cmd_list)}\n"
        f"{changed_header}"
        "CARD ROLES:\n"
        + "\n".join(
            f"  {c['card']}: {catalog[c['card']].get('name')} — {c['role']}"
            for c in all_cards
            if c.get("card") in catalog
        )
    )
    sw_chapter: dict[str, Any] | None = None
    for attempt in range(2):
        msg = (
            sw_user
            if attempt == 0
            else "Previous switches output failed validation; supply all 4 required keys with valid catalog card IDs and STRICT valid JSON.\n"
            + sw_user
        )
        try:
            parsed_sw, rt = _call_study_backend(
                backend, sw_system, msg, cancel_check=cancel_check, extra_kwargs=extra_complete_kwargs
            )
            reasoning_delta += rt
            calls_delta += 1
        except ValueError as exc:
            logger.debug("Switches pass attempt %d failed (%s)", attempt + 1, exc)
            if attempt == 1:
                raise
            continue
        try:
            candidate_sw = _normalize_switches_chapter(
                parsed_sw, catalog, drop_unknown=(attempt == 1), allow_defaults=(attempt == 1)
            )
            chapters = {"sequencing": seq_chapter, "switches": candidate_sw}
            validate_study_chapters(chapters, catalog)
            sw_chapter = candidate_sw
            break
        except ValueError as exc:
            logger.debug("Switches validation failed attempt %d: %s", attempt + 1, exc)
            if attempt == 1:
                raise

    return {"sequencing": seq_chapter, "switches": sw_chapter}, reasoning_delta, calls_delta


def build_deck_study(
    catalog: dict[int, dict[str, Any]],
    commanders: Iterable[int],
    backend: Any,
    *,
    format_name: str = "",
    deck_counts: dict[int, int] | None = None,
    card_facts_map: dict[str, dict[str, Any]] | None = None,
    study_client: DeckStudyClient | None = None,
    cancel_check: Callable[[], bool] | None = None,
    batch_size: int = 16,
    extra_complete_kwargs: dict[str, Any] | None = None,
) -> StudyBuildResult:
    """Build a validated multi-pass deck study (cards, clusters, discovery, sequencing, switches)."""
    from arenamcp.deck_profile import build_profile

    started = time.monotonic()
    cmd_list = sorted({int(gid) for gid in commanders if type(gid) is int and gid > 0})
    cmd_set = set(cmd_list)
    counts = dict(deck_counts or {gid: 1 for gid in catalog})
    dhash = deck_study_hash(catalog, cmd_list, counts=counts)

    if study_client is not None:
        server_hit = study_client.fetch_study(dhash, version=STUDY_VERSION)
        if isinstance(server_hit, dict) and isinstance(server_hit.get("study"), dict):
            try:
                cov = validate_study(server_hit["study"], catalog)
                chapters_hit = server_hit.get("chapters")
                if isinstance(chapters_hit, dict):
                    chap_cov = validate_study_chapters(chapters_hit, catalog)
                    cov["chapter_coverage"] = chap_cov
                else:
                    chapters_hit = None
                if chapters_hit is not None:
                    return StudyBuildResult(
                        deck_hash=dhash,
                        study=server_hit["study"],
                        chapters=chapters_hit,
                        coverage=cov,
                        from_server=True,
                        elapsed_s=time.monotonic() - started,
                        reasoning_tokens=0,
                        calls_made=0,
                    )
            except (ValueError, TypeError, KeyError) as exc:
                logger.info("Ignoring invalid server deck study for %s: %s", dhash[:12], exc)

    facts_map = _load_card_facts_map(catalog, card_facts_map)
    profile = build_profile(
        [{**catalog[gid], "grp_id": gid, "count": counts.get(gid, 1)} for gid in sorted(catalog)],
        facts=facts_map,
        commanders=[catalog[g].get("name", "") for g in cmd_list if g in catalog],
    )
    nonlands = sorted(gid for gid, card in catalog.items() if _land(card) is False)
    if not nonlands:
        raise ValueError("Deck study requires at least one resolved nonland card")

    reasoning_total = 0
    calls_total = 0
    all_cards: list[dict[str, Any]] = []
    for start in range(0, len(nonlands), max(1, int(batch_size))):
        batch = nonlands[start : start + max(1, int(batch_size))]
        cards_out, rt, ncalls = _study_cards_pass(
            batch,
            catalog,
            cmd_set,
            profile.seed_text,
            facts_map,
            backend,
            cancel_check,
            extra_complete_kwargs,
        )
        all_cards.extend(cards_out)
        reasoning_total += rt
        calls_total += ncalls

    # Pass 2A: Relationship clusters
    expected_reviewed, all_edges = _all_deck_relationships(catalog, facts_map)
    clusters = _cluster_edges(all_edges)
    interactions: list[dict[str, Any]] = []
    minor_links: list[dict[str, Any]] = []
    used_interaction_ids: set[str] = set()
    used_signatures: set[tuple[int, ...]] = set()
    covered_reviewed: set[tuple[int, int, str]] = set()

    def _add_interaction_list(items: list[dict[str, Any]]) -> None:
        for entry in items:
            sig = tuple(sorted(p["card"] for p in entry["conditions"]["pieces"]))
            eid = entry["id"]
            if eid in used_interaction_ids:
                suffix = 1
                while f"{eid}_{suffix}" in used_interaction_ids:
                    suffix += 1
                entry["id"] = f"{eid}_{suffix}"
            used_interaction_ids.add(entry["id"])
            if sig in used_signatures and not entry["links"]:
                continue
            used_signatures.add(sig)
            interactions.append(entry)
            for ln in entry["links"]:
                covered_reviewed.add((ln["source"], ln["target"], ln["role"]))

    if clusters:
        cluster_system = (
            "DECK_STUDY_PASS: cluster\n"
            "For each connected relationship cluster, explain what the pieces do together or mark minor reviewed edges.\n"
            'Return JSON: {"interactions": [{"id": str, "kind": "synergy"|"combo", '
            '"conditions": {"pieces": [{"card": int, "zone": str}], "mana": {"generic": int}, "first_turn": int}, '
            '"outcome": str, "stops": str, "evidence": [{"card": int, "rule": int}], '
            '"links": [{"source": int, "target": int, "role": str}]}], '
            '"minor_links": [{"link": {"source": int, "target": int, "role": str}, "reason": str}]}.\n'
            "Every reviewed edge in expected_reviewed must appear in either an interaction.links or minor_links."
        )
        for cluster_nodes, cluster_edges in clusters:
            cluster_reviewed = [e for e in cluster_edges if e in expected_reviewed]
            cards_text = "\n\n".join(
                _format_card_context_line(
                    gid,
                    catalog[gid],
                    _fact_for_card(catalog[gid], facts_map),
                    is_commander=(gid in cmd_set),
                )
                for gid in cluster_nodes
            )
            edges_json = json.dumps(
                [
                    {"source": e[0], "target": e[1], "role": e[2], "reviewed": e in expected_reviewed}
                    for e in cluster_edges
                ]
            )
            c_user = f"CLUSTER CARDS: {json.dumps(cluster_nodes)}\nEDGES: {edges_json}\n\n{cards_text}"
            for attempt in range(2):
                msg = (
                    c_user
                    if attempt == 0
                    else "Repair uncovered cluster edges: "
                    + json.dumps(
                        [
                            {"source": e[0], "target": e[1], "role": e[2]}
                            for e in cluster_reviewed
                            if e not in covered_reviewed
                        ]
                    )
                    + "\n\n"
                    + c_user
                )
                try:
                    parsed, rt = _call_study_backend(
                        backend,
                        cluster_system,
                        msg,
                        cancel_check=cancel_check,
                        extra_kwargs=extra_complete_kwargs,
                    )
                except ValueError as exc:
                    logger.debug("Cluster pass attempt %d failed (%s)", attempt + 1, exc)
                    if attempt == 1:
                        raise
                    continue
                reasoning_total += rt
                calls_total += 1
                try:
                    cand_int, cand_min = _normalize_interactions_and_minor(
                        parsed,
                        allowed_ids=set(catalog),
                        expected_edges=expected_reviewed,
                        catalog=catalog,
                        next_idx_start=len(interactions) + 1,
                        skip_invalid=True,
                    )
                except ValueError as exc:
                    logger.debug("Cluster normalization attempt %d failed (%s)", attempt + 1, exc)
                    if attempt == 1:
                        raise
                    continue
                _add_interaction_list(cand_int)
                for ml in cand_min:
                    edge = (ml["link"]["source"], ml["link"]["target"], ml["link"]["role"])
                    if edge not in covered_reviewed:
                        minor_links.append(ml)
                        covered_reviewed.add(edge)
                if all(e in covered_reviewed for e in cluster_reviewed):
                    break

    # Pass 2B: Whole-deck discovery over card effects & roles
    discovery_system = (
        "DECK_STUDY_PASS: discovery\n"
        "Review all nonland cards and effects in this deck to find multi-card synergies and combos NOT yet listed in static links.\n"
        "Every interaction MUST cite at least 2 DISTINCT valid card IDs in conditions.pieces.\n"
        'Return JSON: {"interactions": [{"id": str, "kind": "synergy"|"combo", '
        '"conditions": {"pieces": [{"card": int, "zone": str}], "mana": {"generic": int}, "first_turn": int}, '
        '"outcome": str, "stops": str, "evidence": [{"card": int, "rule": int}], "links": []}]}.'
    )
    digest_lines = [
        _format_card_context_line(
            gid,
            catalog[gid],
            _fact_for_card(catalog[gid], facts_map),
            is_commander=(gid in cmd_set),
        )
        for gid in nonlands
    ]
    d_user = (
        f"FORMAT: {format_name or 'Constructed'}\nCOMMANDERS: {json.dumps(cmd_list)}\n"
        f"EXISTING INTERACTION SIGNATURES: {json.dumps([sorted(p['card'] for p in e['conditions']['pieces']) for e in interactions])}\n\n"
        + "\n".join(digest_lines)
    )
    for attempt in range(2):
        msg = (
            d_user
            if attempt == 0
            else "Every interaction needs >= 2 distinct supplied card IDs. Return STRICT valid JSON only.\n"
            + d_user
        )
        try:
            parsed_disc, rt = _call_study_backend(
                backend,
                discovery_system,
                msg,
                cancel_check=cancel_check,
                extra_kwargs=extra_kwargs_or(extra_complete_kwargs),
            )
            reasoning_total += rt
            calls_total += 1
            disc_ints, _ = _normalize_interactions_and_minor(
                parsed_disc,
                allowed_ids=set(catalog),
                expected_edges=expected_reviewed,
                catalog=catalog,
                next_idx_start=len(interactions) + 1,
                skip_invalid=True,
            )
            break
        except ValueError as exc:
            logger.debug("Discovery pass attempt %d failed (%s)", attempt + 1, exc)
            if attempt == 1:
                raise
    _add_interaction_list(disc_ints)

    study = {
        "version": STUDY_VERSION,
        "cards": all_cards,
        "interactions": interactions,
        "minor_links": minor_links,
    }
    cov = validate_study(study, catalog)

    # Passes 3 & 4: Sequencing & matchup threat-class switches
    chapters, rt_ch, ncalls_ch = _run_chapters_passes(
        catalog=catalog,
        cmd_list=cmd_list,
        seed_text=profile.seed_text,
        all_cards=all_cards,
        interactions=interactions,
        format_name=format_name,
        backend=backend,
        cancel_check=cancel_check,
        extra_complete_kwargs=extra_complete_kwargs,
    )
    reasoning_total += rt_ch
    calls_total += ncalls_ch
    cov["chapter_coverage"] = validate_study_chapters(chapters, catalog)
    cov["nonland_cards"] = len(nonlands)

    model_id = str(getattr(backend, "model", "") or "")
    if study_client is not None:
        study_client.upload_study(
            dhash, {"study": study, "chapters": chapters}, version=STUDY_VERSION, model_id=model_id
        )

    return StudyBuildResult(
        deck_hash=dhash,
        study=study,
        chapters=chapters,
        coverage=cov,
        from_server=False,
        elapsed_s=time.monotonic() - started,
        reasoning_tokens=reasoning_total,
        calls_made=calls_total,
        model_id=model_id,
    )


def extra_kwargs_or(val: dict[str, Any] | None) -> dict[str, Any] | None:
    return val


def reuse_deck_study_for_edit(
    old_study: dict[str, Any],
    old_chapters: dict[str, Any],
    old_catalog: dict[int, dict[str, Any]],
    new_catalog: dict[int, dict[str, Any]],
    commanders: Iterable[int],
    backend: Any,
    *,
    format_name: str = "",
    deck_counts: dict[int, int] | None = None,
    card_facts_map: dict[str, dict[str, Any]] | None = None,
    study_client: DeckStudyClient | None = None,
    cancel_check: Callable[[], bool] | None = None,
    batch_size: int = 16,
    extra_complete_kwargs: dict[str, Any] | None = None,
) -> StudyBuildResult:
    """Reuse unchanged card entries and unaffected interactions after a small deck edit."""
    from arenamcp.deck_profile import build_profile

    started = time.monotonic()
    cmd_list = sorted({int(gid) for gid in commanders if type(gid) is int and gid > 0})
    cmd_set = set(cmd_list)
    counts = dict(deck_counts or {gid: 1 for gid in new_catalog})
    dhash = deck_study_hash(new_catalog, cmd_list, counts=counts)
    facts_map = _load_card_facts_map(new_catalog, card_facts_map)

    old_sources = {gid: study_source(card) for gid, card in old_catalog.items()}
    new_sources = {gid: study_source(card) for gid, card in new_catalog.items()}
    unchanged_gids = {
        gid for gid, digest in new_sources.items() if gid in old_sources and old_sources[gid] == digest
    }
    new_nonlands = sorted(gid for gid, card in new_catalog.items() if _land(card) is False)

    reused_card_map: dict[int, dict[str, Any]] = {}
    for row in old_study.get("cards") or ():
        if not isinstance(row, dict):
            continue
        gid = row.get("card")
        if type(gid) is not int or gid not in unchanged_gids or _land(new_catalog[gid]) is not False:
            continue
        pieces = {
            p.get("card")
            for k in ("play", "hold", "avoid")
            for p in ((row.get(k) or {}).get("conditions") or {}).get("pieces") or ()
        }
        ev_cards = {e.get("card") for e in row.get("evidence") or ()}
        if (pieces | ev_cards) <= unchanged_gids:
            try:
                validate_card_rule(row, new_catalog, [])
                reused_card_map[gid] = row
            except ValueError:
                pass

    dirty_cards = [gid for gid in new_nonlands if gid not in reused_card_map]
    reasoning_total = 0
    calls_total = 0

    profile = build_profile(
        [{**new_catalog[gid], "grp_id": gid, "count": counts.get(gid, 1)} for gid in sorted(new_catalog)],
        facts=facts_map,
        commanders=[new_catalog[g].get("name", "") for g in cmd_list if g in new_catalog],
    )
    if dirty_cards:
        for start in range(0, len(dirty_cards), max(1, int(batch_size))):
            batch = dirty_cards[start : start + max(1, int(batch_size))]
            built_cards, rt, ncalls = _study_cards_pass(
                batch,
                new_catalog,
                cmd_set,
                profile.seed_text,
                facts_map,
                backend,
                cancel_check,
                extra_complete_kwargs,
            )
            reasoning_total += rt
            calls_total += ncalls
            for c in built_cards:
                reused_card_map[c["card"]] = c

    all_cards = [reused_card_map[gid] for gid in new_nonlands]
    reused_cards_count = len(new_nonlands) - len(dirty_cards)

    expected_reviewed, all_edges = _all_deck_relationships(new_catalog, facts_map)
    reused_interactions: list[dict[str, Any]] = []
    reused_minor: list[dict[str, Any]] = []
    covered_reviewed: set[tuple[int, int, str]] = set()
    used_ids: set[str] = set()

    for entry in old_study.get("interactions") or ():
        if not isinstance(entry, dict):
            continue
        pieces = {p.get("card") for p in (entry.get("conditions") or {}).get("pieces") or ()}
        ev_cards = {e.get("card") for e in entry.get("evidence") or ()}
        if not (pieces | ev_cards) <= unchanged_gids:
            continue
        links = [
            ln
            for ln in entry.get("links") or ()
            if isinstance(ln, dict)
            and (ln.get("source"), ln.get("target"), ln.get("role")) in expected_reviewed
        ]
        updated = {**entry, "links": links}
        try:
            _evidence(updated["evidence"], pieces, new_catalog)
        except ValueError:
            continue
        reused_interactions.append(updated)
        used_ids.add(updated["id"])
        for ln in links:
            covered_reviewed.add((ln["source"], ln["target"], ln["role"]))

    for item in old_study.get("minor_links") or ():
        if not isinstance(item, dict):
            continue
        ln = item.get("link") or {}
        edge = (ln.get("source"), ln.get("target"), ln.get("role"))
        if (
            isinstance(ln, dict)
            and edge in expected_reviewed
            and edge not in covered_reviewed
            and {ln.get("source"), ln.get("target")} <= unchanged_gids
        ):
            reused_minor.append(item)
            covered_reviewed.add(edge)

    reused_int_count = len(reused_interactions)

    # Run cluster pass only for clusters touching changed/added cards or uncovered expected edges
    touched_edges = [
        e for e in all_edges if e not in covered_reviewed or e[0] in dirty_cards or e[1] in dirty_cards
    ]
    if touched_edges:
        cluster_system = (
            "DECK_STUDY_PASS: cluster\n"
            "Analyze only the newly touched relationship cluster(s) after a deck edit.\n"
            'Return JSON: {"interactions": [...], "minor_links": [...]}.'
        )
        for cluster_nodes, cluster_edges in _cluster_edges(touched_edges):
            cluster_reviewed = [
                e for e in cluster_edges if e in expected_reviewed and e not in covered_reviewed
            ]
            if not cluster_reviewed and not any(n in dirty_cards for n in cluster_nodes):
                continue
            cards_text = "\n\n".join(
                _format_card_context_line(
                    gid,
                    new_catalog[gid],
                    _fact_for_card(new_catalog[gid], facts_map),
                    is_commander=(gid in cmd_set),
                )
                for gid in cluster_nodes
            )
            edges_json = json.dumps(
                [
                    {"source": e[0], "target": e[1], "role": e[2], "reviewed": e in expected_reviewed}
                    for e in cluster_edges
                ]
            )
            c_user = f"CLUSTER CARDS: {json.dumps(cluster_nodes)}\nEDGES: {edges_json}\n\n{cards_text}"
            for attempt in range(2):
                msg = (
                    c_user
                    if attempt == 0
                    else "Repair uncovered cluster edges: "
                    + json.dumps(
                        [
                            {"source": e[0], "target": e[1], "role": e[2]}
                            for e in cluster_reviewed
                            if e not in covered_reviewed
                        ]
                    )
                    + "\n\n"
                    + c_user
                )
                try:
                    parsed, rt = _call_study_backend(
                        backend,
                        cluster_system,
                        msg,
                        cancel_check=cancel_check,
                        extra_kwargs=extra_complete_kwargs,
                    )
                except ValueError as exc:
                    logger.debug("Edit cluster pass attempt %d failed (%s)", attempt + 1, exc)
                    if attempt == 1:
                        raise
                    continue
                reasoning_total += rt
                calls_total += 1
                try:
                    cand_int, cand_min = _normalize_interactions_and_minor(
                        parsed,
                        allowed_ids=set(new_catalog),
                        expected_edges=expected_reviewed,
                        catalog=new_catalog,
                        next_idx_start=len(reused_interactions) + 1,
                        skip_invalid=True,
                    )
                except ValueError as exc:
                    logger.debug("Edit cluster normalization attempt %d failed (%s)", attempt + 1, exc)
                    if attempt == 1:
                        raise
                    continue
                existing_sigs = {
                    tuple(sorted(p["card"] for p in e["conditions"]["pieces"])) for e in reused_interactions
                }
                for entry in cand_int:
                    sig = tuple(sorted(p["card"] for p in entry["conditions"]["pieces"]))
                    while entry["id"] in used_ids:
                        entry["id"] = f"{entry['id']}_edit"
                    used_ids.add(entry["id"])
                    if sig in existing_sigs and not entry["links"]:
                        continue
                    existing_sigs.add(sig)
                    reused_interactions.append(entry)
                    for ln in entry["links"]:
                        covered_reviewed.add((ln["source"], ln["target"], ln["role"]))
                for ml in cand_min:
                    edge = (ml["link"]["source"], ml["link"]["target"], ml["link"]["role"])
                    if edge in expected_reviewed and edge not in covered_reviewed:
                        reused_minor.append(ml)
                        covered_reviewed.add(edge)
                if all(e in covered_reviewed for e in cluster_reviewed):
                    break

    study = {
        "version": STUDY_VERSION,
        "cards": all_cards,
        "interactions": reused_interactions,
        "minor_links": reused_minor,
    }
    cov = validate_study(study, new_catalog)

    # Reuse chapters unless referenced cards left the catalog or sequencing/switches fail validation
    chapters_valid = False
    chapters = old_chapters
    try:
        validate_study_chapters(old_chapters, new_catalog)
        chapters_valid = not dirty_cards
    except (ValueError, TypeError, KeyError):
        chapters_valid = False

    if not chapters_valid:
        chapters, rt_ch, ncalls_ch = _run_chapters_passes(
            catalog=new_catalog,
            cmd_list=cmd_list,
            seed_text=profile.seed_text,
            all_cards=all_cards,
            interactions=reused_interactions,
            format_name=format_name,
            backend=backend,
            cancel_check=cancel_check,
            extra_complete_kwargs=extra_complete_kwargs,
            changed_cards=dirty_cards,
        )
        reasoning_total += rt_ch
        calls_total += ncalls_ch

    cov["chapter_coverage"] = validate_study_chapters(chapters, new_catalog)
    cov["nonland_cards"] = len(new_nonlands)

    model_id = str(getattr(backend, "model", "") or "")
    if study_client is not None:
        study_client.upload_study(
            dhash, {"study": study, "chapters": chapters}, version=STUDY_VERSION, model_id=model_id
        )

    return StudyBuildResult(
        deck_hash=dhash,
        study=study,
        chapters=chapters,
        coverage=cov,
        from_server=False,
        elapsed_s=time.monotonic() - started,
        reasoning_tokens=reasoning_total,
        calls_made=calls_total,
        reused_cards=reused_cards_count,
        reused_interactions=reused_int_count,
        model_id=model_id,
    )
