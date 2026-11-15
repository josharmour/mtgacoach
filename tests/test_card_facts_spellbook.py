"""Complete-recipe contract from ArenaOnAir test_card_knowledge exported fixture.

Synthetic conditional recipe, not a real combo or N1 semantic acceptance record.
"""

import copy

import pytest

from arenamcp.card_facts.spellbook import SOURCE_URL, project_relationships, validate_recipe

SOURCE = {
    "url": SOURCE_URL,
    "updated_at": "2026-11-15T00:00:00+00:00",
    "retrieved_at": "2026-11-15T01:00:00+00:00",
}


def recipe():
    return {
        "id": "test",
        "source_url": "https://commanderspellbook.com/combo/test/",
        "pieces": [
            {
                "name": name,
                "oracle_id": oid,
                "quantity": 1,
                "zoneLocations": ["B"],
                "battlefieldCardState": "Untapped",
                "mustBeCommander": True,
            }
            for name, oid in [("Opt", "opt"), ("Partner", "partner")]
        ],
        "additional_requirements": [
            {
                "name": "Extra permanent",
                "scryfall_query": "is:permanent",
                "quantity": 2,
                "zoneLocations": ["H"],
            }
        ],
        "results": [{"name": "Infinite test counters", "quantity": 1}],
        "mana_needed": "{G}",
        "easy_prerequisites": "At least 10 life.",
        "notable_prerequisites": "A token entered this turn.",
        "steps": "Pay the cost. Repeat the actions.",
        "notes": "Conditional fixture only.",
        "legalities": {"commander": True, "brawl": False},
        "future_field": {"preserved": [1]},
    }


def test_full_recipe_preserved_without_two_card_claim_or_mutation():
    r = recipe()
    before = copy.deepcopy(r)
    output = project_relationships("Opt", "opt", [r], SOURCE)
    assert output["recipes"] == [r]
    assert output["recipe_links"][0]["role"] == "combo_piece"
    assert "all recipe requirements apply" in output["recipe_links"][0]["text"]
    output["recipes"][0]["future_field"]["preserved"].append(2)
    assert r == before
    assert output["source"] == SOURCE


@pytest.mark.parametrize("field", list(recipe()))
def test_missing_required_recipe_field(field):
    if field == "future_field":
        return
    r = recipe()
    del r[field]
    assert validate_recipe(r)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pieces", []),
        ("pieces", None),
        ("additional_requirements", None),
        ("results", []),
        ("steps", ""),
        ("legalities", {}),
        ("legalities", {"brawl": "yes"}),
        ("source_url", "https://evil.invalid"),
        ("id", "../path"),
    ],
)
def test_malformed_recipes_fail_closed(field, value):
    r = recipe()
    r[field] = value
    with pytest.raises(ValueError):
        project_relationships("Opt", "opt", [r], SOURCE)


@pytest.mark.parametrize(
    "field,value", [("quantity", True), ("quantity", 0), ("zoneLocations", []), ("oracle_id", "")]
)
def test_piece_contract(field, value):
    r = recipe()
    r["pieces"][0][field] = value
    assert validate_recipe(r)


def test_exact_identity_not_name_guess():
    assert project_relationships("Opt", "other", [recipe()], SOURCE)["recipes"] == []
    with pytest.raises(ValueError):
        project_relationships("A-Opt", "opt", [recipe()], SOURCE)


def test_duplicate_recipe_rejected_and_no_partial_result():
    with pytest.raises(ValueError):
        project_relationships("Opt", "opt", [recipe(), recipe()], SOURCE)
    with pytest.raises(ValueError):
        project_relationships("Opt", "opt", [recipe(), {}], SOURCE)


@pytest.mark.parametrize(
    "field,value", [("url", "http://example.com"), ("updated_at", "2026-11-15"), ("retrieved_at", None)]
)
def test_provenance_required(field, value):
    source = dict(SOURCE, **{field: value})
    with pytest.raises(ValueError):
        project_relationships("Opt", "opt", [recipe()], source)


def test_conflicting_partner_identity_rejected():
    r = recipe()
    p = dict(r["pieces"][1], name="Different")
    r["pieces"].append(p)
    assert validate_recipe(r)
