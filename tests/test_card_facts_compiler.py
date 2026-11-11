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

import json

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
)
from arenamcp.game_plan import BACKGROUND_PRIORITY
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
    assert result.record is None
    assert result.problems == ["answer contained no JSON object"]


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
