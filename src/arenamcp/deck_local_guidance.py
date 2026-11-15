"""Private outside-match guidance for sequencing and hypothetical opposing threat classes."""

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import replace
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
    additional_activation,
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
the necessary pieces. All conditions are hypothetical. Do not invent permissions, extra loyalty uses or exact odds."""


def _guidance_input(book, state, scope):
    if scope not in TOPICS:
        raise ValueError("Unknown deck study topic")
    main = state.get("deck_cards")
    commanders = state.get("commander_grp_ids", [])
    if (
        not isinstance(main, list)
        or not isinstance(commanders, list)
        or any(type(gid) is not int or gid <= 4 for gid in (*main, *commanders))
        or set(main) | set(commanders) != set(book.catalog)
        or sorted(commanders) != sorted(book.commanders)
        or deck_identity(state) != book.identity
    ):
        raise ValueError("The complete deck and its current prints could not be verified")
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
    book = replace(book, catalog=catalog)
    counts = Counter(main)
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
        **(
            {"loyalty_timing": LOYALTY_TIMING}
            if walkers_in(book.catalog.values()) and not additional_activation(book.catalog.values())
            else {}
        ),
        "commanders": sorted(commanders),
        "prints": prints,
    }


def _validate(rows, book, scope):
    _object(rows, TOPICS[scope])
    walkers = walkers_in(book.catalog.values())
    multiple = additional_activation(book.catalog.values())
    for row in rows.values():
        _object(row, {"advice", "conditions", "evidence"})
        _text(row["advice"])
        ids = _conditions(row["conditions"], book.catalog)
        _evidence(row["evidence"], ids, book.catalog)
        if walkers and not multiple and conflicting_loyalty_sequence(row["advice"], walkers):
            raise ValueError("Deck guidance spends loyalty twice in one turn")
    return rows


def _detail(rows, book, scope, digest):
    lines = ["Local model advice · awaiting review", f"Local model weights • {digest[:12]}"]
    for key, title in TOPICS[scope].items():
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


def deck_guidance(book, state, scope, directory, *, model=None, allowed=None):
    book, context = _guidance_input(book, state, scope)
    job = json.dumps([1, _SYSTEM, scope, context], sort_keys=True, allow_nan=False)
    folder = Path(directory) / hashlib.sha256(job.encode()).hexdigest()
    if model is None:
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    else:
        if (
            not allowed()
            or not isinstance(model.digest, str)
            or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", model.digest)
        ):
            raise ValueError("The local model or deck is unavailable")
        files = [folder / (model.digest.removeprefix("sha256:") + ".json")]
    for path in files:
        try:
            if not re.fullmatch(r"[a-f0-9]{64}", path.stem):
                continue
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["input"] != folder.name or saved["model"] != path.stem:
                continue
            return _detail(_validate(saved["row"], book, scope), book, scope, path.stem), True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if model is None:
        return "No local study for these topics yet.", False

    advice, evidence = advice_fields(context["prints"])
    row_shape = schema_object({**advice["properties"], "evidence": evidence})
    shape = schema_object({name: row_shape for name in TOPICS[scope]})
    prompt = {**context, "prints": list(context["prints"].values())}
    start = time.monotonic()
    rows = model.complete(_SYSTEM, json.dumps(prompt, sort_keys=True), response_format=shape, allowed=allowed)
    _object(rows, TOPICS[scope])
    rows = _validate(
        {topic: {**row, "evidence": printed_evidence(row, context["prints"])} for topic, row in rows.items()},
        book,
        scope,
    )
    if not allowed() or not _checked(book, book.catalog):
        raise ValueError("The deck or current prints changed during the study")
    path = files[0]
    write_local_result(path, rows, model, time.monotonic() - start)
    return _detail(rows, book, scope, path.stem), False
