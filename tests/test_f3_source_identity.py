from types import SimpleNamespace

import pytest

from arenamcp.card_facts.client import hash_for_card
from arenamcp.card_facts.compiler import _printed_from_scryfall_card, build_user_prompt, compile_one
from arenamcp.card_facts.hashing import rules_hash
from arenamcp.card_facts.source_identity import executable_source_hash
from arenamcp.card_facts.validate import PrintedCard


def card(**changes):
    result = dict(
        name="Example", mana_cost="{1}{R}", type_line="Creature — Human", oracle_text="{T}: Add {R}."
    )
    result.update(changes)
    return result


def test_legacy_identity_exact_and_shared():
    raw = card()
    expected = rules_hash(raw["name"], raw["mana_cost"], raw["type_line"], raw["oracle_text"])
    assert hash_for_card(raw) == executable_source_hash(PrintedCard(**raw)) == expected
    assert "Add {R}." in build_user_prompt(PrintedCard(**raw))


@pytest.mark.parametrize(
    "changes",
    [
        {"oracle_text": "{oT}: Add {oC}. This mana cannot be spent from hand."},
        {"oracle_text": "<i>Unknown markup</i>"},
        {"oracle_text": "{ZZ}"},
        {"mana_cost": "{2"},
        {"related_faces": [{"name": "Back"}]},
        {"card_faces": [{"name": "Back"}]},
        {"name": "Front // Back"},
        {"oracle_text": "Front\n---\nBack"},
        {"mana_cost": None},
    ],
)
def test_client_refuses_unsupported(changes):
    assert hash_for_card(card(**changes)) is None


@pytest.mark.parametrize("changes", [{"oracle_text": "{oT}"}, {"card_faces": [{"name": "Back"}]}])
def test_direct_compiler_never_calls_model(changes):
    printed = PrintedCard(**card(**changes))

    class Resolver:
        def resolve(self, identifier):
            return printed

    class Backend:
        def complete(self, *a, **kw):
            pytest.fail("unsupported source reached backend")

    result = compile_one("Example", Resolver(), Backend())
    assert result.status == "unresolved" and result.attempts == 0
    with pytest.raises(ValueError):
        build_user_prompt(printed)


def test_complete_faces_preserved_without_compilation():
    faces = [
        dict(name="Front", mana_cost="{2}", oracle_text="First", power="3"),
        dict(name="Back", mana_cost="", oracle_text="Second", toughness="4"),
    ]
    source = SimpleNamespace(**card(), power="", toughness="", card_faces=faces)
    printed = _printed_from_scryfall_card(source, SimpleNamespace())
    assert printed.card_faces == faces
    faces[1]["oracle_text"] = "changed"
    assert printed.card_faces[1]["oracle_text"] == "Second"
    assert executable_source_hash(printed) is None


@pytest.mark.parametrize("symbol", ["{W/U}", "{2/B}", "{G/P}", "{W/U/P}", "{X}", "{T}", "{C}"])
def test_native_legal_oracle_symbols_preserved(symbol):
    assert hash_for_card(card(oracle_text=f"Add {symbol}.")) is not None


@pytest.mark.parametrize("key", ["mana_cost", "type_line", "oracle_text"])
def test_missing_required_field_is_not_explicit_empty(key):
    raw = card()
    del raw[key]
    assert hash_for_card(raw) is None
    assert hash_for_card(card(oracle_text="")) is not None


def test_native_link_signal_survives_unresolved_face_and_local_projection(monkeypatch):
    import arenamcp.card_db as db
    import arenamcp.match_context as context

    native = SimpleNamespace(**card(), grp_id=901, linked_face_ids=[902])
    adapter = db.MTGADatabaseAdapter(SimpleNamespace(get_card=lambda grp: native if grp == 901 else None))
    monkeypatch.setattr(adapter, "_type_line", lambda card: "Creature")
    monkeypatch.setattr(adapter, "_colors", lambda card: [])
    info = adapter._card_info(native)
    assert info.related_faces == [] and info.linked_face_ids == [902]
    assert hash_for_card(info) is None
    source = SimpleNamespace(sources=[SimpleNamespace(get_card_by_arena_id=lambda grp: info)])
    monkeypatch.setattr(db, "get_card_database", lambda: source)
    context._local_card.cache_clear()
    result = context._local_card(901, 999)
    assert result["linked_face_ids"] == [902]
    assert hash_for_card(result) is None


@pytest.mark.parametrize("method,arg", [("get_card_by_name", "Front"), ("get_card_by_arena_id", 901)])
def test_scryfall_adapter_keeps_face_signal(method, arg):
    from arenamcp.card_db import ScryfallAdapter

    raw = SimpleNamespace(
        **card(), card_faces=[{"name": "Back"}], cmc=2, colors=[], arena_id=901, scryfall_uri=""
    )
    adapter = ScryfallAdapter(SimpleNamespace(**{method: lambda arg: raw}))
    result = getattr(adapter, method)(arg)
    assert result.card_faces == raw.card_faces
    assert hash_for_card(result) is None


def test_direct_compiler_rejects_native_link_ids():
    printed = PrintedCard(**card())
    printed.linked_face_ids = [902]
    result = compile_one("Example", SimpleNamespace(resolve=lambda _: printed), object())
    assert result.status == "unresolved" and result.attempts == 0


@pytest.mark.parametrize("key", ["card_faces", "related_faces", "linked_faces", "linked_face_ids"])
@pytest.mark.parametrize("value", [False, 0, {}, "", (), [902]])
def test_face_metadata_requires_supported_empty_shape(key, value):
    assert hash_for_card(card(**{key: value})) is None


@pytest.mark.parametrize("key", ["card_faces", "related_faces", "linked_faces", "linked_face_ids"])
@pytest.mark.parametrize("value", [None, []])
def test_known_absent_face_shapes_remain_compatible(key, value):
    assert hash_for_card(card(**{key: value})) == hash_for_card(card())


@pytest.mark.parametrize("value", [False, 0, {}, ""])
def test_raw_scryfall_malformed_metadata_not_coerced_empty(value):
    source = SimpleNamespace(**card(), power="", toughness="")
    cache = SimpleNamespace(_name_index={"example": {"card_faces": value}})
    printed = _printed_from_scryfall_card(source, cache)
    assert executable_source_hash(printed) is None


@pytest.mark.parametrize(
    "raw_faces,object_faces",
    [
        (None, [{"name": "Back"}]),
        ([], [{"name": "Back"}]),
        ([{"name": "Back"}], None),
        ([{"name": "Back"}], []),
        ([], False),
        (False, []),
        (None, {}),
        ({}, None),
    ],
)
def test_neither_representation_can_erase_refusal_signal(raw_faces, object_faces):
    source = SimpleNamespace(**card(), power="", toughness="", card_faces=object_faces)
    cache = SimpleNamespace(_name_index={"example": {"card_faces": raw_faces}})
    printed = _printed_from_scryfall_card(source, cache)
    assert executable_source_hash(printed) is None
    result = compile_one("Example", SimpleNamespace(resolve=lambda _: printed), object())
    assert result.status == "unresolved" and result.attempts == 0
