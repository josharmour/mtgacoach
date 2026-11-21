"""NN7 tests: fail-closed CardFacts triggers in afterstate & line_search + conservative trick risk."""

from __future__ import annotations

import time
from copy import deepcopy

import pytest

from arenamcp import line_search_moves as lsm
from arenamcp.afterstate import (
    AfterstateEngine,
    BoardState,
    CastSpell,
    Permanent,
    PlayLand,
    extract_verified_triggers,
    is_verified_card_facts,
)
from arenamcp.board_model import build_board_model
from arenamcp.card_facts import client as cf_client
from arenamcp.line_search import ALIVE, WIN, search_lines


@pytest.fixture(autouse=True)
def _reset_cf_cache():
    lsm.reset_record_cache()
    yield
    lsm.reset_record_cache()


def _patch_records(monkeypatch, pairs: list[tuple[dict, dict]]) -> None:
    store = {lsm.hash_for_card(card): dict(rec) for card, rec in pairs}

    def fake_facts_for(cards: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for c in cards:
            digest = lsm.hash_for_card(c)
            if digest in store:
                out[digest] = store[digest]
        return out

    monkeypatch.setattr(lsm, "facts_for", fake_facts_for)
    monkeypatch.setattr(cf_client, "facts_for", fake_facts_for)
    lsm.reset_record_cache()


def _norm_card(raw: dict, iid: int, ctrl: int = 1, *, tapped: bool = False) -> dict:
    c = dict(raw)
    c["instance_id"] = iid
    c.setdefault("owner_seat_id", ctrl)
    c.setdefault("controller_seat_id", ctrl)
    c.setdefault("turn_entered_battlefield", 1 if ctrl in (1, 2) else -1)
    c["is_tapped"] = tapped
    if "card_types" not in c:
        head = str(c.get("type_line") or "").split("—", 1)[0]
        types = []
        for t in ("Land", "Creature", "Artifact", "Enchantment", "Instant", "Sorcery"):
            if t in head:
                types.append(f"CardType_{t}")
        c["card_types"] = types
    return c


def _state(
    *,
    hand: list[dict] | None = None,
    battlefield: list[dict] | None = None,
    our_life: int = 20,
    opp_life: int = 20,
    opp_hand_count: int = 0,
    turn: int = 5,
    extra_zones: dict | None = None,
) -> dict:
    norm_bf = []
    for i, item in enumerate(battlefield or []):
        nb = dict(item)
        ctrl = int(nb.get("controller_seat_id") or 1)
        iid = int(nb.get("instance_id") or (100 + i))
        norm_bf.append(_norm_card(nb, iid, ctrl, tapped=bool(nb.get("is_tapped"))))
    zones = {
        "library_count": 30,
        "library_count_source": "log_zone_membership",
        "opponent_hand_count": opp_hand_count,
    }
    if extra_zones:
        zones.update(extra_zones)
    return {
        "match_id": "nn7-test",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": turn,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "life_total": our_life, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "life_total": opp_life, "is_local": False, "lands_played": 0},
        ],
        "hand": [_norm_card(c, 900 + i, 1) for i, c in enumerate(hand or [])],
        "battlefield": norm_bf,
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": zones,
    }


def _search(state: dict, *, survival: bool = False, lethal_now: bool = False):
    return search_lines(build_board_model(state), survival=survival, lethal_now=lethal_now)


def _plains(iid: int = 10, ctrl: int = 1, *, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": "Plains",
        "mana_cost": "",
        "type_line": "Basic Land — Plains",
        "oracle_text": "{T}: Add {W}.",
        "controller_seat_id": ctrl,
        "is_tapped": tapped,
    }


def _forest(iid: int = 20, ctrl: int = 1, *, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": "Forest",
        "mana_cost": "",
        "type_line": "Basic Land — Forest",
        "oracle_text": "{T}: Add {G}.",
        "controller_seat_id": ctrl,
        "is_tapped": tapped,
    }


def _swamp(iid: int = 30, ctrl: int = 2, *, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": "Swamp",
        "mana_cost": "",
        "type_line": "Basic Land — Swamp",
        "oracle_text": "{T}: Add {B}.",
        "controller_seat_id": ctrl,
        "is_tapped": tapped,
    }


def _island(iid: int = 40, ctrl: int = 1, *, tapped: bool = False) -> dict:
    return {
        "instance_id": iid,
        "name": "Island",
        "mana_cost": "",
        "type_line": "Basic Land — Island",
        "oracle_text": "{T}: Add {U}.",
        "controller_seat_id": ctrl,
        "is_tapped": tapped,
    }


def _rec(
    card: dict,
    *,
    effects: list[dict],
    modes: list[dict] | None = None,
    confidence: str = "high",
    extra: dict | None = None,
) -> dict:
    out = {
        "name": card["name"],
        "mana_cost": card.get("mana_cost", ""),
        "type_line": card.get("type_line", ""),
        "confidence": confidence,
        "compiled_by": "test-compiler",
        "effects": effects,
        "modes": modes
        or [
            {
                "name": "cast",
                "cost": {"mana": card.get("mana_cost", "")},
                "cost_text": card.get("mana_cost", ""),
                "stays_on_battlefield": "Instant" not in card.get("type_line", "")
                and "Sorcery" not in card.get("type_line", ""),
            }
        ],
    }
    if extra:
        out.update(extra)
    return out


# ── Part 1A: AfterstateEngine & fail-closed CardFacts extraction ────────────


def test_afterstate_card_facts_triggers_and_fail_closed_gate():
    eerie_card = {
        "name": "Glimmer Seeker",
        "mana_cost": "{1}{W}",
        "type_line": "Enchantment Creature — Spirit",
        "oracle_text": "Eerie — Whenever an enchantment you control enters, create a 1/1 Spirit creature token.",
    }
    eerie_rec = _rec(
        eerie_card,
        effects=[
            {
                "trigger": "Whenever an enchantment you control enters",
                "effect": {
                    "effect_type": "CreateToken",
                    "spec": {
                        "count": 1,
                        "power": 1,
                        "toughness": 1,
                        "name": "Spirit",
                        "types": ["Creature"],
                    },
                },
            }
        ],
    )
    trigs = extract_verified_triggers(eerie_rec, eerie_card)
    assert len(trigs) == 1
    assert trigs[0].event == "ENCHANTMENT_ENTERS"

    board = BoardState(
        hero_life=20,
        opp_life=20,
        available_mana=3,
        permanents=(
            Permanent(
                name="Glimmer Seeker",
                power=1,
                toughness=1,
                type_line=eerie_card["type_line"],
                mana_cost=eerie_card["mana_cost"],
                verified_triggers=trigs,
            ),
        ),
        hand=("Blessing Aura",),
    )
    after = AfterstateEngine.apply(
        board,
        CastSpell(
            card_name="Blessing Aura",
            cmc=1,
            type_line="Enchantment — Aura",
            oracle_text="Enchanted creature gets +1/+1.",
        ),
    )
    assert after.delta.tokens_created.get("Spirit") == 1
    assert len(after.board.permanents) == 3

    # Landfall trigger in AfterstateEngine
    landfall_card = {
        "name": "Canopy Guide",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Elf",
        "oracle_text": "Landfall — Whenever a land you control enters, you gain 2 life.",
    }
    landfall_rec = _rec(
        landfall_card,
        effects=[
            {
                "trigger": "Whenever a land you control enters",
                "effect": {"effect_type": "GainLife", "amount": 2},
            }
        ],
    )
    l_trigs = extract_verified_triggers(landfall_rec, landfall_card)
    l_board = BoardState(
        hero_life=15,
        opp_life=20,
        available_mana=2,
        permanents=(Permanent(name="Canopy Guide", power=2, toughness=2, verified_triggers=l_trigs),),
        hand=("Forest",),
    )
    l_after = AfterstateEngine.apply(l_board, PlayLand(land_name="Forest"))
    assert l_after.board.hero_life == 17
    assert l_after.delta.life_gained == 2

    # Fail-closed controls: UnknownEffect, _unknown_fields, low confidence, metadata keys
    bad_unknown = _rec(
        eerie_card,
        effects=[{"trigger": "etb", "effect": {"effect_type": "Unknown", "text": "do weird thing"}}],
    )
    assert not is_verified_card_facts(bad_unknown, eerie_card)
    assert extract_verified_triggers(bad_unknown, eerie_card) == ()

    bad_extra_key = _rec(
        eerie_card,
        effects=[
            {
                "trigger": "Whenever an enchantment you control enters",
                "effect": {
                    "effect_type": "PutCounters",
                    "counter_type": "+1/+1",
                    "count": 1,
                    "target": "target creature",
                },
            }
        ],
    )
    assert not is_verified_card_facts(bad_extra_key, eerie_card)
    assert extract_verified_triggers(bad_extra_key, eerie_card) == ()

    bad_conf = _rec(eerie_card, effects=eerie_rec["effects"], confidence="medium")
    assert not is_verified_card_facts(bad_conf, eerie_card)
    assert extract_verified_triggers(bad_conf, eerie_card) == ()

    bad_threat = _rec(eerie_card, effects=eerie_rec["effects"], extra={"threat": "high"})
    assert not is_verified_card_facts(bad_threat, eerie_card)


# ── Part 1B: CardFacts-driven triggers & ordering payoffs in line_search ───


def test_eerie_engine_first_aura_second_ordering_payoff(monkeypatch):
    engine = {
        "name": "Glimmer Seeker",
        "mana_cost": "{1}{W}",
        "type_line": "Enchantment Creature — Human Spirit",
        "power": 1,
        "toughness": 1,
        "oracle_text": "Eerie — Whenever an enchantment you control enters, put a +1/+1 counter on target creature.",
    }
    aura = {
        "name": "Holy Role",
        "mana_cost": "{W}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.",
    }
    _patch_records(
        monkeypatch,
        [
            (
                engine,
                _rec(
                    engine,
                    effects=[
                        {
                            "trigger": "Whenever an enchantment you control enters",
                            "effect": {
                                "effect_type": "PutCounters",
                                "counter_type": "+1/+1",
                                "count": 1,
                            },
                        }
                    ],
                ),
            ),
            (
                aura,
                _rec(
                    aura,
                    effects=[
                        {
                            "trigger": "static",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "enchanted creature",
                            },
                        }
                    ],
                ),
            ),
        ],
    )

    state = _state(
        hand=[aura, engine],  # intentionally put Aura before Engine in hand!
        battlefield=[_plains(10), _plains(11), _plains(12)],
        opp_hand_count=0,
    )
    res = _search(state)

    assert res.best.steps[0].casts == ("Glimmer Seeker", "Holy Role")
    assert res.best.steps[0].order_sensitive is True
    # Resulting Glimmer Seeker has base 1/1 + Aura +1/+1 + Eerie counter +1/+1 = 3/3!
    assert len(res.best._leaf.ours) == 1
    seeker = res.best._leaf.ours[0]
    assert (seeker["power"], seeker["toughness"]) == (3, 3)

    # Reverse order (Holy Role before creature) must not appear on empty board
    aura_first_lines = [
        line for line in res.lines if line.steps and line.steps[0].casts == ("Holy Role", "Glimmer Seeker")
    ]
    assert not aura_first_lines


def test_constellation_noncreature_engine_ordering_payoff(monkeypatch):
    shrine = {
        "name": "Constellation Shrine",
        "mana_cost": "{1}{W}",
        "type_line": "Enchantment",
        "oracle_text": "Constellation — Whenever an enchantment you control enters, you gain 2 life, draw a card, and each opponent loses 2 life.",
    }
    aura = {
        "name": "Holy Role",
        "mana_cost": "{W}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.",
    }
    bear = {
        "name": "Savannah Bear",
        "mana_cost": "{1}{W}",
        "type_line": "Creature — Bear",
        "power": 2,
        "toughness": 2,
        "oracle_text": "",
    }
    _patch_records(
        monkeypatch,
        [
            (
                shrine,
                _rec(
                    shrine,
                    effects=[
                        {
                            "trigger": "Whenever this enchantment or another enchantment you control enters",
                            "effect": {"effect_type": "GainLife", "amount": 2},
                        },
                        {
                            "trigger": "Whenever an enchantment you control enters",
                            "effect": {"effect_type": "Damage", "amount": 2, "target": "each opponent"},
                        },
                        {
                            "trigger": "Whenever an enchantment you control enters",
                            "effect": {"effect_type": "Draw", "count": 1},
                        },
                    ],
                ),
            ),
            (
                aura,
                _rec(
                    aura,
                    effects=[
                        {
                            "trigger": "static",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "enchanted creature",
                            },
                        }
                    ],
                ),
            ),
        ],
    )

    state = _state(
        hand=[aura, shrine],  # reverse hand order
        battlefield=[bear, _plains(10), _plains(11), _plains(12)],
        opp_hand_count=0,
    )
    res = _search(state)
    assert res.best.steps[0].casts == ("Constellation Shrine", "Holy Role")
    assert res.best.steps[0].draws >= 1
    assert res.best._leaf.our_life >= 22
    # Opp took Constellation triggers + buffed Bear combat damage
    assert res.best._leaf.opp_life <= 15


def test_prowess_noncreature_cast_card_facts_and_no_double_count(monkeypatch):
    # Part A: noncreature-cast CardFacts token generator ordered first before instant
    mentor = {
        "name": "Sigil Crafter",
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Wizard",
        "power": 1,
        "toughness": 2,
        "oracle_text": "Whenever you cast a noncreature spell, create a 1/1 Soldier creature token.",
    }
    cantrip = {
        "name": "Quick Study Note",
        "mana_cost": "{U}",
        "type_line": "Instant",
        "oracle_text": "Target creature gets +1/+1 until end of turn. Draw a card.",
    }
    attacker = {
        "name": "Mist Scout",
        "mana_cost": "{U}",
        "type_line": "Creature — Merfolk",
        "power": 1,
        "toughness": 1,
        "oracle_text": "",
    }
    _patch_records(
        monkeypatch,
        [
            (
                mentor,
                _rec(
                    mentor,
                    effects=[
                        {
                            "trigger": "Whenever you cast a noncreature spell",
                            "effect": {
                                "effect_type": "CreateToken",
                                "spec": {
                                    "count": 1,
                                    "power": 1,
                                    "toughness": 1,
                                    "name": "Soldier",
                                    "types": ["Creature"],
                                },
                            },
                        }
                    ],
                ),
            ),
            (
                cantrip,
                _rec(
                    cantrip,
                    effects=[
                        {
                            "trigger": "resolves",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "target creature",
                            },
                        },
                        {"trigger": "resolves", "effect": {"effect_type": "Draw", "count": 1}},
                    ],
                ),
            ),
        ],
    )
    state = _state(
        hand=[cantrip, mentor],
        battlefield=[attacker, _island(40), _island(41), _island(42)],
        opp_hand_count=0,
    )
    res = _search(state)
    assert res.best.steps[0].casts == ("Sigil Crafter", "Quick Study Note")
    assert len(res.best._leaf.ours) == 3  # Mist Scout + Sigil Crafter + 1/1 Soldier token!
    assert res.best.steps[0].draws >= 1

    # Part B: deduplication against text prowess (must not pump +2/+2 instead of +1/+1)
    prowess_guy = {
        "name": "Otter Initiate",
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Otter",
        "power": 1,
        "toughness": 2,
        "oracle_text": "Prowess",
    }
    bolt = {
        "name": "Static Spark",
        "mana_cost": "{U}",
        "type_line": "Instant",
        "oracle_text": "Static Spark deals 1 damage to each opponent.",
    }
    _patch_records(
        monkeypatch,
        [
            (
                prowess_guy,
                _rec(
                    prowess_guy,
                    effects=[
                        {
                            "trigger": "Whenever you cast a noncreature spell",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "self",
                            },
                        }
                    ],
                ),
            )
        ],
    )
    p_state = _state(
        hand=[bolt],
        battlefield=[
            _norm_card({**prowess_guy, "turn_entered_battlefield": 1}, 101, 1),
            _island(40),
        ],
        opp_life=4,
        opp_hand_count=0,
    )
    p_res = _search(p_state)
    # Base power 1 + 1 prowess (NOT 2!) + 1 Static Spark face damage = 3 damage on T (opp at 1, not lethal T1)
    assert p_res.best.win_at != 1
    step0_lives = [
        line.steps[0].opp_life_after
        for line in p_res.lines
        if line.steps and line.steps[0].casts == ("Static Spark",) and line.steps[0].attack
    ]
    assert step0_lives and all(life == 1 for life in step0_lives)


def test_landfall_creature_etb_and_creature_dies_card_facts_triggers(monkeypatch):
    # 1. Landfall trigger on board (Token + GainLife)
    landfall_druid = {
        "name": "Root Tender",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Elf",
        "power": 2,
        "toughness": 2,
        "oracle_text": "Landfall — Whenever a land you control enters, create a 1/1 Plant creature token and you gain 2 life.",
    }
    _patch_records(
        monkeypatch,
        [
            (
                landfall_druid,
                _rec(
                    landfall_druid,
                    effects=[
                        {
                            "trigger": "Whenever a land you control enters",
                            "effect": {
                                "effect_type": "CreateToken",
                                "spec": {
                                    "count": 1,
                                    "power": 1,
                                    "toughness": 1,
                                    "name": "Plant",
                                    "types": ["Creature"],
                                },
                            },
                        },
                        {
                            "trigger": "Whenever a land you control enters",
                            "effect": {"effect_type": "GainLife", "amount": 2},
                        },
                    ],
                ),
            )
        ],
    )
    lf_state = _state(
        hand=[_forest(90)],
        battlefield=[landfall_druid],
        opp_hand_count=0,
    )
    lf_res = _search(lf_state)
    assert lf_res.best.steps[0].land == "Forest"
    assert lf_res.best._leaf.our_life == 22
    assert len(lf_res.best._leaf.ours) == 2

    # 2. Creature ETB + Creature Dies CardFacts triggers
    etb_lord = {
        "name": "Watchful Mentor",
        "mana_cost": "{1}{W}",
        "type_line": "Creature — Cleric",
        "power": 1,
        "toughness": 2,
        "oracle_text": "Whenever another creature you control enters, put a +1/+1 counter on target creature.\nWhenever a creature an opponent controls dies, each opponent loses 2 life.",
    }
    squire = {
        "name": "Bear Cub",
        "mana_cost": "{1}{W}",
        "type_line": "Creature — Bear",
        "power": 2,
        "toughness": 2,
        "oracle_text": "",
    }
    smite = {
        "name": "Clean Smite",
        "mana_cost": "{W}",
        "type_line": "Instant",
        "oracle_text": "Destroy target creature.",
    }
    opp_bear = {
        "name": "Enemy Bear",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "power": 2,
        "toughness": 2,
        "oracle_text": "",
        "controller_seat_id": 2,
    }
    _patch_records(
        monkeypatch,
        [
            (
                etb_lord,
                _rec(
                    etb_lord,
                    effects=[
                        {
                            "trigger": "Whenever another creature you control enters",
                            "effect": {
                                "effect_type": "PutCounters",
                                "counter_type": "+1/+1",
                                "count": 1,
                            },
                        },
                        {
                            "trigger": "Whenever a creature an opponent controls dies",
                            "effect": {"effect_type": "Damage", "amount": 2, "target": "each opponent"},
                        },
                    ],
                ),
            )
        ],
    )
    ed_state = _state(
        hand=[smite, squire],
        battlefield=[etb_lord, opp_bear, _plains(10), _plains(11), _plains(12)],
        opp_hand_count=0,
    )
    ed_res = _search(ed_state)
    assert "Clean Smite" in ed_res.best.steps[0].casts
    assert "Bear Cub" in ed_res.best.steps[0].casts
    assert ed_res.best._leaf.opp_life <= 16
    assert any(b["power"] == 3 and b["toughness"] == 3 for b in ed_res.best._leaf.ours)


def test_uncompiled_and_corrupt_card_facts_abstain_in_line_search(monkeypatch):
    eerie = {
        "name": "Glimmer Seeker",
        "mana_cost": "{1}{W}",
        "type_line": "Enchantment Creature — Spirit",
        "oracle_text": "Eerie — Whenever an enchantment you control enters, exile target creature until this leaves the battlefield.",
    }
    aura = {
        "name": "Holy Role",
        "mana_cost": "{W}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.\nWhenever enchanted creature attacks, scry 1.",
    }
    # 1. No records -> both keep original unmodelled reasons
    _patch_records(monkeypatch, [])
    assert lsm.unmodelled_effect(aura) == "its effect"
    assert lsm.unmodelled_effect(eerie) == "its triggered ability"

    # 2. _unknown_fields or medium confidence -> must abstain cleanly
    _patch_records(
        monkeypatch,
        [
            (
                eerie,
                _rec(
                    eerie,
                    effects=[
                        {
                            "trigger": "Whenever an enchantment you control enters",
                            "effect": {
                                "effect_type": "PutCounters",
                                "counter_type": "+1/+1",
                                "count": 1,
                                "target": "target creature",
                            },
                        }
                    ],
                ),
            ),
            (
                aura,
                _rec(
                    aura,
                    confidence="medium",
                    effects=[
                        {
                            "trigger": "static",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "enchanted creature",
                            },
                        }
                    ],
                ),
            ),
        ],
    )
    assert lsm.unmodelled_effect(eerie) == "its triggered ability"
    assert lsm.unmodelled_effect(aura) == "its effect"


# ── Part 2: Conservative Open-Mana Trick / Blowout Sensitivity ─────────────


def _trick_risk_fixture(
    monkeypatch,
    *,
    opp_mana: int,
    opp_hand: int,
    our_life: int = 20,
    opp_life: int = 20,
    extra_bf: list[dict] | None = None,
    host_ward: bool = False,
    extra_zones: dict | None = None,
    survival: bool = False,
):
    sole_bear = {
        "name": "Sky Drake",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Drake",
        "power": 2,
        "toughness": 2,
        "oracle_text": "Flying\nWard {2}" if host_ward else "Flying",
        "keywords": ["Flying", "Ward"] if host_ward else ["Flying"],
    }
    widen_bear = {
        "name": "Companion Bear",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "power": 1,
        "toughness": 2,
        "oracle_text": "",
    }
    aura = {
        "name": "Oak Blessing",
        "mana_cost": "{G}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +3/+2.",
    }
    _patch_records(
        monkeypatch,
        [
            (
                aura,
                _rec(
                    aura,
                    effects=[
                        {
                            "trigger": "static",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 3,
                                "toughness": 2,
                                "target": "enchanted creature",
                            },
                        }
                    ],
                ),
            )
        ],
    )
    opp_lands = [_swamp(300 + i, ctrl=2, tapped=(i >= opp_mana)) for i in range(2)]
    bf = [sole_bear, _forest(20), _forest(21), *opp_lands, *(extra_bf or [])]
    st = _state(
        hand=[aura, widen_bear],
        battlefield=bf,
        our_life=our_life,
        opp_life=opp_life,
        opp_hand_count=opp_hand,
        extra_zones=extra_zones,
    )
    return _search(st, survival=survival)


def test_open_mana_blowout_prefers_widening_and_opposite_controls_take_aura(monkeypatch):
    # Open mana == 2, opp_hand == 2 -> widen with Companion Bear instead of putting Oak Blessing on sole 2/2
    cautious = _trick_risk_fixture(monkeypatch, opp_mana=2, opp_hand=2)
    assert cautious.best.steps[0].casts == ("Companion Bear",)

    # Opposite control 1: opp_open_mana == 0 -> zero blowout penalty -> takes Oak Blessing + attack!
    tapped_out = _trick_risk_fixture(monkeypatch, opp_mana=0, opp_hand=2)
    assert tapped_out.best.steps[0].casts == ("Oak Blessing",)
    assert tapped_out.best._leaf.risk_penalty == 0.0

    # Opposite control 2: opp_hand_count == 0 -> zero hidden-hand blowout penalty -> takes Oak Blessing!
    empty_hand = _trick_risk_fixture(monkeypatch, opp_mana=2, opp_hand=0)
    assert empty_hand.best.steps[0].casts == ("Oak Blessing",)
    assert empty_hand.best._leaf.risk_penalty == 0.0

    # Public info invariance: hidden hand entries ignored when opp_hand_count == 0
    hidden_peeking = _trick_risk_fixture(
        monkeypatch,
        opp_mana=2,
        opp_hand=0,
        extra_zones={"opponent_hand": [{"name": "Go for the Throat", "mana_cost": "{1}{B}"}]},
    )
    assert hidden_peeking.best.steps[0].casts == ("Oak Blessing",)

    # Ward {2} vs 2 open mana -> cannot pay removal + Ward {2} -> zero blowout penalty
    warded = _trick_risk_fixture(monkeypatch, opp_mana=2, opp_hand=2, host_ward=True)
    assert warded.best.steps[0].casts == ("Oak Blessing",)
    assert warded.best._leaf.risk_penalty == 0.0


def test_open_mana_blowout_sorcery_pump_and_controls(monkeypatch):
    bear = {
        "name": "Grizzly Bear",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "power": 2,
        "toughness": 2,
        "oracle_text": "",
    }
    opp_wall = {
        "instance_id": 500,
        "name": "Bronze Wall",
        "mana_cost": "{1}{B}",
        "type_line": "Creature — Construct",
        "power": 2,
        "toughness": 3,
        "oracle_text": "",
        "controller_seat_id": 2,
    }
    surge = {
        "name": "Sorcery Surge",
        "mana_cost": "{G}",
        "type_line": "Sorcery",
        "oracle_text": "Target creature gets +3/+3 until end of turn.",
    }
    _patch_records(
        monkeypatch,
        [
            (
                surge,
                _rec(
                    surge,
                    effects=[
                        {
                            "trigger": "resolves",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 3,
                                "toughness": 3,
                                "target": "target creature",
                            },
                        }
                    ],
                ),
            )
        ],
    )
    # 1. Open mana + hidden hand -> hold Sorcery Surge safely instead of walking into 2-for-1 blowout
    st_risk = _state(
        hand=[surge],
        battlefield=[bear, opp_wall, _forest(20), _swamp(300, ctrl=2), _swamp(301, ctrl=2)],
        opp_hand_count=2,
    )
    assert _search(st_risk).best.steps[0].casts == ()

    # 2. Tapped out -> cast Sorcery Surge and punch past the 2/3 blocker!
    st_tapped = _state(
        hand=[surge],
        battlefield=[
            bear,
            opp_wall,
            _forest(20),
            _swamp(300, ctrl=2, tapped=True),
            _swamp(301, ctrl=2, tapped=True),
        ],
        opp_hand_count=2,
    )
    res_tapped = _search(st_tapped)
    assert res_tapped.best.steps[0].casts == ("Sorcery Surge",)
    assert res_tapped.best._leaf.risk_penalty == 0.0


def test_open_mana_hard_invariant_lethal_and_survival_never_abstain(monkeypatch):
    # Immediate Lethal: opponent at 4 life, 2/2 + (+2/+2 Oak Blessing) = 4/4 lethal win!
    lethal = _trick_risk_fixture(monkeypatch, opp_mana=2, opp_hand=2, opp_life=4)
    assert lethal.best.cls == WIN
    assert lethal.best.win_at == 1
    assert lethal.best.steps[0].casts == ("Oak Blessing",)

    # Mandatory Survival: opponent has a 4/4 attacker that kills us at 4 life unless our 2/2 becomes 4/4!
    opp_beast = {
        "instance_id": 500,
        "name": "Ravenous Beast",
        "mana_cost": "{2}{B}{B}",
        "type_line": "Creature — Beast",
        "power": 4,
        "toughness": 4,
        "oracle_text": "",
        "controller_seat_id": 2,
    }
    survival = _trick_risk_fixture(
        monkeypatch,
        opp_mana=2,
        opp_hand=2,
        our_life=4,
        opp_life=20,
        extra_bf=[opp_beast],
        survival=True,
    )
    assert survival.best.cls == ALIVE
    assert survival.best.steps[0].casts == ("Oak Blessing",)


# ── Part 3: Latency Budget (<= 80 ms) ──────────────────────────────────────


@pytest.mark.search_latency
def test_nn7_trigger_chain_and_benchmark_latency_within_80ms(monkeypatch):
    from tests import strategic_states

    # 1. Trigger chain + open mana search latency
    engine = {
        "name": "Glimmer Seeker",
        "mana_cost": "{1}{W}",
        "type_line": "Enchantment Creature — Human Spirit",
        "power": 1,
        "toughness": 1,
        "oracle_text": "Eerie — Whenever an enchantment you control enters, put a +1/+1 counter on target creature.",
    }
    aura = {
        "name": "Holy Role",
        "mana_cost": "{W}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.",
    }
    _patch_records(
        monkeypatch,
        [
            (
                engine,
                _rec(
                    engine,
                    effects=[
                        {
                            "trigger": "Whenever an enchantment you control enters",
                            "effect": {
                                "effect_type": "PutCounters",
                                "counter_type": "+1/+1",
                                "count": 1,
                            },
                        }
                    ],
                ),
            ),
            (
                aura,
                _rec(
                    aura,
                    effects=[
                        {
                            "trigger": "static",
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "enchanted creature",
                            },
                        }
                    ],
                ),
            ),
        ],
    )
    st = _state(
        hand=[aura, engine],
        battlefield=[_plains(10), _plains(11), _plains(12), _swamp(300, ctrl=2)],
        opp_hand_count=1,
    )
    model = build_board_model(st)
    t0 = time.process_time()
    search_lines(model, survival=False, lethal_now=False)
    trigger_ms = (time.process_time() - t0) * 1000.0
    assert trigger_ms < strategic_states.latency_bound(80), trigger_ms

    # 2. Existing slowest benchmark board
    slow_model = build_board_model(deepcopy(strategic_states.SLOW_212608))
    slow_ms = strategic_states.cpu_ms(lambda: search_lines(slow_model, survival=False, lethal_now=False))
    assert slow_ms < strategic_states.latency_bound(80), slow_ms


# ── Part 4: Reviewer Findings #1-#6 & #9 Regressions ───────────────────────


def test_nn7_finding1_from_game_state_dedup_and_cast_spell_validation(monkeypatch):
    mage_card = {
        "name": "Prowess Scholar",
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Human Wizard",
        "power": 1,
        "toughness": 1,
        "oracle_text": "Whenever you cast a noncreature spell, put a +1/+1 counter on this creature.",
    }
    mage_rec = _rec(
        mage_card,
        effects=[
            {
                "trigger": "Whenever you cast a noncreature spell",
                "effect": {
                    "effect_type": "PutCounters",
                    "counter_type": "+1/+1",
                    "count": 1,
                },
            }
        ],
    )
    monkeypatch.setattr(
        "arenamcp.card_facts.client.facts_for",
        lambda cards: {lsm.hash_for_card(c): mage_rec for c in cards if c.get("name") == "Prowess Scholar"},
    )
    st = _state(
        battlefield=[
            _norm_card(
                {**mage_card, "card_facts": mage_rec},
                101,
                1,
            )
        ]
    )
    board = BoardState.from_game_state(st)
    assert len(board.permanents) == 1
    assert len(board.permanents[0].verified_triggers) == 1
    assert board.permanents[0].abilities == ()

    # Only +1/+1 should apply (never +2/+2 double-fire from abilities + verified_triggers)
    opt_after = AfterstateEngine.apply(
        board,
        CastSpell(
            card_name="Opt",
            cmc=1,
            type_line="Instant",
            oracle_text="Scry 1. Draw a card.",
        ),
    )
    assert opt_after.board.permanents[0].power == 2
    assert opt_after.board.permanents[0].toughness == 2

    # CastSpell without type_line still enforces card_dict (name/mana_cost) vs rec verification
    aura_card = {
        "name": "White Space Aura",
        "mana_cost": "{W}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchanted creature gets +2/+2.",
    }
    aura_rec_ok = _rec(
        aura_card,
        effects=[
            {
                "trigger": "static",
                "effect": {
                    "effect_type": "Pump",
                    "power": 2,
                    "toughness": 2,
                    "target": "  enchanted   creature  ",
                },
            }
        ],
    )
    bad_cast_after = AfterstateEngine.apply(
        board,
        CastSpell(
            card_name="Wrong Name",
            mana_cost="{9}",
            cmc=1,
            type_line="",
            card_types=("CardType_Instant",),
            card_facts=aura_rec_ok,
        ),
    )
    # Name/mana mismatch rejects aura_rec_ok even when type_line == "" (only +1 counter, no +2 aura)
    assert bad_cast_after.board.permanents[0].power == 2

    # Aura Pump validation: whitespace-normalized target accepted; out-of-range >10 rejected
    ok_after = AfterstateEngine.apply(
        board,
        CastSpell(
            card_name="White Space Aura",
            mana_cost="{W}",
            cmc=1,
            type_line="Enchantment — Aura",
            card_facts=aura_rec_ok,
        ),
    )
    assert ok_after.board.permanents[0].power == 4

    aura_rec_bad = _rec(
        aura_card,
        effects=[
            {
                "trigger": "static",
                "effect": {
                    "effect_type": "Pump",
                    "power": 25,
                    "toughness": 2,
                    "target": "enchanted creature",
                },
            }
        ],
    )
    bad_after = AfterstateEngine.apply(
        board,
        CastSpell(
            card_name="White Space Aura",
            mana_cost="{W}",
            cmc=1,
            type_line="Enchantment — Aura",
            card_facts=aura_rec_bad,
        ),
    )
    assert bad_after.board.permanents[0].power == 2


def test_nn7_finding2_filter_fact_triggers_normalizes_instant_or_sorcery(monkeypatch):
    spell_singer = {
        "name": "Magecraft Duelist",
        "mana_cost": "{1}{R}",
        "type_line": "Creature — Human Shaman",
        "power": 2,
        "toughness": 2,
        "oracle_text": "Whenever you cast an instant or sorcery spell, Magecraft Duelist gets +1/+1 until end of turn.",
    }
    ping = {
        "name": "Shock",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Shock deals 2 damage to any target.",
    }
    rec = _rec(
        spell_singer,
        effects=[
            {
                "trigger": "Whenever you cast an instant or sorcery spell",
                "effect": {"effect_type": "Pump", "power": 1, "toughness": 1, "target": "self"},
            }
        ],
    )
    _patch_records(monkeypatch, [(spell_singer, rec)])
    mountain = {
        "instance_id": 50,
        "name": "Mountain",
        "mana_cost": "",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "{T}: Add {R}.",
        "controller_seat_id": 1,
        "is_tapped": False,
    }
    # 2/2 base + 1/1 magecraft + 2 Shock = 5 total turn-T damage (not turn-T lethal vs opp_life=6)
    st = _state(
        hand=[ping],
        battlefield=[mountain, {**spell_singer, "instance_id": 101, "turn_entered_battlefield": 1}],
        opp_life=6,
    )
    res_6 = search_lines(build_board_model(st), survival=False, lethal_now=True, k_next=0)
    assert res_6.best.win_at != 1
    assert res_6.best.steps[0].opp_life_after == 1

    # Control: opp_life=5 IS turn-T lethal with 2/2 + 1/1 magecraft + 2 Shock = 5
    st_5 = _state(
        hand=[ping],
        battlefield=[mountain, {**spell_singer, "instance_id": 101, "turn_entered_battlefield": 1}],
        opp_life=5,
    )
    res_5 = search_lines(build_board_model(st_5), survival=False, lethal_now=True, k_next=0)
    assert res_5.best.cls == WIN and res_5.best.win_at == 1


def test_nn7_finding3_deterministic_ids_rescale_aura_and_pairwise_dominance(monkeypatch):
    from arenamcp.board_assessment import _body
    from arenamcp.card_facts.schema import TokenSpec
    from arenamcp.line_search import Line
    from arenamcp.line_search_moves import Node, Step

    scaler = {
        "name": "Pack Ally",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Wolf",
        "power": 2,
        "toughness": 2,
        "oracle_text": "Pack Ally gets +1/+0 for each other creature you control.",
    }
    aura = {
        "name": "Wild Growth Role",
        "mana_cost": "{G}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+2.",
    }
    ally_maker = {
        "name": "Wolf Call",
        "mana_cost": "{G}",
        "type_line": "Sorcery",
        "oracle_text": "Create a 1/1 green Wolf creature token.",
    }
    aura_rec = _rec(
        aura,
        effects=[
            {
                "trigger": "static",
                "effect": {"effect_type": "Pump", "power": 1, "toughness": 2, "target": "enchanted creature"},
            }
        ],
    )
    _patch_records(monkeypatch, [(aura, aura_rec)])
    st = _state(
        hand=[aura, ally_maker],
        battlefield=[_forest(10), _forest(11), {**scaler, "instance_id": 101, "turn_entered_battlefield": 1}],
        opp_life=20,
    )
    model = build_board_model(st)
    res = search_lines(model, survival=False, lethal_now=False)
    # Scaling creature gets +1/+0 from Wolf token AND keeps +1/+2 from Aura -> 2 + 1 + 1 = 4 power, 4 toughness
    assert res.best.steps[0].opp_life_after == 16  # 4 power + summoning-sick Wolf token
    scaled_host = next(b for b in res.best._leaf.ours if b["name"] == "Pack Ally")
    assert scaled_host["power"] == 4
    assert scaled_host["toughness"] == 4

    # Deterministic IDs across independent Moves instances regardless of prior memo history
    m1 = res._search
    m2 = search_lines(model, survival=False, lethal_now=False)._search
    dummy_b1 = {"instance_id": 101, "power": 2, "toughness": 2, "_fact_iid": 101}
    dummy_b2 = {"instance_id": 102, "power": 3, "toughness": 3, "_fact_iid": 102}
    _ = m1._buffed(dummy_b2, 2, 2)
    b1_id_first = m1._buffed(dummy_b1, 1, 1)["instance_id"]
    b1_id_second = m2._buffed(dummy_b1, 1, 1)["instance_id"]
    assert b1_id_first == b1_id_second

    tok = TokenSpec(count=1, power=1, toughness=1, name="Spirit", types=("Creature",))
    t_id_first = m1._fact_tokens(77, 0, 0, tok)[0]["instance_id"]
    _ = m2._fact_tokens(88, 1, 0, tok)
    t_id_second = m2._fact_tokens(77, 0, 0, tok)[0]["instance_id"]
    assert t_id_first == t_id_second

    _ = m1._countered(dummy_b2, 1)
    c1_id_first = m1._countered(dummy_b1, 1)["instance_id"]
    c1_id_second = m2._countered(dummy_b1, 1)["instance_id"]
    assert c1_id_first == c1_id_second
    assert len({b1_id_first, t_id_first, c1_id_first}) == 3

    # Pairwise keyword/entered dominance with real _body() dicts (keywords/oracle_text, never fake keys)
    searcher = res._search
    vanilla_3_3 = _body(
        {"instance_id": 101, "name": "Vanilla", "power": 3, "toughness": 3, "keywords": []}, 5, {}
    )
    flying_1_1 = _body(
        {"instance_id": 999, "name": "Token", "power": 1, "toughness": 1, "keywords": ["Flying"]}, 5, {}
    )
    flying_3_3 = _body(
        {"instance_id": 101, "name": "Flyer", "power": 3, "toughness": 3, "keywords": ["Flying"]}, 5, {}
    )
    lifelink_3_3 = _body(
        {"instance_id": 101, "name": "Lifer", "power": 3, "toughness": 3, "keywords": ["Lifelink"]}, 5, {}
    )
    assert (
        vanilla_3_3 is not None
        and flying_1_1 is not None
        and flying_3_3 is not None
        and lifelink_3_3 is not None
    )
    n_x = Node(
        k=1,
        abs_turn=7,
        our_life=20,
        opp_life=16,
        ours=(vanilla_3_3, flying_1_1),
        theirs=(),
        our_tapped=frozenset(),
        their_tapped=frozenset(),
        entered=((999, 5),),
        hand=(),
        lands=(),
        played=(),
        rocks=(),
        returning=(),
    )
    n_y = Node(
        k=1,
        abs_turn=7,
        our_life=20,
        opp_life=16,
        ours=(flying_3_3,),
        theirs=(),
        our_tapped=frozenset(),
        their_tapped=frozenset(),
        entered=(),
        hand=(),
        lands=(),
        played=(),
        rocks=(),
        returning=(),
    )
    n_y_life = Node(
        k=1,
        abs_turn=7,
        our_life=20,
        opp_life=16,
        ours=(lifelink_3_3,),
        theirs=(),
        our_tapped=frozenset(),
        their_tapped=frozenset(),
        entered=(),
        hand=(),
        lands=(),
        played=(),
        rocks=(),
        returning=(),
    )
    n_x_life = Node(
        k=1,
        abs_turn=7,
        our_life=20,
        opp_life=16,
        ours=(lifelink_3_3, flying_1_1),
        theirs=(),
        our_tapped=frozenset(),
        their_tapped=frozenset(),
        entered=((999, 5),),
        hand=(),
        lands=(),
        played=(),
        rocks=(),
        returning=(),
    )
    s_a = Step(
        k=0,
        turn=5,
        label="T",
        land="",
        casts=("A", "B"),
        modes=(),
        targets=(),
        cycles=(),
        attack=(),
        mana=2,
        colors="",
        opp_life_after=16,
        life_after=20,
        their_creatures_after=0,
    )
    s_b = Step(
        k=0,
        turn=5,
        label="T",
        land="",
        casts=("B", "A"),
        modes=(),
        targets=(),
        cycles=(),
        attack=(),
        mana=2,
        colors="",
        opp_life_after=16,
        life_after=20,
        their_creatures_after=0,
    )
    line_x = Line(steps=(s_a,), cls=ALIVE, timing=0, v=1.01, _leaf=n_x)
    line_y = Line(steps=(s_b,), cls=ALIVE, timing=0, v=1.0, baseline=True, _leaf=n_y)
    # Vanilla 3/3 + 1/1 flyer must NOT dominate real 3/3 Flyer
    assert searcher._pick_best([line_x], line_y) is line_y
    # Lifelink 3/3 + 1/1 flyer DOES dominate Lifelink 3/3 (same lifelink body + extra flyer)
    line_x_life = Line(steps=(s_a,), cls=ALIVE, timing=0, v=1.01, _leaf=n_x_life)
    line_y_life = Line(steps=(s_b,), cls=ALIVE, timing=0, v=1.0, baseline=True, _leaf=n_y_life)
    assert searcher._pick_best([line_x_life], line_y_life) is line_x_life


def test_nn7_finding4_any_target_no_double_face_and_self_etb_gate(monkeypatch):
    from arenamcp.card_facts.schema import card_facts_from_dict
    from arenamcp.line_search_moves import _card_facts_buff_aura, _card_facts_models_permanent

    pump_bolt = {
        "name": "Blazing Strike",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Target creature gets +2/+2 until end of turn. Blazing Strike deals 2 damage to any target.",
    }
    pump_bolt_rec = _rec(
        pump_bolt,
        effects=[
            {
                "trigger": "resolves",
                "effect": {"effect_type": "Pump", "power": 2, "toughness": 2, "target": "target creature"},
            },
            {"trigger": "resolves", "effect": {"effect_type": "Damage", "amount": 2, "target": "any target"}},
        ],
    )
    aura_bolt = {
        "name": "Fiery Mantle",
        "mana_cost": "{R}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.\nWhen Fiery Mantle enters, it deals 2 damage to any target.",
    }
    aura_bolt_rec = _rec(
        aura_bolt,
        effects=[
            {
                "trigger": "static",
                "effect": {"effect_type": "Pump", "power": 1, "toughness": 1, "target": "enchanted creature"},
            },
            {"trigger": "etb", "effect": {"effect_type": "Damage", "amount": 2, "target": "any target"}},
        ],
    )
    _patch_records(monkeypatch, [(pump_bolt, pump_bolt_rec), (aura_bolt, aura_bolt_rec)])
    mountain = {
        "instance_id": 50,
        "name": "Mountain",
        "mana_cost": "",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "{T}: Add {R}.",
        "controller_seat_id": 1,
        "is_tapped": False,
    }
    bear = {
        "instance_id": 101,
        "name": "Bear",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Bear",
        "power": 1,
        "toughness": 1,
        "turn_entered_battlefield": 1,
    }
    st = _state(hand=[pump_bolt, aura_bolt], battlefield=[mountain, _norm_card(bear, 101, 1)], opp_life=10)
    moves = _search(st)._search
    for hs in moves.spells:
        variants = hs.variants
        assert all(v.face <= 2 for v in variants), (hs.name, [v.face for v in variants])
        assert any(v.face == 2 and v.aim == "opponent" for v in variants)

    # Unsupported SELF_ETB triggers (Pump or symmetric each_player Damage) fail closed -> None
    etb_pump_aura = _rec(
        aura_bolt,
        effects=[
            {
                "trigger": "static",
                "effect": {"effect_type": "Pump", "power": 1, "toughness": 1, "target": "enchanted creature"},
            },
            {
                "trigger": "etb",
                "effect": {"effect_type": "Pump", "power": 1, "toughness": 1, "target": "target creature"},
            },
        ],
    )
    assert _card_facts_buff_aura(card_facts_from_dict(etb_pump_aura), aura_bolt) is None

    perm_card = {
        "name": "Symmetry Idol",
        "mana_cost": "{2}",
        "type_line": "Artifact",
        "oracle_text": "When Symmetry Idol enters, it deals 2 damage to each player.\nWhenever you cast a noncreature spell, you gain 1 life.",
    }
    sym_rec = _rec(
        perm_card,
        effects=[
            {"trigger": "etb", "effect": {"effect_type": "Damage", "amount": 2, "target": "each player"}},
            {
                "trigger": "Whenever you cast a noncreature spell",
                "effect": {"effect_type": "GainLife", "amount": 1},
            },
        ],
    )
    assert _card_facts_models_permanent(card_facts_from_dict(sym_rec), perm_card) is None


def test_nn7_findings5_6_9_aura_creature_order_counter_value_dead_host_and_open_mana(monkeypatch):
    from arenamcp.card_facts.schema import card_facts_from_dict
    from arenamcp.line_search_moves import _opponent_open_mana

    aura = {
        "name": "Searing Carapace",
        "mana_cost": "{W}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +1/+1.\nWhenever you cast a noncreature spell, Searing Carapace deals 3 damage to each opponent.",
    }
    aura_rec = _rec(
        aura,
        effects=[
            {
                "trigger": "static",
                "effect": {"effect_type": "Pump", "power": 1, "toughness": 1, "target": "enchanted creature"},
            },
            {
                "trigger": "Whenever you cast a noncreature spell",
                "effect": {"effect_type": "Damage", "amount": 3, "target": "each opponent"},
            },
        ],
    )
    bear = {
        "name": "Savannah Lions",
        "mana_cost": "{W}",
        "type_line": "Creature — Cat",
        "power": 2,
        "toughness": 1,
        "oracle_text": "",
    }
    counter_spell = {
        "name": "Boon Counter",
        "mana_cost": "{W}",
        "type_line": "Instant",
        "oracle_text": "Put a +1/+1 counter on target creature.",
    }
    counter_rec = _rec(
        counter_spell,
        effects=[
            {
                "trigger": "resolves",
                "effect": {"effect_type": "PutCounters", "counter_type": "+1/+1", "count": 1},
            }
        ],
    )
    _patch_records(monkeypatch, [(aura, aura_rec), (counter_spell, counter_rec)])

    # 1. Hand order [Aura, Creature] on empty creature board: baseline legally casts Creature then Aura
    st_empty = _state(hand=[aura, bear], battlefield=[_plains(10), _plains(11)], opp_life=20)
    assert _search(st_empty).baseline.steps[0].casts == ("Savannah Lions", "Searing Carapace")

    # Hand [Aura] with no creatures drops Aura in baseline instead of crashing/mis-ordering
    st_no_cre = _state(hand=[aura], battlefield=[_plains(10)], opp_life=20)
    assert _search(st_no_cre).baseline.steps[0].casts == ()

    # 2. Counter-only verified Instant gets positive item_value
    st_cnt = _state(hand=[counter_spell], battlefield=[_plains(10), {**bear, "instance_id": 101}])
    m_cnt = _search(st_cnt)._search
    cnt_var = m_cnt.spells[0].plain
    assert cnt_var is not None and cnt_var.temp_pump == (0, 0) and cnt_var.counter == 1
    assert m_cnt.item_value(cnt_var, None, m_cnt.model.theirs) >= 1.5

    # 3. Dead-host Aura pruning across turn transition (combat trade / block kills host)
    noncreature_filler = {
        "name": "Revitalize",
        "mana_cost": "{1}{W}",
        "type_line": "Instant",
        "oracle_text": "You gain 3 life.\nDraw a card.",
    }
    opp_4_4 = {
        "instance_id": 201,
        "name": "Enormous Baloth",
        "mana_cost": "{4}{G}",
        "type_line": "Creature — Beast",
        "power": 4,
        "toughness": 4,
        "controller_seat_id": 2,
        "turn_entered_battlefield": 1,
    }
    # Host 1/1 + Aura (+1/+1) blocks opp's 4/4 at our_life=4 to survive, so host dies and Aura is pruned
    host_1_1 = {
        **bear,
        "name": "Squire",
        "power": 1,
        "toughness": 1,
        "instance_id": 101,
        "turn_entered_battlefield": 1,
    }
    st_dead_host = _state(
        hand=[aura, noncreature_filler],
        battlefield=[_plains(10), host_1_1, opp_4_4],
        our_life=4,
        opp_life=3,
    )
    st_dead_host["hand"].append(_norm_card(_plains(12), 902, 1))
    res_dead = _search(st_dead_host, survival=True)
    # Turn 2 Revitalize cannot fire pruned Aura for 3 lethal damage -> NOT a WIN
    assert res_dead.best.cls != WIN
    assert res_dead.best._leaf.fact_permanents == ()

    # Positive control: when opp only has a 1/1 attacker, our 2/2 host survives blocking and Aura triggers on T2 -> WIN
    opp_1_1 = {**opp_4_4, "power": 1, "toughness": 1}
    st_live_host = _state(
        hand=[aura, noncreature_filler],
        battlefield=[_plains(10), host_1_1, opp_1_1],
        our_life=4,
        opp_life=3,
    )
    st_live_host["hand"].append(_norm_card(_plains(12), 902, 1))
    res_live = _search(st_live_host, survival=True)
    assert res_live.best.cls == WIN

    # 4. _opponent_open_mana excludes tapped and face-down lands
    st_mana = _state(
        battlefield=[
            {**_swamp(30, ctrl=2), "tapped": True, "is_tapped": False},
            {**_swamp(31, ctrl=2), "face_down": True},
            {**_swamp(32, ctrl=2), "isFacedown": True},
            {**_swamp(33, ctrl=2), "face_down_reason": "manifest"},
            _swamp(34, ctrl=2, tapped=False),
        ]
    )
    open_count, open_colors = _opponent_open_mana(build_board_model(st_mana))
    assert open_count == 1
    assert open_colors == frozenset({"B"})
    assert card_facts_from_dict(aura_rec) is not None
