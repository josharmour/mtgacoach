"""Synthetic preparatory renderer controls; not E3 size acceptance."""

import copy
from dataclasses import replace

import pytest

from arenamcp.ability_synthesizer import Cost, UnknownEffect
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.prompt_render import render, source_text
from arenamcp.card_facts.schema import CardBody, CardFacts, EffectEntry, Mode


def sample():
    card = dict(
        name="Air Cadet",
        mana_cost="{2}{U}",
        type_line="Creature — Bird",
        power="2",
        toughness="3",
        oracle_text="Flying",
    )
    record = CardFacts(
        name=card["name"],
        mana_cost=card["mana_cost"],
        type_line=card["type_line"],
        rules_hash=rules_hash(card["name"], card["mana_cost"], card["type_line"], card["oracle_text"]),
        confidence="high",
        body=CardBody(power=2, toughness=3, keywords=("Flying",)),
        modes=(Mode(name="cast", cost=Cost(mana="{2}{U}"), cost_text="{2}{U}"),),
    )
    return card, record, source_text(card)


def test_default_off_and_role_guards_preserve_exact_text():
    card, record, text = sample()
    assert render(card, record, text) == text
    for role in ("option", "permanent", "stack", "hand", "unknown"):
        assert render(card, record, text, enabled=True, role=role) == text


def test_supported_body_preserves_characteristics_without_mutation():
    card, record, text = sample()
    before = copy.deepcopy((card, record))
    out = render(card, record, text, enabled=True, role="stable_deck")
    assert out == "Air Cadet — {2}{U}; Creature — Bird; 2/3; Flying"
    assert len(out) < len(text)
    assert (card, record) == before


@pytest.mark.parametrize(
    "kind",
    [
        "unknown",
        "modal",
        "cost",
        "hash",
        "face",
        "extension",
        "reserved",
        "notes",
        "body",
        "oracle",
        "annotated",
        "failed",
    ],
)
def test_unsupported_information_keeps_original_exactly(kind):
    card, record, text = sample()
    if kind == "unknown":
        record.effects = (EffectEntry(effect=UnknownEffect(text="Draw a card.")),)
    elif kind == "modal":
        record.modes += (Mode(name="evoke"),)
    elif kind == "cost":
        record.modes[0].cost = replace(record.modes[0].cost, life=2)
    elif kind == "hash":
        record.rules_hash = "0" * 64
    elif kind == "face":
        card["related_faces"] = [dict(oracle_text="Draw two cards.")]
    elif kind == "extension":
        record.body._unknown_fields = {"obligation": "Extra cost"}
    elif kind == "reserved":
        record.relationships = {"condition": "Other card required"}
    elif kind == "notes":
        record.notes = "Additional obligation"
    elif kind == "body":
        record.body.power = 4
    elif kind == "oracle":
        card["oracle_text"] = "Flying, ward {2}"
    elif kind == "annotated":
        text += "\nDo not spend this before combat."
    elif kind == "failed":
        record.confidence = "failed"
    assert render(card, record, text, enabled=True, role="stable_deck") == text


def test_missing_record_and_arbitrary_source_keep_all_modal_text():
    text = "Choose one — attack; or defend. Other face: flying. Option 7."
    assert render({}, None, text, enabled=True, role="stable_deck") == text


@pytest.mark.parametrize(
    "metadata",
    [{"layout": "meld"}, {"linked_face_ids": [19]}, {"related_faces": False}, {"single_face_source": False}],
)
def test_cached_record_stays_out_of_graph_and_render_after_public_projection(monkeypatch, metadata):
    from types import SimpleNamespace

    from arenamcp import server
    from arenamcp.card_db import CardInfo
    from arenamcp.card_facts.source_identity import legacy_lookup_hash
    from arenamcp.line_search_moves import _record_key

    card, record, text = sample()
    assert render(dict(card, **metadata), record, text, enabled=True, role="stable_deck") == text
    info = CardInfo(
        name=card["name"],
        oracle_text=card["oracle_text"],
        type_line=card["type_line"],
        mana_cost=card["mana_cost"],
        cmc=3.0,
        colors=["U"],
        arena_id=81100,
        source="local",
        **metadata,
    )
    monkeypatch.setattr(server, "_get_card_db", lambda: SimpleNamespace(get_card_by_arena_id=lambda _: info))
    raw = {
        "grp_id": 81100,
        "instance_id": 100,
        "object_kind": "CARD",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    for obj in [
        server.enrich_with_oracle_text(81100),
        server._serialize_snapshot_obj(raw),
        server._serialize_bridge_card(raw, {100: raw}),
    ]:
        assert obj["single_face_source"] is False
        assert legacy_lookup_hash(obj) is None and _record_key(obj) == ("", "", "", "")
        assert "linked_face_ids" not in obj and "card_faces" not in obj


@pytest.mark.parametrize(
    "bridge",
    [
        {"grp_id": 3},
        {"grp_id": 81101, "face_down": True},
        {"grp_id": 81101, "isFacedown": True},
        {"grp_id": 81101, "face_down_reason": 6},
        {"grp_id": 81101},
    ],
)
def test_conflicting_or_face_down_bridge_print_never_borrows_cached_identity(monkeypatch, bridge):
    from arenamcp import server
    from arenamcp.line_search_moves import _record_key

    monkeypatch.setattr(
        server, "enrich_with_oracle_text", lambda _: {"name": "Known Front", "oracle_text": ""}
    )
    stale = {
        "grp_id": 81100,
        "name": "Private print canary",
        "oracle_text": "Private rule canary",
        "mana_cost": "{8}{G}",
        "card_faces": [{"name": "Private second face"}],
    }
    newer = dict(instance_id=5, object_type="Card", **bridge)
    row = server._serialize_bridge_card(newer, {5: stale})
    assert "Private" not in str(row) and "{8}{G}" not in str(row)
    if server.is_face_down(newer):
        assert "single_face_source" not in row
    else:
        assert row["single_face_source"] is False and _record_key(row) == ("", "", "", "")
