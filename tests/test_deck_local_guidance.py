import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Signal
from tests.deck_fakes import deck_case
from tests.test_deck_study import conditions, native_catalog
from tests.test_decks_tab import FakeSession

from arenamcp.deck_local_guidance import TOPICS, deck_guidance
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity
from arenamcp.desktop.deck_card_study import DeckCardStudyDialog
from arenamcp.standalone_decks import _DeckLibraryMixin


@pytest.fixture
def deck(monkeypatch):
    state, catalog, data = deck_case()
    native = native_catalog(monkeypatch, catalog)
    book = DeckPlaybook.parse(json.dumps(data), catalog, commander_ids(state), deck_identity(state))
    return state, book, native


def writer():
    model = Mock(digest="d" * 64, last_metrics={"generated_tokens": 15})

    def complete(system, prompt, **kwargs):
        payload = json.loads(prompt)
        assert [(x["card"], x["main_copies"]) for x in payload["prints"]] == [(1001, 0), (1002, 20)]
        assert payload["commanders"] == [1001]
        assert not any(
            k in prompt for k in ("HIDDEN-CANARY", "match_id", "player_names", "battlefield_snapshot")
        )
        topics = payload["topics"]
        row = {
            "advice": "Preserve this piece until its printed effect will matter.",
            "conditions": conditions(1001),
            "evidence": [{"card": 1001, "rule": 1}],
        }
        rows = {topic: deepcopy(row) for topic in topics}
        from jsonschema import Draft202012Validator

        assert Draft202012Validator(kwargs["response_format"]).is_valid(rows)
        return rows

    model.complete.side_effect = complete
    return model


def test_two_explicit_deck_studies_render_and_reuse_without_model_calls(qapp, deck, monkeypatch, tmp_path):
    from arenamcp import logging_config

    state, book, native = deck
    state["hand"] = [{"name": "HIDDEN-CANARY"}]
    record = SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    owner = _DeckLibraryMixin()
    owner._deck_library = {"d": record}
    owner._coach = SimpleNamespace(_cached_playbook=lambda *_: book)
    owner._study_outside_match = lambda: True
    model = writer()
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", lambda name: model)
    monkeypatch.setattr(
        "arenamcp.local_study_model.installed_models", lambda: {"fixture:model": model.digest}
    )
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)

        def request_deck_card_study(self, req):
            self.deckCardStudyChanged.emit(owner.deck_card_study(req))

    session = Session()
    dialog = DeckCardStudyDialog(session, {"deck_id": "d", "identity": book.identity})
    dialog.show()
    for n, scope in enumerate(("sequencing", "switches")):
        dialog.view.setCurrentIndex(dialog.view.findData(scope))
        assert model.complete.call_count == n and "No local study" in dialog.detail.toPlainText()
        assert not dialog.pass_button.isVisible()
        dialog.build.click()
        detail = dialog.detail.toPlainText()
        assert model.complete.call_count == n + 1 and "awaiting review" in detail
        assert all(title in detail for title in TOPICS[scope].values())
        assert book.catalog[1001]["oracle_text"] in detail and "HIDDEN-CANARY" not in detail
        dialog.build.click()
        assert model.complete.call_count == n + 1
    assert len(list((tmp_path / "cache" / "local_deck_guidance").rglob("*.json"))) == 2
    assert "study" not in book.data
    owner._study_outside_match = lambda: False
    dialog.build.click()
    assert "match" in dialog.detail.toPlainText() and model.complete.call_count == 2
    dialog.view.setCurrentIndex(dialog.view.findData("sequencing"))
    assert "Opening hand" in dialog.detail.toPlainText()
    native[1002].oracle_text = "Changed printed land rules."
    dialog.read()
    assert "No local study" in dialog.detail.toPlainText()
    dialog.close()


@pytest.mark.parametrize(
    "fault",
    [
        "stale_name",
        "missing_topic",
        "unknown_card",
        "missing_piece_evidence",
        "changed_counts",
        "wrong_commander",
        "began_match",
        "changed_print",
        "wrong_rule",
    ],
)
def test_guidance_rejects_incomplete_and_misbound_jobs(deck, tmp_path, fault):
    state, book, native = deck
    model = writer()
    normal = model.complete.side_effect

    def complete(*args, **kwargs):
        rows = normal(*args, **kwargs)
        row = rows["mana"]
        if fault == "stale_name":
            row["outcome"] = "unrecognized"
        elif fault == "missing_topic":
            rows.pop("recovery")
        elif fault == "unknown_card":
            row["evidence"][0]["card"] = 7777
        elif fault == "missing_piece_evidence":
            row["conditions"]["pieces"].append({"card": 1002, "zone": "hand"})
        elif fault == "wrong_rule":
            row["evidence"][0]["rule"] = 999
        elif fault == "changed_print":
            native[1001].oracle_text = "different print"
        return rows

    model.complete.side_effect = complete
    allowed = Mock(side_effect=[True, False]) if fault == "began_match" else lambda: True
    if fault == "changed_counts":
        state["deck_cards"].pop()
    if fault == "wrong_commander":
        state["commander_grp_ids"] = []
    with pytest.raises(ValueError):
        deck_guidance(book, state, "sequencing", tmp_path, model=model, allowed=allowed)
    assert not list(tmp_path.rglob("*.json"))
    if fault in ("changed_counts", "wrong_commander"):
        model.complete.assert_not_called()


def test_whole_deck_study_uses_only_current_native_rules_even_if_saved_strategy_prints_drift(deck, tmp_path):
    from arenamcp.deck_local_guidance import _guidance_input

    state, book, native = deck
    book.catalog[1002]["oracle_text"] = "STALE-CANARY"
    book.data["primary_plan"] = "PRIVATE-OLD-PLAN-CANARY"
    current, data = _guidance_input(book, state, "sequencing")
    encoded = json.dumps(data)
    assert "STALE-CANARY" not in encoded and "PRIVATE-OLD-PLAN-CANARY" not in encoded
    assert current.catalog[1002]["oracle_text"] == native[1002].oracle_text
    native.pop(1002)
    m = writer()
    with pytest.raises(ValueError, match="could not be verified"):
        deck_guidance(book, state, "sequencing", tmp_path, model=m, allowed=lambda: True)
    m.complete.assert_not_called()
