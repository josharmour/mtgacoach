"""Hermetic tests for NN4 CardFacts trigger canonicalization and search adapter."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from arenamcp import line_search_moves as lsm
from arenamcp.ability_synthesizer import (
    Bounce,
    CreateToken,
    Damage,
    Destroy,
    TokenSpec,
    UnknownEffect,
    cast_time_entries,
    render_effect_sentence,
)
from arenamcp.board_model import build_board_model
from arenamcp.card_facts.compiler import _SYSTEM_PROMPT, normalize_trigger_alias
from arenamcp.card_facts.schema import card_facts_from_dict
from arenamcp.card_facts.search_adapter import canonicalize_trigger, search_record
from arenamcp.card_facts.validate import _aura_line_matches
from arenamcp.line_search import search_lines


def _with_records(monkeypatch, pairs: list[tuple[dict, dict]]) -> None:
    store = {lsm.hash_for_card(card): dict(rec) for card, rec in pairs}

    def fake_facts_for(cards: list[dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for card in cards:
            digest = lsm.hash_for_card(card)
            if digest in store:
                out[digest] = store[digest]
        return out

    monkeypatch.setattr(lsm, "facts_for", fake_facts_for)
    lsm.reset_record_cache()


def _norm_card(raw: dict, iid: int, ctrl: int = 1) -> dict:
    c = dict(raw)
    c["instance_id"] = iid
    c.setdefault("owner_seat_id", ctrl)
    c.setdefault("controller_seat_id", ctrl)
    c.setdefault("turn_entered_battlefield", 1 if ctrl in (1, 2) else -1)
    if "card_types" not in c:
        head = str(c.get("type_line") or "").split("—", 1)[0]
        c["card_types"] = [
            t for t in ("Land", "Creature", "Artifact", "Enchantment", "Instant", "Sorcery") if t in head
        ]
    return c


def _base_state(hand: list[dict] | None = None, battlefield: list[dict] | None = None) -> dict:
    norm_bf = []
    for i, b in enumerate(battlefield or []):
        nb = dict(b)
        ctrl = nb.get("controller_seat_id") or nb.get("controller") or 1
        nb.setdefault("owner_seat_id", ctrl)
        nb.setdefault("controller_seat_id", ctrl)
        nb.setdefault("turn_entered_battlefield", 1)
        nb.setdefault("is_tapped", False)
        if "card_types" not in nb:
            head = str(nb.get("type_line") or "").split("—", 1)[0]
            nb["card_types"] = [
                t for t in ("Land", "Creature", "Artifact", "Enchantment", "Instant", "Sorcery") if t in head
            ]
        norm_bf.append(nb)
    return {
        "match_id": "nn4-test-match",
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 5,
            "active_player": 1,
            "priority_player": 1,
            "phase": "Phase_Main1",
            "step": "",
        },
        "players": [
            {"seat_id": 1, "life_total": 20, "is_local": True, "lands_played": 0},
            {"seat_id": 2, "life_total": 20, "is_local": False, "lands_played": 0},
        ],
        "hand": [_norm_card(c, 900 + i, 1) for i, c in enumerate(hand or [])],
        "battlefield": norm_bf,
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {
            "library_count": 30,
            "library_count_source": "log_zone_membership",
            "opponent_hand_count": 2,
        },
    }


def _live_chrome_mox_card(*, newline_oracle: bool = False) -> dict:
    sep = "\n" if newline_oracle else " "
    return {
        "name": "Chrome Mox",
        "mana_cost": "{0}",
        "type_line": "Artifact",
        "oracle_text": (
            f"Imprint — When this artifact enters, you may exile a nonartifact, nonland card from your hand.{sep}"
            "{T}: Add one mana of any of the exiled card's colors."
        ),
    }


def _live_chrome_mox_record() -> dict:
    return {
        "schema_version": 1,
        "name": "Chrome Mox",
        "rules_hash": "chrome-mox-hash",
        "type_line": "Artifact",
        "mana_cost": "{0}",
        "body": {"power": None, "toughness": None, "loyalty": None, "keywords": ["Imprint"]},
        "modes": [
            {
                "name": "cast",
                "cost": {
                    "mana": "{0}",
                    "tap": False,
                    "sacrifice": None,
                    "life": 0,
                    "discard": 0,
                    "exile_from_graveyard": 0,
                },
                "cost_text": "{0}",
                "stays_on_battlefield": True,
            },
            {
                "name": "activate",
                "cost": {
                    "mana": "",
                    "tap": True,
                    "sacrifice": None,
                    "life": 0,
                    "discard": 0,
                    "exile_from_graveyard": 0,
                },
                "cost_text": "{T}",
                "stays_on_battlefield": True,
            },
        ],
        "effects": [
            {
                "trigger": "ETB",
                "conditions": ["you may exile a nonartifact, nonland card from your hand"],
                "effect": {"effect_type": "Exile", "target": "a nonartifact, nonland card from your hand"},
            },
            {
                "trigger": "activated",
                "conditions": [],
                "effect": {
                    "effect_type": "AddMana",
                    "mana": "{any of the exiled card's colors}",
                    "amount": 1,
                },
            },
        ],
        "mana": [
            {"mana": "{any of the exiled card's colors}", "condition": "requires an exiled card via Imprint"}
        ],
        "compiled_by": "card_facts.compiler",
        "compiled_at": "2026-11-16T22:15:02Z",
        "confidence": "high",
    }


def _pyrokinesis_card() -> dict:
    return {
        "name": "Pyrokinesis",
        "mana_cost": "{4}{R}{R}",
        "type_line": "Instant",
        "oracle_text": (
            "You may exile a red card from your hand rather than pay this spell's mana cost.\n"
            "Pyrokinesis deals 4 damage divided as you choose among any number of target creatures."
        ),
    }


def _pyrokinesis_record() -> dict:
    return {
        "schema_version": 1,
        "name": "Pyrokinesis",
        "type_line": "Instant",
        "mana_cost": "{4}{R}{R}",
        "modes": [
            {
                "name": "cast",
                "cost": {"mana": "{4}{R}{R}"},
                "cost_text": "{4}{R}{R}",
                "stays_on_battlefield": False,
            },
            {
                "name": "alternate_cast",
                "cost": {"mana": ""},
                "cost_text": "You may exile a red card from your hand rather than pay this spell's mana cost.",
                "stays_on_battlefield": False,
            },
        ],
        "effects": [
            {
                "trigger": "cast",
                "conditions": [],
                "effect": {"effect_type": "Damage", "amount": 4, "target": "target creature"},
            },
        ],
        "compiled_by": "card_facts.compiler",
        "compiled_at": "2026-11-16T22:15:02Z",
        "confidence": "high",
    }


def _burst_lightning_card() -> dict:
    return {
        "name": "Burst Lightning",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": (
            "Kicker {4} (You may pay an additional {4} as you cast this spell.)\n"
            "Burst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
        ),
    }


def _burst_lightning_record() -> dict:
    return {
        "schema_version": 1,
        "name": "Burst Lightning",
        "type_line": "Instant",
        "mana_cost": "{R}",
        "modes": [
            {"name": "cast", "cost": {"mana": "{R}"}, "cost_text": "{R}", "stays_on_battlefield": False},
            {
                "name": "cast_kicked",
                "cost": {"mana": "{R}{4}"},
                "cost_text": "Kicker {4} (You may pay an additional {4} as you cast this spell.)",
                "stays_on_battlefield": False,
            },
        ],
        "effects": [
            {
                "trigger": "cast",
                "conditions": [],
                "effect": {"effect_type": "Damage", "amount": 2, "target": "any target"},
            },
            {
                "trigger": "cast",
                "conditions": ["this spell was kicked"],
                "effect": {"effect_type": "Damage", "amount": 4, "target": "any target"},
            },
        ],
        "compiled_by": "card_facts.compiler",
        "compiled_at": "2026-11-16T22:12:57Z",
        "confidence": "high",
    }


def _command_decision_card() -> dict:
    return {
        "name": "Command Decision",
        "mana_cost": "{1}{W}",
        "type_line": "Sorcery",
        "oracle_text": "Choose one —\n• Destroy target creature with power 4 or greater.\n• Draw a card.",
    }


def _command_decision_record() -> dict:
    return {
        "schema_version": 1,
        "name": "Command Decision",
        "type_line": "Sorcery",
        "mana_cost": "{1}{W}",
        "modes": [
            {"name": "cast", "cost": {"mana": "{1}{W}"}, "cost_text": "{1}{W}", "stays_on_battlefield": False}
        ],
        "effects": [
            {
                "trigger": "cast",
                "conditions": ["Choose one — Destroy target creature with power 4 or greater."],
                "effect": {"effect_type": "Destroy", "target": "target creature with power 4 or greater"},
            },
            {
                "trigger": "cast",
                "conditions": ["Choose one — Draw a card."],
                "effect": {"effect_type": "Draw", "count": 1},
            },
        ],
        "compiled_by": "card_facts.compiler",
        "compiled_at": "2026-11-16T22:11:16Z",
        "confidence": "high",
    }


def _llanowar_elves_card() -> dict:
    return {
        "name": "Llanowar Elves",
        "mana_cost": "{G}",
        "type_line": "Creature — Elf Druid",
        "power": "1",
        "toughness": "1",
        "oracle_text": "{T}: Add {G}.",
    }


def _llanowar_elves_record() -> dict:
    return {
        "schema_version": 1,
        "name": "Llanowar Elves",
        "type_line": "Creature — Elf Druid",
        "mana_cost": "{G}",
        "body": {"power": "1", "toughness": "1", "loyalty": None, "keywords": []},
        "modes": [
            {"name": "cast", "cost": {"mana": "{G}"}, "cost_text": "{G}", "stays_on_battlefield": True},
            {
                "name": "activate",
                "cost": {"mana": "", "tap": True},
                "cost_text": "{T}",
                "stays_on_battlefield": True,
            },
        ],
        "effects": [
            {
                "trigger": "activated",
                "conditions": [],
                "effect": {"effect_type": "AddMana", "mana": "{G}", "amount": 1},
            }
        ],
        "mana": [{"mana": "{G}", "condition": ""}],
        "compiled_by": "card_facts.compiler",
        "compiled_at": "2026-11-16T22:15:02Z",
        "confidence": "high",
    }


def test_canonicalize_trigger_exact_mapping_and_fail_closed():
    assert canonicalize_trigger("") == ""
    assert canonicalize_trigger("   ") == ""
    for token in ("cast", "CAST", "Cast", "resolves", "RESOLVES"):
        assert canonicalize_trigger(token) == "CAST"
    for token in ("etb", "ETB", "enters", "ENTERS", "enters_the_battlefield", "ENTERS_THE_BATTLEFIELD"):
        assert canonicalize_trigger(token) == "ENTERS_THE_BATTLEFIELD"
    assert canonicalize_trigger("enters_the_battlefield_or_evoke") == "ENTERS_THE_BATTLEFIELD_OR_EVOKE"
    assert canonicalize_trigger("ENTERS_THE_BATTLEFIELD_OR_EVOKE") == "ENTERS_THE_BATTLEFIELD_OR_EVOKE"
    for token in ("activate", "activated", "ACTIVATED"):
        assert canonicalize_trigger(token) == "ACTIVATED"

    for non_canonical in (
        "attacks",
        "ATTACKS",
        "dies",
        "static",
        "STATIC",
        "I",
        "II",
        "cast via beam me up from graveyard",
        "cast via harmonize",
        "cast or etb",
        "Whenever another creature dies",
        "When this creature enters",
        "landfall",
    ):
        assert canonicalize_trigger(non_canonical) is None


def test_cast_time_entries_rejects_non_exact_triggers():
    rec = card_facts_from_dict(
        {
            "name": "Mixed",
            "effects": [
                {"trigger": "", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "cast", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "resolves", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "etb", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "enters", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "ENTERS_THE_BATTLEFIELD", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "ENTERS_THE_BATTLEFIELD_OR_EVOKE", "effect": {"effect_type": "Draw", "count": 1}},
                {
                    "trigger": "cast via beam me up from graveyard",
                    "effect": {"effect_type": "Draw", "count": 1},
                },
                {"trigger": "cast via harmonize", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "ACTIVATED", "effect": {"effect_type": "AddMana", "mana": "{G}", "amount": 1}},
                {"trigger": "attacks", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "static", "effect": {"effect_type": "Draw", "count": 1}},
                {"trigger": "I", "effect": {"effect_type": "Draw", "count": 1}},
            ],
        }
    )
    entries = cast_time_entries(rec)
    assert [e.trigger for e in entries] == [
        "",
        "cast",
        "resolves",
        "etb",
        "enters",
        "ENTERS_THE_BATTLEFIELD",
        "ENTERS_THE_BATTLEFIELD_OR_EVOKE",
    ]


def test_render_effect_sentence_safety_fixes():
    assert render_effect_sentence(Destroy(target="target artifact")) == "Destroy target artifact."
    assert (
        render_effect_sentence(Destroy(target="target creature with power 4 or greater"))
        == "Destroy target creature with power 4 or greater."
    )
    assert (
        render_effect_sentence(Bounce(target="target creature an opponent controls"))
        == "Return target creature an opponent controls to its owner's hand."
    )
    assert (
        render_effect_sentence(Bounce(target="up to one target creature"))
        == "Return up to one target creature to its owner's hand."
    )
    # Tuck / exile / graveyard / self-damage must fail closed to "".
    assert render_effect_sentence(Bounce(target="target creature on top of its owner's library")) == ""
    assert render_effect_sentence(Bounce(target="target artifact into exile")) == ""
    assert render_effect_sentence(Bounce(target="target nonland permanent on bottom of library")) == ""
    assert render_effect_sentence(Damage(amount=2, target="you")) == ""
    assert render_effect_sentence(Damage(amount=2, target="its controller")) == ""
    assert render_effect_sentence(Damage(amount=0, target="any target")) == ""
    assert render_effect_sentence(Damage(amount=True, target="any target")) == ""
    assert render_effect_sentence(Damage(amount=3, target="any target")) == "~ deals 3 damage to any target."
    assert (
        render_effect_sentence(
            CreateToken(spec=TokenSpec(name="Goblin", power=1, toughness=1, count=1, per_opponent=True))
        )
        == ""
    )


def test_compiler_prompt_and_validation_canonical_compatibility():
    assert "CAST" in _SYSTEM_PROMPT
    assert "ENTERS_THE_BATTLEFIELD" in _SYSTEM_PROMPT
    assert "ACTIVATED" in _SYSTEM_PROMPT
    assert normalize_trigger_alias("etb") == "ENTERS_THE_BATTLEFIELD"
    assert normalize_trigger_alias("enters") == "ENTERS_THE_BATTLEFIELD"
    assert normalize_trigger_alias("resolves") == "CAST"
    assert normalize_trigger_alias("activate") == "ACTIVATED"
    assert normalize_trigger_alias("attacks") == "ATTACKS"
    assert normalize_trigger_alias("dies") == "DIES"
    assert normalize_trigger_alias("static") == "STATIC"
    assert normalize_trigger_alias("cast via harmonize") == "cast via harmonize"

    for trig in ("ETB", "ENTERS_THE_BATTLEFIELD"):
        assert _aura_line_matches(
            card_facts_from_dict(
                {"effects": [{"trigger": trig, "effect": {"effect_type": "Draw", "count": 1}}]}
            ).effects[0],
            "When this Aura enters, draw a card.",
        )
    for trig in ("", "activate", "activated", "ACTIVATED"):
        assert _aura_line_matches(
            card_facts_from_dict(
                {
                    "effects": [
                        {
                            "trigger": trig,
                            "effect": {
                                "effect_type": "Unknown",
                                "text": "{2}, Sacrifice this Aura: Draw a card.",
                            },
                        }
                    ]
                }
            ).effects[0],
            "{2}, Sacrifice this Aura: Draw a card.",
        )
    for trig in ("", "static", "STATIC"):
        assert _aura_line_matches(
            card_facts_from_dict(
                {
                    "effects": [
                        {
                            "trigger": trig,
                            "effect": {
                                "effect_type": "Pump",
                                "power": 1,
                                "toughness": 1,
                                "target": "enchanted creature",
                            },
                        }
                    ]
                }
            ).effects[0],
            "Enchanted creature gets +1/+1.",
        )


def test_live_compiled_chrome_mox_projects_and_models_in_search(monkeypatch):
    raw_rec = _live_chrome_mox_record()
    rec = card_facts_from_dict(raw_rec)
    for nl in (False, True):
        card = _live_chrome_mox_card(newline_oracle=nl)
        projected = search_record(rec, card)
        assert projected is not rec
        assert canonicalize_trigger(projected.effects[1].trigger) == "ENTERS_THE_BATTLEFIELD"
        sem = lsm._record_semantics(rec, "Chrome Mox", card)
        assert sem is not None
        assert sem["rock"] is True
        assert sem["imprint_rock"] is True
        assert sem["consume"] == (1, "", frozenset({"nonartifact", "nonland"}))

    # Mutation negative controls still fail closed.
    for mut in ("extra_kw", "extra_cost", "bad_trigger", "unknown_field", "low_conf"):
        bad = deepcopy(raw_rec)
        if mut == "extra_kw":
            bad["body"]["keywords"] = ["Imprint", "Flying"]
        elif mut == "extra_cost":
            bad["modes"][0]["cost"]["life"] = 1
        elif mut == "bad_trigger":
            bad["effects"][1]["trigger"] = "attacks"
        elif mut == "unknown_field":
            bad["modes"][0]["cost"]["_extra"] = 1
        else:
            bad["confidence"] = "low"
        bad_rec = card_facts_from_dict(bad)
        card = _live_chrome_mox_card()
        assert search_record(bad_rec, card) is bad_rec
        assert lsm._record_semantics(bad_rec, "Chrome Mox", card) is None

    # Full search wiring with live Chrome Mox + red card.
    card = _live_chrome_mox_card(newline_oracle=False)
    red = {
        "name": "Shock",
        "mana_cost": "{1}{R}",
        "type_line": "Instant",
        "oracle_text": "Shock deals 2 damage to any target.",
    }
    _with_records(monkeypatch, [(card, raw_rec)])
    assert lsm.unmodelled_effect(dict(card)) == ""

    state = _base_state(
        hand=[card, red],
        battlefield=[
            {
                "instance_id": 1,
                "name": "Bear",
                "type_line": "Creature — Bear",
                "power": 2,
                "toughness": 2,
                "controller": 2,
                "owner": 2,
            }
        ],
    )
    bm = build_board_model(deepcopy(state))
    mv = lsm.Moves(bm, survival=False, lethal_now=False, hard_ms=100, opponent=None)
    mox_hs = next(hs for hs in mv.spells if hs.name == "Chrome Mox")
    variants, _ = mv._variants(mox_hs)
    assert any(v.rock and v.imprint_rock and v.consumes == 1 for v in variants)
    assert any(not v.rock and not v.imprint_rock and v.consumes == 0 for v in variants)


def test_live_compiled_fury_and_pyrokinesis_wiring(monkeypatch):
    # Fury live-cache shape: ETB trigger + "Evoke—Exile a red card from your hand."
    assert lsm._hand_consumption("Evoke—Exile a red card from your hand.", mode_name="evoke") == (
        1,
        "red",
        frozenset(),
    )
    fury_card = {
        "name": "Fury",
        "mana_cost": "{3}{R}{R}",
        "type_line": "Creature — Elemental Incarnation",
        "power": "3",
        "toughness": "3",
        "keywords": ["Double strike"],
        "oracle_text": (
            "Double strike\nWhen Fury enters, it deals 4 damage divided as you choose among any number "
            "of target creatures and/or planeswalkers.\nEvoke—Exile a red card from your hand."
        ),
    }
    fury_rec = {
        "schema_version": 1,
        "name": "Fury",
        "type_line": fury_card["type_line"],
        "mana_cost": fury_card["mana_cost"],
        "body": {"power": "3", "toughness": "3", "keywords": ["Double strike"]},
        "modes": [
            {
                "name": "cast",
                "cost": {"mana": "{3}{R}{R}"},
                "cost_text": "{3}{R}{R}",
                "stays_on_battlefield": True,
            },
            {
                "name": "evoke",
                "cost": {"mana": ""},
                "cost_text": "Evoke—Exile a red card from your hand.",
                "stays_on_battlefield": False,
            },
        ],
        "effects": [
            {
                "trigger": "ETB",
                "conditions": [],
                "effect": {"effect_type": "Damage", "amount": 4, "target": "target creature or planeswalker"},
            }
        ],
        "compiled_by": "card_facts.compiler",
        "compiled_at": "2026-11-16T22:15:02Z",
        "confidence": "high",
    }

    # Pyrokinesis live-cache shape: alternate_cast unconditional exile.
    pyro_card = _pyrokinesis_card()
    pyro_rec = _pyrokinesis_record()
    red_card = {
        "name": "Mountain Bear",
        "mana_cost": "{1}{R}",
        "type_line": "Creature — Bear",
        "power": "2",
        "toughness": "2",
        "oracle_text": "",
    }

    _with_records(monkeypatch, [(fury_card, fury_rec), (pyro_card, pyro_rec)])
    assert lsm.unmodelled_effect(dict(fury_card)) == ""
    assert lsm.unmodelled_effect(dict(pyro_card)) == ""

    state = _base_state(
        hand=[pyro_card, red_card],
        battlefield=[
            {
                "instance_id": 20,
                "name": "Threat",
                "type_line": "Creature — Beast",
                "power": 4,
                "toughness": 4,
                "controller": 2,
                "owner": 2,
            }
        ],
    )
    bm = build_board_model(deepcopy(state))
    result = search_lines(bm, survival=True, lethal_now=False, hard_ms=1e9)
    assert result.best is not None
    # No lands on board -> search MUST take Pyrokinesis's free alternate_cast mode exiling Mountain Bear.
    assert "Pyrokinesis" in result.best.steps[0].casts
    assert result.best.steps[0].targets


def test_force_of_vigor_and_guidance_failure_fail_closed():
    assert (
        lsm._hand_consumption(
            "If it's not your turn, you may exile a green card from your hand rather than pay this spell's mana cost.",
            mode_name="alternate_cast",
        )
        is None
    )
    fov_rec = card_facts_from_dict(
        {
            "name": "Force of Vigor",
            "type_line": "Instant",
            "mana_cost": "{2}{G}{G}",
            "modes": [
                {
                    "name": "cast",
                    "cost": {"mana": "{2}{G}{G}"},
                    "cost_text": "{2}{G}{G}",
                    "stays_on_battlefield": False,
                },
                {
                    "name": "alternate_cast",
                    "cost": {"mana": ""},
                    "cost_text": "If it's not your turn, you may exile a green card from your hand rather than pay this spell's mana cost.",
                    "stays_on_battlefield": False,
                },
            ],
            "effects": [
                {
                    "trigger": "cast",
                    "conditions": [],
                    "effect": {"effect_type": "Destroy", "target": "target artifact or enchantment"},
                }
            ],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    assert lsm._record_semantics(fov_rec, "Force of Vigor") is None

    gf_rec = card_facts_from_dict(
        {
            "name": "Guidance Failure",
            "type_line": "Instant",
            "mana_cost": "{1}{U}",
            "modes": [
                {
                    "name": "cast",
                    "cost": {"mana": "{1}{U}"},
                    "cost_text": "{1}{U}",
                    "stays_on_battlefield": False,
                }
            ],
            "effects": [
                {
                    "trigger": "cast",
                    "conditions": [],
                    "effect": {
                        "effect_type": "Bounce",
                        "target": "target spell or permanent on top of its owner's library",
                    },
                }
            ],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    assert lsm._record_semantics(gf_rec, "Guidance Failure") is None


def test_burst_lightning_kicker_and_command_decision_modal(monkeypatch):
    burst_card = _burst_lightning_card()
    burst_rec = _burst_lightning_record()
    cmd_card = _command_decision_card()
    cmd_rec = _command_decision_record()

    _with_records(monkeypatch, [(burst_card, burst_rec), (cmd_card, cmd_rec)])
    assert lsm.unmodelled_effect(dict(burst_card)) == ""
    assert lsm.unmodelled_effect(dict(cmd_card)) == ""

    state = _base_state(
        hand=[burst_card, cmd_card],
        battlefield=[
            {
                "instance_id": 1,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "oracle_text": "({T}: Add {R}.)",
                "controller": 1,
                "owner": 1,
            },
            {
                "instance_id": 2,
                "name": "Plains",
                "type_line": "Basic Land — Plains",
                "oracle_text": "({T}: Add {W}.)",
                "controller": 1,
                "owner": 1,
            },
            {
                "instance_id": 3,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "oracle_text": "({T}: Add {R}.)",
                "controller": 1,
                "owner": 1,
            },
            {
                "instance_id": 4,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "oracle_text": "({T}: Add {R}.)",
                "controller": 1,
                "owner": 1,
            },
            {
                "instance_id": 5,
                "name": "Mountain",
                "type_line": "Basic Land — Mountain",
                "oracle_text": "({T}: Add {R}.)",
                "controller": 1,
                "owner": 1,
            },
            {
                "instance_id": 21,
                "name": "Small Bear",
                "type_line": "Creature — Bear",
                "power": 3,
                "toughness": 3,
                "controller": 2,
                "owner": 2,
            },
            {
                "instance_id": 22,
                "name": "Big Bear",
                "type_line": "Creature — Bear",
                "power": 4,
                "toughness": 4,
                "controller": 2,
                "owner": 2,
            },
        ],
    )
    bm = build_board_model(deepcopy(state))
    mv = lsm.Moves(bm, survival=False, lethal_now=False, hard_ms=100, opponent=None)

    burst_hs = next(hs for hs in mv.spells if hs.name == "Burst Lightning")
    burst_vars, _ = mv._variants(burst_hs)
    assert any(v.mode_text == "" and v.mana_value == 1 and v.face == 2 for v in burst_vars)
    assert any(v.mode_text == "cast_kicked" and v.mana_value == 5 and v.face == 4 for v in burst_vars)
    assert any(v.mode_text == "cast_kicked" and v.mana_value == 5 and v.reach is not None for v in burst_vars)

    # Kicked condition without cast_kicked mode fails closed.
    bad_burst = deepcopy(burst_rec)
    bad_burst["modes"] = bad_burst["modes"][:1]
    assert lsm._record_semantics(card_facts_from_dict(bad_burst), "Burst Lightning", burst_card) is None

    # Command Decision kills 4/4, NOT 3/3.
    cmd_hs = next(hs for hs in mv.spells if hs.name == "Command Decision")
    cmd_vars, _ = mv._variants(cmd_hs)
    kill_var = next(v for v in cmd_vars if v.reach is not None)
    assert lsm._kills(kill_var.reach, bm.theirs[1]) is True
    assert lsm._kills(kill_var.reach, bm.theirs[0]) is False


def test_mana_creature_respects_summoning_sickness_and_blocker_death(monkeypatch):
    elf_card = _llanowar_elves_card()
    elf_rec = _llanowar_elves_record()
    _with_records(monkeypatch, [(elf_card, elf_rec)])
    assert lsm.unmodelled_effect(dict(elf_card)) == ""

    sem = lsm._record_semantics(card_facts_from_dict(elf_rec), "Llanowar Elves", elf_card)
    assert sem is not None
    assert sem["rock"] is True
    assert sem["rock_produces"] == frozenset({"G"})

    state = _base_state(
        hand=[elf_card],
        battlefield=[
            {
                "instance_id": 1,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "oracle_text": "({T}: Add {G}.)",
                "controller": 1,
                "owner": 1,
            }
        ],
    )
    bm = build_board_model(deepcopy(state))
    mv = lsm.Moves(bm, survival=False, lethal_now=False, hard_ms=100, opponent=None)
    elf_hs = next(hs for hs in mv.spells if hs.name == "Llanowar Elves")
    elf_vars, _ = mv._variants(elf_hs)
    elf_var = elf_vars[0]
    assert elf_var.rock is True and elf_var.rock_produces == frozenset({"G"}) and elf_var.body is not None

    # Living Elf on node.ours supplies mana on future turns; dead Elf is filtered out.
    living_body = dict(elf_var.body)
    rock_obj = SimpleNamespace(
        produces=frozenset({"G"}), name="Llanowar Elves", creature_iid=living_body["instance_id"]
    )
    node_alive = SimpleNamespace(k=1, played=(), rocks=(rock_obj,), ours=(living_body,))
    node_dead = SimpleNamespace(k=1, played=(), rocks=(rock_obj,), ours=())
    _, srcs_alive = mv.sources(node_alive, None)
    _, srcs_dead = mv.sources(node_dead, None)
    assert any(s.name == "Llanowar Elves" and "G" in s.produces for s in srcs_alive)
    assert not any(s.name == "Llanowar Elves" for s in srcs_dead)


def test_mana_lands_and_landfall_variant():
    orion_rec = card_facts_from_dict(
        {
            "name": "Orion",
            "type_line": "Land",
            "mana_cost": "",
            "modes": [
                {
                    "name": "activate",
                    "cost": {"mana": "", "tap": True},
                    "cost_text": "{T}: Add {R} or {G}.",
                    "stays_on_battlefield": True,
                }
            ],
            "effects": [
                {"trigger": "ETB", "conditions": [], "effect": {"effect_type": "GainLife", "amount": 1}},
                {
                    "trigger": "activated",
                    "conditions": [],
                    "effect": {"effect_type": "AddMana", "mana": "{R} or {G}", "amount": 1},
                },
            ],
            "mana": [{"mana": "{R} or {G}", "condition": ""}],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    orion_sem = lsm._record_semantics(orion_rec, "Orion")
    assert orion_sem is not None
    assert orion_sem["land_mana"] is True
    assert orion_sem["rock_produces"] == frozenset({"R", "G"})
    assert orion_sem["etb_gain"] == 1

    tomb_rec = card_facts_from_dict(
        {
            "name": "Ancient Tomb",
            "type_line": "Land",
            "mana_cost": "",
            "modes": [
                {
                    "name": "activate",
                    "cost": {"mana": "", "tap": True},
                    "cost_text": "{T}: Add {C}{C}.",
                    "stays_on_battlefield": True,
                }
            ],
            "effects": [
                {
                    "trigger": "activated",
                    "conditions": [],
                    "effect": {"effect_type": "AddMana", "mana": "{C}{C}", "amount": 2},
                },
                {
                    "trigger": "activated",
                    "conditions": [],
                    "effect": {"effect_type": "Damage", "amount": 2, "target": "you"},
                },
            ],
            "mana": [{"mana": "{C}{C}", "condition": ""}],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    tomb_sem = lsm._record_semantics(tomb_rec, "Ancient Tomb")
    assert tomb_sem is not None
    assert tomb_sem["rock_produces"] == frozenset({"C"})
    assert tomb_sem["pain_damage"] == 2
    assert tomb_sem["mana_amount"] == 2

    # Nonland artifact may NOT pass with pain Damage(you).
    bad_rock = deepcopy(tomb_rec)
    bad_rock = card_facts_from_dict(
        {
            "name": "Pain Rock",
            "type_line": "Artifact",
            "mana_cost": "{0}",
            "modes": [
                {"name": "cast", "cost": {"mana": "{0}"}, "cost_text": "{0}", "stays_on_battlefield": True},
                {
                    "name": "activate",
                    "cost": {"mana": "", "tap": True},
                    "cost_text": "{T}",
                    "stays_on_battlefield": True,
                },
            ],
            "effects": [
                {
                    "trigger": "activated",
                    "conditions": [],
                    "effect": {"effect_type": "AddMana", "mana": "{C}", "amount": 1},
                },
                {
                    "trigger": "activated",
                    "conditions": [],
                    "effect": {"effect_type": "Damage", "amount": 1, "target": "you"},
                },
            ],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    assert lsm._record_semantics(bad_rock, "Pain Rock") is None

    # Commander identity & basic land type conditional land mana.
    tower_rec = card_facts_from_dict(
        {
            "name": "Command Tower",
            "type_line": "Land",
            "mana_cost": "",
            "modes": [
                {
                    "name": "activate",
                    "cost": {"mana": "", "tap": True},
                    "cost_text": "{T}",
                    "stays_on_battlefield": True,
                }
            ],
            "effects": [
                {
                    "trigger": "activated",
                    "conditions": ["one mana of any color in your commander's color identity"],
                    "effect": {"effect_type": "AddMana", "mana": "any color", "amount": 1},
                }
            ],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    assert lsm._record_semantics(tower_rec, "Command Tower")["rock_produces"] == frozenset("WUBRG")

    # Landfall "(landfall)" trigger works in _event_landfall_mana while _record_semantics abstains.
    cobra_card = {
        "name": "Lotus Cobra",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Snake",
        "oracle_text": "Landfall — Whenever a land you control enters, add one mana of any color.",
    }
    cobra_rec = card_facts_from_dict(
        {
            "name": "Lotus Cobra",
            "mana_cost": "{1}{G}",
            "type_line": "Creature — Snake",
            "modes": [
                {
                    "name": "cast",
                    "cost": {"mana": "{1}{G}"},
                    "cost_text": "{1}{G}",
                    "stays_on_battlefield": True,
                }
            ],
            "effects": [
                {
                    "trigger": "Whenever a land you control enters (landfall)",
                    "conditions": [],
                    "effect": {"effect_type": "AddMana", "mana": "any color", "amount": 1},
                }
            ],
            "compiled_by": "card_facts.compiler",
            "confidence": "high",
        }
    )
    assert lsm._event_landfall_mana(cobra_rec, cobra_card) is True
    assert lsm._record_semantics(cobra_rec, "Lotus Cobra", cobra_card) is None


@pytest.mark.parametrize(
    "bad_kind",
    [
        "record_unknown_field",
        "body_unknown_field",
        "effect_unknown_field",
        "mode_unknown_field",
        "cost_unknown_field",
        "unknown_effect",
        "non_high_conf",
        "ritual_spell",
        "sacrifice_land",
    ],
)
def test_strict_fail_closed_invariants_on_expanded_paths(bad_kind):
    if bad_kind in ("ritual_spell", "sacrifice_land"):
        if bad_kind == "ritual_spell":
            rec = card_facts_from_dict(
                {
                    "name": "Pyretic Ritual",
                    "type_line": "Instant",
                    "mana_cost": "{1}{R}",
                    "modes": [
                        {
                            "name": "cast",
                            "cost": {"mana": "{1}{R}"},
                            "cost_text": "{1}{R}",
                            "stays_on_battlefield": False,
                        }
                    ],
                    "effects": [
                        {
                            "trigger": "cast",
                            "conditions": [],
                            "effect": {"effect_type": "AddMana", "mana": "{R}{R}{R}", "amount": 3},
                        }
                    ],
                    "compiled_by": "card_facts.compiler",
                    "confidence": "high",
                }
            )
            assert lsm._record_semantics(rec, "Pyretic Ritual") is None
        else:
            rec = card_facts_from_dict(
                {
                    "name": "Strip Mine",
                    "type_line": "Land",
                    "mana_cost": "",
                    "modes": [
                        {
                            "name": "activate",
                            "cost": {
                                "mana": "",
                                "tap": True,
                                "sacrifice": {"target": "this land", "count": 1},
                            },
                            "cost_text": "{T}, Sacrifice ~",
                            "stays_on_battlefield": True,
                        }
                    ],
                    "effects": [
                        {
                            "trigger": "activated",
                            "conditions": [],
                            "effect": {"effect_type": "Destroy", "target": "target land"},
                        }
                    ],
                    "compiled_by": "card_facts.compiler",
                    "confidence": "high",
                }
            )
            assert lsm._record_semantics(rec, "Strip Mine") is None
        return

    raw = deepcopy(_llanowar_elves_record())
    if bad_kind == "record_unknown_field":
        raw["future_obligation"] = True
    elif bad_kind == "body_unknown_field":
        raw["body"]["future_stat"] = 99
    elif bad_kind == "effect_unknown_field":
        raw["effects"][0]["effect"]["duration"] = "until end of turn"
    elif bad_kind == "mode_unknown_field":
        raw["modes"][0]["timing"] = "sorcery"
    elif bad_kind == "cost_unknown_field":
        raw["modes"][0]["cost"]["energy"] = 2
    elif bad_kind == "unknown_effect":
        raw["effects"].append(
            {"trigger": "cast", "conditions": [], "effect": {"effect_type": "Unknown", "text": "mystery"}}
        )
    elif bad_kind == "non_high_conf":
        raw["confidence"] = "medium"

    rec = card_facts_from_dict(raw)
    if bad_kind == "unknown_effect":
        assert any(isinstance(e.effect, UnknownEffect) for e in rec.effects)
    assert lsm._record_semantics(rec, "Llanowar Elves") is None
    assert search_record(rec, _llanowar_elves_card()) is rec


def test_rejected_records_do_not_leak_and_noncreature_permanent_gets_variants(monkeypatch):
    # Finding #1a: Creature whose record is rejected by _record_semantics must NOT leak into plain ETB reach/gain.
    bad_creature = {
        "name": "Etched Hunter",
        "mana_cost": "{2}{R}",
        "type_line": "Creature — Elemental",
        "power": "2",
        "toughness": "2",
        "oracle_text": "When this creature enters, it deals 4 damage divided evenly among opponent blockers.",
    }
    bad_creature_rec = {
        "schema_version": 1,
        "name": "Etched Hunter",
        "type_line": bad_creature["type_line"],
        "mana_cost": bad_creature["mana_cost"],
        "body": {"power": "2", "toughness": "2", "keywords": []},
        "modes": [
            {
                "name": "cast",
                "cost": {"mana": "{2}{R}"},
                "cost_text": "{2}{R}",
                "stays_on_battlefield": True,
                "_extra_unknown": True,
            }
        ],
        "effects": [
            {
                "trigger": "ETB",
                "conditions": [],
                "effect": {"effect_type": "Damage", "amount": 4, "target": "target creature"},
            }
        ],
        "compiled_by": "card_facts.compiler",
        "confidence": "high",
    }

    # Finding #1b: Rejected non-creature (e.g., Aura whose text is unmodelled) must NOT be rescued via record_cast_text.
    bad_aura = {
        "name": "Unholy Aura",
        "mana_cost": "{1}{B}",
        "type_line": "Enchantment — Aura",
        "oracle_text": "Enchant creature\nEnchanted creature gets +2/+2 and has '{T}: Scry 1.'",
    }
    bad_aura_rec = {
        "schema_version": 1,
        "name": "Unholy Aura",
        "type_line": bad_aura["type_line"],
        "mana_cost": bad_aura["mana_cost"],
        "modes": [
            {"name": "cast", "cost": {"mana": "{1}{B}"}, "cost_text": "{1}{B}", "stays_on_battlefield": True}
        ],
        "effects": [
            {"trigger": "ETB", "conditions": [], "effect": {"effect_type": "GainLife", "amount": 3}},
        ],
        "compiled_by": "card_facts.compiler",
        "confidence": "high",
    }

    # Finding #2: Non-creature permanent cleared via "its triggered ability" gets Moves._variants parity.
    etb_artifact = {
        "name": "Spark Relic",
        "mana_cost": "{1}{R}",
        "type_line": "Artifact",
        "oracle_text": "When this artifact enters, it deals 2 damage to up to one target creature if able.",
    }
    etb_artifact_rec = {
        "schema_version": 1,
        "name": "Spark Relic",
        "type_line": etb_artifact["type_line"],
        "mana_cost": etb_artifact["mana_cost"],
        "modes": [
            {"name": "cast", "cost": {"mana": "{1}{R}"}, "cost_text": "{1}{R}", "stays_on_battlefield": True}
        ],
        "effects": [
            {
                "trigger": "ETB",
                "conditions": [],
                "effect": {"effect_type": "Damage", "amount": 2, "target": "target creature"},
            }
        ],
        "compiled_by": "card_facts.compiler",
        "confidence": "high",
    }

    _with_records(
        monkeypatch,
        [
            (bad_creature, bad_creature_rec),
            (bad_aura, bad_aura_rec),
            (etb_artifact, etb_artifact_rec),
        ],
    )
    assert lsm.unmodelled_effect(dict(bad_creature)) == "its triggered ability"
    assert lsm.unmodelled_effect(dict(bad_aura)) == "its effect"
    assert lsm.unmodelled_effect(dict(etb_artifact)) == ""

    state = _base_state(
        hand=[bad_creature, bad_aura, etb_artifact],
        battlefield=[
            {
                "instance_id": 40,
                "name": "Bear",
                "type_line": "Creature — Bear",
                "power": 2,
                "toughness": 2,
                "controller": 2,
                "owner": 2,
            }
        ],
    )
    mv = lsm.Moves(
        build_board_model(deepcopy(state)), survival=False, lethal_now=False, hard_ms=100, opponent=None
    )

    hunter_vars, hunter_plain = mv._variants(next(hs for hs in mv.spells if hs.name == "Etched Hunter"))
    assert hunter_plain is not None
    assert hunter_plain.reach is None and all(v.reach is None for v in hunter_vars)

    aura_vars, aura_plain = mv._variants(next(hs for hs in mv.spells if hs.name == "Unholy Aura"))
    assert aura_plain is None and not aura_vars

    relic_vars, relic_plain = mv._variants(next(hs for hs in mv.spells if hs.name == "Spark Relic"))
    assert relic_plain is not None and relic_plain.reach is not None
    assert relic_vars and relic_vars[0].reach is not None


def test_landfall_metadata_and_nonland_mana_conditions_abstain():
    # _event_landfall_mana tolerates explanatory compiler notes (e.g., Lotus Cobra),
    # while _record_semantics still rejects record.notes and _event_landfall_mana rejects
    # mismatched type_line or non-empty reserved semantic fields.
    cobra_card = {
        "name": "Lotus Cobra",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Snake",
        "oracle_text": "Landfall — Whenever a land you control enters, add one mana of any color.",
    }
    base_cobra = {
        "name": "Lotus Cobra",
        "mana_cost": "{1}{G}",
        "type_line": "Creature — Snake",
        "modes": [
            {"name": "cast", "cost": {"mana": "{1}{G}"}, "cost_text": "{1}{G}", "stays_on_battlefield": True}
        ],
        "effects": [
            {
                "trigger": "Whenever a land you control enters (landfall)",
                "conditions": [],
                "effect": {"effect_type": "AddMana", "mana": "any color", "amount": 1},
            }
        ],
        "compiled_by": "card_facts.compiler",
        "confidence": "high",
    }
    with_notes = deepcopy(base_cobra)
    with_notes["notes"] = "caveat"
    bad_type = deepcopy(base_cobra)
    bad_type["type_line"] = "Enchantment"
    with_threat = deepcopy(base_cobra)
    with_threat["threat"] = {"note": "x"}
    with_notes_rec = card_facts_from_dict(with_notes)
    assert lsm._event_landfall_mana(with_notes_rec, cobra_card) is True
    assert lsm._record_semantics(with_notes_rec, "Lotus Cobra", cobra_card) is None
    assert lsm._event_landfall_mana(card_facts_from_dict(bad_type), cobra_card) is False
    assert lsm._event_landfall_mana(card_facts_from_dict(with_threat), cobra_card) is False

    # _record_semantics rejects notes on a cast-time/activated mana card.
    elf_with_notes = deepcopy(_llanowar_elves_record())
    elf_with_notes["notes"] = "caveat"
    assert lsm._record_semantics(card_facts_from_dict(elf_with_notes), "Llanowar Elves") is None

    # Finding #5: Nonland with conditional record.mana abstains even if effect itself looks clean.
    cond_elf = deepcopy(_llanowar_elves_record())
    cond_elf["mana"] = [{"mana": "{G}", "condition": "Spend this mana only to cast a creature spell."}]
    assert lsm._record_semantics(card_facts_from_dict(cond_elf), "Llanowar Elves") is None
