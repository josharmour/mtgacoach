import dataclasses
import json
from copy import deepcopy
from pathlib import Path

import pytest
from tests.test_line_search import (
    _CHROME_MOX_CARD,
    _FURY_CARD,
    _LOTUS_COBRA_CARD,
    _SHOCK_CARD,
    _a1_board,
    _a1_variants,
    _with_records,
)

from arenamcp import line_search_moves as lsm
from arenamcp.card_facts.schema import card_facts_from_dict
from arenamcp.card_facts.search_adapter import search_record

ROOT = Path(__file__).parent / "fixtures/f2_compiler_retained"
RECORDS = {d["name"]: d for p in ROOT.glob("*.json") if "effects" in (d := json.loads(p.read_text()))}


def test_real_compiler_mox_preserved_and_actual_paid_or_declined_transitions(monkeypatch):
    raw = deepcopy(RECORDS["Chrome Mox"])
    record = card_facts_from_dict(raw)
    original = dataclasses.asdict(record)
    view = search_record(record, _CHROME_MOX_CARD)
    assert view is not record
    assert dataclasses.asdict(record) == original
    assert raw == RECORDS["Chrome Mox"]
    _with_records(monkeypatch, [(_CHROME_MOX_CARD, raw)])
    assert lsm.unmodelled_effect(_CHROME_MOX_CARD) == ""
    apply = lsm.Moves._apply_casts
    outcomes = []

    def inspect(self, mid, *args, **kwargs):
        before = set(mid.hand)
        apply(self, mid, *args, **kwargs)
        for var, _ in mid.casts:
            if self.spells[var.spell].name != "Chrome Mox":
                continue
            rocks = [r for r in mid.rocks if r.name == "Chrome Mox"]
            if var.imprint_rock:
                assert var.payment and set(var.payment) <= before
                assert not set(var.payment) & set(mid.hand)
                expected = frozenset().union(
                    *(lsm._body_colors(self.spells[i].spell.card) for i in var.payment)
                )
                assert rocks[-1].produces == expected
                outcomes.append(tuple(sorted(expected)))
            else:
                assert not var.payment and not rocks
                outcomes.append("decline")

    monkeypatch.setattr(lsm.Moves, "_apply_casts", inspect)
    _a1_variants(_a1_board(_CHROME_MOX_CARD, _SHOCK_CARD))
    assert "decline" in outcomes and ("R",) in outcomes


@pytest.mark.parametrize("name,card", [("Fury", _FURY_CARD), ("Lotus Cobra", _LOTUS_COBRA_CARD)])
def test_original_unknown_stays_unknown(monkeypatch, name, card):
    raw = RECORDS[name]
    record = card_facts_from_dict(raw)
    assert search_record(record, card) is record
    _with_records(monkeypatch, [(card, raw)])
    assert lsm.unmodelled_effect(card) == "its triggered ability"
    variants, _ = _a1_variants(_a1_board(card, _SHOCK_CARD))[name]
    assert all(not v.ephemeral and not v.imprint_rock and not v.consumes for v in variants)


@pytest.mark.parametrize(
    "change", ["amount", "color", "payment", "condition", "tap", "cost", "extra", "unknown", "oracle"]
)
def test_imprint_projection_rejects_contradictory_or_extra_obligations(change):
    raw, card = deepcopy(RECORDS["Chrome Mox"]), dict(_CHROME_MOX_CARD)
    if change == "amount":
        raw["effects"][1]["effect"]["amount"] = 2
    elif change == "color":
        raw["effects"][1]["effect"]["mana"] = "{R}"
    elif change == "payment":
        raw["effects"][0]["effect"]["target"] = "two cards from your hand"
    elif change == "condition":
        raw["effects"][0]["conditions"].append("only if you control a creature")
    elif change == "tap":
        raw["modes"][0]["cost"]["tap"] = False
    elif change == "cost":
        raw["modes"][0]["cost"]["life"] = 2
    elif change == "extra":
        raw["effects"].append(deepcopy(raw["effects"][1]))
    elif change == "unknown":
        raw["effects"].append({"effect": {"effect_type": "Unknown", "text": "additional effect"}})
    else:
        card["oracle_text"] += " Activate only once each turn."
    record = card_facts_from_dict(raw)
    assert search_record(record, card) is record


def test_all_original_five_records_are_retained():
    assert set(RECORDS) == {
        "Fury",
        "Lotus Cobra",
        "Chrome Mox",
        "Sylvan Library",
        "Minsc & Boo, Timeless Heroes",
    }
    for raw in RECORDS.values():
        assert raw["compiled_by"] == "card_facts.compiler"
        assert raw["compiled_at"].startswith("2026-11-11")


def test_adapter_is_mechanics_based_not_card_name():
    raw = deepcopy(RECORDS["Chrome Mox"])
    raw["name"] = "Different printed name"
    card = dict(_CHROME_MOX_CARD, name=raw["name"])
    record = card_facts_from_dict(raw)
    assert search_record(record, card) is not record


def test_future_unknown_schema_obligation_is_not_dropped():
    raw = deepcopy(RECORDS["Chrome Mox"])
    raw["effects"][1]["additional_payment"] = "sacrifice a creature"
    record = card_facts_from_dict(raw)
    assert search_record(record, _CHROME_MOX_CARD) is record
