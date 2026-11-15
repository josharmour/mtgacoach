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


def test_assemble_exact_private_review_copy_only_when_all_local_sections_exist(
    qapp, deck, monkeypatch, tmp_path
):

    from arenamcp.deck_study import validate_study

    state, book, native = deck
    cache = tmp_path / "cache"
    weight = "d" * 64
    role = backend(book)
    role.digest = weight
    role.last_metrics = {"thinking_mode": "low", "generated_tokens": 9, "thinking_tokens": None}
    study_card(book, 1001, role, cache / "local_card_studies", allowed=lambda: True)
    chapter = writer()
    chapter.digest = weight
    chapter.last_metrics = {"thinking_mode": "low"}
    plans = cache / "local_deck_guidance"
    for scope in ("sequencing", "switches"):
        deck_guidance(book, state, scope, plans, model=chapter, allowed=lambda: True)
    links = Mock(digest=weight, last_metrics={"thinking_mode": "low"})
    links.complete.side_effect = lambda system, prompt, **kw: answer(json.loads(prompt))
    report_owner = _DeckLibraryMixin()
    selected = SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    report_owner._deck_library = {"d": selected}
    report_owner._coach = SimpleNamespace(_cached_playbook=lambda *_: book)
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    runner = Mock(side_effect=AssertionError("A review may never generate or send anything"))
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", runner)
    monkeypatch.setattr("arenamcp.local_study_model.installed_models", lambda: {"fixture": weight})

    from arenamcp.desktop.coach_session import CoachSession
    from arenamcp.pipe_adapter import PipeAdapter

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)
        request_deck_card_study = CoachSession.request_deck_card_study

    session = Session()
    pipe = PipeAdapter.__new__(PipeAdapter)
    pipe._coach = report_owner
    pipe._emit = lambda message: session.deckCardStudyChanged.emit(message)
    session._process = SimpleNamespace(send_payload=pipe._handle_deck_card_study)
    dialog = DeckCardStudyDialog(session, {"deck_id": "d", "identity": book.identity})
    dialog.show()
    dialog.view.setCurrentIndex(dialog.view.findData("report"))

    def save(digest=weight):
        return report_owner.deck_card_study(
            {
                "operation": "save_review",
                "scope": "report",
                "deck_id": "d",
                "identity": book.identity,
                "model_digest": digest,
            }
        )

    assert not dialog.save_review.isVisible() and "pending" in save()["detail"]
    assert not (cache / "local_deck_reviews").exists()
    deck_guidance(book, state, "interactions", plans, model=links, allowed=lambda: True)
    dialog.read()
    assert dialog.save_review.isVisible() and "10 of 10" in dialog.detail.toPlainText(), (
        dialog.detail.toPlainText()
    )
    assert not (cache / "local_deck_reviews").exists()
    dialog.save_review.click()
    files = list((cache / "local_deck_reviews").rglob("*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o077 == 0
    packet = json.loads(files[0].read_bytes())
    assert packet["status"] == "local_review_candidate" and packet["model_digest"] == weight
    assert packet["deck_identity"] == book.identity and len(packet["jobs"]) == 10
    assert packet["prints"]["1002"]["main_copies"] == 20
    from arenamcp.deck_local_guidance import _guidance_input

    current, _ = _guidance_input(book, state, "sequencing")
    assert packet["coverage"] == validate_study(packet["study"], current.catalog)
    assert len(packet["chapters"]["sequencing"]) == len(packet["chapters"]["switches"]) == 4
    assert packet["commander_prerequisites"]["1001"]["commander_conditions"]["play"]["command_zone_payment"]
    assert len({m["input_sha256"] for m in packet["jobs"]}) == 10 and all(
        m["thinking_mode"] == "low" for m in packet["jobs"]
    )
    assert str(files[0]) in dialog.detail.toPlainText() and "study" not in book.data
    assert not save("a" * 64)["review_ready"]
    unchanged = files[0].read_bytes()
    assert save()["review_ready"] and files[0].read_bytes() == unchanged
    from arenamcp.deck_local_guidance import topic_files
    from arenamcp.deck_local_report import study_report

    scope = "sequencing"
    _, scoped = _guidance_input(book, state, scope)
    _, _, paths = topic_files(plans, scope, scoped)
    source = paths["mana"] / (weight + ".json")
    raw = json.loads(source.read_bytes())
    source.unlink()
    alternate = source.with_name("a" * 64 + ".json")
    alternate.write_text(json.dumps({**raw, "model": "a" * 64}))
    assert not save()["review_ready"] and len(list((cache / "local_deck_reviews").rglob("*.json"))) == 1
    source.write_text(json.dumps(raw))
    absent = study_report(book, state, cache, weight, save_review=True, current_selection=lambda: False)
    assert not absent["review_ready"] and "changed" in absent["detail"]
    assert len(list((cache / "local_deck_reviews").rglob("*.json"))) == 1
    native[1002].oracle_text = "Changed current native print."
    assert not save()["review_ready"] and len(list((cache / "local_deck_reviews").rglob("*.json"))) == 1
    assert not runner.called
    dialog.close()
