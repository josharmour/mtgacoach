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

        answer = {
            "topics": rows,
            "read_receipt": " ".join(
                [
                    payload["beginning_receipt"],
                    *(p["receipt"] for p in payload["prints"]),
                    payload["ending_receipt"],
                ]
            ),
        }
        assert kwargs["verify_full_input"] and Draft202012Validator(kwargs["response_format"]).is_valid(
            answer
        )
        return answer

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
        assert model.complete.call_count == 4 * n and "No local study" in dialog.detail.toPlainText()
        if n == 0:
            dialog.topic.setCurrentIndex(dialog.topic.findData("mulligan"))
            dialog.build.click()
            assert model.complete.call_count == 1 and "Partial study • 1 of 4" in dialog.detail.toPlainText()
            args = model.complete.call_args
            assert json.loads(args.args[1])["topics"] == ["mulligan"] and args.kwargs["max_tokens"] == 2048
            dialog.topic.setCurrentIndex(0)
        assert not dialog.pass_button.isVisible()
        dialog.build.click()
        detail = dialog.detail.toPlainText()
        assert model.complete.call_count == 4 * (n + 1) and "awaiting review" in detail
        assert all(title in detail for title in TOPICS[scope].values())
        assert book.catalog[1001]["oracle_text"] in detail and "HIDDEN-CANARY" not in detail
        dialog.build.click()
        assert model.complete.call_count == 4 * (n + 1)
    assert len(list((tmp_path / "cache" / "local_deck_guidance").rglob("*.json"))) == 8
    assert "study" not in book.data
    owner._study_outside_match = lambda: False
    dialog.build.click()
    assert "match" in dialog.detail.toPlainText() and model.complete.call_count == 8
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
        answer = normal(*args, **kwargs)
        rows = answer["topics"]
        row = rows["mana"]
        if fault == "stale_name":
            row["outcome"] = "unrecognized"
        elif fault == "missing_topic":
            rows.pop("mana")
        elif fault == "unknown_card":
            row["evidence"][0]["card"] = 7777
        elif fault == "missing_piece_evidence":
            row["conditions"]["pieces"].append({"card": 1002, "zone": "hand"})
        elif fault == "wrong_rule":
            row["evidence"][0]["rule"] = 999
        elif fault == "changed_print":
            native[1001].oracle_text = "different print"
        return answer

    model.complete.side_effect = complete
    allowed = Mock(side_effect=[True, False]) if fault == "began_match" else lambda: True
    if fault == "changed_counts":
        state["deck_cards"].pop()
    if fault == "wrong_commander":
        state["commander_grp_ids"] = []
    with pytest.raises(ValueError):
        deck_guidance(book, state, "sequencing", tmp_path, model=model, allowed=allowed, topic="mana")
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


@pytest.mark.parametrize("fault", ["missing_receipt", "reversed_receipts", "bool_receipt", "loyalty"])
def test_complete_input_and_scoped_loyalty_permissions_are_required(deck, tmp_path, fault):
    state, book, native = deck
    native[1001].type_line = "Planeswalker — Signal"
    native[1001].oracle_text = "+1: Scry 1.\n−2: Draw a card."
    native[
        1002
    ].oracle_text = "You may activate loyalty abilities of planeswalkers you control twice each turn."
    m = writer()
    normal = m.complete.side_effect

    def corrupt(*args, **kwargs):
        answer = normal(*args, **kwargs)
        if fault == "loyalty":
            answer["topics"]["fast_combo"]["advice"] = "Use +1 then −2 in the same turn."
        elif fault == "missing_receipt":
            answer["read_receipt"] = answer["read_receipt"][6:]
        elif fault == "reversed_receipts":
            answer["read_receipt"] = " ".join(reversed(answer["read_receipt"].split()))
        else:
            answer["read_receipt"] = True
        return answer

    m.complete.side_effect = corrupt
    with pytest.raises(ValueError, match="loyalty twice" if fault == "loyalty" else "complete deck input"):
        deck_guidance(book, state, "switches", tmp_path, model=m, allowed=lambda: True)
    assert not list(tmp_path.rglob("*.json"))


def test_letter_receipts_cover_every_print_and_stay_outside_the_output_grammar():
    import re

    from arenamcp.deck_local_guidance import acknowledged_answer

    context = {"prints": {str(gid): {"card": gid} for gid in range(1001, 1094)}}
    seen = set()

    def respond(system, prompt, **kw):
        payload = json.loads(prompt)
        words = [
            payload["beginning_receipt"],
            *(v["receipt"] for v in payload["prints"]),
            payload["ending_receipt"],
        ]
        assert len(words) == len(set(words)) == 95
        assert all(re.fullmatch(r"[a-zA-Z]{5}", w) for w in words)
        receipt = " ".join(words)
        assert kw["response_format"]["properties"]["read_receipt"] == {
            "type": "string",
            "minLength": len(receipt),
            "maxLength": len(receipt),
        }
        assert receipt not in seen
        seen.add(receipt)
        return {"read_receipt": receipt, "result": {}}

    model = Mock()
    model.complete.side_effect = respond
    for _ in range(2):
        assert acknowledged_answer(model, "study", context, "result", {"type": "object"}, lambda: True) == {}


@pytest.mark.parametrize(("can_disable", "diagnostic"), [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("fault", [None, "capacity", "count"])
def test_full_deck_transport_reserves_input_and_output_and_requires_provider_receipt(
    monkeypatch, fault, can_disable, diagnostic
):
    import io

    import arenamcp.local_study_model as local

    m = local.LocalStudyModel.__new__(local.LocalStudyModel)
    m.name, m.digest, m.thinks = "test", "a" * 64, "low"
    m.can_disable_thinking = can_disable
    m.diagnostic_without_thinking = diagnostic
    m.context_length = 64 if fault == "capacity" else 8192
    m.template_bytes = 11
    monkeypatch.setattr(local, "installed_models", lambda: {m.name: m.digest})
    conn = Mock()
    packet = {
        "model": m.name,
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": None if fault == "count" else 24,
        "message": {"role": "assistant", "content": '{"ok":true}'},
    }
    stream = io.BytesIO(json.dumps(packet).encode())
    stream.status = 200
    conn.getresponse.return_value = stream
    monkeypatch.setattr(local.http.client, "HTTPConnection", Mock(return_value=conn))
    if fault:
        with pytest.raises(ValueError, match="cannot verify room|prompt count"):
            m.complete("rules", "deck", allowed=lambda: True, verify_full_input=True, max_tokens=128)
        if fault == "capacity":
            conn.request.assert_not_called()
    else:
        assert m.complete("rules", "deck", allowed=lambda: True, verify_full_input=True, max_tokens=128) == {
            "ok": True
        }
        sent = json.loads(conn.request.call_args.args[2])
        assert sent["think"] == (False if diagnostic else "low")
        assert m.last_metrics["thinking_mode"] == ("disabled" if diagnostic else "low")
        opts = sent["options"]
        assert opts["num_ctx"] >= len(b"rulesdeck") + 128 + 11 and opts["num_predict"] == 128


def test_partial_chapters_never_assemble_mixed_weights_and_resume_only_missing_topics(deck, tmp_path):
    state, book, _ = deck
    a, b = writer(), writer()
    b.digest = "b" * 64
    for model, scope in ((a, "mulligan"), (b, "mana")):
        detail, _ = deck_guidance(
            book, state, "sequencing", tmp_path, model=model, allowed=lambda: True, topic=scope
        )
        assert "Partial study • 1 of 4" in detail
    assert "Partial study • 1 of 4" in deck_guidance(book, state, "sequencing", tmp_path)[0]
    chosen = deck_guidance(book, state, "sequencing", tmp_path, topic="mulligan")[0]
    assert a.digest[:12] in chosen and "Opening hand and mulligan • not yet studied" not in chosen
    normal = a.complete.side_effect

    def broken(system, prompt, **kwargs):
        if json.loads(prompt)["topics"] == ["mana"]:
            raise ValueError("simulated truncation")
        return normal(system, prompt, **kwargs)

    a.complete.side_effect = broken
    with pytest.raises(ValueError, match="simulated truncation"):
        deck_guidance(book, state, "sequencing", tmp_path, model=a, allowed=lambda: True)
    detail, _ = deck_guidance(book, state, "sequencing", tmp_path)
    assert "Partial study • 2 of 4" in detail and "Land and colour plan • not yet studied" in detail
    made = a.complete.call_count
    a.complete.side_effect = normal
    detail, reused = deck_guidance(book, state, "sequencing", tmp_path, model=a, allowed=lambda: True)
    assert not reused and "All four topics" in detail and a.complete.call_count == made + 2
    assert all(c.kwargs["max_tokens"] == 2048 for c in a.complete.call_args_list)
    assert "All four topics" in deck_guidance(book, state, "sequencing", tmp_path)[0]
    assert (
        "Partial study • 1 of 4"
        in deck_guidance(book, state, "sequencing", tmp_path, model=b, allowed=lambda: True, topic="mana")[0]
    )


def test_prior_valid_complete_cache_remains_readable_with_no_model_request(deck, tmp_path):
    import hashlib

    from tests.test_deck_study import evidence

    from arenamcp.deck_local_guidance import _SYSTEM, _guidance_input

    state, book, _ = deck
    current, context = _guidance_input(book, state, "sequencing")
    digest_input = json.dumps([1, _SYSTEM, "sequencing", context], sort_keys=True, allow_nan=False)
    folder = tmp_path / hashlib.sha256(digest_input.encode()).hexdigest()
    folder.mkdir()
    m = writer()
    entry = {
        "advice": "Preserve the printed ability.",
        "conditions": conditions(1001),
        "evidence": [evidence(1001, current.catalog)],
    }
    rows = {key: deepcopy(entry) for key in TOPICS["sequencing"]}
    (folder / (m.digest + ".json")).write_text(
        json.dumps({"input": folder.name, "model": m.digest, "row": rows})
    )
    detail, reused = deck_guidance(
        book, state, "sequencing", tmp_path, model=m, allowed=lambda: True, topic="recovery"
    )
    assert reused and "All four topics" in detail and not m.complete.called
    rows.pop("mana")
    (folder / (m.digest + ".json")).write_text(
        json.dumps({"input": folder.name, "model": m.digest, "row": rows})
    )
    assert "No local study" in deck_guidance(book, state, "sequencing", tmp_path)[0]


def test_no_thinking_diagnostic_is_isolated_and_cannot_complete_a_normal_chapter(deck, tmp_path):
    from shutil import copyfile

    from arenamcp.deck_local_guidance import _guidance_input, topic_coverage, topic_files

    state, book, _ = deck
    model = writer()
    model.diagnostic_without_thinking = True
    model.last_metrics["thinking_mode"] = "disabled"
    for scope, topic in (("sequencing", None), ("interactions", "discovery")):
        with pytest.raises(ValueError, match="single selected"):
            deck_guidance(book, state, scope, tmp_path, model=model, allowed=lambda: True, topic=topic)

    def call():
        return deck_guidance(
            book, state, "sequencing", tmp_path, model=model, allowed=lambda: True, topic="sequencing"
        )

    detail, reused = call()
    assert "Diagnostic · thinking disabled" in detail and "Partial study • 1 of 4" in detail and not reused
    assert call()[1] and model.complete.call_count == 1
    current, context = _guidance_input(book, state, "sequencing")
    assert topic_coverage(current, context, "sequencing", tmp_path, model.digest) == ([], [])
    assert "No local study" in deck_guidance(book, state, "sequencing", tmp_path)[0]
    diag = next((tmp_path / "diagnostic_without_thinking").rglob("*.json"))
    raw = json.loads(diag.read_text())
    assert raw["metrics"]["thinking_mode"] == "disabled"
    _, _, normal = topic_files(tmp_path, "sequencing", context)
    destination = normal["sequencing"] / diag.name
    destination.parent.mkdir()
    copyfile(diag, destination)
    assert not topic_coverage(current, context, "sequencing", tmp_path, model.digest)[0]
    raw["input"] = destination.parent.name
    destination.write_text(json.dumps(raw))
    assert not topic_coverage(current, context, "sequencing", tmp_path, model.digest)[0]
    model.diagnostic_without_thinking = False
    model.last_metrics["thinking_mode"] = "low"
    detail, reused = call()
    assert "Diagnostic" not in detail and not reused
    assert (
        topic_coverage(current, context, "sequencing", tmp_path, model.digest)[1][0]["thinking_mode"] == "low"
    )
