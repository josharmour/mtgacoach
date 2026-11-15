"""Read and study a single public card print in the context of a locally saved deck."""

import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path

from arenamcp.deck_strategy import oracle_rules
from arenamcp.deck_study import (
    _land,
    _reviewed_relationships,
    current_study_sources_match,
    study_source,
    validate_card_rule,
)

_SYSTEM = """Write a descriptive strategic hypothesis for the focal card in this deck.
All card properties and interactions must follow from the numbered printed rules supplied.
The deck plan is context, not evidence. Do not invent targets, costs or timing.
Return one JSON object with fields card (the focal numeric card ID), role (prose),
play, hold, avoid (each an object with advice and conditions), and evidence.
Each conditions object needs pieces (list of {card, zone} for every necessary card, including the focal card),
mana (object with generic plus applicable W,U,B,R,G,C numeric counts), first_turn (positive integer).
Zones: hand, battlefield, graveyard, exile, command, library, stack. Number only mana explicitly needed.
Evidence is a list of {card, rule} numeric references, one or more for every card used by your advice.
Use one short sentence each for role, play, hold and avoid. Prefer the smallest sufficient
set of pieces and rule references. Think briefly, then provide the complete JSON."""
_VERSION = 2


def _checked(book, gids):
    return current_study_sources_match({gid: book.catalog[gid] for gid in gids})


def card_menu(book):
    return sorted(
        ({"card": gid, "name": card["name"]} for gid, card in book.catalog.items() if _land(card) is False),
        key=lambda row: (row["name"].casefold(), row["card"]),
    )


def _context(book, gid):
    if type(gid) is not int or gid not in book.catalog or _land(book.catalog[gid]) is not False:
        raise ValueError("Select a resolved nonland card from this deck")
    mechanisms = [m for m in book.data.get("mechanisms", []) if gid in m["cards"]]
    ids = {gid, *book.commanders}
    for mechanism in mechanisms:
        ids.update(mechanism["cards"])
    edges, _ = _reviewed_relationships(book.catalog)
    links = sorted(edge for edge in edges if gid in edge[:2])
    for source, target, _role in links:
        ids.update((source, target))
    # The same study can be reused only when every input shown to the writer
    # is identical, including commander designation, local plan and partners.
    return {
        "card": gid,
        "reviewed_links": links,
        "commanders": list(book.commanders),
        "plan": book.data.get("primary_plan", ""),
        "backup": book.data.get("backup_plan", ""),
        "playbook_role": book.data.get("card_roles", {}).get(str(gid), ""),
        "mechanisms": [
            {
                "effect": m["effect"],
                "requires": m["requires"],
                "avoid": m["avoid"],
                "cards": m["cards"],
                "evidence": m["evidence"],
            }
            for m in mechanisms
        ],
        "prints": {
            str(i): {
                "name": book.catalog[i]["name"],
                "source": study_source(book.catalog[i]),
                "rules": [
                    {"rule": n, "quote": quote} for n, quote in enumerate(oracle_rules(book.catalog[i]), 1)
                ],
            }
            for i in sorted(ids)
        },
    }


def _role_format(context):
    """Constrain every model-supplied identity to this job's exact printed menu."""

    def obj(fields, required=None):
        return {
            "type": "object",
            "properties": fields,
            "required": list(fields) if required is None else required,
            "additionalProperties": False,
        }

    numeric = {"type": "integer", "minimum": 0}
    ids = [int(key) for key in context["prints"]]
    card = {"type": "integer", "enum": ids}
    text = {"type": "string", "minLength": 1}
    zones = ["hand", "battlefield", "graveyard", "exile", "command", "library", "stack"]
    pieces = {
        "type": "array",
        "minItems": 1,
        "maxItems": len(ids),
        "items": obj({"card": card, "zone": {"type": "string", "enum": zones}}),
    }
    mana = obj({key: numeric for key in ("generic", *"WUBRGC")}, ["generic"])
    conditions = obj({"pieces": pieces, "first_turn": {"type": "integer", "minimum": 1}, "mana": mana})
    advice = obj({"advice": text, "conditions": conditions})
    references = [
        obj(
            {
                "card": {"type": "integer", "enum": [int(gid)]},
                "rule": {"type": "integer", "enum": [r["rule"] for r in printed["rules"]]},
            }
        )
        for gid, printed in context["prints"].items()
    ]
    return obj(
        {
            "card": {"type": "integer", "enum": [context["card"]]},
            "role": text,
            "play": advice,
            "hold": advice,
            "avoid": advice,
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": references[0] if len(references) == 1 else {"anyOf": references},
            },
        }
    )


def _directory(directory, context):
    request = json.dumps([_VERSION, _SYSTEM, context], sort_keys=True, allow_nan=False)
    return Path(directory) / hashlib.sha256(request.encode()).hexdigest()


def _cached(path, book, context):
    if not re.fullmatch(r"[a-f0-9]{64}", path.stem):
        raise ValueError("Missing cached model weights identity")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["input"] != path.parent.name or raw["model"] != path.stem:
        raise ValueError("The local study does not match this input")
    return _validated_row(raw["row"], book, context), raw.get("metrics", {})


def prepare_card_pass(book, directory, digest):
    """Find remaining source-verifiable jobs for the selected weights without generating any advice."""
    if not isinstance(digest, str) or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", digest):
        raise ValueError("Missing local model identity")
    menu = card_menu(book)
    admitted = {row["card"] for row in (book.data.get("study") or {}).get("cards", [])}
    if admitted and not _checked(book, book.catalog):
        raise ValueError("The saved study no longer matches its native prints")
    jobs = []
    report = {
        "nonlands": len(menu),
        "pending": jobs,
        "withheld": 0,
        "cached": 0,
        "admitted": len(admitted),
        "seconds": 0.0,
        "generated_tokens": 0,
        "thinking_tokens": 0,
        "unreported_thinking": 0,
        "model_digest": digest,
    }
    for card in menu:
        gid = card["card"]
        if gid in admitted:
            continue
        try:
            context = _context(book, gid)
            if not _checked(book, (int(i) for i in context["prints"])):
                raise ValueError("Changed print")
        except (ValueError, KeyError, TypeError):
            report["withheld"] += 1
            continue
        path = _directory(directory, context) / (digest.removeprefix("sha256:") + ".json")
        try:
            _row, metrics = _cached(path, book, context)
        except (ValueError, KeyError, OSError, TypeError):
            jobs.append(gid)
            continue
        report["cached"] += 1
        for key in ("seconds", "generated_tokens", "thinking_tokens"):
            n = metrics.get(key) if isinstance(metrics, dict) else None
            valid = type(n) is int or (type(n) is float and key == "seconds" and math.isfinite(n))
            if valid and n >= 0:
                report[key] += n
            elif key == "thinking_tokens":
                report["unreported_thinking"] += 1
    return report


def read_card(book, gid, directory, *, model_digest=None):
    context = _context(book, gid)
    folder = _directory(directory, context)
    if model_digest is not None:
        if not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", model_digest):
            raise ValueError("Missing local model identity")
        path = folder / (model_digest.removeprefix("sha256:") + ".json")
        row, metrics = _cached(path, book, context)
        return card_detail(book, gid, row, metrics=metrics, model_digest=path.stem)
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        try:
            row, metrics = _cached(path, book, context)
            return card_detail(book, gid, row, metrics=metrics, model_digest=path.stem)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return card_detail(book, gid)


def _validated_row(row, book, context):
    from arenamcp.loyalty_timing import additional_activation, conflicting_loyalty_sequence, walkers_in

    cards = {int(gid): book.catalog[int(gid)] for gid in context["prints"]}
    prose = []
    if validate_card_rule(row, cards, prose) != context["card"]:
        raise ValueError("The study selected another card")
    walkers = walkers_in(cards.values())
    if (
        walkers
        and not additional_activation(cards.values())
        and any(conflicting_loyalty_sequence(s, walkers) for s in prose)
    ):
        raise ValueError("The study uses two loyalty abilities in one turn")
    return row


def study_card(book, gid, model, directory, *, allowed):
    context = _context(book, gid)
    ids = [int(i) for i in context["prints"]]
    if not allowed() or not _checked(book, ids):
        raise ValueError("The local card sources or deck are unavailable")
    if not isinstance(model.digest, str) or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", model.digest):
        raise ValueError("Missing local model identity")
    path = _directory(directory, context) / (model.digest.removeprefix("sha256:") + ".json")
    try:
        row, _metrics = _cached(path, book, context)
        return row, True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    started = time.monotonic()
    # JSON map keys are strings. Put numeric IDs directly beside each rule
    # menu so the model does not copy a string key back as a card reference.
    prompt = {
        **context,
        "prints": [{"card": int(gid), **printed} for gid, printed in context["prints"].items()],
    }
    row = model.complete(
        _SYSTEM, json.dumps(prompt, sort_keys=True), allowed=allowed, response_format=_role_format(context)
    )
    duration = round(time.monotonic() - started, 2)
    if not isinstance(row, dict) or not isinstance(row.get("evidence"), list):
        raise ValueError("The model supplied no usable printed evidence")
    refs = []
    for ref in row["evidence"]:
        if type(ref) is not dict or set(ref) != {"card", "rule"}:
            raise ValueError("The study needs numeric references to supplied printed rules")
        card, number = ref["card"], ref["rule"]
        if type(card) is not int or type(number) is not int:
            raise ValueError("The local model returned a nonnumeric card or rule reference")
        if str(card) not in context["prints"]:
            raise ValueError("The local model cited a print outside this card study")
        source = context["prints"][str(card)]
        if not 1 <= number <= len(source["rules"]):
            raise ValueError("Printed rule reference is out of range")
        refs.append({**ref, "source": source["source"], "quote": source["rules"][number - 1]["quote"]})
    row = _validated_row({**row, "evidence": refs}, book, context)
    if not allowed() or not _checked(book, ids):
        raise ValueError("The deck or card sources changed during the study")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            reported = getattr(model, "last_metrics", {})
            metrics = {"seconds": duration}
            for key in ("generated_tokens", "thinking_tokens"):
                n = reported.get(key) if isinstance(reported, dict) else None
                metrics[key] = n if type(n) is int and n >= 0 else None
            json.dump(
                {"row": row, "model": path.stem, "input": path.parent.name, "metrics": metrics},
                output,
                allow_nan=False,
            )
            output.flush()
            output.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return row, False


def card_detail(book, gid, row=None, *, metrics=None, model_digest=None):
    context = _context(book, gid)
    if not _checked(book, [int(i) for i in context["prints"]]):
        raise ValueError("The current Arena prints could not be matched to the saved strategy")
    study = book.data.get("study") or {}
    if study and not _checked(book, book.catalog):
        raise ValueError("The saved study could not be matched to all current Arena prints")
    admitted = next((r for r in study.get("cards", []) if r["card"] == gid), None)
    selected = admitted or row
    lines = [book.catalog[gid]["name"]]
    if selected:
        lines += [
            "",
            "Saved deck study" if admitted else "Local model advice · awaiting review",
            selected["role"],
        ]
        if not admitted and model_digest:
            lines.append(f"Local model weights • {model_digest[:12]}")
        for action in ("play", "hold", "avoid"):
            entry = selected[action]
            lines.extend([f"{action.title()} • {entry['advice']}", _conditions(entry["conditions"], book)])
        lines += ["", "Cited printed rules", *_refs(selected["evidence"], book)]
    if (
        selected
        and not admitted
        and isinstance(metrics, dict)
        and type(metrics.get("seconds")) in (int, float)
    ):
        counts = [f"Built in {metrics['seconds']:.1f}s"]
        if type(metrics.get("generated_tokens")) is int:
            counts.append(f"{metrics['generated_tokens']} generated tokens")
        counts.append(
            f"{metrics['thinking_tokens']} thinking tokens"
            if type(metrics.get("thinking_tokens")) is int
            else "Thinking tokens unreported"
        )
        lines.append(" · ".join(counts))
    if not admitted and context["playbook_role"]:
        lines += ["", "Saved playbook role", context["playbook_role"]]
    for m in context["mechanisms"]:
        lines += ["", "Saved playbook mechanism", m["effect"], *m["requires"], *m["avoid"]]
        for ref in m["evidence"]:
            source = context["prints"][str(ref["card"])]
            lines.append(f"{source['name']} · {ref['rule']}  {source['rules'][ref['rule'] - 1]['quote']}")

    for entry in study.get("interactions", []):
        if any(piece["card"] == gid for piece in entry["conditions"]["pieces"]):
            lines += [
                "",
                entry["kind"].title(),
                entry["outcome"],
                _conditions(entry["conditions"], book),
                f"Limits • {entry['stops']}",
                *_refs(entry["evidence"], book),
            ]
    if not selected:
        lines += ["", "No structured play, hold or avoid rules have been studied for this card."]
    lines += ["", "Printed rules", *(r["quote"] for r in context["prints"][str(gid)]["rules"])]
    return "\n".join(lines)


def _refs(refs, book):
    return [f"{book.catalog[r['card']]['name']} · {r['rule']}  {r['quote']}" for r in refs]


def _conditions(conditions, book):
    pieces = ", ".join(f"{book.catalog[p['card']]['name']} in {p['zone']}" for p in conditions["pieces"])
    mana = conditions["mana"]
    cost = (f"{{{mana['generic']}}}" if mana["generic"] else "") + "".join(
        f"{{{key}}} × {mana[key]} " for key in "WUBRGC" if mana.get(key)
    )
    return f"Turn {conditions['first_turn']} or later · {pieces} · mana {cost.strip() or '0'}"
