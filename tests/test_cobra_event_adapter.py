import copy
import dataclasses
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.test_line_search import _LOTUS_COBRA_CARD, _with_records

from arenamcp import line_search_moves as lsm
from arenamcp.card_facts.schema import card_facts_from_dict


def raw():
    data = (Path(__file__).parent / "fixtures/f3_pilot_20261114/lotus_cobra.json").read_bytes()
    assert (
        hashlib.sha256(data).hexdigest() == "3252a5562ec62dcac5ec9561d7e3048d067bbcd785d5c3903dfc21f4dfd0ee80"
    )
    return json.loads(data)["record"]


def test_actual_record_event_projection_preserves_cast_and_record(monkeypatch):
    record = card_facts_from_dict(raw())
    before = dataclasses.asdict(record)
    assert lsm._event_landfall_mana(record, _LOTUS_COBRA_CARD)
    assert lsm._record_semantics(record, record.name, _LOTUS_COBRA_CARD) is None
    assert dataclasses.asdict(record) == before
    _with_records(monkeypatch, [(_LOTUS_COBRA_CARD, raw())])
    moves = lsm.Moves.__new__(lsm.Moves)
    moves.our_turn = True
    moves.model = SimpleNamespace(sources_now=[], sources_all=[], our_permanents=[])
    node = SimpleNamespace(
        k=0, played=(), rocks=(), ours=({"instance_id": 900, "_card": dict(_LOTUS_COBRA_CARD)},)
    )
    land = lsm.Land(
        42,
        "Tapped land",
        frozenset({"G"}),
        True,
        SimpleNamespace(produces=frozenset({"G"}), name="Tapped land"),
    )
    assert moves.sources(node, None) == ([], [])
    base, sources = moves.sources(node, land)
    assert base == [] and len(sources) == 1 and sources[0].produces == frozenset("WUBRG")
    assert node.rocks == ()
    node.ours = ()
    assert moves.sources(node, land) == ([], [])


@pytest.mark.parametrize(
    "change",
    [
        "amount",
        "color",
        "condition",
        "self_entry",
        "opponent",
        "extra",
        "unknown",
        "castcost",
        "additionalcost",
        "mode",
        "oracle",
    ],
)
def test_unsupported_or_different_event_abstains(change):
    data = raw()
    card = dict(_LOTUS_COBRA_CARD)
    if change == "amount":
        data["effects"][0]["effect"]["amount"] = 2
    elif change == "color":
        data["effects"][0]["effect"]["mana"] = "{R}"
    elif change == "condition":
        data["effects"][0]["conditions"] = ["only once"]
    elif change == "self_entry":
        data["effects"][0]["trigger"] = "ENTERS_THE_BATTLEFIELD"
    elif change == "opponent":
        data["effects"][0]["trigger"] = "whenever a land an opponent controls enters"
    elif change == "extra":
        data["effects"].append(copy.deepcopy(data["effects"][0]))
    elif change == "unknown":
        data["effects"][0]["effect"] = {"effect_type": "Unknown", "text": "unknown"}
    elif change == "castcost":
        data["modes"][0]["cost"]["mana"] = "{0}"
    elif change == "additionalcost":
        data["modes"][0]["cost"]["life"] = 2
    elif change == "mode":
        data["modes"][0]["name"] = "activate"
    else:
        card["oracle_text"] += " Draw a card."
    assert not lsm._event_landfall_mana(card_facts_from_dict(data), card)


@pytest.mark.parametrize("where", ["record", "entry", "effect", "mode", "cost", "body", "mana"])
def test_preserved_unknown_obligations_abstain(where):
    data = raw()
    targets = {
        "record": data,
        "entry": data["effects"][0],
        "effect": data["effects"][0]["effect"],
        "mode": data["modes"][0],
        "cost": data["modes"][0]["cost"],
        "body": data["body"],
        "mana": data["mana"][0],
    }
    targets[where]["additional_condition"] = "Only if an additional payment was made"
    assert not lsm._event_landfall_mana(card_facts_from_dict(data), _LOTUS_COBRA_CARD)


@pytest.mark.parametrize("confidence", ["failed", "low", "", None])
def test_only_high_confidence_event_admission(confidence):
    data = raw()
    data["confidence"] = confidence
    assert not lsm._event_landfall_mana(card_facts_from_dict(data), _LOTUS_COBRA_CARD)


@pytest.mark.parametrize("field", ["relationships", "limited", "threat"])
def test_reserved_semantic_fields_must_be_empty(field):
    data = raw()
    data[field] = {"additional_condition": "Only after another payment"}
    assert not lsm._event_landfall_mana(card_facts_from_dict(data), _LOTUS_COBRA_CARD)
