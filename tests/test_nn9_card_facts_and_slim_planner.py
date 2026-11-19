"""Focused regression and contrary-control tests for Batch 4 NN9 (CardFacts migration & slimmed planner)."""

from __future__ import annotations

import logging
from unittest.mock import Mock

from arenamcp import action_planner as ap
from arenamcp import action_response_parser as arp
from arenamcp import action_types as at
from arenamcp import board_math_picks as bmp
from arenamcp.card_facts import client as cf_client
from arenamcp.card_facts.prompt_render import candidate_fact_hashes, normalized_scryfall_card
from arenamcp.cast_payoffs import _cast_exile_trigger, _possible_target_restriction, colored_exile_payoffs
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.play_safety import (
    attack_trigger_damage,
    each_opponent_damage,
    exiles_graveyard_card,
    face_burn_damage,
    record_for_card,
    record_is_complete,
)
from arenamcp.public_sequence import (
    TriggerUseMemory,
    _discard_indestructible_protection,
    _draw_entry,
    _draw_entry_spec,
    _draw_punisher_spec,
    public_sequence_concerns,
)


def _record(
    card: dict,
    *,
    modes=(),
    effects=(),
    mana=(),
    keywords=(),
    rules_hash: str | None = None,
) -> dict:
    h = rules_hash or cf_client.hash_for_card(card)
    return {
        "schema_version": 1,
        "name": card["name"],
        "mana_cost": card.get("mana_cost", ""),
        "type_line": card.get("type_line", ""),
        "rules_hash": h,
        "body": {"keywords": list(keywords)},
        "modes": list(modes),
        "effects": list(effects),
        "mana": list(mana),
        "confidence": "high",
        "notes": "",
    }


def _seed_store(monkeypatch, records: list[tuple[dict, dict]]) -> None:
    store: dict[str, dict] = {}
    for card, rec in records:
        norm = normalized_scryfall_card(card)
        h = cf_client.hash_for_card(norm or card)
        assert h
        rec["rules_hash"] = h
        store[h] = rec

    def _facts_for(cards, **kwargs):
        wanted: set[str] = set()
        for card in cards:
            wanted.update(candidate_fact_hashes(card))
            norm = normalized_scryfall_card(card)
            if norm:
                h = cf_client.hash_for_card(norm)
                if h:
                    wanted.add(h)
        return {k: store[k] for k in wanted if k in store}

    monkeypatch.setattr(cf_client, "facts_for", _facts_for)
    monkeypatch.setattr("arenamcp.play_safety.facts_for", _facts_for, raising=False)


# ---------------------------------------------------------------------------
# 1. play_safety CardFacts-first helpers (+ opposite / fallback controls)
# ---------------------------------------------------------------------------


def test_each_opponent_damage_record_first_and_fallback(monkeypatch):
    card = {
        "name": "Boltwave",
        "mana_cost": "{R}",
        "type_line": "Sorcery",
        "oracle_text": "Boltwave deals 5 damage to target creature.",
    }
    complete = _record(
        card,
        effects=[
            {"trigger": "CAST", "effect": {"effect_type": "Damage", "amount": 3, "target": "each_opponent"}}
        ],
    )
    _seed_store(monkeypatch, [(card, complete)])
    assert each_opponent_damage(card) == 3

    # Opposite control: complete record with non-opponent Damage suppresses regex even if oracle mentions each opponent.
    contradicted = {
        **card,
        "name": "SingleTarget",
        "oracle_text": "SingleTarget deals 4 damage to each opponent.",
    }
    complete_single = _record(
        contradicted,
        effects=[
            {"trigger": "CAST", "effect": {"effect_type": "Damage", "amount": 4, "target": "target_creature"}}
        ],
    )
    _seed_store(monkeypatch, [(contradicted, complete_single)])
    assert each_opponent_damage(contradicted) == 0

    # Incomplete (UnknownEffect) or missing record falls back to regex.
    incomplete = _record(
        contradicted,
        effects=[{"trigger": "CAST", "effect": {"effect_type": "Unknown", "text": "deals 4"}}],
    )
    _seed_store(monkeypatch, [(contradicted, incomplete)])
    assert not record_is_complete(record_for_card(contradicted))
    assert each_opponent_damage(contradicted) == 4


def test_face_burn_damage_record_first_and_fallback(monkeypatch):
    card = {
        "name": "Shock",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Shock deals 2 damage to target creature only.",
    }
    complete = _record(
        card,
        effects=[
            {"trigger": "CAST", "effect": {"effect_type": "Damage", "amount": 2, "target": "any_target"}}
        ],
    )
    _seed_store(monkeypatch, [(card, complete)])
    assert face_burn_damage(card) == 2

    # Complete creature-only Damage suppresses face burn regex even if oracle claims "any target".
    creature_only = {**card, "name": "Flame Slash", "oracle_text": "Deals 4 damage to any target."}
    _seed_store(
        monkeypatch,
        [
            (
                creature_only,
                _record(
                    creature_only,
                    effects=[
                        {
                            "trigger": "CAST",
                            "effect": {"effect_type": "Damage", "amount": 4, "target": "target_creature"},
                        }
                    ],
                ),
            )
        ],
    )
    assert face_burn_damage(creature_only) == 0

    # Missing record falls back cleanly to regex.
    _seed_store(monkeypatch, [])
    assert face_burn_damage({"name": "Lava Spike", "oracle_text": "Deals 3 damage to target opponent."}) == 3
    assert face_burn_damage({"name": "Chop", "oracle_text": "Deals 3 damage to target creature."}) == 0


def test_attack_trigger_damage_record_first_and_fallback(monkeypatch):
    card = {
        "name": "Hired Claw",
        "mana_cost": "{R}",
        "type_line": "Creature — Lizard Mercenary",
        "oracle_text": "No ping in text.",
    }
    _seed_store(
        monkeypatch,
        [
            (
                card,
                _record(
                    card,
                    effects=[
                        {
                            "trigger": "ATTACKS",
                            "effect": {"effect_type": "Damage", "amount": 1, "target": "defending_player"},
                        }
                    ],
                ),
            )
        ],
    )
    assert attack_trigger_damage(card) == 1

    # Opposite control: complete record with ETB (non-attack) damage suppresses attack regex.
    etb_card = {
        **card,
        "name": "Fake Attacker",
        "oracle_text": "Whenever Fake Attacker attacks, it deals 2 damage to defending player.",
    }
    _seed_store(
        monkeypatch,
        [
            (
                etb_card,
                _record(
                    etb_card,
                    effects=[
                        {
                            "trigger": "ETB",
                            "effect": {"effect_type": "Damage", "amount": 2, "target": "opponent"},
                        }
                    ],
                ),
            )
        ],
    )
    assert attack_trigger_damage(etb_card) == 0

    # Incomplete record falls back to regex.
    _seed_store(
        monkeypatch,
        [
            (
                etb_card,
                _record(etb_card, effects=[{"trigger": "ATTACKS", "effect": {"effect_type": "Unknown"}}]),
            )
        ],
    )
    assert attack_trigger_damage(etb_card) == 2


def test_exiles_graveyard_card_record_first_and_fallback(monkeypatch):
    card = {
        "name": "Ghost Vacuum",
        "mana_cost": "{1}",
        "type_line": "Artifact",
        "oracle_text": "Tap to do something.",
    }
    _seed_store(
        monkeypatch,
        [
            (
                card,
                _record(
                    card,
                    effects=[
                        {
                            "trigger": "ACTIVATED",
                            "effect": {"effect_type": "Exile", "target": "target_card_from_graveyard"},
                        }
                    ],
                ),
            )
        ],
    )
    assert exiles_graveyard_card(card, card["oracle_text"]) is True

    # Opposite control: complete Exile without graveyard suppresses graveyard regex in oracle.
    non_gy = {
        **card,
        "name": "False Relic",
        "oracle_text": "Exile target card from a graveyard.",
    }
    _seed_store(
        monkeypatch,
        [
            (
                non_gy,
                _record(
                    non_gy,
                    effects=[
                        {"trigger": "CAST", "effect": {"effect_type": "Exile", "target": "target_creature"}}
                    ],
                ),
            )
        ],
    )
    assert exiles_graveyard_card(non_gy, non_gy["oracle_text"]) is False

    # Missing record uses regex fallback.
    _seed_store(monkeypatch, [])
    assert (
        exiles_graveyard_card(None, "Exile up to one target creature card from an opponent's graveyard.")
        is True
    )
    assert exiles_graveyard_card(None, "Exile target creature.") is False


# ---------------------------------------------------------------------------
# 2. cast_payoffs CardFacts-first migration
# ---------------------------------------------------------------------------


def test_cast_payoffs_record_first_suppresses_contradictory_regex(monkeypatch):
    ward_card = {
        "instance_id": 10,
        "name": "Warded Giant",
        "mana_cost": "{2}{G}",
        "type_line": "Creature",
        "oracle_text": "Vanilla body.",
        "colors": ["CardColor_Green"],
        "card_types": ["CardType_Creature"],
        "controller_seat_id": 2,
    }
    vanilla_lied = {
        "instance_id": 11,
        "name": "Fake Shield Bear",
        "mana_cost": "{1}{G}",
        "type_line": "Creature",
        "oracle_text": "Ward {3}. This creature cannot be the target of spells.",
        "colors": ["CardColor_Green"],
        "card_types": ["CardType_Creature"],
        "controller_seat_id": 2,
    }
    ugin = {
        "instance_id": 20,
        "name": "Ugin, Eye of the Storms",
        "mana_cost": "{7}",
        "type_line": "Legendary Planeswalker — Ugin",
        "oracle_text": "Synthetic text only.",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    fake_exiler = {
        "instance_id": 21,
        "name": "Not Ugin",
        "mana_cost": "{7}",
        "type_line": "Artifact",
        "oracle_text": "When you cast this spell, exile up to one target permanent that's one or more colors.",
        "owner_seat_id": 1,
        "controller_seat_id": 1,
    }
    _seed_store(
        monkeypatch,
        [
            (ward_card, _record(ward_card, keywords=["Ward {2}"])),
            (vanilla_lied, _record(vanilla_lied, keywords=[])),
            (
                ugin,
                _record(
                    ugin,
                    effects=[
                        {
                            "trigger": "CAST",
                            "effect": {
                                "effect_type": "Exile",
                                "target": "target_permanent_one_or_more_colors",
                            },
                        }
                    ],
                ),
            ),
            (
                fake_exiler,
                _record(
                    fake_exiler, effects=[{"trigger": "CAST", "effect": {"effect_type": "Draw", "count": 1}}]
                ),
            ),
        ],
    )

    assert _possible_target_restriction(ward_card) is True
    assert _possible_target_restriction(vanilla_lied) is False
    assert _cast_exile_trigger(ugin) != ""
    assert _cast_exile_trigger(fake_exiler) == ""

    # Missing record still uses regex fallback.
    _seed_store(monkeypatch, [])
    assert _possible_target_restriction(vanilla_lied) is True
    assert _cast_exile_trigger(fake_exiler) != ""

    # End-to-end colored_exile_payoffs with CardFacts records keeps Fake Shield Bear and excludes Warded Giant.
    _seed_store(
        monkeypatch,
        [
            (ward_card, _record(ward_card, keywords=["Ward {2}"])),
            (vanilla_lied, _record(vanilla_lied, keywords=[])),
            (
                ugin,
                _record(
                    ugin,
                    effects=[
                        {
                            "trigger": "CAST",
                            "effect": {
                                "effect_type": "Exile",
                                "target": "target_permanent_one_or_more_colors",
                            },
                        }
                    ],
                ),
            ),
        ],
    )
    state = {"local_seat_id": 1, "battlefield": [ward_card, vanilla_lied], "hand": [ugin]}
    decision = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        can_pass=True,
        options=(
            DecisionOption(
                "idx:0", "Cast Ugin", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 20}
            ),
        ),
    )
    payoffs = colored_exile_payoffs(decision, state)
    assert len(payoffs) == 1
    assert [c["instance_id"] for c in payoffs[0][3]] == [11]


# ---------------------------------------------------------------------------
# 3. public_sequence CardFacts-first migration & legacy _draw_entry compat
# ---------------------------------------------------------------------------


def test_public_sequence_record_first_and_draw_entry_compat(monkeypatch):
    engine = {
        "instance_id": 100,
        "name": "Enduring Innocence",
        "mana_cost": "{1}{W}{W}",
        "type_line": "Enchantment Creature",
        "oracle_text": "Custom rules.",
        "controller_seat_id": 1,
    }
    non_engine = {
        "instance_id": 101,
        "name": "Not An Engine",
        "mana_cost": "{1}{W}",
        "type_line": "Creature",
        "oracle_text": "Whenever one or more other creatures you control with power 2 or less enter, draw a card.",
        "controller_seat_id": 1,
    }
    protector = {
        "instance_id": 200,
        "name": "Seasoned Hallowblade",
        "mana_cost": "{1}{W}",
        "type_line": "Creature",
        "oracle_text": "Custom.",
        "controller_seat_id": 2,
    }
    looter = {
        "instance_id": 201,
        "name": "Fake Protector",
        "mana_cost": "{1}{U}",
        "type_line": "Creature",
        "oracle_text": "Discard a card: Fake Protector gains indestructible until end of turn.",
        "controller_seat_id": 2,
    }
    bowmasters = {
        "instance_id": 202,
        "name": "Orcish Bowmasters",
        "mana_cost": "{1}{B}",
        "type_line": "Creature",
        "oracle_text": "Custom.",
        "controller_seat_id": 2,
    }
    non_punisher = {
        "instance_id": 203,
        "name": "Plain Orc",
        "mana_cost": "{1}{B}",
        "type_line": "Creature",
        "oracle_text": "Whenever an opponent draws a card, Plain Orc deals 1 damage to any target. Then amass Orcs 1.",
        "controller_seat_id": 2,
    }
    _seed_store(
        monkeypatch,
        [
            (
                engine,
                _record(
                    engine,
                    effects=[
                        {
                            "trigger": "OTHER_CREATURE_ETB",
                            "effect": {"effect_type": "Draw", "count": 1},
                            "conditions": ["other creature power <= 2", "only once each turn"],
                        }
                    ],
                ),
            ),
            (
                non_engine,
                _record(
                    non_engine, effects=[{"trigger": "ETB", "effect": {"effect_type": "Draw", "count": 1}}]
                ),
            ),
            (
                protector,
                _record(
                    protector,
                    modes=[{"cost": {"discard": 1}, "name": "activate"}],
                    effects=[
                        {
                            "trigger": "ACTIVATED",
                            "effect": {"effect_type": "Pump", "power": 0, "toughness": 0},
                            "conditions": ["gains indestructible until end of turn"],
                        }
                    ],
                ),
            ),
            (
                looter,
                _record(
                    looter,
                    modes=[{"cost": {"discard": 1}, "name": "activate"}],
                    effects=[{"trigger": "ACTIVATED", "effect": {"effect_type": "Draw", "count": 1}}],
                ),
            ),
            (
                bowmasters,
                _record(
                    bowmasters,
                    effects=[
                        {
                            "trigger": "OPPONENT_DRAWS",
                            "effect": {"effect_type": "Damage", "amount": 1, "target": "any_target"},
                        },
                        {
                            "trigger": "OPPONENT_DRAWS",
                            "effect": {
                                "effect_type": "CreateToken",
                                "spec": {
                                    "name": "Orc Army",
                                    "power": 1,
                                    "toughness": 1,
                                    "count": 1,
                                    "subtypes": ["Orc", "Army"],
                                },
                            },
                        },
                    ],
                ),
            ),
            (non_punisher, _record(non_punisher, effects=[])),
        ],
    )

    assert _draw_entry_spec(engine) == (2, True)
    assert _draw_entry(engine)[1] == "2"  # entry_draw_projection compatibility
    assert _draw_entry_spec(non_engine) == (None, False)
    assert _draw_entry(non_engine) is None

    assert _discard_indestructible_protection(protector) is True
    assert _discard_indestructible_protection(looter) is False

    assert _draw_punisher_spec(bowmasters) == (True, True)
    assert _draw_punisher_spec(non_punisher) == (False, False)

    # TriggerUseMemory works with CardFacts engine record even without regex text in source oracle.
    mem = TriggerUseMemory()
    used = mem.observe(
        {
            "match_id": "m1",
            "game_number": 1,
            "turn": {"turn_number": 3},
            "battlefield": [engine],
            "stack": [
                {
                    "parent_instance_id": 100,
                    "ability_text": "Whenever one or more other creatures you control with power 2 or less enter, draw a card.",
                }
            ],
        }
    )
    assert 100 in used

    # End-to-end public_sequence_concerns fires against Bowmasters + Enduring Innocence on CardFacts.
    shock = {
        "instance_id": 300,
        "name": "Bear",
        "type_line": "Creature",
        "power": 2,
        "toughness": 2,
        "controller_seat_id": 1,
    }
    state = {
        "local_seat_id": 1,
        "opponent_hand_count": 2,
        "battlefield": [engine, bowmasters],
        "hand": [shock],
    }
    dec = PendingDecision(
        request_id=(1, 2),
        request_type="ActionsAvailable",
        can_pass=True,
        options=(
            DecisionOption(
                "idx:0", "Cast Bear", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 300}
            ),
        ),
    )
    concerns = public_sequence_concerns(dec, state, ["idx:0"], "Cast Bear to curve out.")
    assert len(concerns) == 1
    assert "Orcish Bowmasters" in concerns[0] and "amass Orcs 1" in concerns[0]

    # Missing record falls back to regex cleanly.
    _seed_store(monkeypatch, [])
    assert _draw_entry_spec(non_engine) == (2, False)
    assert _discard_indestructible_protection(looter) is True
    assert _draw_punisher_spec(non_punisher) == (True, True)


# ---------------------------------------------------------------------------
# 4. action_planner extracted sub-modules, re-exports, and monkeypatch surface
# ---------------------------------------------------------------------------


def test_action_planner_reexports_and_monkeypatch_surface(monkeypatch):
    # Identity equality for extracted public & compatibility symbols.
    assert ap.ActionPlan is at.ActionPlan
    assert ap.ActionType is at.ActionType
    assert ap.GameAction is at.GameAction
    assert ap.TurnPlan is at.TurnPlan
    assert ap.TurnPlanStep is at.TurnPlanStep
    assert ap.DECLINE_DECISION is at.DECLINE_DECISION
    assert ap.FALLBACK_BIG_BOARD is at.FALLBACK_BIG_BOARD
    assert ap.ACTION_SCHEMA is at.ACTION_SCHEMA
    assert ap.AUTOPILOT_SYSTEM_PROMPT is at.AUTOPILOT_SYSTEM_PROMPT
    assert ap.strip_json_fences is arp.strip_json_fences
    assert ap.normalize_action_payload is arp.normalize_action_payload
    assert ap.parse_turn_plan_response is arp.parse_turn_plan_response
    assert ap.parse_action_plan_response is arp.parse_action_plan_response
    assert ap.board_math_option_pick is bmp.board_math_option_pick
    assert ap.line_fallback_option_pick is bmp.line_fallback_option_pick
    assert ap.own_stack_only is bmp.own_stack_only

    # Monkeypatching on arenamcp.action_planner is honored inside board_math_picks!
    decision = PendingDecision(
        request_id=(1, 3),
        request_type="ActionsAvailable",
        can_pass=True,
        min_select=0,
        max_select=1,
        options=(
            DecisionOption(
                "idx:0",
                "Activate [Pay 2 life]",
                payable=True,
                meta={"actionType": "ActionType_Activate", "abilityGrpId": 99},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    assert bmp._option_text(decision, "idx:0") == "Activate"

    monkeypatch.setattr(ap, "_own_main_phase_with_empty_stack", lambda gs: False)
    picked, reason = bmp.board_math_option_pick(decision, {"hand": [], "battlefield": []})
    assert picked == ["pass"] and "outside our main phase" in reason

    cast_decision = PendingDecision(
        request_id=(1, 4),
        request_type="ActionsAvailable",
        can_pass=True,
        min_select=0,
        max_select=1,
        options=(
            DecisionOption(
                "idx:0",
                "Cast Grizzly Bears",
                payable=True,
                meta={"actionType": "ActionType_Cast", "instanceId": 55},
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    monkeypatch.setattr("arenamcp.board_assessment.assess", lambda gs: None)
    monkeypatch.setattr("arenamcp.board_assessment.option_role", lambda opt, gs: "creature")
    monkeypatch.setattr(ap, "_own_main_phase_with_empty_stack", lambda gs: True)
    monkeypatch.setattr(
        ap,
        "find_source",
        lambda gs, meta: {
            "instance_id": 55,
            "name": "Grizzly Bears",
            "mana_cost": "{1}{G}",
            "type_line": "Creature — Bear",
            "oracle_text": "",
        },
    )
    picked_cast, _ = bmp.board_math_option_pick(
        cast_decision, {"hand": [], "battlefield": [], "local_seat_id": 1}
    )
    assert picked_cast == ["idx:0"]

    # Turn plan and response parser delegation work and log via arenamcp.action_planner.
    assert arp.logger.name == "arenamcp.action_planner"
    tp = ap.ActionPlanner(Mock())._parse_turn_plan_response(
        '{"turn_plan": {"steps": [{"action_type": "cast_spell", "card_name": "Opt", "rationale": "draw"}]}}',
        current_turn=2,
    )
    assert tp is not None and len(tp.steps) == 1 and tp.steps[0].card_name == "Opt"
    assert logging.getLogger("arenamcp.action_planner").name == arp.logger.name
    assert bmp.logger.name == "arenamcp.action_planner"


def test_nn9_review_edge_cases_and_standalone_legality_import(monkeypatch):
    import subprocess
    import sys

    # Finding 6: action_legality can be imported standalone before action_planner.
    subprocess.run(
        [sys.executable, "-c", "import arenamcp.action_legality"],
        check=True,
    )

    # Finding 2 & 3: lower-bound power >= 4, self ETB draw, _ONCE leak, and _draw_entry oracle mismatch.
    uprising = {
        "name": "Garruk's Uprising",
        "mana_cost": "{2}{G}",
        "type_line": "Enchantment",
        "oracle_text": "Whenever a creature with power 4 or greater you control enters, draw a card.",
    }
    self_draw = {
        "name": "Self Draw",
        "mana_cost": "{1}{W}",
        "type_line": "Creature",
        "oracle_text": "Whenever one or more other creatures you control with power 2 or less enter, draw a card.",
    }
    mismatch_engine = {
        "name": "Power Two Engine",
        "mana_cost": "{2}{W}",
        "type_line": "Enchantment",
        "oracle_text": (
            "Whenever one or more other creatures you control with power 5 or less enter, draw a card. "
            "This ability triggers only once each turn."
        ),
    }

    # Finding 4: Hexproof oracle leak suppressed by empty complete record, and colorless exile rejected.
    hex_oracle = {
        "name": "Oracle Only Hex",
        "mana_cost": "{1}{G}",
        "type_line": "Creature",
        "oracle_text": "Hexproof",
    }
    colorless_exiler = {
        "name": "Colorless Exiler",
        "mana_cost": "{7}",
        "type_line": "Artifact",
        "oracle_text": "Synthetic",
    }

    # Finding 5 & 9: conditional kicker burn not double-counted; non-cast mode ignored; creature-only punisher ignored; graveyard threshold condition ignored.
    kicker_burn = {
        "name": "Burst Style",
        "mana_cost": "{R}",
        "type_line": "Instant",
        "oracle_text": "Deals 2 damage.",
    }
    activated_only = {
        "name": "Activated Only",
        "mana_cost": "{R}",
        "type_line": "Artifact",
        "oracle_text": "Deals 3 damage to any target.",
    }
    creature_pinger = {
        "name": "Creature Pinger",
        "mana_cost": "{1}{B}",
        "type_line": "Creature",
        "oracle_text": "Whenever an opponent draws a card, deals 1 damage to any target.",
    }
    threshold_exile = {
        "name": "Threshold Exile",
        "mana_cost": "{1}{W}",
        "type_line": "Instant",
        "oracle_text": "Exile target card from a graveyard.",
    }

    _seed_store(
        monkeypatch,
        [
            (
                uprising,
                _record(
                    uprising,
                    effects=[
                        {
                            "trigger": "ENTERS_THE_BATTLEFIELD",
                            "effect": {"effect_type": "Draw", "count": 1},
                            "conditions": ["creature you control", "power >= 4", "another creature"],
                        }
                    ],
                ),
            ),
            (
                self_draw,
                _record(
                    self_draw,
                    effects=[
                        {
                            "trigger": "ENTERS_THE_BATTLEFIELD",
                            "effect": {"effect_type": "Draw", "count": 1},
                            "conditions": ["self"],
                        }
                    ],
                ),
            ),
            (
                mismatch_engine,
                _record(
                    mismatch_engine,
                    effects=[
                        {
                            "trigger": "OTHER_CREATURE_ETB",
                            "effect": {"effect_type": "Draw", "count": 1},
                            "conditions": ["other creature power <= 2"],
                        }
                    ],
                ),
            ),
            (hex_oracle, _record(hex_oracle, keywords=[])),
            (
                colorless_exiler,
                _record(
                    colorless_exiler,
                    effects=[
                        {
                            "trigger": "CAST",
                            "effect": {"effect_type": "Exile", "target": "target_colorless_permanent"},
                        }
                    ],
                ),
            ),
            (
                kicker_burn,
                _record(
                    kicker_burn,
                    modes=[{"name": "cast"}, {"name": "kicker"}],
                    effects=[
                        {
                            "trigger": "CAST",
                            "effect": {"effect_type": "Damage", "amount": 2, "target": "any_target"},
                            "conditions": ["not kicked"],
                        },
                        {
                            "trigger": "CAST",
                            "effect": {"effect_type": "Damage", "amount": 4, "target": "any_target"},
                            "conditions": ["if kicked"],
                        },
                    ],
                ),
            ),
            (
                activated_only,
                _record(
                    activated_only,
                    modes=[{"name": "activate"}],
                    effects=[
                        {
                            "trigger": "",
                            "effect": {"effect_type": "Damage", "amount": 3, "target": "any_target"},
                        }
                    ],
                ),
            ),
            (
                creature_pinger,
                _record(
                    creature_pinger,
                    effects=[
                        {
                            "trigger": "OPPONENT_DRAWS",
                            "effect": {"effect_type": "Damage", "amount": 1, "target": "target_creature"},
                        }
                    ],
                ),
            ),
            (
                threshold_exile,
                _record(
                    threshold_exile,
                    effects=[
                        {
                            "trigger": "CAST",
                            "effect": {"effect_type": "Exile", "target": "target_creature"},
                            "conditions": ["if threshold in your graveyard"],
                        }
                    ],
                ),
            ),
        ],
    )

    assert _draw_entry_spec(uprising) == (None, False)
    assert _draw_entry_spec(self_draw) == (None, False)
    assert _draw_entry_spec(mismatch_engine) == (2, False)
    assert _draw_entry(mismatch_engine)[1] == "2"

    assert _possible_target_restriction(hex_oracle) is False
    assert _cast_exile_trigger(colorless_exiler) == ""

    assert face_burn_damage(kicker_burn) == 4
    assert face_burn_damage(activated_only) == 0
    assert each_opponent_damage(activated_only) == 0
    assert _draw_punisher_spec(creature_pinger) == (False, False)
    assert exiles_graveyard_card(threshold_exile, threshold_exile["oracle_text"]) is False
