"""Private outside-match guidance for sequencing and hypothetical opposing threat classes."""

import hashlib
import json
import re
import secrets
import string
import time
from collections import Counter
from dataclasses import dataclass, field, replace
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
    _conditions as render_conditions,
)
from arenamcp.deck_strategy import deck_identity, oracle_rules
from arenamcp.deck_study import _conditions, _evidence, _object, _text, study_source
from arenamcp.loyalty_timing import (
    LOYALTY_TIMING,
    conflicting_loyalty_sequence,
    walkers_in,
)

TOPICS = {
    "sequencing": {
        "sequencing": "Sequencing",
        "mulligan": "Opening hand and mulligan",
        "mana": "Land and colour plan",
        "recovery": "Recovering after disruption",
    },
    "interactions": {"clusters": "Reviewed links", "discovery": "Other interactions"},
    "switches": {
        "fast_combo": "Against fast combo",
        "wide_board": "Against a wide board",
        "counterspells": "Against counterspells",
        "graveyard": "Against a graveyard strategy",
    },
}
_SYSTEM = """Study the supplied deck outside a game. Provide one concise, deck-specific choice for EACH named topic.
Explain what to do, what changes the choice and which supplied prints support it. Treat opponent classes as
hypotheticals. Never assert a real opponent's cards, a free mulligan or an unobserved resource. Land, colour and
opening-hand advice must respect main-deck copy counts. The commander is designated separately. Printed rules
and characteristics are the only evidence for what a card can do.
For each topic return advice, conditions (pieces with numeric card IDs and zones, mana with generic and optional
WUBRGC quantities, positive first_turn), and evidence (numeric card and numbered printed rule). Cite all and only
the necessary pieces. Echo read_receipt as the beginning receipt, each print's receipt in the provided order,
then the ending receipt. Return topic entries in topics. All conditions are hypothetical. Do not invent permissions, extra loyalty uses or exact odds."""


@dataclass
class PrintedStudyDeck:
    """A local study baseline. Never saved or installed as a Coach playbook."""

    identity: str
    catalog: dict
    commanders: list[int]
    native_profile: dict
    data: dict = field(default_factory=dict)


def study_piles(state, identity):
    main = state.get("deck_cards")
    commanders = state.get("commander_grp_ids", [])
    if (
        not isinstance(main, list)
        or not isinstance(commanders, list)
        or any(type(gid) is not int or gid <= 4 for gid in (*main, *commanders))
        or not main
        or len(commanders) != len(set(commanders))
        or deck_identity(state) != identity
    ):
        raise ValueError("The complete deck and its current prints could not be verified")
    return main, commanders


def _printed_list(state, identity):
    main, commanders = study_piles(state, identity)
    # Resolve the submitted list from today's installed Arena data. A legacy
    # playbook may contain old printed text. Its prose and old catalogue are
    # never evidence for these new passes.
    from arenamcp.card_db import MTGADatabaseAdapter, initialized_card_database

    db = initialized_card_database()
    raw = db.get_raw_mtgadb() if db is not None else None
    if raw is None or not raw.available:
        raise ValueError("The current Arena print database is unavailable")
    adapter = MTGADatabaseAdapter(raw)
    catalog = {}
    for gid in set(main) | set(commanders):
        card = adapter.get_card_by_arena_id(gid)
        if card is None or type(card.arena_id) is not int or card.arena_id != gid:
            raise ValueError("The current Arena print could not be verified")
        catalog[gid] = dict(vars(card))
    return catalog, Counter(main), sorted(commanders)


def printed_study_deck(state, identity):
    from arenamcp.deck_profile import build_profile

    catalog, counts, commanders = _printed_list(state, identity)
    # Native DeckRecord carries the submitted main pile separately from its
    # commanders. An overlapping row leaves their allocation ambiguous.
    if set(counts).intersection(commanders):
        raise ValueError("The submitted list and its designated commander overlap")
    profile = build_profile([{**catalog[gid], "count": n} for gid, n in sorted(counts.items())])
    return PrintedStudyDeck(
        identity,
        catalog,
        commanders,
        {
            "main_copies": profile.card_count,
            "main_lands": profile.land_count,
            "mana_curve": list(profile.curve),
            "estimated_role_counts": list(profile.roles),
        },
    )


def _guidance_input(book, state, scope):
    if scope not in TOPICS:
        raise ValueError("Unknown deck study topic")
    catalog, counts, commanders = _printed_list(state, book.identity)
    if set(catalog) != set(book.catalog) or commanders != sorted(book.commanders):
        raise ValueError("The complete deck and its current prints could not be verified")
    book = replace(book, catalog=catalog)
    prints = {
        str(gid): {
            "card": gid,
            "name": card["name"],
            "main_copies": counts[gid],
            "source": study_source(card),
            "rules": [{"rule": n, "quote": line} for n, line in enumerate(oracle_rules(card), 1)],
        }
        for gid, card in sorted(book.catalog.items())
    }
    return book, {
        "topics": list(TOPICS[scope]),
        **({"loyalty_timing": LOYALTY_TIMING} if walkers_in(book.catalog.values()) else {}),
        "commanders": sorted(commanders),
        "prints": prints,
    }


def _validate(rows, book, expected):
    _object(rows, expected)
    walkers = walkers_in(book.catalog.values())
    for row in rows.values():
        _object(row, {"advice", "conditions", "evidence"})
        _text(row["advice"])
        ids = _conditions(row["conditions"], book.catalog)
        _evidence(row["evidence"], ids, book.catalog)
        # The submitted list does not prove an extra-activation enabler is in
        # play. Describe alternatives or distinct turns in these offline notes.
        if walkers and conflicting_loyalty_sequence(row["advice"], walkers):
            raise ValueError("Deck guidance spends loyalty twice in one turn")
    return rows


def _detail(rows, book, scope, digest, focus=None):
    from_title = (
        "All four topics" if set(rows) == set(TOPICS[scope]) else f"Partial study • {len(rows)} of 4 topics"
    )
    lines = [from_title, "Local model advice · awaiting review", f"Local model weights • {digest[:12]}"]
    ordered = list(TOPICS[scope]) if focus is None else [focus, *(k for k in TOPICS[scope] if k != focus)]
    for key in ordered:
        title = TOPICS[scope][key]
        if key not in rows:
            lines.extend(["", f"{title} • not yet studied"])
            continue
        row = rows[key]
        lines.extend(
            [
                "",
                title,
                row["advice"],
                render_conditions(row["conditions"], book),
                *_refs(row["evidence"], book),
            ]
        )
    return "\n".join(lines)


_TOPIC_SYSTEM = (
    _SYSTEM
    + "\nStudy only the single named topic in this request. Answer in one short actionable paragraph with the minimum sufficient pieces and printed references."
)


def _topic_folder(directory, schema_version, system, scope, context):
    job = json.dumps([schema_version, system, scope, context], sort_keys=True, allow_nan=False)
    return Path(directory) / hashlib.sha256(job.encode()).hexdigest()


def _read_rows(folder, digest, book, expected, *, diagnostic=False):
    path = folder / (digest + ".json")
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        metrics = saved.get("metrics", {})
        if not isinstance(metrics, dict) or (metrics.get("thinking_mode") == "disabled") != diagnostic:
            return {}, None
        if saved["input"] == folder.name and saved["model"] == digest:
            return _validate(saved["row"], book, expected), saved.get("metrics", {})
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}, None


def topic_files(directory, scope, context):
    topics = TOPICS[scope]
    legacy = _topic_folder(directory, 1, _SYSTEM, scope, context)
    jobs = {key: {**context, "topics": [key]} for key in topics}
    folders = {key: _topic_folder(directory, 2, _TOPIC_SYSTEM, scope, job) for key, job in jobs.items()}
    return legacy, jobs, folders


def topic_coverage(book, context, scope, directory, digest):
    legacy, _, folders = topic_files(directory, scope, context)
    rows, metrics = _read_rows(legacy, digest, book, TOPICS[scope])
    if rows:
        return list(rows), [metrics]
    hits = []
    usage = []
    for key, folder in folders.items():
        rows, metrics = _read_rows(folder, digest, book, [key])
        if rows:
            hits.append(key)
            usage.append(metrics)
    return hits, usage


def deck_guidance(book, state, scope, directory, *, model=None, allowed=None, topic=None):
    book, context = _guidance_input(book, state, scope)
    diagnostic = getattr(model, "diagnostic_without_thinking", False) is True
    if diagnostic:
        if scope == "interactions" or topic not in TOPICS[scope]:
            raise ValueError("A diagnostic studies a single selected strategy topic")
        directory = Path(directory) / "diagnostic_without_thinking"
        context = {**context, "diagnostic": "single topic without thinking"}
    if scope == "interactions":
        from arenamcp.deck_local_interactions import study_interactions

        return study_interactions(book, context, directory, model=model, allowed=allowed)
    topics = TOPICS[scope]
    if topic is not None and topic not in topics:
        raise ValueError("Unknown topic in this deck study")
    legacy, jobs, folders = topic_files(directory, scope, context)
    if model is not None:
        if (
            not allowed()
            or not isinstance(model.digest, str)
            or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", model.digest)
        ):
            raise ValueError("The deck or selected model is unavailable")
        digests = [model.digest.removeprefix("sha256:")]
    else:
        available = {}
        for folder in (legacy, *folders.values()):
            for p in folder.glob("*.json"):
                if re.fullmatch("[a-f0-9]{64}", p.stem):
                    available[p.stem] = max(available.get(p.stem, 0), p.stat().st_mtime_ns)
        digests = sorted(available, key=available.get, reverse=True)
    best_rows, digest_for_best = {}, None
    for digest in digests:
        rows, _ = ({}, None) if diagnostic else _read_rows(legacy, digest, book, topics)
        if rows:
            return _detail(rows, book, scope, digest, topic), True
        reused = True
        for key in topics:
            if diagnostic and key != topic:
                continue
            saved, _ = _read_rows(folders[key], digest, book, [key], diagnostic=diagnostic)
            rows.update(saved)
            if key in rows or model is None or (topic is not None and topic != key):
                continue
            if not allowed() or not _checked(book, book.catalog):
                raise ValueError("The deck or current prints changed before studying this topic")
            start = time.monotonic()
            job = jobs[key]
            advice, evidence = advice_fields(job["prints"])
            row_shape = schema_object({**advice["properties"], "evidence": evidence})
            generated = acknowledged_answer(
                model, _TOPIC_SYSTEM, job, "topics", schema_object({key: row_shape}), allowed, max_tokens=2048
            )
            _object(generated, {key})
            entry = generated[key]
            single = _validate(
                {key: {**entry, "evidence": printed_evidence(entry, job["prints"])}}, book, [key]
            )
            if not allowed() or not _checked(book, book.catalog):
                raise ValueError("The deck or current prints changed while studying this topic")
            if diagnostic and model.last_metrics.get("thinking_mode") != "disabled":
                raise ValueError("The diagnostic did not confirm its requested thinking mode")
            write_local_result(folders[key] / (digest + ".json"), single, model, time.monotonic() - start)
            reused = False
            rows.update(single)
        if rows:
            if diagnostic:
                return "Diagnostic · thinking disabled\n" + _detail(rows, book, scope, digest, topic), reused
            if len(rows) == len(topics):
                _validate(rows, book, topics)
                return _detail(rows, book, scope, digest, topic), reused
            if model is not None:
                return _detail(rows, book, scope, digest, topic), reused
            if (topic in rows, len(rows)) > (topic in best_rows, len(best_rows)):
                best_rows, digest_for_best = rows, digest
    if best_rows:
        return _detail(best_rows, book, scope, digest_for_best, topic), True
    return "No local study for these topics yet.", False


def acknowledged_answer(model, system, context, key, result_schema, allowed, *, max_tokens=4096):
    # Receipts are absent from the response grammar. A silently truncated
    # prefix/suffix cannot satisfy this request by copying schema constants.
    prints = list(context["prints"].values())
    # Numeric receipts cost nine output tokens apiece on the installed Qwen
    # tokenizer. Five random letters provide 52**5 possible values, with
    # enough output budget left for the strategic answer.
    unique = {}
    while len(unique) < len(prints) + 2:
        receipt = "".join(secrets.choice(string.ascii_letters) for _ in range(5))
        unique[receipt] = None
    receipts = list(unique)
    expected = " ".join(receipts)
    shape = schema_object(
        {
            "read_receipt": {"type": "string", "minLength": len(expected), "maxLength": len(expected)},
            key: result_schema,
        }
    )
    prompt = {
        "receipt_format": "Copy the receipt words in order into one string separated by single spaces.",
        "beginning_receipt": receipts[0],
        **context,
        "prints": [{**row, "receipt": receipt} for row, receipt in zip(prints, receipts[1:-1], strict=True)],
        "ending_receipt": receipts[-1],
    }
    answer = model.complete(
        system,
        json.dumps(prompt, separators=(",", ":")),
        response_format=shape,
        allowed=allowed,
        verify_full_input=True,
        max_tokens=max_tokens,
    )
    _object(answer, {key, "read_receipt"})
    echoed = answer["read_receipt"]
    if not isinstance(echoed, str) or echoed != expected:
        raise ValueError("The model did not acknowledge the complete deck input")
    return answer[key]
