import copy

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
    return dict(
        local_seat=1,
        allow_private=False,
        known_cards=(KnownCard("Partner", "battlefield", 1),),
        records={"Threat": record},
        printed=printed,
        reviewed_digests={"Threat": annotation_digest(record)},
    )


def test_opposing_cast_projects_threat_and_possible_link_without_mutation():
    args = fixture()
    before = copy.deepcopy(args)
    result = project_annotation_facts(AnnotationEvent("cast-1", "spell_cast", "Threat", 2), **args)
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
