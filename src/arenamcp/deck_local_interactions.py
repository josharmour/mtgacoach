"""Review each related group, then inspect the complete deck for missed interactions."""

import hashlib
import json
import math
import re
import time
from pathlib import Path

from arenamcp.deck_card_study import (
    _checked,
    _refs,
    advice_fields,
    printed_evidence,
    schema_object,
    write_local_result,
)
from arenamcp.deck_card_study import (
    _conditions as describe_conditions,
)
from arenamcp.deck_local_guidance import acknowledged_answer
from arenamcp.deck_study import _object, _reviewed_relationships, validate_relationship_coverage
from arenamcp.loyalty_timing import conflicting_loyalty_sequence, walkers_in

_SYSTEM = """Study synergies and combos in this player's deck using only supplied numbered printed rules.
The cluster pass must account for every supplied reviewed edge by its index in an interaction or explicitly
mark it minor with a reason. The discovery pass looks for other in-deck interactions from the printed effects.
Do not repeat an existing combination of pieces and evidence. In each interaction state what happens, what
stops it, the pieces and zones, required mana and earliest turn. All resources are hypothetical. Avoid calling
a repeatable value interaction an infinite or game-winning combo; if no new interaction is justified leave
the list empty. Include only the necessary printed rule numbers for every piece. Do not assume
multiple loyalty uses in the same turn. Return result and read_receipt. The receipt must echo the beginning
receipt, each print's receipt in order, then the ending receipt."""


def _clusters(edges):
    remaining = set(edges)
    groups = []
    while remaining:
        nodes = {min(remaining)[0]}
        selected = set()
        while touched := {e for e in remaining if e[0] in nodes or e[1] in nodes}:
            remaining -= touched
            selected |= touched
            nodes.update(gid for e in touched for gid in e[:2])
        groups.append((sorted(nodes), sorted(selected)))
    return groups


def _edge(edge):
    return dict(zip(("source", "target", "role"), edge, strict=True))


def _job(context, mode, ids, edges, known):
    return {
        "mode": mode,
        "reviewed_edges": [{"index": i, **_edge(e)} for i, e in enumerate(edges)],
        "previously_studied_pieces_and_rules": [list(key) for key in sorted(known)],
        "commanders": [g for g in context["commanders"] if g in ids],
        "prints": {str(g): context["prints"][str(g)] for g in sorted(ids)},
    }


def _folder(directory, job):
    request = json.dumps([1, _SYSTEM, job], sort_keys=True, allow_nan=False)
    return Path(directory) / "interactions" / hashlib.sha256(request.encode()).hexdigest()


def _signature(entry):
    return tuple(sorted((r["card"], r["rule"]) for r in entry["evidence"]))


def _shape(prints, edges):
    advice, evidence = advice_fields(prints)
    text = {"type": "string", "minLength": 1}
    edge = {"type": "integer", "minimum": 0}
    if edges:
        edge["maximum"] = len(edges) - 1
    links = {"type": "array", "maxItems": len(edges), "items": edge}
    interaction = schema_object(
        {
            "kind": {"type": "string", "enum": ["synergy", "combo"]},
            "conditions": advice["properties"]["conditions"],
            "outcome": text,
            "stops": text,
            "evidence": evidence,
            "links": links,
        }
    )
    return schema_object(
        {
            "interactions": {"type": "array", "items": interaction},
            "minor_links": {
                "type": "array",
                "maxItems": len(edges),
                "items": schema_object({"link": edge, "reason": text}),
            },
        }
    )


def _normalize(raw, job, edges):
    _object(raw, {"interactions", "minor_links"})

    def edge(index):
        if type(index) is not int or not 0 <= index < len(edges):
            raise ValueError("The study cites an unknown reviewed link")
        return _edge(edges[index])

    if type(raw["interactions"]) is not list or type(raw["minor_links"]) is not list:
        raise ValueError("Invalid interaction study lists")
    entries = []
    for item in raw["interactions"]:
        _object(item, {"kind", "conditions", "outcome", "stops", "evidence", "links"})
        if type(item["links"]) is not list:
            raise ValueError("Invalid relationship indices")
        evidence = printed_evidence(item, job["prints"])
        signature = json.dumps(sorted((r["card"], r["rule"]) for r in evidence))
        entries.append(
            {
                **item,
                "id": hashlib.sha256(signature.encode()).hexdigest(),
                "evidence": evidence,
                "links": [edge(i) for i in item["links"]],
            }
        )
    minor = []
    for item in raw["minor_links"]:
        _object(item, {"link", "reason"})
        minor.append({"link": edge(item["link"]), "reason": item["reason"]})
    return {"interactions": entries, "minor_links": minor}


def _validate(part, book, job, edges, known):
    _object(part, {"interactions", "minor_links"})
    catalog = {int(g): book.catalog[int(g)] for g in job["prints"]}
    prose = []
    validate_relationship_coverage(part["interactions"], part["minor_links"], catalog, set(edges), prose)
    used = set(known)
    for entry in part["interactions"]:
        key = _signature(entry)
        if key in used or (edges and not entry["links"]):
            raise ValueError("A reviewed cluster or discovery pass repeated or omitted its evidence")
        used.add(key)
    walkers = walkers_in(catalog.values())
    if any(conflicting_loyalty_sequence(line, walkers) for line in prose):
        raise ValueError("Interaction study spends loyalty twice in one turn")
    return part


def _part(book, job, edges, known, directory, digest, model, allowed):
    file = _folder(directory, job) / (digest + ".json")
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
        if raw["input"] == file.parent.name and raw["model"] == digest:
            return _validate(raw["row"], book, job, edges, known), raw.get("metrics", {}), True
    except (KeyError, OSError, TypeError, ValueError):
        pass
    if model is None:
        return None
    ids = [int(g) for g in job["prints"]]
    if not allowed() or not _checked(book, ids):
        raise ValueError("Deck, match or current interaction prints changed")
    start = time.monotonic()
    answer = acknowledged_answer(model, _SYSTEM, job, "result", _shape(job["prints"], edges), allowed)
    result = _validate(_normalize(answer, job, edges), book, job, edges, known)
    if not allowed() or not _checked(book, ids):
        raise ValueError("Deck, match or current interaction prints changed")
    duration = time.monotonic() - start
    write_local_result(file, result, model, duration)
    metrics = getattr(model, "last_metrics", {})
    return result, {**(metrics if isinstance(metrics, dict) else {}), "seconds": duration}, False


def _describe(parts, book, digest, edge_count):
    entries = [item for part, _ in parts for item in part["interactions"]]
    minor = [item for part, _ in parts for item in part["minor_links"]]
    lines = [
        "Local model advice · awaiting review",
        f"Local model weights • {digest[:12]}",
        f"{sum(x['kind'] == 'synergy' for x in entries)} synergies and {sum(x['kind'] == 'combo' for x in entries)} combos proposed. "
        f"{edge_count - len(minor)} of {edge_count} reviewed links in interactions, {len(minor)} marked minor.",
    ]
    for entry in entries:
        lines.extend(
            [
                "",
                entry["kind"].title(),
                entry["outcome"],
                describe_conditions(entry["conditions"], book),
                f"What stops it • {entry['stops']}",
                *_refs(entry["evidence"], book),
            ]
        )
    for row in minor:
        e = row["link"]
        lines.append(
            f"Minor • {book.catalog[e['source']]['name']} and {book.catalog[e['target']]['name']} • {row['reason']}"
        )
    additional = len(parts[-1][0]["interactions"])
    lines += ["", f"The whole-deck search proposed {additional} additional interactions."]
    for field, label in (
        ("seconds", "seconds"),
        ("generated_tokens", "generated tokens"),
        ("thinking_tokens", "thinking tokens"),
    ):
        known = []
        for _, metrics in parts:
            value = metrics.get(field) if isinstance(metrics, dict) else None
            if (
                type(value) is int or (field == "seconds" and type(value) is float and math.isfinite(value))
            ) and value >= 0:
                known.append(value)
        total = round(sum(known), 1)
        lines.append(f"{total} reported {label} across {len(known)} of {len(parts)} completed passes.")
    return "\n".join(lines)


def retained_interactions(book, context, directory, digest):
    edges, printed = _reviewed_relationships(book.catalog)
    groups = _clusters(edges)
    parts, usage, known = [], [], set()
    complete_groups = 0
    for nodes, links in groups:
        job = _job(context, "cluster", nodes, links, set())
        found = _part(book, job, links, set(), directory, digest, None, None)
        if found:
            part, metrics, _ = found
            parts.append(part)
            usage.append({**metrics, "input_sha256": _folder(directory, job).name})
            complete_groups += 1
            known.update(_signature(entry) for entry in part["interactions"])
    discovery = False
    if complete_groups == len(groups):
        job = _job(context, "discovery", sorted(book.catalog), [], known)
        found = _part(book, job, [], known, directory, digest, None, None)
        if found:
            part, metrics, _ = found
            parts.append(part)
            usage.append({**metrics, "input_sha256": _folder(directory, job).name})
            discovery = True
    entries = [entry for part in parts for entry in part["interactions"]]
    covered = {(edge["source"], edge["target"], edge["role"]) for entry in entries for edge in entry["links"]}
    return (
        {
            "reviewed_prints": printed,
            "links": len(edges),
            "covered_links": len(covered),
            "minor_links": sum(len(p["minor_links"]) for p in parts),
            "groups": len(groups),
            "complete_groups": complete_groups,
            "discovery": discovery,
            "synergies": sum(e["kind"] == "synergy" for e in entries),
            "combos": sum(e["kind"] == "combo" for e in entries),
        },
        usage,
        parts,
    )


def interaction_coverage(book, context, directory, digest):
    counts, usage, _ = retained_interactions(book, context, directory, digest)
    return counts, usage


def study_interactions(book, context, directory, *, model=None, allowed=None):
    edges, _ = _reviewed_relationships(book.catalog)
    groups = _clusters(edges)
    all_ids = sorted(book.catalog)
    initial = (
        _job(context, "cluster", *groups[0], set())
        if groups
        else _job(context, "discovery", all_ids, [], set())
    )
    if model is None:
        files = sorted(
            _folder(directory, initial).glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True
        )
        digests = [p.stem for p in files if re.fullmatch("[a-f0-9]{64}", p.stem)]
    else:
        if (
            not allowed()
            or not isinstance(model.digest, str)
            or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", model.digest)
        ):
            raise ValueError("The deck or selected model is unavailable")
        digests = [model.digest.removeprefix("sha256:")]
    for digest in digests:
        parts, known = [], set()
        reused = True
        for mode, ids, related in [
            *(("cluster", nodes, group) for nodes, group in groups),
            ("discovery", all_ids, []),
        ]:
            previous = known if mode == "discovery" else set()
            job = _job(context, mode, ids, related, previous)
            found = _part(book, job, related, previous, directory, digest, model, allowed)
            if found is None:
                break
            part, metrics, cached = found
            parts.append((part, metrics))
            known.update(_signature(item) for item in part["interactions"])
            reused = reused and cached
        else:
            if (model is not None and not allowed()) or not _checked(book, all_ids):
                raise ValueError("Current interaction prints changed")
            return _describe(parts, book, digest, len(edges)), reused
    return "No complete local interaction study for this deck yet.", False
