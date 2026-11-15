"""Explicit, opt-in N1 annotation enrichment. No backend discovery or activation.

The caller owns capacity checks and authorization for the injected backend.
One invocation makes at most one background backend call (provider transport
retries, if any, remain the responsibility of the injected backend). Evidence validation is not
semantic acceptance: successful output remains marked as requiring review.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass

from arenamcp.card_facts.compiler import _call_backend, extract_json_object
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.narrative import build_layer_prompt, validate_layers
from arenamcp.card_facts.schema import CardFacts, card_facts_from_dict, card_facts_to_dict
from arenamcp.card_facts.spellbook import project_relationships
from arenamcp.card_facts.validate import PrintedCard, validate


@dataclass
class EnrichmentResult:
    record: CardFacts | None
    problems: tuple[str, ...] = ()
    calls: int = 0
    review_required: bool = True


def enrich_record(
    record: CardFacts,
    printed: PrintedCard,
    *,
    backend,
    related_sources: dict[str, str] | None = None,
    oracle_id: str | None = None,
    recipes: list[dict] | None = None,
    catalog_source: dict | None = None,
) -> EnrichmentResult:
    """Return a separate annotated candidate, never a trusted/executable upgrade.

    related_sources must be a caller-reviewed cohort (same-set for N1's model
    pass); no external lookups occur here. Existing annotations are not replaced
    silently. Invalid local inputs fail before any backend call. No retry occurs.
    """
    if type(record) is not CardFacts or type(printed) is not PrintedCard:
        return EnrichmentResult(None, ("native record and printed source required",))
    if record.confidence != "high":
        return EnrichmentResult(None, ("high-confidence base record required",))
    if record.threat or record.relationships:
        return EnrichmentResult(None, ("existing annotations require explicit reconciliation",))
    if record.rules_hash != rules_hash(
        printed.name, printed.mana_cost, printed.type_line, printed.oracle_text
    ):
        return EnrichmentResult(None, ("printed source hash mismatch",))
    problems = validate(record, printed)
    if problems:
        return EnrichmentResult(None, tuple(problems))
    sources = copy.deepcopy(related_sources) if related_sources is not None else {}
    if not isinstance(sources, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in sources.items()
    ):
        return EnrichmentResult(None, ("related sources must map card names to printed text",))
    if printed.name in sources and sources[printed.name] != printed.oracle_text:
        return EnrichmentResult(None, ("conflicting focal source text",))
    sources[printed.name] = printed.oracle_text
    if len(sources) > 100 or sum(len(k) + len(v) for k, v in sources.items()) > 100_000:
        return EnrichmentResult(None, ("source cohort exceeds annotation input bound",))
    catalog = None
    if any(value is not None for value in (oracle_id, recipes, catalog_source)):
        if oracle_id is None or recipes is None or catalog_source is None:
            return EnrichmentResult(None, ("complete catalog inputs required",))
        try:
            catalog = project_relationships(printed.name, oracle_id, recipes, catalog_source)
        except (TypeError, ValueError, KeyError):
            return EnrichmentResult(None, ("invalid catalog inputs",))
    prompt = build_layer_prompt(printed.name, sources)
    try:
        answer = _call_backend(backend, prompt, "Return only the requested threat and relationships JSON.")
    except Exception:
        # Do not echo provider exceptions: they can contain credentials/URLs.
        return EnrichmentResult(None, ("annotation backend failed",), calls=1)
    data = extract_json_object(answer) if isinstance(answer, str) else None
    if not isinstance(data, dict) or set(data) != {"threat", "relationships"}:
        return EnrichmentResult(None, ("annotation answer must contain only both requested layers",), calls=1)
    if any(
        not isinstance(data[key], dict)
        or type(data[key].get("version")) is not int
        or data[key]["version"] != 1
        for key in ("threat", "relationships")
    ):
        return EnrichmentResult(None, ("explicit versioned annotation layers required",), calls=1)
    problems = validate_layers(data["threat"], data["relationships"], sources, card_name=printed.name)
    if problems:
        return EnrichmentResult(None, tuple(problems), calls=1)
    output = card_facts_to_dict(copy.deepcopy(record))
    # The new annotations have not been semantically verified. Do not let a
    # high-confidence base promote them into trusted record consumers.
    output["confidence"] = "low"
    output["threat"] = copy.deepcopy(data["threat"])
    output["relationships"] = copy.deepcopy(data["relationships"])
    if catalog is not None:
        # Catalog evidence is never misrepresented as model-written prose.
        output["relationships"]["catalog"] = catalog
    output["narrative_provenance"] = {
        "status": "requires_independent_semantic_review",
        "origin": "model",
        "source_rules_hash": record.rules_hash,
        "source_text_sha256": {
            name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in sorted(sources.items())
        },
    }
    return EnrichmentResult(card_facts_from_dict(output), calls=1)
