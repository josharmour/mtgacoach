"""N1 opt-in contract, Griselbrand bug_20261109_212434 printed evidence.

No live inference. Fake answers are contract fixtures, not accepted card facts.
"""

import copy
import json

import pytest

from arenamcp.card_facts.enrichment import enrich_record
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.schema import CardBody, CardFacts, card_facts_to_dict
from arenamcp.card_facts.validate import PrintedCard

PRINTED = PrintedCard(
    "Griselbrand",
    "{4}{B}{B}{B}{B}",
    "Legendary Creature — Demon",
    "Flying, lifelink\nPay 7 life: Draw seven cards.",
    "7",
    "7",
)


def record():
    return CardFacts(
        name=PRINTED.name,
        mana_cost=PRINTED.mana_cost,
        type_line=PRINTED.type_line,
        rules_hash=rules_hash(PRINTED.name, PRINTED.mana_cost, PRINTED.type_line, PRINTED.oracle_text),
        body=CardBody(power="7", toughness="7", keywords=("flying", "lifelink")),
        confidence="high",
    )


def answer():
    return {
        "threat": {
            "version": 1,
            "plan": [
                {
                    "text": "Pay 7 life to draw seven cards.",
                    "origin": "model",
                    "evidence": [{"card": PRINTED.name, "quote": "Pay 7 life: Draw seven cards."}],
                }
            ],
            "clock": [],
            "answers": [],
        },
        "relationships": {"version": 1, "links": []},
    }


class Backend:
    def __init__(self, response=None, error=False):
        self.response = json.dumps(answer()) if response is None else response
        self.error = error
        self.calls = []

    def complete(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise RuntimeError("secret-do-not-echo")
        return self.response


def test_opt_in_candidate_preserves_base_and_has_review_gate():
    base = record()
    before = copy.deepcopy(base)
    b = Backend()
    result = enrich_record(base, PRINTED, backend=b)
    assert result.record is not None and not result.problems
    assert base == before and result.record is not base
    assert base.confidence == "high" and result.record.confidence == "low"
    assert result.calls == 1 and result.review_required
    assert len(b.calls) == 1
    assert b.calls[0][1]["background"] is True
    assert "Griselbrand" in b.calls[0][0][0]
    out = card_facts_to_dict(result.record)
    assert out["narrative_provenance"]["status"] == "requires_independent_semantic_review"
    assert len(out["narrative_provenance"]["source_text_sha256"]["Griselbrand"]) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence", "failed"),
        ("confidence", "low"),
        ("rules_hash", "stale"),
        ("name", "Other"),
        ("threat", {"old": 1}),
        ("relationships", {"old": 1}),
    ],
)
def test_invalid_base_rejected_before_call(field, value):
    base = record()
    setattr(base, field, value)
    b = Backend()
    result = enrich_record(base, PRINTED, backend=b)
    assert result.record is None and result.problems and result.calls == 0
    assert b.calls == []


@pytest.mark.parametrize(
    "sources",
    [
        [],
        {"Griselbrand": "conflict"},
        {"Bad": None},
        {str(i): "x" for i in range(101)},
        {"Big": "x" * 100001},
    ],
)
def test_invalid_source_cohort_rejected_before_call(sources):
    b = Backend()
    result = enrich_record(record(), PRINTED, backend=b, related_sources=sources)
    assert result.record is None and result.calls == 0 and not b.calls


@pytest.mark.parametrize(
    "response",
    [
        "no json",
        "[]",
        "{}",
        '{"threat":{},"relationships":{}}',
        '{"threat":{},"relationships":{},"name":"forged"}',
        42,
    ],
)
def test_invalid_answer_fails_once_without_retry(response):
    b = Backend(response)
    result = enrich_record(record(), PRINTED, backend=b)
    assert result.record is None and result.calls == 1 and len(b.calls) == 1


def test_bad_quote_or_forged_derived_origin_rejected():
    for field, value in [
        ("origin", "derived"),
        ("evidence", [{"card": "Griselbrand", "quote": "Draw eight cards."}]),
    ]:
        a = answer()
        a["threat"]["plan"][0][field] = value
        result = enrich_record(record(), PRINTED, backend=Backend(json.dumps(a)))
        assert result.record is None and result.problems


def test_backend_error_sanitized_no_retry():
    b = Backend(error=True)
    result = enrich_record(record(), PRINTED, backend=b)
    assert result.record is None and result.calls == 1 and len(b.calls) == 1
    assert "secret" not in str(result)


def test_partial_catalog_rejected_before_call():
    b = Backend()
    result = enrich_record(record(), PRINTED, backend=b, oracle_id="test")
    assert result.record is None and result.calls == 0 and not b.calls


def test_catalog_provenance_is_separate_and_inputs_not_mutated():
    source = {
        "url": "https://json.commanderspellbook.com/variants.json.gz",
        "updated_at": "2026-11-15T00:00:00+00:00",
        "retrieved_at": "2026-11-15T01:00:00+00:00",
    }
    before = copy.deepcopy(source)
    result = enrich_record(
        record(), PRINTED, backend=Backend(), oracle_id="test", recipes=[], catalog_source=source
    )
    assert not result.problems
    catalog = result.record.relationships["catalog"]
    assert catalog["origin"] == "spellbook"
    assert catalog["completeness"] == "source_asserted_unverified"
    assert catalog["recipes"] == []
    assert source == before


@pytest.mark.parametrize(
    "layer,field", [("relationships", "catalog"), ("relationships", "source"), ("threat", "provenance")]
)
def test_model_cannot_forge_provenance_or_unknown_layer_fields(layer, field):
    a = answer()
    a[layer][field] = {"origin": "spellbook", "recipes": ["invented"]}
    result = enrich_record(record(), PRINTED, backend=Backend(json.dumps(a)))
    assert result.record is None and result.calls == 1


def test_model_cannot_hide_unvalidated_fields_in_claim():
    a = answer()
    a["threat"]["plan"][0]["compiled_by"] = "trusted"
    result = enrich_record(record(), PRINTED, backend=Backend(json.dumps(a)))
    assert result.record is None


@pytest.mark.parametrize("response", ["{" + '"nested":' + "[" * 1200 + "0" + "]" * 1200 + "}", "x" * 100001])
def test_nested_or_oversized_answer_returns_bounded_sanitized_failure(response):
    result = enrich_record(record(), PRINTED, backend=Backend(response))
    assert result.record is None and result.calls == 1 and result.problems


def test_observed_annotation_shape_failure_remains_rejected_without_retry():
    # Shape-only reproduction of the rejected live response, not a repaired answer.
    malformed = {
        "threat": {"plan": [], "clock": [], "answers": []},
        "relationships": [{"card": "Other", "role": "enabler", "claim": "flat string"}],
    }
    backend = Backend(response="```json\n" + json.dumps(malformed) + "\n```")
    result = enrich_record(record(), PRINTED, backend=backend)
    assert result.record is None
    assert result.problems == ("annotation answer contains unsupported model fields",)
    assert result.calls == 1 and len(backend.calls) == 1
