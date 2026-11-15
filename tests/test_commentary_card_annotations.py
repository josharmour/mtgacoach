import copy
import hashlib

import pytest

from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.validate import PrintedCard
from arenamcp.commentary.card_annotations import (
    AnnotationEvent,
    KnownCard,
    annotation_digest,
    project_annotation_facts,
)


def fixture():
    text = "Whenever you draw a card, you gain 2 life."
    printed = {
        "Threat": PrintedCard("Threat", "{B}", "Creature", text),
        "Partner": PrintedCard("Partner", oracle_text="Draw a card."),
    }
    claim = {
        "text": "Drawing cards gains life.",
        "origin": "model",
        "evidence": [{"card": "Threat", "quote": text}],
    }
    link = copy.deepcopy(claim)
    link["evidence"].append({"card": "Partner", "quote": "Draw a card."})
    record = {
        "name": "Threat",
        "rules_hash": rules_hash("Threat", "{B}", "Creature", text),
        "threat": {"version": 1, "plan": [claim], "clock": [], "answers": []},
        "relationships": {"version": 1, "links": [{"card": "Partner", "role": "payoff", "claim": link}]},
    }
    record["narrative_provenance"] = {
        "source_text_sha256": {
            name: hashlib.sha256(p.oracle_text.encode()).hexdigest() for name, p in printed.items()
        }
    }
    return dict(
        local_seat=1,
        allow_private=False,
        known_cards=(KnownCard("Partner", "battlefield", 1),),
        records={"Threat": record},
        printed=printed,
        reviewed_digests={"Threat": annotation_digest(record)},
    )


def test_opposing_enter_projects_threat_and_possible_link_without_mutation():
    args = fixture()
    before = copy.deepcopy(args)
    result = project_annotation_facts(AnnotationEvent("cast-1", "permanent_entered", "Threat", 2), **args)
    assert [f["kind"] for f in result.values()] == ["card_threat", "card_relationship"]
    assert all(
        f["event_id"] == "cast-1" and f["record_sha256"] == args["reviewed_digests"]["Threat"]
        for f in result.values()
    )
    assert list(result.values())[1]["scope"] == "possibility"
    assert list(result.values())[1]["text"].startswith("Potential interaction:")
    assert args == before


@pytest.mark.parametrize("kind", ["action_submitted", "spell_resolved", "unknown"])
def test_submission_is_not_cast(kind):
    assert not project_annotation_facts(AnnotationEvent("e", kind, "Threat", 2), **fixture())


@pytest.mark.parametrize("change", ["missing", "unreviewed", "stale", "changed", "bad_quote"])
def test_missing_or_unreviewed_or_stale_record_yields_no_fact(change):
    args = fixture()
    record = args["records"]["Threat"]
    if change == "missing":
        args["records"] = {}
    elif change == "unreviewed":
        args["reviewed_digests"] = {}
    elif change == "stale":
        args["printed"]["Threat"].oracle_text = "New text."
    elif change == "changed":
        record["confidence"] = "high"
    else:
        record["threat"]["plan"][0]["evidence"][0]["quote"] = "invented"
        args["reviewed_digests"]["Threat"] = annotation_digest(record)
    assert not project_annotation_facts(AnnotationEvent("e", "spell_cast", "Threat", 2), **args)


@pytest.mark.parametrize("zone", ["battlefield", "graveyard", "played"])
def test_known_public_zones(zone):
    args = fixture()
    args["known_cards"] = (KnownCard("Partner", zone, 2),)
    result = project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)
    assert len(result) == 1 and next(iter(result.values()))["kind"] == "card_relationship"


@pytest.mark.parametrize("allow,controller,expected", [(False, 1, 0), (True, 1, 1), (True, 2, 0)])
def test_private_presence_only_local_opt_in(allow, controller, expected):
    args = fixture()
    args["allow_private"] = allow
    args["known_cards"] = (KnownCard("Partner", "battlefield", controller, "private"),)
    result = project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)
    assert len(result) == expected
    if result:
        assert next(iter(result.values()))["visibility"] == "private"


def test_hidden_identity_never_leaks_even_when_private_enabled():
    args = fixture()
    args["allow_private"] = True
    event = AnnotationEvent("e", "spell_cast", "Threat", 2, face_down=True)
    assert not project_annotation_facts(event, **args)
    args["known_cards"] = (KnownCard("Partner", "battlefield", 1, face_down=True),)
    assert not project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)


def test_hand_library_and_missing_partner_not_inferred():
    for zone in ("hand", "library", "exile"):
        args = fixture()
        args["known_cards"] = (KnownCard("Partner", zone, 1),)
        assert not project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)


def test_public_partner_preferred_over_same_name_private():
    args = fixture()
    args["allow_private"] = True
    args["known_cards"] = (
        KnownCard("Partner", "battlefield", 1, "private"),
        KnownCard("Partner", "graveyard", 2),
    )
    result = project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)
    assert next(iter(result.values()))["visibility"] == "public"


def test_cast_does_not_claim_partner_has_entered():
    result = project_annotation_facts(AnnotationEvent("e", "spell_cast", "Threat", 2), **fixture())
    assert [fact["kind"] for fact in result.values()] == ["card_threat"]


def catalog_args():
    from arenamcp.card_facts.spellbook import SOURCE_URL, project_relationships

    args = fixture()
    recipe = {
        "id": "fixture",
        "source_url": "https://commanderspellbook.com/combo/fixture/",
        "pieces": [
            {
                "name": name,
                "oracle_id": oid,
                "quantity": 1,
                "zoneLocations": ["B"],
                "battlefieldCardState": "Untapped",
                "mustBeCommander": True,
            }
            for name, oid in [("Threat", "t"), ("Partner", "p"), ("Missing third", "m")]
        ],
        "additional_requirements": [],
        "results": [{"name": "Fixture result", "quantity": 1}],
        "mana_needed": "{G}",
        "easy_prerequisites": "At least 10 life.",
        "notable_prerequisites": "A token entered this turn.",
        "steps": "Fixture only.",
        "notes": "Synthetic, not a real combo",
        "legalities": {"commander": True, "brawl": False},
    }
    source = {
        "url": SOURCE_URL,
        "updated_at": "2026-11-15T00:00:00+00:00",
        "retrieved_at": "2026-11-15T01:00:00+00:00",
    }
    record = args["records"]["Threat"]
    record["relationships"]["catalog"] = project_relationships("Threat", "t", [recipe], source)
    args["reviewed_digests"]["Threat"] = annotation_digest(record)
    return args


def test_catalogue_is_only_possibility_and_preserves_all_requirements():
    result = project_annotation_facts(
        AnnotationEvent("e", "permanent_entered", "Threat", 1), **catalog_args()
    )
    facts = [f for f in result.values() if f["kind"] == "catalogue_relationship"]
    assert len(facts) == 1
    fact = facts[0]
    assert fact["scope"] == "possibility" and fact["activation"].startswith("not_established")
    assert "all recipe requirements apply" in fact["text"]
    recipe = fact["evidence"][0]["recipe"]
    assert len(recipe["pieces"]) == 3 and recipe["mana_needed"] == "{G}"
    assert recipe["legalities"]["brawl"] is False
    assert fact["observed_partner"] == {"name": "Partner", "zone": "battlefield", "controller": 1}


def test_catalogue_forged_link_rejected_even_with_new_review_digest():
    args = catalog_args()
    record = args["records"]["Threat"]
    record["relationships"]["catalog"]["recipe_links"][0]["text"] = "Combo is active."
    args["reviewed_digests"]["Threat"] = annotation_digest(record)
    assert not project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)


def test_changed_partner_source_is_stale_even_when_quote_still_present():
    args = fixture()
    args["printed"]["Partner"].oracle_text += " Only during your turn."
    assert not project_annotation_facts(AnnotationEvent("e", "permanent_entered", "Threat", 1), **args)
