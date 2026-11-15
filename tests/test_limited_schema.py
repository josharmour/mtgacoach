"""Synthetic schema fixtures only, not a current-set semantic report."""

import json
from copy import deepcopy

import pytest

from arenamcp.card_facts.limited_schema import COLOR_PAIRS, card_digest, catalog_digest, validate_bundle


def fixture():
    cards = [
        dict(
            card_id="synthetic:1",
            name="Synthetic Card",
            set_code="tst",
            mana_cost="{1}{W}",
            type_line="Creature",
            oracle_text="Whenever you draw a card, gain 1 life.",
        )
    ]
    evidence = [dict(card_id="synthetic:1", field="oracle_text", quote="Whenever you draw a card")]
    record = dict(
        card_id="synthetic:1",
        name=cards[0]["name"],
        set_code="tst",
        source_sha256=card_digest(cards[0]),
        status="known",
        roles=["enabler", "payoff"],
        removal="none",
        archetypes=sorted(COLOR_PAIRS),
        evidence=evidence,
    )
    bundle = dict(
        schema_version=1,
        set_code="tst",
        provenance=dict(
            provider="synthetic_fixture",
            source_version="1",
            source_sha256=catalog_digest(cards),
            producer_version="fixture_v1",
        ),
        set_record=dict(
            status="known",
            mechanics=[dict(name="Synthetic mechanic", evidence=evidence)],
            archetypes=[
                dict(
                    colors=pair,
                    name="Synthetic pair",
                    enablers=["synthetic:1"],
                    payoffs=["synthetic:1"],
                    evidence=evidence,
                )
                for pair in sorted(COLOR_PAIRS)
            ],
        ),
        card_records=[record],
    )
    return cards, bundle


def check(cards, bundle):
    return validate_bundle(bundle, cards=cards, provider="synthetic_fixture", source_version="1")


def test_roundtrip_source_bound_no_mutation():
    cards, bundle = fixture()
    before = deepcopy((cards, bundle))
    assert check(cards, json.loads(json.dumps(bundle))) == ()
    assert (cards, bundle) == before


def test_unknown_explicitly_empty_is_valid_not_a_semantic_approval():
    cards, bundle = fixture()
    bundle["set_record"] = dict(status="unknown", mechanics=[], archetypes=[])
    bundle["card_records"][0].update(
        status="unknown", roles=[], removal="unknown", archetypes=[], evidence=[]
    )
    assert check(cards, bundle) == ()


@pytest.mark.parametrize(
    "path,value",
    [
        (["schema_version"], True),
        (["schema_version"], 2),
        (["set_code"], "TRK"),
        (["provenance", "provider"], "other"),
        (["provenance", "source_version"], "2"),
        (["provenance", "source_sha256"], "0" * 64),
        (["provenance", "producer_version"], ""),
        (["set_record", "status"], "verified"),
        (["set_record", "mechanics"], []),
        (["set_record", "archetypes", 0, "colors"], "UW"),
        (["set_record", "archetypes", 0, "enablers"], ["unknown"]),
        (["set_record", "archetypes", 0, "enablers"], ["synthetic:1"] * 2),
        (["set_record", "archetypes", 0, "evidence", 0, "quote"], "invented"),
        (["card_records", 0, "name"], "Other Card"),
        (["card_records", 0, "card_id"], "unknown"),
        (["card_records", 0, "source_sha256"], "0" * 64),
        (["card_records", 0, "roles"], ["bomb"]),
        (["card_records", 0, "roles"], ["payoff"] * 2),
        (["card_records", 0, "removal"], 0.95),
        (["card_records", 0, "removal"], float("nan")),
        (["card_records", 0, "status"], "unknown"),
        (["card_records", 0, "evidence"], []),
        (["card_records", 0, "archetypes"], ["WUB"]),
    ],
)
def test_rejects_unsupported_types_roles_identity_and_evidence(path, value):
    cards, bundle = fixture()
    target = bundle
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert check(cards, bundle)


@pytest.mark.parametrize(
    "path",
    [
        [],
        ["provenance"],
        ["set_record"],
        ["set_record", "mechanics", 0],
        ["set_record", "archetypes", 0],
        ["card_records", 0],
        ["card_records", 0, "evidence", 0],
    ],
)
def test_unknown_fields_and_invented_quality_metrics_rejected(path):
    cards, bundle = fixture()
    target = bundle
    for key in path:
        target = target[key]
    target["17lands_win_rate"] = 0.75
    assert check(cards, bundle)


def test_quote_cannot_borrow_other_cards_or_mismatched_source_digest():
    cards, bundle = fixture()
    cards[0]["oracle_text"] = "Changed source."
    assert check(cards, bundle) == ("source digest mismatch",)


def test_role_membership_must_agree_with_set_record():
    cards, bundle = fixture()
    next(a for a in bundle["set_record"]["archetypes"] if a["colors"] == "WU")["payoffs"] = []
    assert check(cards, bundle) == ("card/set role disagreement",)


def test_duplicate_sources_and_records_rejected():
    cards, bundle = fixture()
    assert check(cards + cards, bundle)
    bundle["card_records"] *= 2
    assert check(cards, bundle)


def test_no_untrusted_payload_is_echoed_on_error():
    cards, bundle = fixture()
    bundle["card_records"][0]["roles"] = [{"secret": "never print me"}]
    errors = check(cards, bundle)
    assert errors and "never print me" not in str(errors)


def test_set_cannot_override_explicit_unknown_card():
    cards, bundle = fixture()
    bundle["card_records"][0].update(
        status="unknown", roles=[], removal="unknown", archetypes=[], evidence=[]
    )
    assert check(cards, bundle) == ("set/card role disagreement",)


def test_set_cannot_call_detailed_filler_a_payoff():
    cards, bundle = fixture()
    bundle["card_records"][0]["roles"] = ["filler"]
    assert check(cards, bundle) == ("set/card role disagreement",)


def test_each_named_archetype_member_has_its_own_source_evidence():
    cards, bundle = fixture()
    other = dict(cards[0], card_id="synthetic:2", name="Other synthetic card")
    cards.append(other)
    bundle["provenance"]["source_sha256"] = catalog_digest(cards)
    bundle["set_record"]["archetypes"][0]["payoffs"].append("synthetic:2")
    assert check(cards, bundle) == ("archetype member lacks source evidence",)
