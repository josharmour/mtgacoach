import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.deck_fakes import deck_case

from arenamcp.card_facts.reviewed_notes import corpus
from arenamcp.coach import CoachEngine
from arenamcp.deck_library import playbook_report
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity, oracle_rules
from arenamcp.deck_study import coverage, study_source, validate_study
from arenamcp.desktop.decks_tab import report_html
from arenamcp.standalone_diagnostics import _DiagnosticsMixin


def evidence(gid, catalog):
    card = catalog[gid]
    return {"card": gid, "source": study_source(card), "rule": 1, "quote": oracle_rules(card)[0]}


def conditions(*gids):
    return {
        "pieces": [{"card": gid, "zone": "battlefield"} for gid in gids],
        "mana": {"generic": 0},
        "first_turn": 1,
    }


def card_rule(gid, catalog):
    action = {"advice": "Consider the printed ability.", "conditions": conditions(gid)}
    return {
        "card": gid,
        "role": "Local fixture",
        "play": deepcopy(action),
        "hold": deepcopy(action),
        "avoid": deepcopy(action),
        "evidence": [evidence(gid, catalog)],
    }


def study(*rules):
    return {"version": 1, "cards": list(rules), "interactions": [], "minor_links": []}


def test_owner_strategy_restores_and_reports_coverage_without_generation(monkeypatch, tmp_path):
    state, catalog, response = deck_case()
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    monkeypatch.setattr("arenamcp.match_context._local_card", lambda gid, epoch: catalog[gid])
    loader = CoachEngine(backend=Mock())
    for with_study in (False, True):
        if with_study:
            response["study"] = study(card_rule(1001, catalog))
        book = DeckPlaybook.parse(json.dumps(response), catalog, commander_ids(state), deck_identity(state))
        assert book.export()["version"] == (3 if with_study else 2)
        loader.save_deck_strategy(book)
        new = CoachEngine(backend=Mock())
        restored = new.prepare_playbook(state)
        assert restored.data == book.data
        assert new.analyze_deck(state)
        assert new._backend.complete.call_count == 0
        report = playbook_report(restored)
        numbers = report["study_coverage"]
        assert numbers["legacy_nonland_roles"] == numbers["nonland_cards"] == 1
        assert numbers["structured_cards"] == int(with_study)
        assert f"{int(with_study)} have structured play" in report_html(report)
        assert "Local fixture" not in restored.render()
        diagnostic = _DiagnosticsMixin._get_llm_context(SimpleNamespace(_coach=new, _advice_history=[]))
        assert diagnostic["deck_playbook"]["data"] == {
            key: value for key, value in response.items() if key != "study"
        }
        payload = book.export()
        payload["version"] = 2 if with_study else 3
        with pytest.raises(ValueError, match="version"):
            DeckPlaybook.restore(payload, state)


def test_missing_rule_duplicate_or_unverifiable_card():
    _, catalog, _ = deck_case()
    row = card_rule(1001, catalog)
    for rows in ([], [row, row], [card_rule(1002, catalog)]):
        with pytest.raises(ValueError):
            validate_study(study(*rows), catalog)
    for key, value in (
        ("type_line", "N/A"),
        ("name", 1002),
        ("linked_face_ids", [23]),
        ("related_faces", {"bad": True}),
        ("type_line", "1,2"),
    ):
        changed = {**catalog, 1002: {**catalog[1002], key: value}}
        assert coverage({}, changed)["unresolved_types"] == 1
        with pytest.raises(ValueError, match="unresolved"):
            validate_study(study(row), changed)
    changed = {**catalog, 1002: {**catalog[1002], "related_faces": [catalog[1001]]}}
    assert coverage({}, changed)["nonland_cards"] == 2
    with pytest.raises(ValueError, match="missing 1 nonland"):
        validate_study(study(row), changed)


@pytest.mark.parametrize(
    "change",
    [
        lambda row: row["evidence"][0].update(rule=True),
        lambda row: row["evidence"][0].update(source="stale"),
        lambda row: row["evidence"][0].update(quote="Some other card's words."),
        lambda row: row["evidence"].append(deepcopy(row["evidence"][0])),
        lambda row: row["play"]["conditions"].update(first_turn=False),
        lambda row: row["play"]["conditions"]["mana"].update(W=-1),
        lambda row: row["play"]["conditions"]["mana"].update(C=1.5),
        lambda row: row["play"]["conditions"]["pieces"][0].update(zone="any"),
        lambda row: row["play"]["conditions"]["pieces"][0].update(card=1002),
        lambda row: row["play"]["conditions"]["pieces"].append({"card": 1002, "zone": "hand"}),
        lambda row: row["hold"].update(advice=" "),
        lambda row: row.pop("avoid"),
    ],
)
def test_per_card_admission_fails_closed(change):
    _, catalog, _ = deck_case()
    row = card_rule(1001, catalog)
    change(row)
    with pytest.raises(ValueError):
        validate_study(study(row), catalog)


def test_source_includes_other_faces_and_printed_stats():
    _, catalog, _ = deck_case()
    with_face = {**catalog[1001], "related_faces": [catalog[1002]]}
    catalog[1001] = with_face
    row = card_rule(1001, catalog)
    validate_study(study(row), catalog)
    for replacement in ({"power": "7"}, {"related_faces": [{**catalog[1002], "mana_cost": "{2}"}]}):
        changed = {**catalog, 1001: {**with_face, **replacement}}
        with pytest.raises(ValueError, match="printed source"):
            validate_study(study(row), changed)


def test_in_deck_reviewed_links_need_coverage_or_explicit_omissions():
    cards = {}
    for name in ("Case of the Uneaten Feast", "Lunar Convocation"):
        gid, printed = next((gid, c) for gid, c in corpus()[0].items() if c.name == name)
        cards[gid] = {key: getattr(printed, key) for key in ("name", "type_line", "mana_cost", "oracle_text")}
    left, right = cards
    doc = study(*(card_rule(gid, cards) for gid in cards))
    actual = coverage({}, cards)
    assert actual["reviewed_prints"] == 2 and actual["reviewed_links"] == 2
    with pytest.raises(ValueError, match="missing 2 reviewed"):
        validate_study(doc, cards)
    doc["interactions"].append(
        {
            "id": "life",
            "kind": "synergy",
            "conditions": conditions(left, right),
            "outcome": "Evaluate the lifegain payoff.",
            "stops": "An absent enchantment.",
            "evidence": [evidence(gid, cards) for gid in cards],
            "links": [{"source": left, "target": right, "role": "enabler"}],
        }
    )
    with pytest.raises(ValueError, match="missing 1 reviewed"):
        validate_study(doc, cards)
    omission = {
        "link": {"source": right, "target": left, "role": "payoff"},
        "reason": "Reverse direction of the same interaction.",
    }
    doc["minor_links"].append(omission)
    counts = validate_study(doc, cards)
    assert counts["structured_cards"] == counts["reviewed_links"] == 2
    assert counts["links_in_entries"] == counts["minor_links"] == counts["synergies"] == 1
    assert counts["combos"] == 0
    for change in (
        lambda d: d["minor_links"].append(omission),
        lambda d: omission["link"].update(role="fabricated"),
    ):
        change(doc)
        with pytest.raises(ValueError):
            validate_study(doc, cards)
        doc["minor_links"] = [omission]
    mismatched = {**cards, left: {**cards[left], "mana_cost": "{0}"}}
    assert coverage({}, mismatched)["reviewed_links"] == 0
    assert coverage({}, mismatched)["reviewed_prints"] == 1
