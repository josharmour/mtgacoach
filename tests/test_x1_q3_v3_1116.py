"""Focused regression and contrary-control tests for batch X1 + Q3 + V3 (2026-11-16)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

from arenamcp import standalone_decks
from arenamcp.card_facts import client as cf_client
from arenamcp.card_facts.client import CardFactsClient
from arenamcp.card_facts.prompt_render import candidate_fact_hashes, normalized_scryfall_card
from arenamcp.deck_library import DeckRecord
from arenamcp.match_bundle import MatchBundleContext, _app_info, build_bundle
from arenamcp.play_safety import (
    _damage_protected,
    _kicker_affordable,
    card_facts_removal_reason,
    damage_effect,
    death_trigger_benefit,
    exact_mana_total,
    fixed_damage_removal,
    our_stack_damage,
    pending_ward_reserve_reason,
    power_only_debuff_wasted,
    removal_lacks_opponent_target,
)


def _record(card: dict, *, modes=(), effects=(), mana=(), keywords=(), rules_hash: str | None = None) -> dict:
    h = rules_hash or cf_client.hash_for_card(card)
    return {
        "schema_version": 1,
        "name": card["name"],
        "mana_cost": card["mana_cost"],
        "type_line": card["type_line"],
        "rules_hash": h,
        "body": {"keywords": list(keywords)},
        "modes": list(modes),
        "effects": list(effects),
        "mana": list(mana),
        "confidence": "high",
        "notes": "",
    }


def _seed_store(monkeypatch, records: list[tuple[dict, dict]]):
    store: dict[str, dict] = {}
    for card, rec in records:
        norm = normalized_scryfall_card(card)
        h = cf_client.hash_for_card(norm or card)
        assert h
        rec["rules_hash"] = h
        store[h] = rec
    seen_calls: list[dict] = []

    def _facts_for(cards, **kwargs):
        card_list = list(cards)
        seen_calls.append({"cards": card_list, "kwargs": dict(kwargs)})
        wanted: set[str] = set()
        for card in card_list:
            wanted.update(candidate_fact_hashes(card))
            norm = normalized_scryfall_card(card)
            if norm:
                h = cf_client.hash_for_card(norm)
                if h:
                    wanted.add(h)
        return {k: store[k] for k in wanted if k in store}

    monkeypatch.setattr(cf_client, "facts_for", _facts_for)
    return store, seen_calls


def test_x1_record_first_and_deleted_patterns_with_contrary_controls(monkeypatch, tmp_path):
    # Unparsed custom oracle_text proves answers come strictly from CardFacts records, never regexes.
    ring = {
        "instance_id": 10,
        "grp_id": 1001,
        "name": "Sol Ring",
        "mana_cost": "{1}",
        "type_line": "Artifact",
        "oracle_text": "{T}: Add {C} (opaque oracle).",
        "controller_seat_id": 1,
    }
    ring_rec = _record(
        ring,
        modes=[
            {"name": "cast", "cost": {"mana": "{1}"}, "stays_on_battlefield": True},
            {"name": "activate", "cost": {"tap": True}, "cost_text": "{T}", "stays_on_battlefield": True},
        ],
        effects=[{"trigger": "activated", "effect": {"effect_type": "AddMana", "mana": "{C}", "amount": 2}}],
        mana=[{"mana": "{C}{C}", "condition": ""}],
    )
    uncached_filter = {
        "instance_id": 11,
        "name": "Uncached Filter",
        "mana_cost": "{2}",
        "type_line": "Artifact",
        "oracle_text": "{1}, {T}: Add {G}.",
        "controller_seat_id": 1,
    }
    forest = {
        "instance_id": 12,
        "name": "Forest",
        "mana_cost": "",
        "type_line": "Basic Land — Forest",
        "oracle_text": "",
        "controller_seat_id": 1,
    }
    counterspell = {
        "name": "Opaque Counter",
        "mana_cost": "{U}{U}",
        "type_line": "Instant",
        "oracle_text": "Unparsed custom counter text.",
    }
    counter_rec = _record(
        counterspell,
        modes=[{"name": "cast", "cost": {"mana": "{U}{U}"}, "stays_on_battlefield": False}],
        effects=[{"trigger": "cast", "effect": {"effect_type": "Counter", "target": "target spell"}}],
    )
    burn_spell = {
        "name": "Opaque Bolt",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Unparsed custom damage text.",
    }
    burn_rec = _record(
        burn_spell,
        modes=[{"name": "cast", "cost": {"mana": "{R}"}, "stays_on_battlefield": False}],
        effects=[
            {
                "trigger": "cast",
                "effect": {"effect_type": "Damage", "amount": 3, "target": "target creature or planeswalker"},
            }
        ],
    )
    sweeper = {
        "name": "Opaque Sweep",
        "mana_cost": "{R}",
        "type_line": "Sorcery",
        "oracle_text": "Unparsed dual each clause.",
    }
    sweeper_rec = _record(
        sweeper,
        modes=[{"name": "cast", "cost": {"mana": "{R}"}, "stays_on_battlefield": False}],
        effects=[
            {"trigger": "cast", "effect": {"effect_type": "Damage", "amount": 2, "target": "each opponent"}},
            {
                "trigger": "cast",
                "effect": {"effect_type": "Damage", "amount": 2, "target": "each creature they control"},
            },
        ],
    )
    boltwave = {
        "instance_id": 21,
        "name": "Opaque Boltwave",
        "mana_cost": "{R}",
        "type_line": "Sorcery",
        "oracle_text": "Unparsed face damage.",
        "controller_seat_id": 1,
    }
    boltwave_rec = _record(
        boltwave,
        modes=[{"name": "cast", "cost": {"mana": "{R}"}, "stays_on_battlefield": False}],
        effects=[
            {"trigger": "cast", "effect": {"effect_type": "Damage", "amount": 3, "target": "each opponent"}}
        ],
    )
    kill_spell = {
        "name": "Opaque Murder",
        "mana_cost": "{1}{B}{B}",
        "type_line": "Instant",
        "oracle_text": "Unparsed removal.",
    }
    kill_rec = _record(
        kill_spell,
        modes=[{"name": "cast", "cost": {"mana": "{1}{B}{B}"}, "stays_on_battlefield": False}],
        effects=[{"trigger": "cast", "effect": {"effect_type": "Destroy", "target": "target creature"}}],
    )
    kicked_card = {
        "name": "Opaque Kicker",
        "mana_cost": "{1}{G}",
        "type_line": "Instant",
        "oracle_text": "Opaque text.",
    }
    kicker_rec = _record(
        kicked_card,
        modes=[
            {"name": "cast", "cost": {"mana": "{1}{G}"}, "stays_on_battlefield": False},
            {"name": "kicker", "cost": {"mana": "{2}{B}"}, "stays_on_battlefield": False},
        ],
    )
    death_card = {
        "name": "Opaque Death Trigger",
        "mana_cost": "{B}",
        "type_line": "Creature — Zombie",
        "oracle_text": "Unparsed dies trigger.",
    }
    death_rec = _record(
        death_card,
        effects=[{"trigger": "dies", "effect": {"effect_type": "Draw", "count": 1}}],
    )
    ward_target_card = {
        "name": "Opaque Hex",
        "mana_cost": "{1}{G}",
        "type_line": "Creature",
        "oracle_text": "Plain text.",
        "keywords": [],
    }
    hex_rec = _record(ward_target_card, keywords=["Hexproof"])
    icy = {
        "name": "Opaque Shrink",
        "mana_cost": "{1}{U}",
        "type_line": "Instant",
        "oracle_text": "Unparsed modal shrink.",
    }
    icy_rec = _record(
        icy,
        modes=[{"name": "cast", "cost": {"mana": "{1}{U}"}, "stays_on_battlefield": False}],
        effects=[
            {"trigger": "cast", "effect": {"effect_type": "Counter", "target": "target creature spell"}},
            {
                "trigger": "cast",
                "effect": {"effect_type": "Pump", "power": -5, "toughness": 0, "target": "target creature"},
            },
        ],
    )
    etb_evoke = {
        "name": "Opaque Evoke",
        "mana_cost": "{4}{R}",
        "type_line": "Creature — Elemental",
        "oracle_text": "Unparsed evoke damage.",
    }
    etb_rec = _record(
        etb_evoke,
        modes=[{"name": "evoke", "cost_text": "Evoke—Exile red", "stays_on_battlefield": False}],
        effects=[
            {"trigger": "ETB", "effect": {"effect_type": "Damage", "amount": 2, "target": "target creature"}}
        ],
    )

    orig_facts_for = cf_client.facts_for
    _, calls = _seed_store(
        monkeypatch,
        [
            (ring, ring_rec),
            (counterspell, counter_rec),
            (burn_spell, burn_rec),
            (sweeper, sweeper_rec),
            (boltwave, boltwave_rec),
            (kill_spell, kill_rec),
            (kicked_card, kicker_rec),
            (death_card, death_rec),
            (ward_target_card, hex_rec),
            (icy, icy_rec),
            (etb_evoke, etb_rec),
        ],
    )

    state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 4, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": [forest, ring],
        "stack": [boltwave],
        "hand": [],
    }
    assert exact_mana_total(state) == 3  # Forest(1) + Sol Ring(1 base + 1 extra via CardFacts)
    assert all(c["kwargs"].get("memory_only") is True for c in calls)

    # Deleted uncached paid-filter falls safe to None via _INEXACT_MANA instead of phantom +1.
    assert exact_mana_total({**state, "battlefield": [forest, uncached_filter]}) is None

    # pending_ward_reserve_reason exempts CounterSpell via CardFacts without oracle_text regex.
    ward_board = {
        **state,
        "battlefield": [
            forest,
            {
                "instance_id": 99,
                "name": "Forest",
                "controller_seat_id": 1,
                "type_line": "Land",
                "oracle_text": "",
            },
            {
                "instance_id": 50,
                "name": "Ward Guy",
                "controller_seat_id": 2,
                "type_line": "Creature",
                "keywords": ["Ward"],
                "oracle_text": "Ward {2}",
            },
        ],
        "stack": [
            {"instance_id": 60, "controller_seat_id": 1, "targeting": [50]},
            {
                "instance_id": 61,
                "controller_seat_id": 2,
                "object_kind": "ABILITY",
                "parent_instance_id": 50,
                "source_instance_id": 50,
                "oracle_text": "Ward {2}",
            },
        ],
    }
    assert pending_ward_reserve_reason(ward_board, counterspell, "cast", {"manaCost": "{U}{U}"}) == ""
    # Cold uncached counterspell (rec is None) and modal UnknownEffect counterspell both still exempt.
    cold_counter = {
        "name": "Cold Counter",
        "mana_cost": "{1}{U}",
        "type_line": "Instant",
        "oracle_text": "Counter target spell.",
    }
    prime_directive = {
        "name": "Consider the Prime Directive",
        "mana_cost": "{2}{U}",
        "type_line": "Instant",
        "oracle_text": "Choose one — Interfere — Counter target spell unless its controller pays {3}. Observe — Draw a card.",
    }
    prime_rec = _record(
        prime_directive,
        modes=[{"name": "cast", "cost": {"mana": "{2}{U}"}, "stays_on_battlefield": False}],
        effects=[
            {
                "trigger": "cast (choose Interfere)",
                "conditions": ["Choose Interfere"],
                "effect": {
                    "effect_type": "Unknown",
                    "text": "Interfere — Counter target spell unless its controller pays {3}.",
                },
            }
        ],
    )
    non_counter_unknown = {
        "name": "Observe Only",
        "mana_cost": "{U}",
        "type_line": "Instant",
        "oracle_text": "Tap target creature.",
    }
    non_counter_rec = _record(
        non_counter_unknown,
        modes=[{"name": "cast", "cost": {"mana": "{U}"}, "stays_on_battlefield": False}],
        effects=[{"trigger": "cast", "effect": {"effect_type": "Unknown", "text": "Tap target creature."}}],
    )
    for card_obj, card_rec in ((prime_directive, prime_rec), (non_counter_unknown, non_counter_rec)):
        h = cf_client.hash_for_card(normalized_scryfall_card(card_obj) or card_obj)
        assert h
        card_rec["rules_hash"] = h
        _[h] = card_rec
    assert pending_ward_reserve_reason(ward_board, cold_counter, "cast", {"manaCost": "{1}{U}"}) == ""
    assert pending_ward_reserve_reason(ward_board, prime_directive, "cast", {"manaCost": "{2}{U}"}) == ""
    assert "Reserve 2 mana" in pending_ward_reserve_reason(
        ward_board, non_counter_unknown, "cast", {"manaCost": "{U}"}
    )
    assert "Reserve 2 mana" in pending_ward_reserve_reason(
        ward_board, burn_spell, "cast", {"manaCost": "{R}"}
    )

    assert fixed_damage_removal(burn_spell) == (3, True)
    assert damage_effect(burn_spell, state) == (3, "target")
    assert damage_effect(sweeper, state) == (2, "each")
    assert our_stack_damage(state).face == 3
    assert _kicker_affordable(kicked_card, state) is False  # needs 5 vs 2 mana
    assert removal_lacks_opponent_target(kill_spell, state) is True
    assert (
        removal_lacks_opponent_target(
            kill_spell,
            {
                **state,
                "battlefield": [
                    *state["battlefield"],
                    {
                        "instance_id": 77,
                        "name": "Bear",
                        "controller_seat_id": 2,
                        "type_line": "Creature — Bear",
                    },
                ],
            },
        )
        is False
    )
    assert death_trigger_benefit(death_card)
    assert _damage_protected(ward_target_card, targeted=True) is True
    assert _damage_protected(ward_target_card, targeted=False) is False
    assert power_only_debuff_wasted(icy, state)
    assert power_only_debuff_wasted({**icy, "oracle_text": "Until your next turn shrink."}, state) == ""
    assert "leaves no permanent" in card_facts_removal_reason(
        etb_evoke,
        {**state, "stack": []},
        {"alternativeCostText": "Evoke—Exile red card"},
    )

    # Non-blocking verification with a real CardFactsClient (no network opener calls or disk waits).
    net_calls = 0

    def _fail_opener(*a, **k):
        nonlocal net_calls
        net_calls += 1
        return 503, "offline"

    real_client = CardFactsClient(cache_dir=tmp_path / "cf", opener=_fail_opener)
    monkeypatch.setattr(cf_client, "facts_for", orig_facts_for)
    monkeypatch.setattr(cf_client, "_shared", real_client)
    assert fixed_damage_removal(burn_spell) is None
    assert net_calls == 0
    with real_client._lock:
        assert real_client._want_fetch == {}


def test_q3_analyze_all_decks_orders_newest_and_rebuilds_compact_match_playbooks(monkeypatch):
    monkeypatch.setattr(standalone_decks._DeckLibraryMixin, "_ANALYZE_ALL_MATCH_POLL_S", 0.001)
    d_old = DeckRecord("old", "Old Deck", main=[(100, 60)], last_played="2026-11-01T10:00:00Z")
    d_unplayed = DeckRecord("unplayed", "Zebra", main=[(104, 60)], last_played=None)  # type: ignore[arg-type]
    d_snap = DeckRecord(
        "snap", "Snap Deck", main=[(101, 60)], last_played="2026-11-16T12:00:00Z", snapshot=True
    )
    d_new_match = DeckRecord(
        "new_match", "New Match-Only", main=[(102, 60)], last_played="2026-11-16T10:00:00Z"
    )
    d_idle_done = DeckRecord("idle_done", "Idle Done", main=[(103, 60)], last_played="2026-11-16T11:00:00Z")

    built: list[str] = []

    class _FakeCoach:
        def _cached_playbook(self, identity, state, modes=("match", "idle")):
            if identity == d_idle_done.identity():
                return SimpleNamespace(
                    identity=identity,
                    catalog={},
                    data={"archetype": "Done", "mechanisms": [], "card_roles": {}, "phases": {}},
                )
            if identity == d_new_match.identity() and "match" in modes and "idle" not in modes:
                return SimpleNamespace(
                    identity=identity,
                    catalog={},
                    data={"archetype": "Compact", "mechanisms": [], "card_roles": {}, "phases": {}},
                )
            return None

    class _Lib(standalone_decks._DeckLibraryMixin):
        def __init__(self):
            self._coach = _FakeCoach()
            self._deck_library = {r.deck_id: r for r in (d_unplayed, d_old, d_snap, d_new_match, d_idle_done)}
            self.ui = Mock()

        def analyze_deck_record(self, record, *, refresh=False):
            built.append(record.deck_id)
            return {"deck_id": record.deck_id, "status": "ready"}

        def deck_strategy_status(self, record):
            return {"status": "missing"}

    lib = _Lib()
    assert lib.analyze_all_decks_async() is True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and standalone_decks._ANALYZE_ALL_LOCK.locked():
        time.sleep(0.01)
    assert not standalone_decks._ANALYZE_ALL_LOCK.locked()
    # Snapshot & idle-done skipped; compact match-only upgraded; newest last_played first; None last_played last.
    assert built == ["new_match", "old", "unplayed"]


def test_x1_mana_delta_handles_real_compiled_mana_records(monkeypatch):
    # Real compiled CardFacts shapes (Command Tower, Ruby, Orion, Strip Mine, Barbarian Ring,
    # Karplusan Forest, Thornspire Verge, Ancient Tomb, Fanatic of Rhonas, Dal R'El).
    cmd_tower = {
        "instance_id": 1,
        "name": "Command Tower",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "{T}: Add one mana of any color in your commander's color identity.",
        "controller_seat_id": 1,
    }
    ruby = {
        "instance_id": 2,
        "name": "Ruby, Daring Tracker",
        "mana_cost": "{R}{G}",
        "type_line": "Legendary Creature — Human Scout",
        "oracle_text": "{T}: Add {R} or {G}.",
        "controller_seat_id": 1,
    }
    orion = {
        "instance_id": 3,
        "name": "Orion",
        "mana_cost": "",
        "type_line": "Land — Planet",
        "oracle_text": "{T}: Add {R} or {G}.",
        "controller_seat_id": 1,
    }
    strip = {
        "instance_id": 4,
        "name": "Strip Mine",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "{T}: Add {C}.",
        "controller_seat_id": 1,
    }
    tomb = {
        "instance_id": 5,
        "name": "Ancient Tomb",
        "mana_cost": "",
        "type_line": "Land",
        "oracle_text": "{T}: Add {C}{C}.",
        "controller_seat_id": 1,
    }
    fanatic = {
        "instance_id": 6,
        "name": "Fanatic of Rhonas",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Snake Druid",
        "oracle_text": "{T}: Add {G}. Ferocious — {T}: Add {G}{G}{G}{G}.",
        "controller_seat_id": 1,
    }
    dal = {
        "instance_id": 7,
        "name": "Dal R'El, Augment Orphan",
        "mana_cost": "{1}{R}{G}",
        "type_line": "Legendary Creature",
        "oracle_text": "Landfall — Whenever a land enters, add {R} or {G}.",
        "controller_seat_id": 1,
    }
    records = [
        (
            cmd_tower,
            _record(
                cmd_tower,
                modes=[
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}",
                        "stays_on_battlefield": True,
                    }
                ],
                effects=[
                    {
                        "conditions": ["the mana must be of a color in your commander's color identity"],
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{Any}", "amount": 1},
                    }
                ],
                mana=[
                    {"condition": "one mana of any color in your commander's color identity", "mana": "{Any}"}
                ],
            ),
        ),
        (
            ruby,
            _record(
                ruby,
                modes=[
                    {"name": "cast", "cost": {"mana": "{R}{G}"}, "stays_on_battlefield": True},
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}: Add {R} or {G}.",
                        "stays_on_battlefield": True,
                    },
                ],
                effects=[
                    {
                        "trigger": "attacks",
                        "effect": {"effect_type": "Pump", "power": 2, "toughness": 2, "target": "Ruby"},
                    },
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{R} or {G}", "amount": 1},
                    },
                ],
                mana=[{"condition": "", "mana": "{R} or {G}"}],
            ),
        ),
        (
            orion,
            _record(
                orion,
                modes=[
                    {"name": "cast", "cost": {"mana": ""}, "stays_on_battlefield": True},
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}",
                        "stays_on_battlefield": True,
                    },
                ],
                effects=[
                    {"trigger": "ETB", "effect": {"effect_type": "GainLife", "amount": 1}},
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{R}", "amount": 1},
                    },
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{G}", "amount": 1},
                    },
                ],
                mana=[{"condition": "", "mana": "{R}"}, {"condition": "", "mana": "{G}"}],
            ),
        ),
        (
            strip,
            _record(
                strip,
                modes=[
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}",
                        "stays_on_battlefield": True,
                    },
                    {
                        "name": "activate",
                        "cost": {"tap": True, "sacrifice": {"count": 1, "permanent": "this land"}},
                        "cost_text": "{T}, Sacrifice",
                        "stays_on_battlefield": False,
                    },
                ],
                effects=[
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{C}", "amount": 1},
                    },
                    {"trigger": "activated", "effect": {"effect_type": "Destroy", "target": "target land"}},
                ],
                mana=[{"condition": "", "mana": "{C}"}],
            ),
        ),
        (
            tomb,
            _record(
                tomb,
                modes=[
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}",
                        "stays_on_battlefield": True,
                    }
                ],
                effects=[
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{C}{C}", "amount": 2},
                    },
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "Damage", "amount": 2, "target": "you"},
                    },
                ],
                mana=[{"condition": "", "mana": "{C}{C}"}],
            ),
        ),
        (
            fanatic,
            _record(
                fanatic,
                modes=[
                    {"name": "cast", "cost": {"mana": "{1}{G}"}, "stays_on_battlefield": True},
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}",
                        "stays_on_battlefield": True,
                    },
                    {
                        "name": "activate",
                        "cost": {"tap": True},
                        "cost_text": "{T}. Ferocious",
                        "stays_on_battlefield": True,
                    },
                ],
                effects=[
                    {
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{G}", "amount": 1},
                    },
                    {
                        "conditions": ["Ferocious"],
                        "trigger": "activated",
                        "effect": {"effect_type": "AddMana", "mana": "{G}{G}{G}{G}", "amount": 4},
                    },
                ],
                mana=[{"condition": "", "mana": "{G}"}, {"condition": "Ferocious", "mana": "{G}{G}{G}{G}"}],
            ),
        ),
        (
            dal,
            _record(
                dal,
                modes=[
                    {"name": "cast", "cost": {"mana": "{1}{R}{G}"}, "stays_on_battlefield": True},
                    {
                        "name": "activate",
                        "cost": {"mana": "{3}", "tap": False},
                        "cost_text": "{3}",
                        "stays_on_battlefield": True,
                    },
                ],
                effects=[
                    {
                        "trigger": "Whenever a land you control enters (landfall)",
                        "effect": {"effect_type": "AddMana", "mana": "{R} or {G}", "amount": 1},
                    },
                    {"trigger": "activated", "effect": {"effect_type": "Draw", "count": 1}},
                ],
                mana=[],
            ),
        ),
    ]
    _seed_store(monkeypatch, records)
    base_state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 4},
        "players": [{"seat_id": 1, "is_local": True}],
    }
    assert exact_mana_total({**base_state, "battlefield": [cmd_tower, ruby, orion, strip, dal]}) == 4
    assert exact_mana_total({**base_state, "battlefield": [cmd_tower, tomb]}) == 3
    assert exact_mana_total({**base_state, "battlefield": [cmd_tower, {**tomb, "is_tapped": True}]}) == 1
    assert exact_mana_total({**base_state, "battlefield": [cmd_tower, fanatic]}) is None
    assert exact_mana_total({**base_state, "battlefield": [cmd_tower, {**fanatic, "is_tapped": True}]}) == 1
    assert (
        exact_mana_total(
            {**base_state, "battlefield": [cmd_tower, {**fanatic, "turn_entered_battlefield": 4}]}
        )
        == 1
    )


def test_v3_match_bundle_records_code_revision_and_served_model(monkeypatch, tmp_path):
    sha_snap = "a" * 40
    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", sha_snap)
    assert _app_info()["code_revision"] == sha_snap

    monkeypatch.delenv("MTGACOACH_SNAPSHOT_SHA", raising=False)
    sha_dev = "B" * 40
    monkeypatch.setenv("MTGACOACH_DEV_COMMIT", sha_dev)
    assert _app_info()["code_revision"] == sha_dev.lower()

    monkeypatch.delenv("MTGACOACH_DEV_COMMIT", raising=False)
    app_root = tmp_path / "app"
    app_root.mkdir()
    sha_pkg = "c" * 40
    (app_root / "coach-release.json").write_text(json.dumps({"commit": sha_pkg}), encoding="utf-8")
    monkeypatch.setenv("MTGACOACH_APP_ROOT", str(app_root))
    assert _app_info()["code_revision"] == sha_pkg

    monkeypatch.delenv("MTGACOACH_APP_ROOT", raising=False)
    empty_dir = tmp_path / "missing"
    empty_dir.mkdir()
    monkeypatch.setenv("MTGACOACH_DEV_CHECKOUT", str(empty_dir))
    assert _app_info()["code_revision"] == "unknown"

    monkeypatch.setenv("MTGACOACH_SNAPSHOT_SHA", sha_snap)
    coach_log = tmp_path / "standalone.log"
    coach_log.write_text(
        "2026-11-16 12:00:00.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'current', 'served_model': 'glm-5.3-flash', 'status': 'ok', 'call_class': 'decision.typed'}\n"
        "2026-11-16 12:00:05.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'current', 'served_model': None, 'status': 'ttft_timeout', 'call_class': 'decision.typed'}\n"
        "2026-11-16 12:00:10.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'current', 'served_model': None, 'status': 'cancelled', 'call_class': 'decision.pay'}\n",
        encoding="utf-8",
    )
    player_log = tmp_path / "Player.log"
    player_log.write_text("", encoding="utf-8")
    ctx = MatchBundleContext(match_id="m1", coach_log_path=coach_log, player_log_path=player_log)
    b1 = build_bundle(ctx, install_id="inst_1", result="win")
    assert b1["app"]["code_revision"] == sha_snap
    assert b1["served_model"] == "glm-5.3-flash"

    # Only failed/timed-out decision calls with served_model=None must yield None (never 'current').
    coach_log.write_text(
        "2026-11-16 12:00:00.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'current', 'served_model': None, 'status': 'error', 'call_class': 'decision.typed'}\n",
        encoding="utf-8",
    )
    assert build_bundle(ctx, install_id="inst_1", result="loss")["served_model"] is None

    # Mixed models & OpenRouter alias detection
    coach_log.write_text(
        "2026-11-16 12:00:00.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'or-gemma-4-26b', 'served_model': 'google/gemma-4-26b', 'call_class': 'decision.typed'}\n",
        encoding="utf-8",
    )
    b_or = build_bundle(ctx, install_id="inst_1", result="win")
    assert b_or["served_model"] == "or-gemma-4-26b"

    coach_log.write_text(
        "2026-11-16 12:00:00.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'or-gemma-4-26b', 'served_model': 'google/gemma-4-26b', 'call_class': 'decision.typed'}\n"
        "2026-11-16 12:01:00.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'current', 'served_model': 'glm-5.3-flash', 'call_class': 'decision.pay'}\n",
        encoding="utf-8",
    )
    b_mix = build_bundle(ctx, install_id="inst_1", result="loss")
    assert b_mix["served_model"] == "mixed"

    coach_log.write_text(
        "2026-11-16 12:00:00.000 | INFO | arenamcp.backends.proxy | [PROXY] Request metrics: {'model': 'current', 'served_model': 'glm-5.3-flash', 'call_class': 'background.game_plan'}\n",
        encoding="utf-8",
    )
    b_none = build_bundle(ctx, install_id="inst_1", result="win")
    assert b_none["served_model"] is None
