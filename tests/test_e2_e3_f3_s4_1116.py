"""Focused regression & contrary-control tests for batch E2 + E3 + F3 + S4 (2026-11-16)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from tests.deck_fakes import playbook_for
from tools.ingest_card_facts import CardFactsServer

from arenamcp.action_planner import ActionPlanner, _option_prints
from arenamcp.card_facts import client as cf_client
from arenamcp.card_facts.client import CardFactsClient
from arenamcp.card_facts.compiler import (
    _raw_mana_problems,
    _raw_notes_problems,
    _token_contract_problems,
    compile_one,
)
from arenamcp.card_facts.prompt_render import (
    _format_effect,
    _format_mode,
    _normalize_native_text,
    candidate_fact_hashes,
    compact_facts_summary,
    compact_history_rules,
    normalized_scryfall_card,
)
from arenamcp.card_facts.schema import (
    AddMana,
    Bounce,
    CardBody,
    CardFacts,
    Cost,
    CreateToken,
    Destroy,
    Draw,
    EffectEntry,
    Filter,
    GainLife,
    ManaProduction,
    Mode,
    Pump,
    PutCounters,
    ReturnFromGraveyard,
    TokenSpec,
    UnknownEffect,
    card_facts_from_dict,
)
from arenamcp.card_facts.source_identity import executable_source_hash
from arenamcp.card_facts.validate import PrintedCard, validate
from arenamcp.coach import CoachEngine
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.game_plan import GAME_PLAN_PROMPT, LIVE_PLAN_PROMPT, GamePlanManager
from arenamcp.match_context import (
    STRATEGIC_POLICY_CORE,
    decision_library_in_play,
    prepare_match_context,
    strategic_policy,
)
from arenamcp.situational_rules import _subjects_in_play

_REAL_DEFAULT_OPENER = cf_client._default_opener


# ── F3 compiler & validator tightening ─────────────────────────────────────────


def test_raw_notes_and_mana_guards_and_contrary_controls():
    fury = PrintedCard(
        name="Fury",
        mana_cost="{3}{R}{R}",
        type_line="Creature — Elemental Incarnation",
        oracle_text="Double strike\nEvoke—Exile a red card from your hand.",
        power="3",
        toughness="3",
    )
    assert _raw_notes_problems({"notes": ""}) == []
    assert _raw_notes_problems({"notes": "Do not play on empty board"})
    assert _raw_mana_problems({"mana": []}, fury) == []
    assert _raw_mana_problems({"mana": [{"mana": "{3}{R}{R}", "condition": ""}]}, fury)

    command_tower = PrintedCard(
        name="Command Tower",
        mana_cost="",
        type_line="Land",
        oracle_text="{T}: Add one mana of any color in your commander's color identity.",
    )
    assert _raw_mana_problems({"mana": [{"mana": "{C}", "condition": ""}]}, command_tower)
    assert (
        _raw_mana_problems(
            {
                "mana": [
                    {"mana": "{Any}", "condition": "one mana of any color in your commander's color identity"}
                ]
            },
            command_tower,
        )
        == []
    )


def test_threshold_and_parameterized_keyword_validation():
    spacecraft = PrintedCard(
        name="Class-F Shuttlecraft",
        mana_cost="{3}",
        type_line="Artifact — Spacecraft",
        oracle_text="{T}: Add one mana of any color.\nStation\n6+ | Flying, vigilance",
        power="2",
        toughness="4",
    )
    base_ok = CardFacts(
        name=spacecraft.name,
        mana_cost=spacecraft.mana_cost,
        type_line=spacecraft.type_line,
        body=CardBody(power="2", toughness="4", keywords=()),
        modes=(Mode(name="cast", cost=Cost(mana="{3}")),),
    )
    assert validate(base_ok, spacecraft) == []

    # Conditional threshold keywords must NOT be claimed as unconditional intrinsic keywords.
    claimed_conditional = CardFacts(
        name=spacecraft.name,
        mana_cost=spacecraft.mana_cost,
        type_line=spacecraft.type_line,
        body=CardBody(power="2", toughness="4", keywords=("flying", "vigilance")),
        modes=(Mode(name="cast", cost=Cost(mana="{3}")),),
    )
    problems = validate(claimed_conditional, spacecraft)
    assert any("conditional or granted" in p for p in problems)

    # Parameterized Ward satisfies stated "ward", while granted-only Ward fails if claimed intrinsic.
    hexing = PrintedCard(
        name="Hexing Squelcher",
        mana_cost="{1}{R}",
        type_line="Creature — Goblin Sorcerer",
        oracle_text='Ward—Pay 2 life.\nOther creatures you control have "Ward—Pay 2 life."',
        power="2",
        toughness="2",
    )
    ok_ward = CardFacts(
        name=hexing.name,
        mana_cost=hexing.mana_cost,
        type_line=hexing.type_line,
        body=CardBody(power="2", toughness="2", keywords=("Ward—Pay 2 life.",)),
        modes=(Mode(name="cast", cost=Cost(mana="{1}{R}")),),
    )
    assert validate(ok_ward, hexing) == []

    granted_only = PrintedCard(
        name="Ward Granter",
        mana_cost="{1}{W}",
        type_line="Creature — Human",
        oracle_text='Other creatures you control have "Ward—Pay 2 life."',
        power="2",
        toughness="2",
    )
    false_ward = CardFacts(
        name=granted_only.name,
        mana_cost=granted_only.mana_cost,
        type_line=granted_only.type_line,
        body=CardBody(power="2", toughness="2", keywords=("Ward—Pay 2 life.",)),
        modes=(Mode(name="cast", cost=Cost(mana="{1}{W}")),),
    )
    assert any("conditional or granted" in p for p in validate(false_ward, granted_only))


def test_token_contract_accepts_verbatim_paragraph_and_post_colon_tail():
    printed = PrintedCard(
        name="Photosynthetic Physician",
        mana_cost="{1}{G}",
        type_line="Creature — Elf Druid",
        oracle_text="Reach\n{G}, Exile this card from your graveyard: Create a 1/1 red Officer creature token. Activate only as a sorcery.",
        power="2",
        toughness="2",
    )
    tail_unknown = {
        "effects": [
            {
                "trigger": "activated",
                "effect": {
                    "effect_type": "Unknown",
                    "text": "Create a 1/1 red Officer creature token. Activate only as a sorcery.",
                },
            }
        ]
    }
    assert _token_contract_problems(tail_unknown, printed) == []

    bad_summary = {
        "effects": [{"trigger": "activated", "effect": {"effect_type": "Unknown", "text": "Create a token."}}]
    }
    assert _token_contract_problems(bad_summary, printed)

    class _FakeBackend:
        last_request_metrics = None

        def complete(self, *a, **k):
            return json.dumps(
                {
                    "name": "Test Spell",
                    "mana_cost": "{1}{R}",
                    "type_line": "Instant",
                    "body": {"power": None, "toughness": None, "loyalty": None, "keywords": []},
                    "modes": [
                        {
                            "name": "cast",
                            "cost": {"mana": "{1}{R}"},
                            "cost_text": "{1}{R}",
                            "stays_on_battlefield": False,
                        }
                    ],
                    "effects": [],
                    "mana": [],
                    "confidence": "high",
                    "notes": "must stay empty",
                }
            )

    card = PrintedCard("Test Spell", "{1}{R}", "Instant", "Deal 2 damage.")
    res = compile_one(card.name, SimpleNamespace(resolve=lambda _: card), _FakeBackend())
    assert res.status == "failed"


# ── S3/S4 User-Agent & disk warm ───────────────────────────────────────────────


def test_client_and_server_send_user_agent_header(monkeypatch, tmp_path):
    captured: list[dict[str, str]] = []

    class _DummyResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"found":{}}'

    def fake_urlopen(req, timeout=None):
        captured.append(dict(req.header_items()))
        return _DummyResp()

    monkeypatch.setattr("arenamcp.card_facts.client._default_opener", _REAL_DEFAULT_OPENER)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    st, _ = cf_client._default_opener(
        "https://example.invalid/lookup", b"{}", {"Content-Type": "application/json"}
    )
    assert st == 200 and captured[-1].get("User-agent", "").startswith("mtgacoach/")

    client = CardFactsClient(license_key="k", base_url="https://example.invalid", cache_dir=tmp_path)
    client._fetch(["a" * 64])
    assert captured[-1].get("User-agent", "").startswith("mtgacoach/")

    srv = CardFactsServer("https://example.invalid", "admin-secret")
    srv.statuses(["b" * 64])
    assert captured[-1].get("User-agent") == "mtgacoach-ingest/1.0"


def test_server_status_field_ignored_but_future_extension_preserved():
    base = {
        "schema_version": 1,
        "name": "Llanowar Elves",
        "rules_hash": "a" * 64,
        "type_line": "Creature — Elf Druid",
        "mana_cost": "{G}",
        "body": {"power": "1", "toughness": "1", "loyalty": None, "keywords": []},
        "modes": [],
        "effects": [],
        "mana": [],
        "confidence": "high",
        "notes": "",
    }
    rec_ok = card_facts_from_dict({**base, "status": "ok"})
    assert not getattr(rec_ok, "_unknown_fields", None)

    rec_ext = card_facts_from_dict({**base, "future_ext": 1})
    assert getattr(rec_ext, "_unknown_fields", None) == {"future_ext": 1}


def test_warm_memory_from_disk_positive_and_negative_controls(tmp_path):
    card = {
        "name": "Llanowar Elves",
        "mana_cost": "{G}",
        "type_line": "Creature — Elf Druid",
        "oracle_text": "{T}: Add {G}.",
    }
    digest = executable_source_hash(card)
    assert digest is not None
    client = CardFactsClient(cache_dir=tmp_path)
    client._store(digest, {"rules_hash": digest, "name": "Llanowar Elves"})
    client._memory.clear()

    # Corrupt/wrong hash file must not load; fresh valid file loads without queueing fetches.
    bad_hash = "0" * 64
    (tmp_path / f"{bad_hash}.json").write_text(
        json.dumps({"fetched_at": time.time(), "record": {"rules_hash": "1" * 64}})
    )
    loaded = client.warm_memory_from_disk([card], alt_hashes=[bad_hash])
    assert loaded == 1
    assert digest in client.fresh_memory_facts([card])
    assert bad_hash not in client._memory
    assert not client._want_fetch


# ── E2 prefix caching & structural invariance ─────────────────────────────────


def test_e2_shared_prefix_across_prompts_and_subjects_invariance():
    # STRATEGIC_POLICY_CORE leads strategic_policy for both empty and arbitrary subjects,
    # as well as GamePlan & Pay/Decline system prompts.
    for subjects in (frozenset(), frozenset({"equipment", "vehicle", "library"})):
        assert strategic_policy(subjects).startswith(STRATEGIC_POLICY_CORE)
    assert GAME_PLAN_PROMPT.startswith(STRATEGIC_POLICY_CORE)
    assert LIVE_PLAN_PROMPT.startswith(STRATEGIC_POLICY_CORE)
    assert ActionPlanner._PAY_DECLINE_SYSTEM_PROMPT.startswith(STRATEGIC_POLICY_CORE)

    catalog = {
        101: {
            "grp_id": 101,
            "name": "Short Sword",
            "type_line": "Artifact — Equipment",
            "oracle_text": "Equipped creature gets +1/+1.\nEquip {1}",
        }
    }
    state_unseen = {"deck_catalog": catalog, "battlefield": [], "hand": [], "graveyard": [], "exile": []}
    assert "equipment" not in _subjects_in_play(state_unseen, None, ())

    state_visible = {
        "deck_catalog": catalog,
        "battlefield": [{"grp_id": 101, "name": "Short Sword", "oracle_text": catalog[101]["oracle_text"]}],
        "hand": [],
        "graveyard": [],
        "exile": [],
    }
    assert "equipment" in _subjects_in_play(state_visible, None, ())


def test_e2_playbook_render_parts_and_seed_parts_partition():
    book = playbook_for()
    quoted = {e["card"] for m in book.data["mechanisms"] for e in m["evidence"]}
    full_unquoted, core_u, tail_u = book.render_parts(omit_oracle_for=())
    full_quoted, core_q, tail_q = book.render_parts(omit_oracle_for=quoted)
    assert full_unquoted == book.render(omit_oracle_for=())
    assert full_quoted == book.render(omit_oracle_for=quoted)
    assert core_u == core_q and core_u and tail_u != tail_q

    class _DummyBackend:
        def complete(self, *a, **k):
            return ""

    gp = GamePlanManager(_DummyBackend())
    gp.set_playbook_source(lambda: book)
    seed = gp._seed_text({"deck_rules_grp_ids": sorted(quoted)})
    core_block, tail_block = gp._seed_parts(seed, {"deck_rules_grp_ids": sorted(quoted)})
    assert core_block.startswith("DECK PLAYBOOK / STRATEGY:\n")
    assert "MECHANISM " in tail_block and "MECHANISM " not in core_block

    # Contrary control: altered seed outside render_parts falls back cleanly to single core block.
    fb_core, fb_tail = gp._seed_parts("Custom altered seed", {"deck_rules_grp_ids": sorted(quoted)})
    assert fb_core == "DECK PLAYBOOK / STRATEGY:\nCustom altered seed\n" and fb_tail == ""


# ── E3 compaction, {oX}/<i> normalization & library scoping ────────────────────


def test_e3_native_normalization_and_compact_summary():
    native_elves = {
        "grp_id": 10,
        "name": "Llanowar Elves",
        "mana_cost": "{oG}",
        "type_line": "Creature — Elf Druid",
        "oracle_text": "{oT}: Add {oG}. (<i>Tap to add green mana.</i>)",
    }
    canon = normalized_scryfall_card(
        {
            "name": "Llanowar Elves",
            "mana_cost": "{oG}",
            "type_line": "Creature — Elf Druid",
            "oracle_text": "{oT}: Add {oG}.",
        }
    )
    assert canon is not None and canon["mana_cost"] == "{G}" and canon["oracle_text"] == "{T}: Add {G}."
    assert normalized_scryfall_card(canon) is None
    assert "<i>" in native_elves["oracle_text"] and _normalize_native_text(
        native_elves["oracle_text"]
    ).startswith("{T}:")

    # Loyalty bracket normalization + non-creature dict without power/toughness + reminder-stripping UnknownEffect.
    assert _normalize_native_text("[+1]: Return land.\n[-2]: Deal 2.") == "+1: Return land.\n-2: Deal 2."

    long_card = {
        "name": "Verbose Beacon",
        "mana_cost": "{3}{U}{U}",
        "type_line": "Artifact",
        "oracle_text": "{T}: Add {U}.\n(Activated abilities that include the tap symbol cannot be activated while affected by summoning sickness.)",
    }
    long_digest = executable_source_hash(long_card)
    assert long_digest is not None and len(candidate_fact_hashes(long_card)) == 1
    rec = CardFacts(
        name=long_card["name"],
        mana_cost=long_card["mana_cost"],
        type_line=long_card["type_line"],
        modes=(Mode(name="cast", cost=Cost(mana="{3}{U}{U}")), Mode(name="activate", cost=Cost(tap=True))),
        effects=(EffectEntry(trigger="activated", effect=AddMana(mana="{U}", amount=1)),),
        mana=(ManaProduction(mana="{U}"),),
        rules_hash=long_digest,
        confidence="high",
        notes="",
    )
    summary = compact_facts_summary(long_card, rec)
    assert summary is not None and len(summary) < len(long_card["oracle_text"])

    rec_unknown_effect = CardFacts(
        name=long_card["name"],
        mana_cost=long_card["mana_cost"],
        type_line=long_card["type_line"],
        modes=rec.modes,
        effects=(
            EffectEntry(
                trigger="activated",
                effect=UnknownEffect(text="{T}: Add {U}.\n(Reminder text stripped.)"),
            ),
        ),
        mana=rec.mana,
        rules_hash=long_digest,
        confidence="high",
        notes="",
    )
    unknown_summary = compact_facts_summary(long_card, rec_unknown_effect)
    assert unknown_summary is not None and len(unknown_summary) < len(long_card["oracle_text"])

    # Contrary controls: unknown extension field, medium confidence, or notes!="" refuse compaction.
    rec_ext = card_facts_from_dict(
        {
            "name": long_card["name"],
            "mana_cost": long_card["mana_cost"],
            "type_line": long_card["type_line"],
            "rules_hash": long_digest,
            "confidence": "high",
            "notes": "",
            "future_ext": True,
        }
    )
    assert compact_facts_summary(long_card, rec_ext) is None
    rec_med = CardFacts(
        name=long_card["name"],
        mana_cost=long_card["mana_cost"],
        type_line=long_card["type_line"],
        modes=rec.modes,
        effects=rec.effects,
        mana=rec.mana,
        confidence="medium",
        rules_hash=long_digest,
    )
    assert compact_facts_summary(long_card, rec_med) is None
    rec_notes = CardFacts(
        name=long_card["name"],
        mana_cost=long_card["mana_cost"],
        type_line=long_card["type_line"],
        modes=rec.modes,
        effects=rec.effects,
        mana=rec.mana,
        confidence="high",
        notes="non-empty",
        rules_hash=long_digest,
    )
    assert compact_facts_summary(long_card, rec_notes) is None


def test_e3_coach_rules_dedup_native_and_library_scoping():
    # Native {oG} + (<i>...</i>) in zone matches clean Scryfall deck_reference entry.
    state = {
        "deck_reference": "DECK REFERENCE ...",
        "deck_rules_grp_ids": {55},
        "deck_catalog": {55: {"grp_id": 55, "name": "Llanowar Elves", "oracle_text": "{T}: Add {G}."}},
    }
    native_entry = {
        "grp_id": 55,
        "name": "Llanowar Elves",
        "oracle_text": "{oT}: Add {oG}. (<i>Spend only on Tuesday.</i>)",
    }
    assert CoachEngine._rules_in_deck_reference(native_entry, state) is True
    assert (
        CoachEngine._rules_in_deck_reference({**native_entry, "oracle_text": "{oT}: Add {oR}."}, state)
        is False
    )

    # Window-scoped library: unpayable fetchland in hand stays unlisted when compact;
    # payable/synthetic hand scry or Search request keeps full library listing.
    catalog = {
        101: {"grp_id": 101, "name": "Mountain", "type_line": "Basic Land — Mountain", "oracle_text": ""},
        102: {"grp_id": 102, "name": "Forest", "type_line": "Basic Land — Forest", "oracle_text": ""},
        103: {
            "grp_id": 103,
            "name": "Misty Rainforest",
            "type_line": "Land",
            "oracle_text": "{T}, Pay 1 life, Sacrifice this land: Search your library for a Forest or Island card, put it onto the battlefield, then shuffle.",
        },
    }
    gs_non_search = {
        "decision_context": {"type": "actions_available"},
        "legal_actions_raw": [],
        "deck_cards": [101, 101, 102, 102, 103],
        "library": [101, 101, 102, 102],
        "hand": [{"grp_id": 103, "name": "Misty Rainforest", "oracle_text": catalog[103]["oracle_text"]}],
        "battlefield": [],
        "graveyard": [],
        "exile": [],
        "deck_catalog": catalog,
        "pending_options": [],
    }
    assert decision_library_in_play(gs_non_search, offered=(), catalog=catalog) is False
    prep_compact = prepare_match_context(dict(gs_non_search), compact_decision=True, card_lookup=catalog.get)
    assert prep_compact["library_listed"] is False

    gs_hand_scry = {
        **gs_non_search,
        "hand": [{"grp_id": 103, "name": "Misty Rainforest", "oracle_text": "Scry 2, then draw a card."}],
    }
    gs_hand_scry.pop("legal_actions_raw")
    assert decision_library_in_play(gs_hand_scry, offered=(), catalog=catalog) is True

    gs_search = dict(gs_non_search, decision_context={"type": "search"})
    assert decision_library_in_play(gs_search, offered={103}, catalog=catalog) is True
    prep_search = prepare_match_context(dict(gs_search), compact_decision=True, card_lookup=catalog.get)
    assert prep_search["library_listed"] is True

    # _option_prints resolves permanent instance targets and marks mull:/optional:/seat/token bound.
    dec = SimpleNamespace(
        options=(
            SimpleNamespace(option_id="mull:keep", meta={}),
            SimpleNamespace(option_id="optional:accept", meta={}),
            SimpleNamespace(option_id="tgt:2", meta={}),
            SimpleNamespace(option_id="tgt:500", meta={}),
            SimpleNamespace(option_id="tgt:501", meta={}),
        )
    )
    gs_targets = {
        "battlefield": [
            {"instance_id": 500, "grp_id": 101, "is_token": False},
            {"instance_id": 501, "grp_id": 0, "is_token": True},
        ]
    }
    prints, all_bound = _option_prints(dec, gs_targets)
    assert prints == [101] and all_bound is True

    # Without cached record loaded, compact_history_rules returns {}.
    assert compact_history_rules(state["deck_catalog"]) == {}


def test_reviewer_findings_regressions_and_controls(tmp_path):
    # 1. _format_mode non-duplication, sacrifice count/condition, and Additional cost label.
    assert (
        _format_mode(Mode(name="activate", cost=Cost(tap=True), cost_text="{T}"), "{2}", True)
        == "activate: {T}"
    )
    assert (
        _format_mode(
            Mode(
                name="activate",
                cost=Cost(sacrifice=Filter(target="artifact", count=2, condition="other")),
                cost_text="",
            ),
            "{2}",
            True,
        )
        == "activate: Sac 2 artifact (other)"
    )
    assert (
        _format_mode(
            Mode(name="cast", cost=Cost(mana="{1}{B}"), cost_text="{1}{B}"),
            "{1}{B}",
            True,
        )
        == ""
    )
    assert (
        _format_mode(
            Mode(
                name="cast", cost=Cost(mana="{1}{B}", sacrifice=Filter(target="creature")), cost_text="{1}{B}"
            ),
            "{1}{B}",
            True,
        )
        == "Additional cost: Sac creature"
    )
    assert (
        _format_mode(
            Mode(
                name="cast",
                cost=Cost(mana="{1}{B}", sacrifice=Filter(target="creature")),
                cost_text="{1}{B}, Sacrifice a creature",
            ),
            "{1}{B}",
            True,
        )
        == "Additional cost: {1}{B}, Sacrifice a creature"
    )

    # 2. _format_effect (CreateToken) modifiers/copy/types/per_opponent + compact_facts_summary lossy/multi-mode rejection.
    warrior = EffectEntry(
        trigger="attacks",
        effect=CreateToken(
            TokenSpec(
                name="Warrior",
                count=1,
                power=1,
                toughness=1,
                types=("Artifact", "Creature"),
                tapped=True,
                attacking=True,
                not_legendary=True,
                per_opponent=True,
            )
        ),
    )
    rendered_warrior = _format_effect(warrior, is_permanent=True)
    for expected in ("nonlegendary", "tapped", "attacking", "Artifact", "per opponent"):
        assert expected in rendered_warrior
    copy_eff = EffectEntry(
        trigger="ETB",
        effect=CreateToken(TokenSpec(name="Self", count=1, copy_of_self=True)),
    )
    assert "copy of self" in _format_effect(copy_eff, is_permanent=True)

    lossy_card = {
        "name": "Wordy Spell",
        "mana_cost": "{1}{G}",
        "type_line": "Instant",
        "oracle_text": "Target creature you control gets +2/+2 until end of turn. Draw a card if you control a Mount.",
    }
    lossy_hash = executable_source_hash(lossy_card)
    assert lossy_hash is not None
    for eff in (
        PutCounters(counter_type="+1/+1", count=2),
        Draw(count=1),
        GainLife(amount=3),
        Pump(power=2, toughness=2, target="target creature"),
        ReturnFromGraveyard(target_filter="creature"),
    ):
        lossy_rec = CardFacts(
            name=lossy_card["name"],
            mana_cost=lossy_card["mana_cost"],
            type_line=lossy_card["type_line"],
            modes=(Mode(name="cast", cost=Cost(mana="{1}{G}"), stays_on_battlefield=False),),
            effects=(EffectEntry(trigger="cast", effect=eff),),
            rules_hash=lossy_hash,
            confidence="high",
            notes="",
        )
        assert compact_facts_summary(lossy_card, lossy_rec) is None

    multi_non_cast = CardFacts(
        name=lossy_card["name"],
        mana_cost=lossy_card["mana_cost"],
        type_line=lossy_card["type_line"],
        modes=(
            Mode(name="cast", cost=Cost(mana="{1}{G}"), stays_on_battlefield=False),
            Mode(name="activate", cost=Cost(tap=True)),
            Mode(name="activate", cost=Cost(mana="{2}")),
        ),
        effects=(EffectEntry(trigger="activated", effect=Destroy(target="artifact")),),
        rules_hash=lossy_hash,
        confidence="high",
        notes="",
    )
    assert compact_facts_summary(lossy_card, multi_non_cast) is None
    one_non_cast_two_effs = CardFacts(
        name=lossy_card["name"],
        mana_cost=lossy_card["mana_cost"],
        type_line=lossy_card["type_line"],
        modes=(
            Mode(name="cast", cost=Cost(mana="{1}{G}"), stays_on_battlefield=False),
            Mode(name="activate", cost=Cost(tap=True)),
        ),
        effects=(
            EffectEntry(trigger="activated", effect=Destroy(target="artifact")),
            EffectEntry(trigger="activated", effect=Bounce(target="creature")),
        ),
        rules_hash=lossy_hash,
        confidence="high",
        notes="",
    )
    assert compact_facts_summary(lossy_card, one_non_cast_two_effs) is None

    # 3. validate: parameterized Ward/Protection/Hexproof from checks and granted-keyword separation.
    ward_card = PrintedCard(
        name="Ward Test",
        mana_cost="{2}{U}",
        type_line="Creature — Sphinx",
        oracle_text="Flying, ward {2}",
        power="3",
        toughness="3",
    )
    assert (
        validate(
            CardFacts(
                name=ward_card.name,
                mana_cost=ward_card.mana_cost,
                type_line=ward_card.type_line,
                body=CardBody(power="3", toughness="3", keywords=("Flying", "Ward {2}")),
            ),
            ward_card,
        )
        == []
    )
    assert validate(
        CardFacts(
            name=ward_card.name,
            mana_cost=ward_card.mana_cost,
            type_line=ward_card.type_line,
            body=CardBody(power="3", toughness="3", keywords=("Flying", "Ward")),
        ),
        ward_card,
    )
    assert validate(
        CardFacts(
            name=ward_card.name,
            mana_cost=ward_card.mana_cost,
            type_line=ward_card.type_line,
            body=CardBody(power="3", toughness="3", keywords=("Flying", "Ward {1}")),
        ),
        ward_card,
    )
    ward_pay = PrintedCard(
        name="Ward Pay Test",
        mana_cost="{1}{B}",
        type_line="Creature — Zombie",
        oracle_text="Ward—Pay 2 life (Whenever this creature becomes the target of a spell or ability an opponent controls, counter it unless that player pays 2 life.)",
        power="2",
        toughness="2",
    )
    assert (
        validate(
            CardFacts(
                name=ward_pay.name,
                mana_cost=ward_pay.mana_cost,
                type_line=ward_pay.type_line,
                body=CardBody(power="2", toughness="2", keywords=("Ward—Pay 2 life",)),
            ),
            ward_pay,
        )
        == []
    )
    assert validate(
        CardFacts(
            name=ward_pay.name,
            mana_cost=ward_pay.mana_cost,
            type_line=ward_pay.type_line,
            body=CardBody(power="2", toughness="2", keywords=("Ward—Pay 2",)),
        ),
        ward_pay,
    )
    hex_from = PrintedCard(
        name="Hex Test",
        mana_cost="{1}{W}",
        type_line="Creature — Knight",
        oracle_text="Hexproof from black",
        power="2",
        toughness="2",
    )
    assert (
        validate(
            CardFacts(
                name=hex_from.name,
                mana_cost=hex_from.mana_cost,
                type_line=hex_from.type_line,
                body=CardBody(power="2", toughness="2", keywords=("Hexproof from black",)),
            ),
            hex_from,
        )
        == []
    )
    assert validate(
        CardFacts(
            name=hex_from.name,
            mana_cost=hex_from.mana_cost,
            type_line=hex_from.type_line,
            body=CardBody(power="2", toughness="2", keywords=("Hexproof",)),
        ),
        hex_from,
    )
    ward_grant = PrintedCard(
        name="Greaves Test",
        mana_cost="{2}",
        type_line="Artifact — Equipment",
        oracle_text="Equipped creature has ward {2}.\nEquip {1}",
    )
    assert (
        validate(
            CardFacts(
                name=ward_grant.name,
                mana_cost=ward_grant.mana_cost,
                type_line=ward_grant.type_line,
                body=CardBody(keywords=()),
            ),
            ward_grant,
        )
        == []
    )
    assert validate(
        CardFacts(
            name=ward_grant.name,
            mana_cost=ward_grant.mana_cost,
            type_line=ward_grant.type_line,
            body=CardBody(keywords=("Ward {2}",)),
        ),
        ward_grant,
    )

    # 4. _token_contract_problems pre-colon grants + trailing "Activate only ..." + non-list mana guard.
    grant_token_card = PrintedCard(
        name="Goblin Hammer",
        mana_cost="{2}",
        type_line="Artifact — Equipment",
        oracle_text='Equipped creature has "{T}: Create a 1/1 red Goblin creature token."\nEquip {2}',
    )
    assert _token_contract_problems(
        {
            "effects": [
                {
                    "trigger": "static",
                    "effect": {"effect_type": "Unknown", "text": "Create a 1/1 red Goblin creature token."},
                }
            ]
        },
        grant_token_card,
    )
    assert (
        _token_contract_problems(
            {
                "effects": [
                    {
                        "trigger": "static",
                        "effect": {
                            "effect_type": "Unknown",
                            "text": 'Equipped creature has "{T}: Create a 1/1 red Goblin creature token."',
                        },
                    }
                ]
            },
            grant_token_card,
        )
        == []
    )
    physician_like = PrintedCard(
        name="Photosynthetic Physician",
        mana_cost="{4}{G}",
        type_line="Creature — Plant Doctor",
        oracle_text="Reach\n{G}, Exile this card from your graveyard: Create a 1/1 red Officer creature token. Activate only as a sorcery.",
        power="5",
        toughness="5",
    )
    assert _token_contract_problems(
        {
            "effects": [
                {
                    "trigger": "activated",
                    "effect": {"effect_type": "Unknown", "text": "Create a 1/1 red Officer creature token."},
                }
            ]
        },
        physician_like,
    )
    assert (
        _token_contract_problems(
            {
                "effects": [
                    {
                        "trigger": "activated",
                        "effect": {
                            "effect_type": "Unknown",
                            "text": "Create a 1/1 red Officer creature token. Activate only as a sorcery.",
                        },
                    }
                ]
            },
            physician_like,
        )
        == []
    )
    assert _raw_mana_problems({"mana": "{G}"}, physician_like) == ["mana must be a list"]

    # 5. decision_library_in_play: groupContext scry/surveil & SPELL_CAST_TYPES.
    base_lib_state = {
        "deck_cards": [101, 102, 103],
        "library": [101, 102],
        "hand": [{"grp_id": 103, "name": "Opt Venture", "oracle_text": "Scry 2, then draw a card."}],
        "battlefield": [],
        "graveyard": [],
        "exile": [],
        "legal_actions_raw": [],
    }
    assert (
        decision_library_in_play(
            {
                **base_lib_state,
                "decision_context": {
                    "type": "group_selection",
                    "raw": {"groupContext": "GroupingContext_Scry"},
                },
            }
        )
        is True
    )
    assert (
        decision_library_in_play(
            {
                **base_lib_state,
                "decision_context": {
                    "type": "group_selection",
                    "raw": {"groupContext": "GroupingContext_Other"},
                },
            }
        )
        is False
    )
    assert (
        decision_library_in_play(
            {
                **base_lib_state,
                "decision_context": {"type": "actions_available"},
                "legal_actions_raw": [
                    {"actionType": "ActionType_CastAdventure", "grpId": 103, "hasAutoTap": True}
                ],
            }
        )
        is True
    )
    assert (
        decision_library_in_play(
            {
                **base_lib_state,
                "decision_context": {"type": "actions_available"},
                "legal_actions_raw": [
                    {"actionType": "ActionType_CastAdventure", "grpId": 103, "hasAutoTap": False}
                ],
            }
        )
        is False
    )

    # 6. _option_prints zone_by_iid vs player seat fallback + window_request_type benign source_label.
    seat_or_perm = SimpleNamespace(
        options=(SimpleNamespace(option_id="tgt:1", meta={}), SimpleNamespace(option_id="tgt:2", meta={}))
    )
    prints_perm, bound_perm = _option_prints(
        seat_or_perm,
        {"battlefield": [{"instance_id": 1, "grp_id": 101, "is_token": False}]},
    )
    assert prints_perm == [101] and bound_perm is True
    _, bound_unknown = _option_prints(
        SimpleNamespace(options=(SimpleNamespace(option_id="tgt:999", meta={}),)),
        {"battlefield": []},
    )
    assert bound_unknown is False

    seen_reqs: list[str | None] = []

    class _StubBackend:
        def complete(self, *a, **k):
            return '{"option_ids": ["tgt:500"], "reason": "ok"}'

    planner = ActionPlanner(_StubBackend(), timeout=5.0, land_drop_first=False, deck_playbook_fn=playbook_for)
    orig_strat = planner._stable_strategy_context
    planner._stable_strategy_context = lambda s, *, request_type=None: (
        seen_reqs.append(request_type) or orig_strat(s, request_type=request_type)
    )
    plan_state = {
        "players": [
            {"seat_id": 1, "is_local": True, "life": 20},
            {"seat_id": 2, "is_local": False, "life": 20},
        ],
        "turn_info": {"turn_number": 3, "active_player": 1, "phase": "Phase_Main1"},
        "deck_cards": [101],
        "battlefield": [
            {"instance_id": 500, "grp_id": 101, "name": "Bear A", "controller_seat_id": 2},
            {"instance_id": 501, "grp_id": 102, "name": "Bear B", "controller_seat_id": 2},
        ],
        "hand": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "legal_actions_raw": [],
        "deck_catalog": {
            101: {"grp_id": 101, "name": "Bear A", "type_line": "Creature — Bear", "oracle_text": ""},
            102: {"grp_id": 102, "name": "Bear B", "type_line": "Creature — Bear", "oracle_text": ""},
        },
    }
    dec_combat_src = PendingDecision(
        request_id=(1, 1),
        request_type="SelectTargets",
        source_label="Combat Celebrant",
        min_select=1,
        max_select=1,
        options=(
            DecisionOption(option_id="tgt:500", label="Bear A"),
            DecisionOption(option_id="tgt:501", label="Bear B"),
        ),
    )
    planner.plan_decision_options(dec_combat_src, dict(plan_state))
    assert seen_reqs[-1] == "SelectTargets"
    dec_mull_src = PendingDecision(
        request_id=(1, 2),
        request_type="GroupSelect",
        source_label="LondonMulligan",
        min_select=1,
        max_select=1,
        options=(
            DecisionOption(option_id="grp:1", label="Card 1"),
            DecisionOption(option_id="grp:2", label="Card 2"),
        ),
    )
    planner.plan_decision_options(dec_mull_src, dict(plan_state))
    assert seen_reqs[-1] == "GroupSelect LondonMulligan"

    # 7. warm_memory_from_disk hex & monotonic freshness guards.
    now = time.time()
    cache_dir = tmp_path / "cache"
    client = CardFactsClient(cache_dir=cache_dir, clock=lambda: now)
    good_hex = "a" * 64
    client._envelope_path(good_hex).parent.mkdir(parents=True, exist_ok=True)
    client._envelope_path(good_hex).write_text(
        json.dumps(
            {
                "fetched_at": now - 100,
                "record": {"name": "OlderDisk", "rules_hash": good_hex, "confidence": "high"},
            }
        ),
        encoding="utf-8",
    )
    # Concurrent fresher memory write arriving before disk load finishes must not be overwritten.
    orig_read = client._read_disk

    def _racy_read(digest: str):
        val = orig_read(digest)
        with client._lock:
            client._memory[good_hex] = ({"name": "InMemoryNewer", "rules_hash": good_hex}, now - 5)
        return val

    client._read_disk = _racy_read  # type: ignore[method-assign]
    assert client.warm_memory_from_disk([], alt_hashes=("g" * 64, "B" * 64, "abcd", good_hex)) == 0
    with client._lock:
        assert client._memory[good_hex][0]["name"] == "InMemoryNewer"

    client2 = CardFactsClient(cache_dir=cache_dir, clock=lambda: now)
    bad_hex_g = "g" * 64
    bad_hex_upper = "b" * 63 + "A"
    for bad in (bad_hex_g, bad_hex_upper):
        client2._envelope_path(bad).write_text(
            json.dumps({"fetched_at": now - 10, "record": {"name": "Bad", "rules_hash": bad}}),
            encoding="utf-8",
        )
    assert client2.warm_memory_from_disk([], alt_hashes=(bad_hex_g, bad_hex_upper, "abcd", good_hex)) == 1
    with client2._lock:
        assert (
            client2._memory[good_hex][0]["name"] == "OlderDisk"
            and bad_hex_g not in client2._memory
            and bad_hex_upper not in client2._memory
        )
