"""Compile card-facts records from printed card text using the model.

One model call per card: the prompt carries the full printed rules text of
every face and asks only for what that text states; anything the text does not
state must come back as ``unknown``/empty rather than a guess. The answer is
validated against the printed card with :func:`arenamcp.card_facts.validate`,
retried once with the problems listed, and written one JSON file per card,
named by ``rules_hash``.

Printed-text source (one choice, stated here so server and client hash the
same bytes): **Scryfall**. Every record's ``rules_hash`` is computed over
Scryfall's ``name``, ``mana_cost``, ``type_line`` and ``oracle_text`` exactly
as Scryfall prints them — ``{T}``, not Arena's ``{oT}``. Arena card IDs are
resolved to card names through the local MTGA database when one is installed,
falling back to Scryfall's arena index; the printed text itself always comes
from Scryfall either way. For multi-face cards the rules text is what
:mod:`arenamcp.scryfall` produces: the faces' oracle text joined with
``\\n---\\n``.

Model access mirrors ``game_plan._complete``: background lane, low scheduling
priority, ``call_class`` ``background.card_facts``, high reasoning effort.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.schema import CardFacts, card_facts_from_dict, to_json
from arenamcp.card_facts.validate import PrintedCard, validate
from arenamcp.game_plan import BACKGROUND_PRIORITY, accepted_call_kwargs

logger = logging.getLogger(__name__)

# Metrics/policy label for these calls (registered in proxy.CALL_CLASSES).
CALL_CLASS = "background.card_facts"

# The facts JSON is small; the budget bounds a long think, not the answer
# (same reasoning as game_plan's compact-plan cap).
_MAX_TOKENS = 2048
_TIMEOUT_S = 90.0
_EFFORT = "high"
_TEMPERATURE = 0.0

# How the record admits it was compiled (consumers filter on this).
COMPILED_BY = "card_facts.compiler"

_STATUS_COMPILED = "compiled"
_STATUS_FAILED = "failed"
_STATUS_SKIPPED = "skipped"
_STATUS_UNRESOLVED = "unresolved"


# ── Resolving the printed card ───────────────────────────────────────────────


def _loyalty_of(raw: dict[str, Any]) -> str | None:
    """Starting loyalty from a raw Scryfall card dict (front face for DFCs)."""
    loyalty = raw.get("loyalty")
    if loyalty in (None, ""):
        faces = raw.get("card_faces") or []
        if faces and isinstance(faces[0], dict):
            loyalty = faces[0].get("loyalty")
    return str(loyalty) if loyalty not in (None, "") else None


def _raw_card_dict(scryfall: Any, name: str) -> dict[str, Any]:
    """The raw Scryfall dict for ``name``, when the cache exposes one.

    ``ScryfallCard`` carries no loyalty field, so the starting loyalty is read
    from the cache's raw index. Read-only and best-effort: a cache without the
    index simply yields no loyalty, and the validator then skips that check.
    """
    index = getattr(scryfall, "_name_index", None)
    if isinstance(index, dict):
        raw = index.get(name.strip().casefold())
        if isinstance(raw, dict):
            return raw
    return {}


def _printed_from_scryfall_card(card: Any, scryfall: Any) -> PrintedCard:
    """PrintedCard from a ScryfallCard (or anything with the same fields)."""
    raw = _raw_card_dict(scryfall, card.name)
    return PrintedCard(
        name=card.name,
        mana_cost=card.mana_cost,
        type_line=card.type_line,
        oracle_text=card.oracle_text,
        power=card.power or None,
        toughness=card.toughness or None,
        loyalty=_loyalty_of(raw),
    )


class CardResolver:
    """Resolves card names or Arena IDs to printed cards, via Scryfall.

    Arena IDs go through the local MTGA database when one is available
    (``mtgadb.get_card``); otherwise Scryfall's arena index resolves them.
    Either way the printed text comes from Scryfall only.
    """

    def __init__(self, scryfall: Any, mtgadb: Any = None):
        self._scryfall = scryfall
        self._mtgadb = mtgadb

    def by_name(self, name: str) -> PrintedCard | None:
        card = self._scryfall.get_card_by_name(name)
        if card is None:
            return None
        return _printed_from_scryfall_card(card, self._scryfall)

    def by_arena_id(self, arena_id: int) -> PrintedCard | None:
        name: str | None = None
        db = self._mtgadb
        if db is not None:
            available = True
            try:
                # ``available`` is a property on the real MTGADatabase and a
                # method on test doubles; accept both.
                attr = db.available
                available = bool(attr() if callable(attr) else attr)
            except Exception:
                available = True
            if available:
                try:
                    mtga_card = db.get_card(int(arena_id))
                except Exception as error:
                    logger.debug("mtgadb lookup failed for %s: %s", arena_id, error)
                    mtga_card = None
                if mtga_card is not None and getattr(mtga_card, "name", ""):
                    name = mtga_card.name
        if name is None:
            card = self._scryfall.get_card_by_arena_id(int(arena_id))
            if card is None:
                return None
            name = card.name
        if not name:
            return None
        return self.by_name(name)

    def resolve(self, identifier: int | str) -> PrintedCard | None:
        if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
            return self.by_arena_id(int(identifier))
        return self.by_name(str(identifier))


def split_name_list(raw: str, resolves: Callable[[str], bool]) -> list[str]:
    """Split a comma-separated name list without breaking names containing commas.

    Card names legitimately contain commas ("Minsc & Boo, Timeless Heroes"),
    so a blind ``split(",")`` corrupts them (Dotty rejection point 2 at
    5beb064). Greedy rejoin instead: accumulate fragments and emit the
    accumulated name as soon as it resolves; a fragment that does not resolve
    on its own is glued onto the previous one. The final accumulation is
    always emitted, so an unresolvable leftover surfaces as ``unresolved``
    in the run summary rather than disappearing. Known limitation: when both
    a fragment and a longer name containing it resolve, the shorter wins.
    """
    names: list[str] = []
    current = ""
    parts = [piece.strip() for piece in raw.split(",")]
    for part in parts:
        if not part:
            continue
        if not current:
            current = part
        elif resolves(current):
            names.append(current)
            current = part
        elif resolves(part):
            # ``current`` is not a card name but this fragment is: emit them
            # separately instead of gluing, so an unresolvable leftover stays
            # visible in the run summary instead of swallowing a real name.
            names.append(current)
            current = part
        else:
            current = f"{current}, {part}"
    if current:
        names.append(current)
    return names


# ── Prompt ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You compile structured facts for one Magic: The Gathering card, for a rules \
engine that executes what the record states.

Answer with ONE JSON object and nothing else. It must match this shape:

{
  "name": "<card name>",
  "type_line": "<type line exactly as printed>",
  "mana_cost": "<mana cost exactly as printed, empty for lands>",
  "body": {
    "power": "<printed power or null>",
    "toughness": "<printed toughness or null>",
    "loyalty": "<printed starting loyalty or null>",
    "keywords": ["<every keyword ability stated by the type line or rules text>"]
  },
  "modes": [
    {
      "name": "<cast|activate|evoke|flashback|...>",
      "cost": {"mana": "", "tap": false, "sacrifice": null,
               "life": 0, "discard": 0, "exile_from_graveyard": 0},
      "cost_text": "<the cost as printed>",
      "stays_on_battlefield": true
    }
  ],
  "effects": [
    {
      "trigger": "<when it happens: ETB, dies, attacks, activated, ...>",
      "effect": {"effect_type": "<see below>", "...": "<fields for that type>"},
      "conditions": ["<only if the text states a condition>"]
    }
  ],
  "mana": [{"mana": "{W}", "condition": "<only if stated>"}],
  "threat": {},
  "relationships": {},
  "limited": {},
  "confidence": "<high|medium|low>",
  "notes": ""
}

Known effect types and their fields (effect_type tag first):
- CreateToken: spec {name, count, power, toughness, types, subtypes,
  keywords, tapped, attacking, copy_of_self, not_legendary, per_opponent}
- GainLife: amount:int
- Draw: count:int
- Damage: amount:int, target:str
- Destroy: target:str
- Exile: target:str
- Counter: target:str
- Pump: power:int, toughness:int, target:str
- AddMana: mana:str (e.g. "{W}"), amount:int
- PutCounters: counter_type:str (e.g. "+1/+1"), count:int
- ReturnFromGraveyard: target_filter:str
- Bounce: target:str
- Unknown: text:"<the original sentence verbatim>" — use this for ANY effect \
the known types cannot represent. Never squeeze an effect into a near fit.

Rules:
1. State only what the printed rules text states. If the text does not state \
a value, use null / an empty list / an empty string — never invent one.
2. Numbers are integers exactly as stated ("up to three" allows 3 only \
because the text says three).
3. Copy costs, alternate costs and activated abilities each get their own \
entry in modes/effects.
4. Keyword abilities named in the type line or rules text (flying, lifelink, \
ward 2, ...) all go in body.keywords; ward keeps its number out of the name.
5. Current TokenSpec cannot represent token colors or affirmative legendary status.
For a token-creation sentence that states either, use Unknown with the entire
original sentence verbatim; do not emit a lossy CreateToken or extension fields.
Keep loyalty costs, optional physical exile payments, linked-card mana colors,
and conditional resolutions explicit; if no native fields express them exactly,
retain the original statement as Unknown rather than guessing executable effects.
6. Output valid JSON only — no markdown fences, no commentary.
"""


def _stats_line(card: PrintedCard) -> str:
    parts = []
    if card.power is not None or card.toughness is not None:
        parts.append(f"Power/Toughness: {card.power or '?'}/{card.toughness or '?'}")
    if card.loyalty is not None:
        parts.append(f"Loyalty: {card.loyalty}")
    return "\n".join(parts)


def build_user_prompt(card: PrintedCard) -> str:
    """The printed-card block shown to the model."""
    lines = [
        f"CARD: {card.name}",
        f"Mana cost: {card.mana_cost or '(none)'}",
        f"Type line: {card.type_line}",
    ]
    stats = _stats_line(card)
    if stats:
        lines.append(stats)
    lines.append("Rules text:")
    lines.append(card.oracle_text or "(none)")
    return "\n".join(lines)


_RETRY_INSTRUCTIONS = (
    "Your previous answer did not agree with the printed card. "
    "Fix every problem below and answer again with ONE corrected JSON object:\n"
)


# ── Answer parsing ───────────────────────────────────────────────────────────

_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def extract_json_object(text: str) -> dict[str, Any] | None:
    """The first decodable JSON object in a model answer (None without one).

    Tries the outermost ``{...}`` first; failing that (prose around the JSON,
    mana symbols like ``{1}{R}`` confusing brace matching), decodes successive
    balanced objects. Trailing commas are tolerated on both paths.
    """
    if not isinstance(text, str):
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1:
        return None
    if end > start:
        blob = text[start : end + 1]
        for candidate in (blob, _TRAILING_COMMA.sub(r"\1", blob)):
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
            break
    decoder = json.JSONDecoder()
    pos = start
    while True:
        brace = text.find("{", pos)
        if brace == -1:
            return None
        try:
            data, end_index = decoder.raw_decode(text[brace:])
        except json.JSONDecodeError:
            pos = brace + 1
            continue
        if isinstance(data, dict):
            return data
        pos = brace + end_index


def _token_contract_problems(data: dict[str, Any], printed: PrintedCard) -> list[str]:
    """Reject a bounded known schema near-fit; never infer new token semantics.

    Only explicit create...color/legendary...token clauses are recognized.
    Other wording is not certified by absence of a finding. Mixed token clauses
    remain conservative because the current schema has no source-span binding.
    """
    sentences = re.split(r"(?<=[.!?])\s+|\n+", printed.oracle_text)
    unsupported = [
        sentence
        for sentence in sentences
        if re.search(
            r"\bcreate\b[^.;\n]*\b(?:white|blue|black|red|green|colorless|legendary)\b[^.;\n]*\btokens?\b",
            sentence,
            re.IGNORECASE,
        )
    ]
    if not unsupported:
        return []
    effects = data.get("effects")
    if not isinstance(effects, list):
        return ["unrepresentable token statement requires verbatim Unknown"]
    typed = False
    unknown = set()
    for entry in effects:
        effect = entry.get("effect") if isinstance(entry, dict) else None
        if not isinstance(effect, dict):
            continue
        typed |= effect.get("effect_type") == "CreateToken"
        if effect.get("effect_type") == "Unknown" and isinstance(effect.get("text"), str):
            unknown.add(effect["text"])
    problems = []
    if typed:
        problems.append(
            "TokenSpec cannot preserve stated token color/legendary status; use verbatim Unknown, not CreateToken"
        )
    if any(sentence not in unknown for sentence in unsupported):
        problems.append("unrepresentable token statement requires verbatim Unknown")
    return problems


def _stamp(record_data: dict[str, Any], printed: PrintedCard) -> CardFacts:
    """Build the record from the model's dict and stamp compiler-owned fields."""
    record_data["rules_hash"] = rules_hash(
        printed.name,
        printed.mana_cost,
        printed.type_line,
        printed.oracle_text,
    )
    # Compiler-owned provenance: always stamped here, never taken from the
    # model's answer.
    record_data["compiled_by"] = COMPILED_BY
    record_data["compiled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return card_facts_from_dict(record_data)


def _failed_record(printed: PrintedCard, problems: list[str]) -> CardFacts:
    """Minimal hash-named record for a card whose answers were unusable.

    Spec Do 3 (Dotty rejection point 1 at 5beb064): when neither answer
    yields a parsable record, the run must still write the record named by
    ``rules_hash`` with confidence ``failed`` and the problems, so the
    failure is visible on disk instead of only in the run summary. The
    printed fields are filled from Scryfall; everything the model never
    stated stays empty/unknown. ``validate`` is deliberately not applied —
    an empty record cannot pass it, which is exactly why it is ``failed``.
    """
    data: dict[str, Any] = {
        "name": printed.name,
        "type_line": printed.type_line,
        "mana_cost": printed.mana_cost,
        "body": {
            "power": printed.power,
            "toughness": printed.toughness,
            "loyalty": printed.loyalty,
            "keywords": [],
        },
        "modes": [],
        "effects": [],
        "mana": [],
        "confidence": _STATUS_FAILED,
        "notes": "; ".join(problems)[:800],
    }
    return _stamp(data, printed)


def _reasoning_tokens(backend: Any) -> int | None:
    """Reasoning tokens the backend reports for its last call (None if unknown)."""
    metrics = getattr(backend, "last_request_metrics", None)
    if isinstance(metrics, dict):
        value = metrics.get("reasoning_tokens")
        if isinstance(value, int):
            return value
    return None


# ── Compilation ──────────────────────────────────────────────────────────────


@dataclass
class CompileResult:
    """Outcome for one card."""

    identifier: str
    name: str = ""
    rules_hash: str = ""
    status: str = _STATUS_COMPILED
    attempts: int = 0
    seconds: float = 0.0
    reasoning_tokens: int | None = None
    problems: list[str] = field(default_factory=list)
    record: CardFacts | None = None


def _call_backend(backend: Any, system: str, user: str) -> str:
    labels = accepted_call_kwargs(
        backend,
        call_class=CALL_CLASS,
        priority=BACKGROUND_PRIORITY,
        response_format={"type": "json_object"},
        lane_wait_s=5.0,
    )
    return backend.complete(
        system,
        user,
        _MAX_TOKENS,
        temperature=_TEMPERATURE,
        request_timeout_s=_TIMEOUT_S,
        background=True,
        reasoning_effort=_EFFORT,
        **labels,
    )


def compile_one(
    identifier: int | str,
    resolver: CardResolver,
    backend: Any,
) -> CompileResult:
    """Compile one card: prompt the model, validate against print, retry once."""
    started = time.perf_counter()
    result = CompileResult(identifier=str(identifier))

    printed = resolver.resolve(identifier)
    if printed is None:
        result.status = _STATUS_UNRESOLVED
        result.problems = [f"could not resolve {identifier!r} to a printed card"]
        result.seconds = time.perf_counter() - started
        return result

    result.name = printed.name
    result.rules_hash = rules_hash(printed.name, printed.mana_cost, printed.type_line, printed.oracle_text)

    system_prompt = _SYSTEM_PROMPT
    user_prompt = build_user_prompt(printed)
    problems: list[str] = []
    record: CardFacts | None = None

    for attempt in (1, 2):
        result.attempts = attempt
        answer = _call_backend(backend, system_prompt, user_prompt)
        tokens = _reasoning_tokens(backend)
        if tokens is not None:
            result.reasoning_tokens = (result.reasoning_tokens or 0) + tokens

        data = extract_json_object(answer)
        if data is None:
            problems = ["answer contained no JSON object"]
            record = None
        else:
            try:
                record_candidate = _stamp(dict(data), printed)
                problems = validate(record_candidate, printed) + _token_contract_problems(data, printed)
                record = record_candidate if not problems else record_candidate
            except Exception as error:
                record = None
                problems = [f"record could not be built from the answer: {error!r}"]
        if not problems and record is not None:
            break

        # Retry once with the problems listed; keep the last parsed record so a
        # final failure still writes something marked confidence "failed".
        user_prompt = (
            build_user_prompt(printed)
            + "\n\n"
            + _RETRY_INSTRUCTIONS
            + "\n".join(f"- {problem}" for problem in problems)
        )

    result.problems = problems
    if record is not None and not problems:
        result.status = _STATUS_COMPILED
        result.record = record
    elif record is not None:
        # Second failure: write the record anyway, marked failed (spec Do 3).
        record.confidence = "failed"
        extra = "; ".join(problems)[:800]
        record.notes = f"{record.notes}; validation failed: {extra}".strip("; ")
        result.status = _STATUS_FAILED
        result.record = record
    else:
        # Neither answer produced a usable record (no JSON object, or the
        # answers could not be built into a record at all). Still write the
        # hash-named record, marked failed with the problems, so the failure
        # is visible on disk instead of only in the run summary (spec Do 3;
        # Dotty rejection point 1 at 5beb064).
        result.status = _STATUS_FAILED
        result.record = _failed_record(printed, problems)

    result.seconds = time.perf_counter() - started
    return result


def compile_cards(
    identifiers: list[int | str],
    out_dir: Path | str,
    *,
    backend: Any | None = None,
    scryfall: Any | None = None,
    mtgadb: Any | None = None,
) -> dict[str, Any]:
    """Compile every identifier into ``out_dir``; return the run summary.

        One JSON file per card named by ``rules_hash``; cards whose hash already has a file there are skipped without a model call. When ``backend`` is omitted the real online backend is created via ``coach.create_backend("online")``.

    Writes only under ``out_dir``; never pushes anything anywhere.
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    if backend is None:
        from arenamcp.coach import create_backend

        backend = create_backend("online")

    if scryfall is None:
        from arenamcp.scryfall import ScryfallCache

        scryfall = ScryfallCache()

    resolver = CardResolver(scryfall=scryfall, mtgadb=mtgadb)

    summary: dict[str, Any] = {
        "compiled": 0,
        "failed": 0,
        "skipped": 0,
        "unresolved": 0,
        "seconds_per_card": None,
        "reasoning_tokens_per_card": None,
        "cards": [],
    }
    total_seconds = 0.0
    total_tokens = 0
    token_cards = 0

    for identifier in identifiers:
        printed = resolver.resolve(identifier)
        if printed is None:
            summary["unresolved"] += 1
            summary["cards"].append({"identifier": str(identifier), "status": _STATUS_UNRESOLVED})
            logger.warning("card_facts: unresolved identifier %r", identifier)
            continue

        digest = rules_hash(printed.name, printed.mana_cost, printed.type_line, printed.oracle_text)
        destination = out_path / f"{digest}.json"
        if destination.exists():
            summary["skipped"] += 1
            summary["cards"].append(
                {
                    "identifier": str(identifier),
                    "name": printed.name,
                    "rules_hash": digest,
                    "status": _STATUS_SKIPPED,
                }
            )
            continue

        result = compile_one(identifier, resolver, backend)
        if result.record is not None:
            destination.write_text(to_json(result.record), encoding="utf-8")

        entry: dict[str, Any] = {
            "identifier": str(identifier),
            "name": result.name,
            "rules_hash": result.rules_hash,
            "status": result.status,
            "attempts": result.attempts,
            "seconds": round(result.seconds, 2),
        }
        if result.problems:
            entry["problems"] = result.problems
        summary["cards"].append(entry)

        if result.status == _STATUS_COMPILED:
            summary["compiled"] += 1
        else:
            summary["failed"] += 1
            logger.warning(
                "card_facts: %s (%s) failed validation: %s",
                result.name,
                digest[:12],
                "; ".join(result.problems),
            )

        total_seconds += result.seconds
        if result.reasoning_tokens is not None:
            total_tokens += result.reasoning_tokens
            token_cards += 1

    attempted = summary["compiled"] + summary["failed"]
    if attempted:
        summary["seconds_per_card"] = round(total_seconds / attempted, 2)
    if token_cards:
        summary["reasoning_tokens_per_card"] = round(total_tokens / token_cards)
    return summary
