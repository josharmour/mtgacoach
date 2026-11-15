"""Tests for the card-facts compiler (card_facts/compiler.py).

Built from the same printed-text fixtures as test_card_facts_schema.py —
Fury, Minsc & Boo, Chrome Mox and Gemstone Caverns (Scryfall oracle text,
fetched 2026-11-10) — and the verified Fury rules_hash from the F1 report
(16de70d3…). The failure-path fixtures reuse Griselbrand ("Flying, lifelink"),
whose 7/7 body and flying/lifelink keywords are the printed facts the coach
mis-modelled in bug report bug_20261109_212434.

The model backend is a fake that records every call; no network access.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from arenamcp.card_facts import (
    PrintedCard,
    card_facts_to_dict,
    from_json,
    rules_hash,
    to_json,
)
from arenamcp.card_facts.compiler import (
    CALL_CLASS,
    CardResolver,
    CompileResult,
    build_user_prompt,
    compile_cards,
    compile_one,
    extract_json_object,
    split_name_list,
)
from arenamcp.game_plan import BACKGROUND_PRIORITY
from arenamcp.mtgadb import MTGADatabase
from test_card_facts_schema import PRINTED_CARDS, fury_record

# Verified in F1 (fable-november-reports/F1.md) and reproduced independently
# by the F1 reviewer from the documented recipe.
FURY_VERIFIED_HASH = "16de70d3bc41dc6b54e586972e52ee768fd860813c36d6d6a23219915bf112f3"


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeBackend:
    """Records calls and returns scripted responses in order."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.requests = []

    def complete(self, system, user, max_tokens, **kwargs):
        self.calls += 1
        self.requests.append({"system": system, "user": user, "kwargs": kwargs})
        if self._responses:
            return self._responses.pop(0)
        return "{}"

    @property
    def last_kwargs(self):
        return self.requests[-1]["kwargs"] if self.requests else {}


class FakeScryfallCard:
    """Just the fields _printed_from_scryfall_card reads."""

    def __init__(self, printed):
        self.name = printed.name
        self.mana_cost = printed.mana_cost
        self.type_line = printed.type_line
        self.oracle_text = printed.oracle_text
        self.power = printed.power
        self.toughness = printed.toughness


class FakeScryfall:
    """Name/arena-id lookups over the F1 printed-text fixtures."""

    def __init__(self):
        self._cards = {name: FakeScryfallCard(p) for name, p in PRINTED_CARDS.items()}
        # Raw dicts as ScryfallCache keeps them; loyalty lives only here.
        self._name_index = {
            "minsc & boo, timeless heroes": {"loyalty": "3"},
            "fury": {},
        }

    def get_card_by_name(self, name):
        return self._cards.get(name)

    def get_card_by_arena_id(self, arena_id):
        # Fury's Arena grp_id in the fixtures; anything else is unknown.
        return self._cards["Fury"] if arena_id == 9707 else None


class FakeMtgaDb:
    def __init__(self, names=None):
        self._names = dict(names or {})
        self.available_value = True

    def available(self):
        return self.available_value

    def get_card(self, grp_id):
        name = self._names.get(grp_id)
        return None if name is None else type("MtgaCard", (), {"name": name})()


def fury_answer() -> str:
    """A model answer that reproduces the hand-built verified Fury record."""
    return json.dumps(card_facts_to_dict(fury_record()))


def wrong_mana_answer() -> str:
    data = card_facts_to_dict(fury_record())
    data["mana_cost"] = "{2}{R}{R}"
    return json.dumps(data)


# ── Hashing ties to the verified F1 value ────────────────────────────────────


def test_fury_hash_matches_verified_f1_value():
    printed = PRINTED_CARDS["Fury"]
    assert (
        rules_hash(printed.name, printed.mana_cost, printed.type_line, printed.oracle_text)
        == FURY_VERIFIED_HASH
    )


# ── Successful compilation ───────────────────────────────────────────────────


def test_compile_fury_success(tmp_path):
    backend = FakeBackend([fury_answer()])
    resolver = CardResolver(FakeScryfall())
    result = compile_one("Fury", resolver, backend)

    assert result.status == "compiled"
    assert result.attempts == 1
    assert result.problems == []
    assert result.record is not None
    assert result.record.name == "Fury"
    assert result.record.rules_hash == FURY_VERIFIED_HASH
    assert result.record.compiled_by == "card_facts.compiler"
    assert result.record.confidence == "high"


def test_compile_cards_writes_hash_named_file_and_summary(tmp_path):
    backend = FakeBackend([fury_answer()])
    summary = compile_cards(["Fury"], tmp_path, backend=backend, scryfall=FakeScryfall())

    written = tmp_path / f"{FURY_VERIFIED_HASH}.json"
    assert written.exists()
    record = from_json(written.read_text(encoding="utf-8"))
    assert record.name == "Fury"
    assert summary["compiled"] == 1
    assert summary["failed"] == 0
    assert summary["cards"][0]["rules_hash"] == FURY_VERIFIED_HASH


def test_existing_hash_file_is_skipped_without_model_call(tmp_path):
    (tmp_path / f"{FURY_VERIFIED_HASH}.json").write_text(to_json(fury_record()), encoding="utf-8")
    backend = FakeBackend([])
    summary = compile_cards(["Fury"], tmp_path, backend=backend, scryfall=FakeScryfall())

    assert backend.calls == 0
    assert summary["skipped"] == 1
    assert summary["compiled"] == 0


def test_unresolved_identifier_never_calls_the_model(tmp_path):
    backend = FakeBackend([])
    summary = compile_cards(["Not A Real Card"], tmp_path, backend=backend, scryfall=FakeScryfall())

    assert backend.calls == 0
    assert summary["unresolved"] == 1
    assert summary["cards"][0]["status"] == "unresolved"


# ── Retry and failure paths ──────────────────────────────────────────────────


def test_wrong_first_answer_is_retried_with_problems_listed(tmp_path):
    backend = FakeBackend([wrong_mana_answer(), fury_answer()])
    result = compile_one("Fury", CardResolver(FakeScryfall()), backend)

    assert result.status == "compiled"
    assert result.attempts == 2
    retry_user = backend.requests[1]["user"]
    assert "did not agree with the printed card" in retry_user
    assert "mana_cost" in retry_user


def test_second_failure_writes_failed_record(tmp_path):
    backend = FakeBackend([wrong_mana_answer(), wrong_mana_answer()])
    summary = compile_cards(["Fury"], tmp_path, backend=backend, scryfall=FakeScryfall())

    assert summary["failed"] == 1
    written = tmp_path / f"{FURY_VERIFIED_HASH}.json"
    record = from_json(written.read_text(encoding="utf-8"))
    assert record.confidence == "failed"
    assert "validation failed" in record.notes
    assert any("mana_cost" in problem for problem in summary["cards"][0]["problems"])


def test_non_json_answer_counts_as_failure(tmp_path):
    backend = FakeBackend(["I cannot help with that.", "Still no JSON here."])
    result = compile_one("Fury", CardResolver(FakeScryfall()), backend)

    assert result.status == "failed"
    assert result.attempts == 2
    # Dotty rejection point 1 at 5beb064: even with no parsable answer the
    # compiler must still produce the hash-named confidence=failed record.
    assert result.record is not None
    assert result.record.confidence == "failed"
    assert result.record.rules_hash == FURY_VERIFIED_HASH
    assert result.problems == ["answer contained no JSON object"]
    assert "answer contained no JSON object" in result.record.notes


def test_two_non_json_answers_still_write_hash_named_failed_record(tmp_path):
    # Real logged case: Dotty's verification of 5beb064 (VERIFICATION-LOG.md
    # 2026-11-11 15:43 UTC, /tmp/dotty-F2-repro.py) — two non-JSON replies gave
    # failed1/attempts2 but wrote no JSON file at all. The run must leave the
    # hash-named confidence=failed record with the problems on disk.
    backend = FakeBackend(["I cannot help with that.", "Still no JSON here."])
    summary = compile_cards(["Fury"], tmp_path, backend=backend, scryfall=FakeScryfall())

    assert summary["failed"] == 1
    written = tmp_path / f"{FURY_VERIFIED_HASH}.json"
    assert written.exists()
    record = from_json(written.read_text(encoding="utf-8"))
    assert record.confidence == "failed"
    assert record.name == "Fury"
    assert record.rules_hash == FURY_VERIFIED_HASH
    assert "answer contained no JSON object" in record.notes


# ── Name-list splitting keeps commas inside card names ──────────────────────


def test_split_name_list_keeps_comma_inside_card_name():
    # Real logged case: Dotty's verification of 5beb064 (VERIFICATION-LOG.md
    # 2026-11-11 15:43 UTC) — CLI --names 'Minsc & Boo, Timeless Heroes'
    # became two identifiers.
    known = {"Minsc & Boo, Timeless Heroes", "Fury"}
    assert split_name_list("Minsc & Boo, Timeless Heroes,Fury", known.__contains__) == [
        "Minsc & Boo, Timeless Heroes",
        "Fury",
    ]


def test_split_name_list_still_splits_plain_lists():
    known = {"Fury", "Chrome Mox"}
    assert split_name_list("Fury,Chrome Mox", known.__contains__) == ["Fury", "Chrome Mox"]


def test_split_name_list_emits_unresolvable_leftover():
    # An unresolvable fragment must reach the run summary as unresolved, not
    # disappear into a glued-together name or swallow a real one.
    known = {"Fury"}
    assert split_name_list("Not A Real Card,Fury", known.__contains__) == [
        "Not A Real Card",
        "Fury",
    ]
    assert split_name_list("Fury,Oops Typo", known.__contains__) == ["Fury", "Oops Typo"]


def test_griselbrand_body_error_surfaces_in_problems():
    # bug_20261109_212434: the coach modelled Griselbrand without his 7/7 body
    # or Flying/lifelink. An answer repeating that mistake must not validate.
    griselbrand = PrintedCard(
        name="Griselbrand",
        mana_cost="{4}{B}{B}{B}{B}",
        type_line="Legendary Creature — Demon",
        oracle_text="Flying, lifelink\nPay 7 life: Draw seven cards.",
        power="7",
        toughness="7",
    )
    bad_answer = json.dumps(
        {
            "name": "Griselbrand",
            "type_line": griselbrand.type_line,
            "mana_cost": griselbrand.mana_cost,
            "body": {"power": None, "toughness": None, "loyalty": None, "keywords": []},
            "modes": [],
            "effects": [],
            "mana": [],
            "threat": {},
            "relationships": {},
            "limited": {},
            "confidence": "high",
            "notes": "",
        }
    )
    backend = FakeBackend([bad_answer])
    resolver = CardResolver(_SingleCardScryfall(griselbrand))
    result = compile_one("Griselbrand", resolver, backend)

    assert result.status == "failed"
    joined = "; ".join(result.problems)
    assert "power" in joined or "toughness" in joined


class _SingleCardScryfall:
    def __init__(self, printed):
        self._card = FakeScryfallCard(printed)
        self._name_index = {}

    def get_card_by_name(self, name):
        return self._card if name == self._card.name else None

    def get_card_by_arena_id(self, arena_id):
        return None


# ── Resolution paths ─────────────────────────────────────────────────────────


def test_arena_id_resolves_through_mtgadb_then_scryfall_text():
    scryfall = FakeScryfall()
    resolver = CardResolver(scryfall, mtgadb=FakeMtgaDb({9707: "Fury"}))
    printed = resolver.resolve(9707)

    assert printed is not None
    assert printed.name == "Fury"
    # The printed text is Scryfall's ({T}-style oracle text), whatever resolved
    # the name — here exercised via the fixture text.
    assert printed.oracle_text == PRINTED_CARDS["Fury"].oracle_text


def test_arena_id_falls_back_to_scryfall_when_db_has_no_entry():
    resolver = CardResolver(FakeScryfall(), mtgadb=FakeMtgaDb({}))
    printed = resolver.resolve(9707)

    assert printed is not None
    assert printed.name == "Fury"


def test_numeric_string_is_treated_as_arena_id():
    resolver = CardResolver(FakeScryfall(), mtgadb=FakeMtgaDb({9707: "Fury"}))
    printed = resolver.resolve("9707")
    assert printed is not None and printed.name == "Fury"


def test_loyalty_comes_from_the_raw_scryfall_index():
    resolver = CardResolver(FakeScryfall())
    printed = resolver.by_name("Minsc & Boo, Timeless Heroes")

    assert printed is not None
    assert printed.loyalty == "3"


def test_card_without_loyalty_yields_none():
    resolver = CardResolver(FakeScryfall())
    printed = resolver.by_name("Fury")

    assert printed is not None
    assert printed.loyalty is None


# ── Prompt construction ──────────────────────────────────────────────────────


def test_user_prompt_carries_every_face_of_printed_text():
    prompt = build_user_prompt(PRINTED_CARDS["Fury"])

    assert "CARD: Fury" in prompt
    assert "Mana cost: {3}{R}{R}" in prompt
    assert "Type line: Creature — Elemental Incarnation" in prompt
    assert "Power/Toughness: 3/3" in prompt
    assert "Double strike" in prompt
    assert "Evoke—Exile a red card from your hand." in prompt


def test_system_prompt_demands_unknown_not_guesses():
    from arenamcp.card_facts.compiler import _SYSTEM_PROMPT

    assert "Unknown" in _SYSTEM_PROMPT
    assert "never invent" in _SYSTEM_PROMPT.lower() or "Never squeeze" in _SYSTEM_PROMPT


# ── Backend call shape (mirrors game_plan._complete) ─────────────────────────


def test_model_calls_use_background_lane_and_registered_call_class(tmp_path):
    backend = FakeBackend([fury_answer()])
    compile_cards(["Fury"], tmp_path, backend=backend, scryfall=FakeScryfall())

    kwargs = backend.last_kwargs
    assert kwargs["call_class"] == CALL_CLASS == "background.card_facts"
    assert kwargs["background"] is True
    assert kwargs["priority"] == BACKGROUND_PRIORITY
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["response_format"] == {"type": "json_object"}
    assert backend.requests[0]["system"].startswith("You compile structured facts")


# ── JSON extraction ──────────────────────────────────────────────────────────


def test_extract_json_object_plain():
    data = extract_json_object('{"name": "Fury"}')
    assert data == {"name": "Fury"}


def test_extract_json_object_inside_markdown_fence():
    text = 'Here you go:\n```json\n{"name": "Fury"}\n```'
    assert extract_json_object(text) == {"name": "Fury"}


def test_extract_json_object_with_trailing_commas():
    text = '{"name": "Fury", "mana": [],}'
    assert extract_json_object(text) == {"name": "Fury", "mana": []}


def test_extract_json_object_braces_confused_by_mana_symbols():
    # Prose around the object plus mana symbols like {1}{R} defeat naive
    # outer-brace slicing; the scanner must still find the object.
    text = 'The cost {1}{R} means: {"name": "Fury", "mana_cost": "{3}{R}{R}"} hope that helps'
    data = extract_json_object(text)
    assert data is not None
    assert data["name"] == "Fury"


def test_extract_json_object_returns_none_without_json():
    assert extract_json_object("no object here") is None
    assert extract_json_object("") is None


# ── CompileResult sanity ─────────────────────────────────────────────────────


def test_compile_result_defaults_are_safe():
    result = CompileResult(identifier="Fury")
    assert result.status == "compiled"
    assert result.problems == []
    assert result.record is None


# ── Local MTGA database route (real MTGADatabase, not a fake) ────────────────


def _write_mtga_fixture_db(path: Path) -> None:
    """Minimal CardDatabase schema holding one card: Fury, grp_id 9707."""
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE Cards ("
        "GrpId INTEGER PRIMARY KEY, TitleId INTEGER, Types TEXT, Power TEXT,"
        " Toughness TEXT, Colors TEXT, IsToken INTEGER, ExpansionCode TEXT,"
        " AbilityIds TEXT, Order_Title TEXT)"
    )
    con.execute("CREATE TABLE Localizations_enUS (LocId INTEGER PRIMARY KEY, Loc TEXT, Formatted INTEGER)")
    con.execute("INSERT INTO Cards VALUES (9707, 1, 'Creature', '3', '3', 'R', 0, 'MH2', '500:600', 'Fury')")
    con.execute("INSERT INTO Localizations_enUS VALUES (1, 'Fury', 1)")
    con.execute("INSERT INTO Localizations_enUS VALUES (600, 'Double strike', 1)")
    con.commit()
    con.close()


def test_local_mtga_database_resolves_arena_id_to_name(tmp_path):
    # Dotty rejection point 3 at 5beb064: only injected fakes exercised the
    # advertised local Arena-ID route; this drives the real MTGADatabase class
    # over a fixture CardDatabase sqlite file.
    db_path = tmp_path / "CardDatabase.sqlite"
    _write_mtga_fixture_db(db_path)
    mtgadb = MTGADatabase(db_path=db_path)
    assert mtgadb.available

    resolver = CardResolver(FakeScryfall(), mtgadb=mtgadb)
    printed = resolver.resolve(9707)

    assert printed is not None
    assert printed.name == "Fury"


def test_cli_initialises_local_mtgadb_and_preserves_comma_names(monkeypatch, tmp_path):
    # Dotty rejection points 2+3 at 5beb064: the CLI must pass a full
    # comma-containing name through as one identifier and must initialise and
    # hand over the local MTGADatabase instead of leaving compile_cards'
    # default None in place.
    cli_path = Path(__file__).resolve().parents[1] / "tools" / "compile_card_facts.py"
    spec = importlib.util.spec_from_file_location("compile_card_facts_under_test", cli_path)
    assert spec is not None and spec.loader is not None
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    class StubScryfall:
        def get_card_by_name(self, name):
            return {"name": name} if name == "Minsc & Boo, Timeless Heroes" else None

    captured = {}

    def fake_compile_cards(identifiers, out_dir, *, backend=None, scryfall=None, mtgadb=None):
        captured["identifiers"] = list(identifiers)
        captured["scryfall"] = scryfall
        captured["mtgadb"] = mtgadb
        return {
            "compiled": 1,
            "failed": 0,
            "skipped": 0,
            "unresolved": 0,
            "seconds_per_card": None,
            "reasoning_tokens_per_card": None,
            "cards": [],
        }

    monkeypatch.setattr(cli, "ScryfallCache", StubScryfall)
    monkeypatch.setattr(cli, "compile_cards", fake_compile_cards)

    rc = cli.main(["--names", "Minsc & Boo, Timeless Heroes", "--out", str(tmp_path)])

    assert rc == 0
    assert captured["identifiers"] == ["Minsc & Boo, Timeless Heroes"]
    assert isinstance(captured["scryfall"], StubScryfall)
    # The real MTGADatabase class is constructed and handed to compile_cards.
    # Auto-detection finds no Arena install in the test environment, so the
    # resolver falls back to Scryfall's arena index — exactly the advertised
    # degradation path.
    assert isinstance(captured["mtgadb"], MTGADatabase)


def test_aura_cast_restriction_is_separate_from_a_granted_keyword():
    card = PrintedCard(
        name="Sheltered by Ghosts",
        mana_cost="{1}{W}",
        type_line="Enchantment — Aura",
        oracle_text=(
            "Enchant creature you control\n"
            "When this Aura enters, exile target nonland permanent an opponent controls until this Aura leaves the battlefield.\n"
            "Enchanted creature gets +1/+0 and has lifelink and ward {2}."
        ),
    )
    source = FakeScryfall()
    source._cards[card.name] = FakeScryfallCard(card)
    resolver = CardResolver(source)
    record = {
        "name": card.name,
        "mana_cost": card.mana_cost,
        "type_line": card.type_line,
        "body": {"keywords": ["lifelink", "ward"]},
        "modes": [{"name": "cast", "cost": {"mana": card.mana_cost}}],
        "effects": [],
    }

    def compile_record():
        answer = json.dumps(record)
        return compile_one(card.name, resolver, FakeBackend([answer, answer]))

    failed = compile_record()
    assert failed.status == "failed"
    assert any("attachment restriction" in problem for problem in failed.problems)
    assert sum("not intrinsic" in problem for problem in failed.problems) == 2
    record["body"]["keywords"] = []
    restriction = {
        "effect": {"effect_type": "Unknown", "text": "Enchant creature you control"},
        "trigger": "ETB",
    }
    record["effects"].append(restriction)
    assert compile_record().status == "failed"
    restriction["trigger"] = "static"
    restriction["conditions"] = ["only on entry"]
    assert compile_record().status == "failed"
    restriction["conditions"] = []
    # A constraint alone cannot answer what resolves on entry, who stays
    # affected and what causes the exiled card to return.
    assert compile_record().status == "failed"
    printed = card.oracle_text.splitlines()
    entry = {"trigger": "ETB", "effect": {"effect_type": "Unknown", "text": printed[1]}}
    grant = {"trigger": "static", "effect": {"effect_type": "Unknown", "text": printed[2]}}
    record["effects"] += [entry, grant]
    assert compile_record().status == "compiled"
    for text in ("Enchanted creature has lifelink.", "Enchanted creature gets +1/+0 and has ward."):
        grant["effect"]["text"] = text
        assert compile_record().status == "failed"
    grant["effect"]["text"] = printed[2]
    entry["effect"] = {
        "effect_type": "Exile",
        "target": "target nonland permanent an opponent controls",
        "duration": "until this Aura leaves the battlefield",
    }
    assert compile_record().status == "failed"
    del entry["effect"]["duration"]
    assert compile_record().status == "failed"
    entry["effect"] = {"effect_type": "Unknown", "text": printed[1]}
    entry["trigger"] = "static"
    assert compile_record().status == "failed"
    entry["trigger"] = "ETB"
    record["effects"].append(
        {
            "trigger": "static",
            "effect": {"effect_type": "Pump", "power": 1, "toughness": 0, "target": "enchanted creature"},
        }
    )
    assert compile_record().status == "failed"
    record["effects"].pop()
    record["body"]["power"] = "1"
    assert compile_record().status == "failed"
    record["body"].pop("power")
    record["body"]["keywords"] = ["enchanted creature"]
    assert compile_record().status == "failed"


def test_aura_simple_typed_projections_and_conditional_grants_are_exact():
    from copy import deepcopy

    card = PrintedCard(
        name="Fixture mantle",
        mana_cost="{W}",
        type_line="Enchantment — Aura",
        oracle_text="Flash\nEnchant creature\nWhen this Aura enters, draw a card.\nEnchanted creature gets +2/-1.\nAs long as enchanted creature is tapped, it has flying.",
    )
    source = FakeScryfall()
    source._cards[card.name] = FakeScryfallCard(card)
    resolver = CardResolver(source)
    lines = card.oracle_text.splitlines()
    record = {
        "name": card.name,
        "mana_cost": card.mana_cost,
        "type_line": card.type_line,
        "body": {"keywords": ["Flash"]},
        "modes": [{"name": "cast", "cost": {"mana": card.mana_cost}}],
        "effects": [
            {"trigger": "static", "effect": {"effect_type": "Unknown", "text": lines[1]}},
            {"trigger": "ETB", "effect": {"effect_type": "Draw", "count": 1}},
            {
                "trigger": "static",
                "effect": {
                    "effect_type": "Pump",
                    "target": "enchanted creature",
                    "power": 2,
                    "toughness": -1,
                },
            },
            {"trigger": "static", "effect": {"effect_type": "Unknown", "text": lines[4]}},
        ],
    }

    def compile_record(data):
        answer = json.dumps(data)
        return compile_one(card.name, resolver, FakeBackend([answer, answer]))

    assert compile_record(record).status == "compiled"
    for index, key, bad in [
        (0, "text", "Enchant permanent"),
        (1, "count", 2),
        (2, "target", "this creature"),
        (2, "power", True),
        (2, "duration", "until end of turn"),
        (3, "text", "Enchanted creature has flying."),
    ]:
        changed = deepcopy(record)
        changed["effects"][index]["effect"][key] = bad
        assert compile_record(changed).status == "failed"
    for invalid in (["Flash", "Flash"], ["Flash", "flying"], "Flash"):
        record["body"]["keywords"] = invalid
        assert compile_record(record).status == "failed"
    record["body"]["keywords"] = ["Flash"]
    record["body"]["grants"] = "flying"
    assert compile_record(record).status == "failed"


@pytest.mark.parametrize(
    "layout",
    [
        "adventure",
        "modal_dfc",
        "transform",
        "prepare",
        "split",
        "meld",
        "flip",
        "battle",
        "reversible_card",
        "future_layout",
        [],
        False,
    ],
)
def test_explicit_layout_only_front_never_becomes_an_executable_source(layout, tmp_path):
    from arenamcp.card_facts.source_identity import executable_source_hash, legacy_lookup_hash
    from arenamcp.line_search_moves import _record_key
    from arenamcp.scryfall import ScryfallCache

    raw = {
        "name": "Example Front",
        "mana_cost": "{2}",
        "type_line": "Artifact",
        "oracle_text": "{T}: Add {C}.",
        "layout": layout,
    }
    cache = ScryfallCache.__new__(ScryfallCache)
    cache._name_index = {"example front": raw}
    source = CardResolver(cache).resolve("Example Front")
    assert executable_source_hash(source) is None
    assert legacy_lookup_hash(raw) is None
    assert _record_key(raw) == ("", "", "", "")
    backend = FakeBackend([])
    summary = compile_cards(["Example Front"], tmp_path, backend=backend, scryfall=cache)
    assert summary["unresolved"] == 1 and summary["compiled"] == summary["skipped"] == 0
    assert summary["cards"][0]["problems"] and not backend.calls and not list(tmp_path.iterdir())


def test_source_layout_binding_preserves_any_incompatible_view():
    from arenamcp.card_facts.compiler import _printed_from_scryfall_card
    from arenamcp.card_facts.source_identity import executable_source_hash
    from arenamcp.scryfall import ScryfallCache

    c = ScryfallCache.__new__(ScryfallCache)
    raw = {"name": "Example", "mana_cost": "{1}", "type_line": "Artifact", "oracle_text": "{T}: Add {C}."}
    expected = executable_source_hash(raw)
    for layout in (None, "normal", "saga", "class"):
        assert executable_source_hash(dict(raw, layout=layout)) == expected
    for upstream, cached in [("normal", "transform"), ("meld", "normal")]:
        card = c._card_dict_to_scryfall_card(dict(raw, layout=upstream))
        c._name_index = {"example": dict(raw, layout=cached)}
        assert executable_source_hash(_printed_from_scryfall_card(card, c)) is None


def test_native_linked_front_refuses_even_an_eligible_same_named_print(tmp_path):
    from types import SimpleNamespace as NS

    from arenamcp.scryfall import ScryfallCache

    c = ScryfallCache.__new__(ScryfallCache)
    native = NS(name="Example", grp_id=42, linked_face_ids=[43], expansion_code="SYN")
    c._arena_index = {}
    c._name_index = {
        "example": dict(name="Example", mana_cost="{1}", type_line="Artifact", oracle_text="{T}: Add {C}.")
    }
    db = NS(available=True, get_card=lambda _: native)
    b = FakeBackend([])
    summary = compile_cards([42], tmp_path, backend=b, scryfall=c, mtgadb=db)
    assert summary["unresolved"] == 1 and not b.calls and not list(tmp_path.iterdir())
    assert "linked" in summary["cards"][0]["problems"][0]
    assert "source_print_id" not in summary["cards"][0]


def test_mtgjson_cached_layout_survives_local_resolution_and_old_indexes_rebuild(tmp_path):
    from arenamcp.card_db import MTGJSONAdapter
    from arenamcp.card_facts.source_identity import legacy_lookup_hash
    from arenamcp.mtgjson import MTGJSONDatabase

    db = MTGJSONDatabase(tmp_path)
    raw = {
        "name": "Example",
        "type": "Artifact",
        "text": "{T}: Add {C}.",
        "manaCost": "{1}",
        "layout": "meld",
        "identifiers": {"mtgArenaId": "25"},
    }
    data = {"data": {"Example": [raw]}}
    db._cache_file.write_text(json.dumps(data))
    db._arena_index, db._name_index = db._build_indexes(data)
    db._save_indexes()
    assert db._load_indexes()
    db._available = True
    assert legacy_lookup_hash(MTGJSONAdapter(db).get_card_by_arena_id(25)) is None
    assert legacy_lookup_hash(MTGJSONAdapter(db).get_card_by_name("Example")) is None
    legacy = json.loads(db._name_index_file.read_text())
    legacy["example"].pop("layout")
    db._name_index_file.write_text(json.dumps(legacy))
    assert not db._load_indexes()


@pytest.mark.parametrize("key", ["linked_face_ids", "related_faces", "linked_faces", "card_faces"])
def test_source_veto_survives_projection_without_native_face_text(key):
    from arenamcp.card_facts.compiler import _printed_from_scryfall_card
    from arenamcp.card_facts.source_identity import single_face_source
    from arenamcp.scryfall import ScryfallCache

    raw = dict(name="Synthetic", mana_cost="{1}", type_line="Artifact", oracle_text="", layout="normal")
    cache = ScryfallCache.__new__(ScryfallCache)
    cache._name_index = {"synthetic": raw}
    for a, b in (
        (dict(raw, **{key: [{"name": "Other"}]}), raw),
        (raw, dict(raw, **{key: [{"name": "Other"}]})),
    ):
        projected = cache._card_dict_to_scryfall_card(a)
        assert not single_face_source(_printed_from_scryfall_card(projected, cache, raw=b))
