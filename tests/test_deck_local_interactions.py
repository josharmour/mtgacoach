import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Signal
from tests.deck_fakes import deck_case
from tests.test_deck_study import conditions, native_catalog
from tests.test_decks_tab import FakeSession

from arenamcp.deck_local_guidance import deck_guidance
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity
from arenamcp.desktop.deck_card_study import DeckCardStudyDialog
from arenamcp.standalone_decks import _DeckLibraryMixin

EDGES = {(1001, 2001, "payoff"), (2001, 2002, "enabler"), (2003, 2004, "payoff")}


@pytest.fixture
def fixture(monkeypatch):
    state, cards, data = deck_case()
    for gid in (2001, 2002, 2003, 2004):
        cards[gid] = {
            "name": f"Fixture filter {gid}",
            "type_line": "Artifact",
            "mana_cost": "{1}",
            "oracle_text": "{T}: Scry 1.",
        }
        state["deck_cards"].append(gid)
    native = native_catalog(monkeypatch, cards)
    book = DeckPlaybook.parse(json.dumps(data), cards, commander_ids(state), deck_identity(state))
    monkeypatch.setattr(
        "arenamcp.deck_local_interactions._reviewed_relationships", lambda catalog: (EDGES, 5)
    )
    return state, book, native


def answer(prompt, fault=None):
    prints = prompt["prints"]
    refs = [{"card": x["card"], "rule": 1} for x in prints]
    edges = prompt["reviewed_edges"]
    ids = [r["card"] for r in refs]
    if prompt["mode"] == "discovery":
        ids = [1001, 1002] if fault != "duplicate" else [1001, 2001, 2002]
    entry = {
        "kind": "synergy",
        "conditions": conditions(*ids),
        "outcome": "Preserve the printed resource.",
        "stops": "A required permanent leaves the battlefield.",
        "evidence": [{"card": g, "rule": 1} for g in ids],
        "links": [e["index"] for e in edges],
    }
    result = {"interactions": [entry], "minor_links": []}
    if fault == "absent_edge":
        entry["links"] = []
    if fault == "foreign_edge":
        entry["links"] = [99]
    if fault == "missing_piece":
        entry["evidence"].pop()
    if fault == "private_text":
        result["finding"] = "unbound free prose"
    if fault == "minor":
        result = {
            "interactions": [],
            "minor_links": [
                {"link": e["index"], "reason": "This printed connection is incidental in the fixture."}
                for e in edges
            ],
        }
    read = [prompt["beginning_receipt"], *[p["receipt"] for p in prints], prompt["ending_receipt"]]
    return {"result": result, "read_receipt": " ".join(read if fault != "stale_receipt" else reversed(read))}


def writer():
    m = Mock(digest="a" * 64, last_metrics={"generated_tokens": 25, "thinking_tokens": None})
    m.complete.side_effect = lambda system, prompt, **kw: answer(json.loads(prompt))
    return m


def test_real_chooser_runs_each_cluster_then_full_discovery_and_reuses_the_same_weights(
    qapp, fixture, monkeypatch, tmp_path
):
    state, book, _ = fixture
    owner = _DeckLibraryMixin()
    owner._deck_library = {
        "d": SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    }
    owner._coach = SimpleNamespace(_cached_playbook=lambda *args: book)
    owner._study_outside_match = lambda: True
    model = writer()
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", lambda name: model)
    monkeypatch.setattr("arenamcp.local_study_model.installed_models", lambda: {"fixture": model.digest})
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)

        def request_deck_card_study(self, req):
            self.deckCardStudyChanged.emit(owner.deck_card_study(req))

    session = Session()
    dialog = DeckCardStudyDialog(session, {"deck_id": "d", "identity": book.identity})
    dialog.show()
    dialog.view.setCurrentIndex(dialog.view.findData("interactions"))
    assert not model.complete.called and "No complete local" in dialog.detail.toPlainText()
    dialog.build.click()
    calls = [json.loads(c.args[1]) for c in model.complete.call_args_list]
    assert [c["mode"] for c in calls] == ["cluster", "cluster", "discovery"]
    assert [len(c["prints"]) for c in calls] == [3, 2, 6]
    assert len(calls[-1]["previously_studied_pieces_and_rules"]) == 2
    text = dialog.detail.toPlainText()
    assert "3 of 3 reviewed links" in text and "3 synergies" in text and "1 additional" in text
    assert "75 reported generated tokens across 3 of 3" in text and "0 of 3 completed passes" in text
    dialog.build.click()
    dialog.read()
    assert model.complete.call_count == 3 and dialog.detail.toPlainText() == text
    assert "study" not in book.data
    dialog.close()


@pytest.mark.parametrize(
    "fault",
    ["absent_edge", "foreign_edge", "missing_piece", "duplicate", "stale_receipt", "private_text", "minor"],
)
def test_reviewed_edges_and_each_candidate_must_have_current_source_bound_coverage(fixture, tmp_path, fault):
    state, book, _ = fixture
    m = writer()

    def complete(system, text, **kw):
        prompt = json.loads(text)
        mode = prompt["mode"]
        active = (
            fault
            if (mode == "cluster" and fault != "duplicate") or (mode == "discovery" and fault == "duplicate")
            else None
        )
        return answer(prompt, active)

    m.complete.side_effect = complete
    if fault == "minor":
        detail, _ = deck_guidance(book, state, "interactions", tmp_path, model=m, allowed=lambda: True)
        assert "3 marked minor" in detail and "1 additional" in detail
        return
    with pytest.raises(ValueError):
        deck_guidance(book, state, "interactions", tmp_path, model=m, allowed=lambda: True)
    detail, _ = deck_guidance(book, state, "interactions", tmp_path)
    assert "No complete local" in detail


def test_failed_discovery_and_small_edits_reuse_only_unchanged_complete_clusters(
    fixture, monkeypatch, tmp_path
):
    state, book, native = fixture
    m = writer()
    once = True

    def complete(system, text, **kw):
        nonlocal once
        prompt = json.loads(text)
        if once and prompt["mode"] == "discovery":
            once = False
            raise ValueError("synthetic interruption")
        return answer(prompt)

    m.complete.side_effect = complete
    with pytest.raises(ValueError):
        deck_guidance(book, state, "interactions", tmp_path, model=m, allowed=lambda: True)
    assert m.complete.call_count == 3
    detail, _ = deck_guidance(book, state, "interactions", tmp_path, model=m, allowed=lambda: True)
    assert "3 synergies" in detail and m.complete.call_count == 4
    state["deck_cards"].append(2004)
    book.identity = deck_identity(state)
    detail, _ = deck_guidance(book, state, "interactions", tmp_path, model=m, allowed=lambda: True)
    assert "3 synergies" in detail and m.complete.call_count == 6
    assert [json.loads(c.args[1])["mode"] for c in m.complete.call_args_list[-2:]] == ["cluster", "discovery"]
    # New native facts also change the corresponding job. Read-only cannot use the stale finished set.
    native[2001].oracle_text = "Changed native condition."
    assert "No complete local" in deck_guidance(book, state, "interactions", tmp_path)[0]


def test_normal_pipe_stop_is_bound_to_the_exact_request_and_prevents_the_next_pass(
    fixture, tmp_path, monkeypatch
):
    import threading

    from arenamcp.pipe_adapter import PipeAdapter

    state, book, _ = fixture
    owner = _DeckLibraryMixin()
    owner._deck_library = {
        "d": SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    }
    owner._coach = SimpleNamespace(_cached_playbook=lambda *args: book)
    owner._study_outside_match = lambda: True
    m = writer()
    started, resume, done = threading.Event(), threading.Event(), threading.Event()

    def wait_for_cancel(system, text, **kwargs):
        started.set()
        assert resume.wait(3)
        if not kwargs["allowed"]():
            raise ValueError("cancelled")
        return answer(json.loads(text))

    m.complete.side_effect = wait_for_cancel
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", lambda name: m)
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    pipe = PipeAdapter()
    pipe._coach = owner
    replies = []

    def emit(payload):
        replies.append(payload)
        done.set()

    pipe._emit = emit
    task = {
        "deck_id": "d",
        "identity": book.identity,
        "token": "scenario-1",
        "scope": "interactions",
        "operation": "build",
        "model": "fixture",
        "card": 1001,
    }
    pipe._start_deck_card_study(task)
    assert started.wait(3)
    pipe._start_deck_card_study({**task, "token": "unrelated", "operation": "cancel"})
    assert not next(iter(pipe._interaction_jobs.values())).is_set()
    pipe._start_deck_card_study({**task, "operation": "cancel"})
    assert next(iter(pipe._interaction_jobs.values())).is_set()
    resume.set()
    assert done.wait(3)
    assert "stopped" in replies[0]["error"] and not pipe._interaction_jobs
    assert m.complete.call_count == 1 and not list(tmp_path.rglob("*.json"))
