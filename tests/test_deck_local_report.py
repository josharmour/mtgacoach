import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Signal
from tests.test_deck_local_guidance import writer
from tests.test_deck_local_interactions import answer
from tests.test_decks_tab import FakeSession
from tests.test_local_deck_card_study import backend

from arenamcp.deck_card_study import study_card
from arenamcp.deck_local_guidance import deck_guidance
from arenamcp.deck_local_report import local_report
from arenamcp.desktop.deck_card_study import DeckCardStudyDialog
from arenamcp.standalone_decks import _DeckLibraryMixin

pytest_plugins = ("tests.test_deck_local_guidance",)


def test_real_coverage_view_reads_current_inputs_in_one_model_without_generation(
    qapp, deck, monkeypatch, tmp_path
):
    state, book, _ = deck
    cache = tmp_path / "cache"
    a = backend(book)
    a.digest = "a" * 64
    a.last_metrics = {"generated_tokens": 7}
    study_card(book, 1001, a, cache / "local_card_studies", allowed=lambda: True)
    plans = cache / "local_deck_guidance"
    chapters = writer()
    chapters.digest = a.digest
    deck_guidance(book, state, "sequencing", plans, model=chapters, allowed=lambda: True, topic="mulligan")
    others = writer()
    others.digest = "b" * 64
    deck_guidance(book, state, "switches", plans, model=others, allowed=lambda: True, topic="counterspells")
    links = Mock(digest=a.digest, last_metrics={"generated_tokens": 9})
    links.complete.side_effect = lambda system, text, **kw: answer(json.loads(text))
    deck_guidance(book, state, "interactions", plans, model=links, allowed=lambda: True)
    owner = _DeckLibraryMixin()
    owner._deck_library = {
        "d": SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    }
    owner._coach = SimpleNamespace(_cached_playbook=lambda *_: book)
    owner._study_outside_match = lambda: False
    bootstrap = Mock(return_value={"fixture:a": a.digest, "fixture:b": others.digest})
    monkeypatch.setattr("arenamcp.local_study_model.installed_models", bootstrap)
    # A report may be read in a match without creating any model connection at all.
    runner = Mock(side_effect=AssertionError("Report opened a local generation endpoint"))
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", runner)
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)

        def request_deck_card_study(self, req):
            self.deckCardStudyChanged.emit(owner.deck_card_study(req))

    session = Session()
    dialog = DeckCardStudyDialog(session, {"deck_id": "d", "identity": book.identity})
    dialog.show()
    dialog.view.setCurrentIndex(dialog.view.findData("report"))
    assert not dialog.build.isVisible() and not dialog.pass_button.isVisible()
    text = dialog.detail.toPlainText()
    assert "Local hypotheses cover 1 of 1" in text and "Whole-deck search • complete" in text
    assert "Sequencing and recovery • 1 of 4" in text and "Opponent switches • 0 of 4" in text
    assert "Verified cached model passes • 3" in text and "31 reported generated tokens" in text
    assert "Thinking tokens unreported for all 3" in text
    dialog.models.setCurrentIndex(1)
    text = dialog.detail.toPlainText()
    assert (
        "Local hypotheses cover 0 of 1" in text
        and "Opponent switches • 1 of 4" in text
        and "Verified cached model passes • 1" in text
    )
    assert "Whole-deck search • pending" in text
    bootstrap.assert_called_once()
    runner.assert_not_called()
    denied = owner.deck_card_study(
        {"deck_id": "d", "identity": book.identity, "scope": "report", "operation": "build"}
    )
    assert "read-only" in denied["error"] and not runner.called
    dialog.close()


def test_local_coverage_withholds_missing_source_and_never_treats_missing_digest_as_zero(deck, tmp_path):
    state, book, native = deck
    assert "Choose local model weights" in local_report(book, state, tmp_path, None)
    native.pop(1002)
    text = local_report(book, state, tmp_path, "c" * 64)
    assert "Full-deck sections withheld" in text and "Whole-deck search • complete" not in text
    with pytest.raises(ValueError, match="weights identity"):
        local_report(book, state, tmp_path, "../wrong")
