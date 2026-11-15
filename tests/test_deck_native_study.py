import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Signal
from tests.deck_fakes import deck_case
from tests.test_deck_study import native_catalog
from tests.test_decks_tab import FakeSession

from arenamcp.deck_library import DeckRecord
from arenamcp.deck_local_guidance import printed_study_deck
from arenamcp.desktop.decks_tab import DecksTab
from arenamcp.standalone_decks import _DeckLibraryMixin


def test_native_selected_deck_studies_current_prints_without_a_saved_strategy(qapp, monkeypatch, tmp_path):
    _state, catalog, _data = deck_case()
    native = native_catalog(monkeypatch, catalog)
    native[2001] = SimpleNamespace(**{**vars(native[1001]), "grp_id": 2001, "name": "Synthetic spark"})
    record = DeckRecord("new", "PRIVATE DECK TITLE", main=[(1002, 20), (2001, 3)], command_zone=[1001])
    owner = _DeckLibraryMixin()
    coach = Mock()
    coach._cached_playbook.return_value = None
    owner._coach, owner._deck_library = coach, {record.deck_id: record}
    owner._study_outside_match = lambda: True
    model = Mock(digest="d" * 64, last_metrics={"thinking_mode": "low", "generated_tokens": 12})
    captured = []

    def complete(_system, prompt, **kwargs):
        job = json.loads(prompt)
        captured.append(job)
        assert job["native_main_profile"]["main_copies"] == 23
        assert job["native_main_profile"]["main_lands"] == 20
        assert job["commanders"] == [1001] and job["card"] == 2001
        assert not job["plan"] and not job["mechanisms"] and "PRIVATE DECK TITLE" not in prompt
        ref = [{"card": 2001, "rule": 1}]
        action = {
            "advice": "Preserve this creature for its printed effect.",
            "conditions": {
                "pieces": [{"card": 2001, "zone": "battlefield"}],
                "mana": {"generic": 0},
                "first_turn": 1,
            },
        }
        return {
            "card": 2001,
            "role": "A source of creature tokens.",
            "play": action,
            "hold": action,
            "avoid": action,
            "evidence": ref,
        }

    model.complete.side_effect = complete
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", lambda _: model)
    monkeypatch.setattr("arenamcp.local_study_model.installed_models", lambda: {"fake": model.digest})
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)

        def request_deck_card_study(self, req):
            self.deckCardStudyChanged.emit(owner.deck_card_study(req))

    tab = DecksTab(Session())
    row = {
        "deck_id": "new",
        "identity": record.identity(),
        "name": record.name,
        "complete": True,
        "strategy": {"status": "none"},
    }
    tab.set_decks([row])
    if tab._table is not None:
        tab._table.selectRow(0)
    else:
        tab._deck_buttons["new"].click()
    assert tab.card_study_btn.isEnabled()
    tab.card_study_btn.click()
    dialog = tab._card_study
    dialog.cards.setCurrentIndex(dialog.cards.findData(2001))
    assert "Current Arena prints" in dialog.detail.toPlainText() and not captured
    dialog.build.click()
    assert len(captured) == 1 and "Cited printed rules" in dialog.detail.toPlainText()
    dialog.build.click()
    assert len(captured) == 1
    dialog.view.setCurrentIndex(dialog.view.findData("report"))
    text = dialog.detail.toPlainText()
    assert "No saved deck strategy" in text and "Local hypotheses cover 1 of 2" in text
    assert {c[0] for c in coach.mock_calls} == {"_cached_playbook"}
    tab.set_decks([{**row, "complete": False, "identity": ""}])
    assert not tab.card_study_btn.isEnabled()
    assert not list(tmp_path.glob("deck_strategies/*"))
    tab.close()


@pytest.mark.parametrize(
    "fault",
    [
        "changed_count",
        "sentinel",
        "bool",
        "duplicate_commander",
        "overlap",
        "missing_print",
        "native_alias",
        "saved_commander",
    ],
)
def test_native_baseline_withholds_invalid_selection_before_local_model(monkeypatch, fault):
    _, catalog, _ = deck_case()
    native = native_catalog(monkeypatch, catalog)
    record = DeckRecord("new", "Synthetic", main=[(1002, 20)], command_zone=[1001])
    state = record.game_state()
    identity = record.identity()
    book = None
    if fault == "changed_count":
        state["deck_cards"].pop()
    elif fault == "sentinel":
        state["deck_cards"].append(3)
    elif fault == "bool":
        state["commander_grp_ids"].append(True)
    elif fault == "duplicate_commander":
        state["commander_grp_ids"].append(1001)
    elif fault == "overlap":
        state["deck_cards"].append(1001)
        from arenamcp.deck_strategy import deck_identity

        identity = deck_identity(state)
    elif fault == "missing_print":
        native.pop(1002)
    elif fault == "native_alias":
        native[1002].grp_id = 9999
    else:
        book = printed_study_deck(deepcopy(state), identity)
        book.commanders = [1002]
    owner = _DeckLibraryMixin()
    owner._coach = SimpleNamespace(_cached_playbook=lambda *_: book)
    owner._deck_library = {
        "new": SimpleNamespace(complete=True, identity=lambda: identity, game_state=lambda: state)
    }
    unexpected = Mock(side_effect=AssertionError("Accessed inference for invalid deck"))
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", unexpected)
    result = owner.deck_card_study(
        {"deck_id": "new", "identity": identity, "operation": "build", "card": 1001}
    )
    assert "error" in result and not unexpected.called
