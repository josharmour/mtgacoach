import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Signal
from tests.deck_fakes import deck_case
from tests.test_deck_study import card_rule, native_catalog
from tests.test_decks_tab import FakeSession

from arenamcp.deck_card_study import read_card, study_card
from arenamcp.deck_strategy import DeckPlaybook, commander_ids, deck_identity
from arenamcp.desktop.decks_tab import DecksTab
from arenamcp.local_study_model import LocalStudyModel, installed_models
from arenamcp.standalone_decks import _DeckLibraryMixin


@pytest.fixture
def sample(monkeypatch):
    state, catalog, data = deck_case()
    native = native_catalog(monkeypatch, catalog)
    book = DeckPlaybook.parse(json.dumps(data), catalog, commander_ids(state), deck_identity(state))
    return state, book, native


def model_row(book, gid):
    row = card_rule(gid, book.catalog)
    row["evidence"] = [{"card": gid, "rule": 1}]
    if gid in book.commanders:
        row["action_rules"] = {name: list(row["evidence"]) for name in ("play", "hold", "avoid")}
        row["play"]["conditions"] = {
            "pieces": [{"card": gid, "zone": "command"}],
            "mana": {"generic": 3, "G": 2},
            "first_turn": 5,
        }
    return row


def backend(book):
    model = Mock(digest="b" * 64)
    model.complete.return_value = model_row(book, 1001)
    return model


def test_local_card_study_cache_reuse_and_context_invalidation(sample, tmp_path):
    state, book, _ = sample
    model = backend(book)
    first, hit = study_card(book, 1001, model, tmp_path, allowed=lambda: True)
    assert not hit and len(first["evidence"][0]["source"]) == 64
    assert "Local model advice" in read_card(book, 1001, tmp_path)
    assert len(list(tmp_path.rglob("*.json"))) == 1 and "study" not in book.data
    assert "match_id" not in model.complete.call_args.args[1]
    # Changing an unrelated card while keeping the focal plan and every
    # supplied print unchanged may reuse this local card hypothesis.
    extra = deepcopy(book)
    extra.identity = "another-list"
    extra.catalog[33591] = {**book.catalog[1002], "name": "Synthetic Plain"}
    assert study_card(extra, 1001, model, tmp_path, allowed=lambda: True) == (first, True)
    model.complete.assert_called_once()
    book.data["primary_plan"] = "Different local deck plan"
    assert "Local model advice" not in read_card(book, 1001, tmp_path)
    assert not study_card(book, 1001, model, tmp_path, allowed=lambda: True)[1]


@pytest.mark.parametrize(
    "fault",
    ["stale_print", "another_card", "bad_rule", "missing_piece", "inactive", "began_match", "malformed"],
)
def test_refused_card_pass_never_replaces_cache(sample, tmp_path, fault):
    _, book, native = sample
    model = backend(book)
    row = model.complete.return_value
    if fault == "stale_print":
        native[1001].oracle_text = "Different native print"
    if fault == "another_card":
        row["card"] = 1002
    if fault == "bad_rule":
        row["evidence"][0]["rule"] = 999
    if fault == "missing_piece":
        row["play"]["conditions"]["pieces"].append({"card": 1002, "zone": "battlefield"})
    if fault == "malformed":
        row["hold"] = "unbound"
    if fault == "began_match":
        allowed = Mock(side_effect=[True, False])
    else:

        def allowed():
            return fault != "inactive"

    with pytest.raises(ValueError):
        study_card(book, 1001, model, tmp_path, allowed=allowed)
    assert not list(tmp_path.rglob("*.json"))
    if fault in ("stale_print", "inactive"):
        model.complete.assert_not_called()


def test_real_decks_button_uses_saved_playbook_and_only_studies_on_click(qapp, sample, monkeypatch, tmp_path):
    from arenamcp import logging_config
    from arenamcp.desktop.coach_session import CoachSession
    from arenamcp.pipe_adapter import PipeAdapter

    state, book, _ = sample
    record = SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    owner = _DeckLibraryMixin()
    owner._deck_library = {"d": record}
    owner._coach = SimpleNamespace(_cached_playbook=lambda identity, state: book)
    owner._study_outside_match = lambda: True
    model = backend(book)
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", lambda _: model)
    monkeypatch.setattr(
        "arenamcp.local_study_model.installed_models", lambda: {"fixture:small": model.digest}
    )
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)
        request_deck_card_study = CoachSession.request_deck_card_study

    session = Session()
    pipe = PipeAdapter.__new__(PipeAdapter)
    pipe._coach = owner
    pipe._emit = lambda event: session.deckCardStudyChanged.emit(event)
    session._process = SimpleNamespace(send_payload=pipe._handle_deck_card_study)
    tab = DecksTab(session)
    tab.set_decks(
        [
            {
                "deck_id": "d",
                "identity": book.identity,
                "name": "Local sample",
                "complete": True,
                "strategy": {"status": "ready"},
            }
        ]
    )
    if tab._table is not None:
        tab._table.selectRow(0)
    else:
        tab._deck_buttons["d"].click()
    tab.card_study_btn.click()
    dialog = tab._card_study
    assert "Saved playbook role" in dialog.detail.toPlainText()
    assert "Saved playbook mechanism" in dialog.detail.toPlainText() and dialog.build.isEnabled()
    model.complete.assert_not_called()
    owner._study_outside_match = lambda: False
    dialog.build.click()
    assert "match" in dialog.detail.toPlainText() and not model.complete.called
    owner._study_outside_match = lambda: True
    dialog.build.click()
    assert "Cited printed rules" in dialog.detail.toPlainText()
    model.complete.assert_called_once()
    old = dialog.detail.toPlainText()
    dialog.received(
        {"token": "stale", "deck_id": "d", "identity": book.identity, "card": 1001, "detail": "OTHER"}
    )
    assert dialog.detail.toPlainText() == old
    tab.set_decks([{"deck_id": "d", "identity": "new", "complete": True, "strategy": {"status": "none"}}])
    assert tab._card_study is None
    tab.close()


def test_never_select_remote_uninstalled_or_ambiguous_model(monkeypatch):
    import arenamcp.local_study_model as local

    row = {"name": "unit:local", "digest": "a" * 64, "size": 9_000_000, "details": {"format": "gguf"}}
    rows = [
        row,
        {**row, "name": "cloud:latest"},
        {**row, "name": "alias:v1", "remote_host": "outside"},
        {**row, "name": "malformed:v1", "size": True},
        {**row, "name": "../unit"},
        {**row, "name": "small", "size": 28},
    ]
    show = {
        "details": {"format": "gguf"},
        "model_info": {"general.parameter_count": 10000},
        "capabilities": ["completion"],
    }
    req = Mock(side_effect=lambda route, body=None: {"models": rows} if route == "/api/tags" else show)
    monkeypatch.setattr(local, "_request", req)
    assert installed_models() == {"unit:local": "a" * 64}
    for name in ["cloud:latest", "absent:local", "alias:v1"]:
        with pytest.raises(ValueError):
            LocalStudyModel(name)
    show["capabilities"].append("thinking")
    show["thinking"] = {"values": [False, "low", "medium", "xhigh"], "default": "medium"}
    model = LocalStudyModel("unit:local")
    assert model.thinks == "low" and model.can_disable_thinking and not model.diagnostic_without_thinking
    assert LocalStudyModel("unit:local", diagnostic_without_thinking=True).diagnostic_without_thinking
    show["thinking"]["values"] = [0, "low"]
    assert not LocalStudyModel("unit:local").can_disable_thinking
    with pytest.raises(ValueError, match="explicitly disable"):
        LocalStudyModel("unit:local", diagnostic_without_thinking=True)
    show["remote_model"] = "remote:large"
    with pytest.raises(ValueError):
        LocalStudyModel("unit:local")
    assert all(call.args[0] in ("/api/tags", "/api/show") for call in req.call_args_list)


@pytest.mark.parametrize("reason", ["stop", "length", "unrecognized"])
def test_local_stream_contract_has_a_fixed_transport_and_explicit_terminal_reason(monkeypatch, reason):
    import io

    import arenamcp.local_study_model as local

    model = LocalStudyModel.__new__(LocalStudyModel)
    model.name = "tiny:fixture"
    model.digest = "a" * 64
    model.thinks = True
    monkeypatch.setattr(local, "installed_models", lambda: {model.name: model.digest})
    monkeypatch.setenv("HTTP_PROXY", "http://synthetic.invalid")
    stream = io.BytesIO(
        (
            json.dumps(
                {
                    "model": model.name,
                    "done": False,
                    "message": {
                        "role": "assistant",
                        "thinking": "private thinking",
                        "content": '{"ok":true}',
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "model": model.name,
                    "done": True,
                    "done_reason": reason,
                    "eval_count": 11,
                    "message": {"content": ""},
                }
            )
            + "\n"
        ).encode()
    )
    stream.status = 200
    client = Mock()
    client.getresponse.return_value = stream
    factory = Mock(return_value=client)
    monkeypatch.setattr(local.http.client, "HTTPConnection", factory)
    if reason == "stop":
        assert model.complete("private system", "private card", allowed=lambda: True) == {"ok": True}
        assert model.last_metrics["ended"] == "stop" and model.last_metrics["generated_tokens"] == 11
        assert model.last_metrics["thinking_tokens"] is None and model.last_metrics["cancelled"] is False
        assert type(model.last_metrics["seconds"]) is float
    else:
        with pytest.raises(ValueError, match="token limit" if reason == "length" else "complete output"):
            model.complete("private system", "private card", allowed=lambda: True)
    assert factory.call_args.args == ("127.0.0.1", 11434)
    method, path, body, headers = client.request.call_args.args
    assert (method, path) == ("POST", "/api/chat")
    request = json.loads(body)
    assert request["model"] == model.name and request["options"]["num_predict"] == 4096
    assert "Authorization" not in headers and "proxy" not in headers
    client.close.assert_called_once()


def test_fixed_transport_rejects_redirect_before_reading_any_body(monkeypatch):
    import arenamcp.local_study_model as local

    conn = Mock()
    response = conn.getresponse.return_value
    response.status = 307
    factory = Mock(return_value=conn)
    monkeypatch.setattr(local.http.client, "HTTPConnection", factory)
    with pytest.raises(ValueError, match="declined"):
        local._request("/api/tags")
    factory.assert_called_once_with("127.0.0.1", 11434, timeout=5)
    response.read.assert_not_called()
    conn.close.assert_called_once()


def test_old_deck_analysis_cannot_overwrite_new_list_same_deck_id(qapp):
    tab = DecksTab(FakeSession())
    tab.set_decks([{"deck_id": "d", "identity": "updated", "complete": True, "strategy": {"status": "none"}}])
    tab.on_analysis({"deck_id": "d", "identity": "prior", "status": "ready", "report": {"summary": "stale"}})
    assert tab._decks[0]["strategy"]["status"] == "none" and "d" not in tab._reports
    tab.close()


def test_local_writer_constrains_every_numeric_card_and_rule_reference(sample, tmp_path):
    from jsonschema import Draft202012Validator

    _, book, _ = sample
    writer = backend(book)
    study_card(book, 1001, writer, tmp_path, allowed=lambda: True)
    call = writer.complete.call_args
    prints = json.loads(call.args[1])["prints"]
    assert all(type(row["card"]) is int for row in prints)
    validator = Draft202012Validator(call.kwargs["response_format"])
    valid = writer.complete.return_value
    assert validator.is_valid(valid)
    for ref in (
        {"card": "1001", "rule": 1},
        {"card": 1001, "rule": "1"},
        {"card": 999999, "rule": 1},
        {"card": 1001, "rule": 0},
        {"card": 1001, "rule": 999},
    ):
        assert not validator.is_valid({**valid, "evidence": [ref]})
    assert not validator.is_valid({**valid, "card": 1002})


def test_reuse_reports_the_requested_model_and_its_metrics(sample, tmp_path, monkeypatch):
    state, book, _ = sample
    owner = _DeckLibraryMixin()
    owner._deck_library = {
        "d": SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    }
    owner._coach = SimpleNamespace(_cached_playbook=lambda identity, state: book)
    owner._study_outside_match = lambda: True
    models = {}
    for name, tokens in (("a", 101), ("b", 202)):
        writer = backend(book)
        writer.digest = name * 64
        writer.complete.return_value["role"] = f"Fixture role {name}"
        writer.last_metrics = {"generated_tokens": tokens}
        models[name] = writer
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", models.__getitem__)
    monkeypatch.setattr("arenamcp.logging_config.LOG_DIR", tmp_path)
    request = {"operation": "build", "card": 1001, "deck_id": "d", "identity": book.identity}
    for name, reused, tokens in (("a", False, 101), ("b", False, 202), ("a", True, 101)):
        reply = owner.deck_card_study({**request, "model": name})
        assert reply["reused"] is reused
        assert f"Fixture role {name}" in reply["detail"]
        assert f"{tokens} generated tokens" in reply["detail"]
        assert name * 12 in reply["detail"]
    assert all(writer.complete.call_count == 1 for writer in models.values())


@pytest.mark.parametrize(
    "extra",
    [
        {"role": "tool"},
        {"role": "user"},
        {"role": "system"},
        {},
        {"role": "assistant", "tool_calls": [{"function": {}}]},
    ],
)
def test_stream_requires_assistant_output_without_tools(monkeypatch, extra):
    import io

    import arenamcp.local_study_model as local

    m = LocalStudyModel.__new__(LocalStudyModel)
    m.name, m.digest, m.thinks = "fixture", "a" * 64, False
    monkeypatch.setattr(local, "installed_models", lambda: {m.name: m.digest})
    stream = io.BytesIO(
        json.dumps(
            {
                "model": m.name,
                "done": True,
                "done_reason": "stop",
                "message": {**extra, "content": '{"ok":true}'},
            }
        ).encode()
    )
    stream.status = 200
    conn = Mock()
    conn.getresponse.return_value = stream
    monkeypatch.setattr(local.http.client, "HTTPConnection", Mock(return_value=conn))
    with pytest.raises(ValueError, match="output channel"):
        m.complete("system", "user", allowed=lambda: True)


def test_model_alias_must_still_identify_the_original_weights_at_completion(monkeypatch):
    import io

    import arenamcp.local_study_model as local

    m = LocalStudyModel.__new__(LocalStudyModel)
    m.name, m.digest, m.thinks = "fixture", "a" * 64, False
    monkeypatch.setattr(local, "installed_models", Mock(side_effect=[{m.name: m.digest}, {m.name: "b" * 64}]))
    stream = io.BytesIO(
        json.dumps(
            {
                "model": m.name,
                "done": True,
                "done_reason": "stop",
                "message": {"role": "assistant", "content": '{"ok":true}'},
            }
        ).encode()
    )
    stream.status = 200
    conn = Mock()
    conn.getresponse.return_value = stream
    monkeypatch.setattr(local.http.client, "HTTPConnection", Mock(return_value=conn))
    with pytest.raises(ValueError, match="weights changed"):
        m.complete("system", "user", allowed=lambda: True)
    conn.request.assert_called_once()


def test_general_cache_read_ignores_unverifiable_model_identity(sample, tmp_path):
    _, book, _ = sample
    study_card(book, 1001, backend(book), tmp_path, allowed=lambda: True)
    file = next(tmp_path.rglob("*.json"))
    data = json.loads(file.read_text())
    data["model"] = "not-local-weights"
    file.rename(file.with_name("not-local-weights.json"))
    file.with_name("not-local-weights.json").write_text(json.dumps(data))
    assert "awaiting review" not in read_card(book, 1001, tmp_path)


def test_full_nonland_pass_resumes_exact_model_caches_and_withholds_changed_prints(
    qapp, sample, tmp_path, monkeypatch
):
    from arenamcp import logging_config
    from arenamcp.desktop.deck_card_study import DeckCardStudyDialog

    state, book, _ = sample
    for gid in (2001, 2002):
        book.catalog[gid] = {**book.catalog[1001], "name": f"Fixture local piece {gid}"}
    native = native_catalog(monkeypatch, book.catalog)
    native[2002].oracle_text = "Changed native synthetic effect"
    owner = _DeckLibraryMixin()
    owner._deck_library = {
        "d": SimpleNamespace(complete=True, identity=lambda: book.identity, game_state=lambda: state)
    }
    owner._coach = SimpleNamespace(_cached_playbook=lambda *_: book)
    owner._study_outside_match = lambda: True
    writer = backend(book)
    writer.last_metrics = {"generated_tokens": 11}

    def complete(system, prompt, **kwargs):
        gid = json.loads(prompt)["card"]
        return model_row(book, gid)

    writer.complete.side_effect = complete
    monkeypatch.setattr("arenamcp.local_study_model.LocalStudyModel", lambda name: writer)
    monkeypatch.setattr("arenamcp.local_study_model.installed_models", lambda: {"fixture": writer.digest})
    monkeypatch.setattr(logging_config, "LOG_DIR", tmp_path)

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)

        def request_deck_card_study(self, request):
            self.deckCardStudyChanged.emit(owner.deck_card_study(request))

    session = Session()
    dialog = DeckCardStudyDialog(session, {"deck_id": "d", "identity": book.identity})
    dialog.show()
    assert not writer.complete.called
    dialog.pass_button.click()
    assert dialog.running_pass and len(dialog.remaining) == 2
    dialog.next_job.stop()
    dialog.advance_pass()
    assert writer.complete.call_count == 1
    dialog.pass_button.click()
    assert not dialog.running_pass and not dialog.next_job.isActive()
    dialog.pass_button.click()
    assert len(dialog.remaining) == 1
    for _ in range(5):
        qapp.processEvents()
    assert not dialog.running_pass and writer.complete.call_count == 2
    assert "2 local roles awaiting review" in dialog.progress.text()
    assert "across 3 known nonland cards" in dialog.progress.text()
    assert "1 withheld" in dialog.progress.text()
    assert "22 generated tokens" in dialog.progress.text() and "2 unreported" in dialog.progress.text()
    dialog.pass_button.click()
    assert writer.complete.call_count == 2 and not dialog.running_pass
    # Re-open after changing the plan. The output must not come from the prior deck context.
    book.data["primary_plan"] = "Different synthetic strategy"
    dialog.pass_button.click()
    assert len(dialog.remaining) == 2
    owner._study_outside_match = lambda: False
    qapp.processEvents()
    assert (
        not dialog.running_pass and "match" in dialog.detail.toPlainText() and writer.complete.call_count == 2
    )
    owner._study_outside_match = lambda: True
    dialog.pass_button.click()
    writer.digest = "c" * 64
    qapp.processEvents()
    assert not dialog.running_pass and "weights changed" in dialog.detail.toPlainText()
    assert writer.complete.call_count == 2
    dialog.close()


@pytest.mark.parametrize(
    "fault", [None, "missing", "reordered", "native_drift", "explicit_empty", "withheld_v3", "typed_v3"]
)
def test_v2_print_equivalence_requires_all_ordered_native_face_ids_and_full_prints(sample, fault):
    from arenamcp.card_db import MTGADatabaseAdapter, initialized_card_database
    from arenamcp.deck_card_study import _checked
    from arenamcp.deck_study import current_study_sources_match

    _, book, native = sample
    native[1001].linked_face_ids = [2001, 2002]
    for gid in native[1001].linked_face_ids:
        native[gid] = SimpleNamespace(
            **{**vars(native[1001]), "grp_id": gid, "name": f"Reversible fixture {gid}"}
        )
    adapter = MTGADatabaseAdapter(initialized_card_database().get_raw_mtgadb())
    book.catalog[1001]["related_faces"] = adapter.get_card_by_arena_id(1001).related_faces
    if fault == "missing":
        book.catalog[1001]["related_faces"].pop()
    if fault == "reordered":
        book.catalog[1001]["related_faces"].reverse()
    if fault == "native_drift":
        native[2002].mana_cost = "{7}"
    if fault == "explicit_empty":
        book.catalog[1001]["linked_face_ids"] = []
    if fault == "withheld_v3":
        book.study_withheld = True
    if fault == "typed_v3":
        book.data["study"] = {"version": 1}
    assert not current_study_sources_match({1001: book.catalog[1001]})
    assert _checked(book, [1001]) is (fault is None)


def test_pass_reports_unknown_card_types_and_unreported_provider_usage(qapp, sample, monkeypatch, tmp_path):
    from arenamcp.deck_card_study import prepare_card_pass
    from arenamcp.desktop.deck_card_study import DeckCardStudyDialog

    _, book, _ = sample
    writer = backend(book)
    writer.last_metrics = {}
    study_card(book, 1001, writer, tmp_path, allowed=lambda: True)
    book.catalog[3003] = {"name": "Unresolved local fixture", "type_line": "", "oracle_text": ""}
    report = prepare_card_pass(book, tmp_path, writer.digest)
    assert (report["nonlands"], report["unresolved"], report["cached"], report["pending"]) == (1, 1, 1, [])
    assert report["unreported_generated"] == report["unreported_thinking"] == 1

    class Session(FakeSession):
        deckCardStudyChanged = Signal(dict)

        def request_deck_card_study(self, request):
            assert request["operation"] == "list"
            self.deckCardStudyChanged.emit({**request, "cards": [], "unresolved": 2, "models": []})

    session = Session()
    dialog = DeckCardStudyDialog(session, {"deck_id": "d", "identity": "synthetic"})
    dialog.show()
    assert (
        "0 known nonland cards" in dialog.progress.text()
        and "2 deck prints have unresolved types" in dialog.progress.text()
    )
    assert dialog.progress.isVisible() and not dialog.pass_button.isEnabled()
    dialog.close()


def test_walker_authoring_receives_the_same_timing_constraint_as_admission(sample, monkeypatch, tmp_path):
    from arenamcp.loyalty_timing import LOYALTY_TIMING

    _, book, _ = sample
    gid = 1001
    card = book.catalog[gid]
    card.update(
        name="Fixture Signal Walker",
        type_line="Legendary Planeswalker — Signal",
        oracle_text="+1: Scry 1.\n−2: Draw a card.",
    )
    native_catalog(monkeypatch, book.catalog)
    writer = backend(book)
    writer.complete.return_value["play"]["advice"] = "Use +1 then −2 in the same turn."
    with pytest.raises(ValueError, match="two loyalty abilities"):
        study_card(book, gid, writer, tmp_path, allowed=lambda: True)
    assert json.loads(writer.complete.call_args.args[1])["loyalty_timing"] == LOYALTY_TIMING
    assert not list(tmp_path.rglob("*.json"))
    writer.complete.return_value["play"]["advice"] = (
        "Use +1, then on a later turn use −2 if the walker remains in play."
    )
    role, reused = study_card(book, gid, writer, tmp_path, allowed=lambda: True)
    assert not reused and "later turn" in role["play"]["advice"]


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "hand_cast",
        "hand_hold",
        "early_without_ramp",
        "missing_pip",
        "wrong_source",
        "unbound_cost",
        "invented_checked_fields",
        "unlisted_exception",
    ],
)
def test_commander_deployment_and_independent_resources_are_proven(sample, monkeypatch, tmp_path, fault):
    _, book, _ = sample
    gid = 1001
    card = book.catalog[gid]
    card.update(
        name="Fixture Ember Magistrate",
        mana_cost="{3}{G}{G}",
        type_line="Legendary Planeswalker — Fixture",
        oracle_text="+1: Scry 1.\n−2: Sacrifice a creature. Draw a card.",
    )
    if fault == "unlisted_exception":
        book.catalog[1002]["oracle_text"] = (
            "You may activate loyalty abilities of planeswalkers you control twice each turn."
        )
    native_catalog(monkeypatch, book.catalog)
    writer = backend(book)
    row = writer.complete.return_value
    row["evidence"].append({"card": gid, "rule": 2})
    row["action_rules"]["avoid"] = [{"card": gid, "rule": 2}]
    row["avoid"]["advice"] = "Keep another creature available if you intend to use the sacrifice ability."
    if fault == "hand_cast":
        row["play"]["conditions"]["pieces"][0]["zone"] = "hand"
    if fault == "hand_hold":
        row["hold"]["conditions"]["pieces"][0]["zone"] = "hand"
    if fault == "early_without_ramp":
        row["play"]["conditions"]["first_turn"] = 3
    if fault == "missing_pip":
        row["play"]["conditions"]["mana"] = {"generic": 5}
    if fault == "wrong_source":
        row["action_rules"]["avoid"] = [{"card": 1002, "rule": 1}]
    if fault == "unbound_cost":
        row["action_rules"]["avoid"] = [{"card": gid, "rule": 1}]
    if fault == "invented_checked_fields":
        row["commander_conditions"] = {"play": {"payable": True}}
    if fault == "unlisted_exception":
        row["avoid"]["advice"] = "Use +1 then −2 in the same turn."
    if fault:
        with pytest.raises(ValueError):
            study_card(book, gid, writer, tmp_path, allowed=lambda: True)
        assert not list(tmp_path.rglob("*.json"))
        return
    result, _ = study_card(book, gid, writer, tmp_path, allowed=lambda: True)
    policy = json.loads(writer.complete.call_args.args[1])["commander_deployment"]
    assert policy["base_payment"] == {"generic": 3, "G": 2}
    certified = result["commander_conditions"]
    assert certified["play"]["command_zone_payment"] == {
        "card": gid,
        "printed_cost": "{3}{G}{G}",
        "generic_per_prior_command_cast": 2,
        "prior_casts": "read_from_current_game",
    }
    assert certified["avoid"]["loyalty_source"] == {
        "card": gid,
        "zone": "battlefield",
        "controller": "self",
        "timing": "own_main_empty_stack",
        "activation_available": "verify_current_game",
        "alternatives": [{"rule": 2, "change": -2}],
    }
    assert certified["avoid"]["independent_battlefield_resources"] == [
        {
            "card": gid,
            "rule": 2,
            "kind": "creature",
            "zone": "battlefield",
            "controller": "self",
            "minimum": 1,
            "exclude_source_instance": True,
        }
    ]
    detail = read_card(book, gid, tmp_path)
    assert "current tax" in detail and "another creature you control" in detail
    assert "commander_conditions" not in writer.complete.return_value and "study" not in book.data


def test_unmodeled_variable_commander_payment_is_reported_before_inference(sample, monkeypatch, tmp_path):
    from arenamcp.deck_card_study import prepare_card_pass

    _, book, _ = sample
    book.catalog[1001]["mana_cost"] = "{X}{G}"
    native_catalog(monkeypatch, book.catalog)
    m = backend(book)
    report = prepare_card_pass(book, tmp_path, m.digest)
    assert report["unsupported_payment"] == 1 and not report["pending"] and not report["withheld"]
    with pytest.raises(ValueError, match="ordinary payment"):
        study_card(book, 1001, m, tmp_path, allowed=lambda: True)
    m.complete.assert_not_called()


@pytest.mark.parametrize(
    "kind",
    ["missing_cast_ref", "mandatory_cast", "tap_sacrifice", "unsupported_cast", "unsupported_activation"],
)
def test_commander_costs_require_real_additional_resources(sample, monkeypatch, tmp_path, kind):
    _, book, _ = sample
    gid = 1001
    abilities = {
        "missing_cast_ref": "As an additional cost to cast this spell, sacrifice a creature.",
        "mandatory_cast": "As an additional cost to cast this spell, sacrifice a creature.",
        "tap_sacrifice": "{T}, Sacrifice another creature: Draw a card.",
        "unsupported_cast": "As an additional cost to cast this spell, discard two cards.",
        "unsupported_activation": "{T}, Discard a card, Sacrifice another creature: Draw a card.",
    }
    book.catalog[gid]["oracle_text"] = "When this creature enters, scry 1.\n" + abilities[kind]
    native_catalog(monkeypatch, book.catalog)
    m = backend(book)
    row = m.complete.return_value
    row["evidence"].append({"card": gid, "rule": 2})
    if kind == "mandatory_cast":
        row["action_rules"]["play"].append({"card": gid, "rule": 2})
    if kind in ("tap_sacrifice", "unsupported_activation"):
        row["action_rules"]["hold"].append({"card": gid, "rule": 2})
        row["hold"]["advice"] = "Keep another creature available for the sacrifice ability."
    if kind in ("missing_cast_ref", "unsupported_activation", "unsupported_cast"):
        with pytest.raises(ValueError, match="mandatory additional|activation cost|ordinary payment"):
            study_card(book, gid, m, tmp_path, allowed=lambda: True)
        assert not list(tmp_path.rglob("*.json"))
        if kind == "unsupported_cast":
            m.complete.assert_not_called()
        return
    result, _ = study_card(book, gid, m, tmp_path, allowed=lambda: True)
    section = result["commander_conditions"]["play" if kind == "mandatory_cast" else "hold"]
    assert section["independent_battlefield_resources"][0]["rule"] == 2
    assert section["independent_battlefield_resources"][0]["exclude_source_instance"] is True
    if kind == "tap_sacrifice":
        assert section["activation_source"] == {
            "card": gid,
            "rules": [2],
            "zone": "battlefield",
            "controller": "self",
            "must_be_untapped": True,
            "tap_legality": "verify_current_game",
        }


@pytest.mark.parametrize(
    "cost", ["{2}, {T}", "Pay 2 life", "Discard a card", "Remove a charge counter from this permanent"]
)
def test_unmodeled_commander_activation_is_never_implicitly_free(sample, monkeypatch, tmp_path, cost):
    _, book, _ = sample
    book.catalog[1001]["oracle_text"] = f"When this creature enters, scry 1.\n{cost}: Draw a card."
    native_catalog(monkeypatch, book.catalog)
    m = backend(book)
    m.complete.return_value["evidence"].append({"card": 1001, "rule": 2})
    m.complete.return_value["action_rules"]["hold"].append({"card": 1001, "rule": 2})
    with pytest.raises(ValueError, match="activation cost"):
        study_card(book, 1001, m, tmp_path, allowed=lambda: True)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "rules",
    [
        "{T}: Draw a card. Activate only if you control an artifact.",
        "{T}: Draw a card. Activate only once each turn.",
        "{T}: Draw a card.\nActivate this ability only if you control an artifact.",
        "+1: Draw a card.\nYou may activate this ability only during combat.",
    ],
)
def test_commander_activation_suffix_and_unassigned_riders_withhold_unconditional_advice(
    sample, monkeypatch, tmp_path, rules
):
    _, book, _ = sample
    book.catalog[1001]["oracle_text"] = "When this permanent enters, scry 1.\n" + rules
    if rules.startswith("+1"):
        book.catalog[1001]["type_line"] = "Legendary Planeswalker — Fixture"
    native_catalog(monkeypatch, book.catalog)
    m = backend(book)
    row = m.complete.return_value
    row["evidence"].append({"card": 1001, "rule": 2})
    row["action_rules"]["hold"].append({"card": 1001, "rule": 2})
    row["hold"]["advice"] = "Activate this commander to draw whenever you need cards."
    with pytest.raises(ValueError, match="activation permission"):
        study_card(book, 1001, m, tmp_path, allowed=lambda: True)
    assert not list(tmp_path.rglob("*.json"))
