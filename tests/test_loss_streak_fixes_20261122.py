"""Regression tests for the 2026-11-22 5-loss streak fixes.

Covers three root-cause bugs found across the 2026-11-22 match bundles
(bug_20261122_101104.json):
1. Planeswalker attack targets hidden/misreported as face damage + missing
   walker-saving block guard on LLM plans (match 639f4fba T14).
2. Stale GamePlan attack step keeping has_pending_attack_intent() True and
   overruling an explicit model hold + combat_choice overvaluing trading into
   an opposing commander (match 639f4fba T15).
3. Reactive protection instants with lifegain/scry riders (Tamiyo's Safekeeping,
   Loran's Escape) classified as proactive lifegain/selection in card_role and
   recommended on Turn 1 Main 1 by LINES (matches b37a03c3 T2, 8fa30fc9 T4-T8).
"""

from __future__ import annotations

import copy

from arenamcp.action_planner import ActionPlan, ActionPlanner, ActionType, GameAction
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.board_assessment import assess, card_role
from arenamcp.coach import CoachEngine
from arenamcp.combat_strategy import combat_choice, walker_saving_blocks
from arenamcp.game_plan import GamePlan, GamePlanManager, board_basis


def _match5_t14_state(*, target_id: int = 664, menace: bool = False) -> dict:
    keywords = ["menace"] if menace else []
    nissa = {
        "instance_id": 664,
        "name": "Nissa, Who Shakes the World",
        "type_line": "Legendary Planeswalker — Nissa",
        "card_types": ["Planeswalker"],
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "counters": {"Loyalty": 6},
    }
    attacker = {
        "instance_id": 486,
        "name": "Studious First-Year",
        "type_line": "Creature — Human",
        "card_types": ["Creature"],
        "power": 6,
        "toughness": 6,
        "keywords": keywords,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": True,
        "is_attacking": True,
        "attack_target_id": target_id,
    }
    aerid = {
        "instance_id": 676,
        "name": "Aerid Konstrari",
        "type_line": "Legendary Creature — Angel",
        "card_types": ["Creature"],
        "power": 7,
        "toughness": 6,
        "keywords": ["flying"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "is_attacking": False,
    }
    forest = {
        "instance_id": 442,
        "name": "Forest",
        "type_line": "Basic Land Creature — Forest Elemental",
        "card_types": ["Creature", "Land"],
        "power": 3,
        "toughness": 3,
        "keywords": ["vigilance", "haste"],
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
    }
    commander_hobbit = {
        "instance_id": 502,
        "name": "The Notary Hobbits",
        "type_line": "Legendary Creature — Halfling Advisor",
        "card_types": ["Creature"],
        "power": 1,
        "toughness": 1,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
        "object_kind": "CARD",
    }
    token_hobbit = {
        "instance_id": 510,
        "name": "The Notary Hobbits",
        "type_line": "Creature — Halfling Advisor",
        "card_types": ["Creature"],
        "power": 1,
        "toughness": 1,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
        "is_token": True,
        "object_kind": "TOKEN",
    }
    titan = {
        "instance_id": 533,
        "name": "Titan of Industry",
        "type_line": "Creature — Elemental",
        "card_types": ["Creature"],
        "power": 7,
        "toughness": 7,
        "keywords": ["reach", "trample"],
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": True,
    }
    return {
        "match_id": "639f4fba-d111-4c9b-9aa7-597926ce3daf",
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "turn": {"turn_number": 14, "active_player": 1, "phase": "Phase_Combat", "step": "Step_DeclareBlock"},
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 18, "commander_ids": [676]},
            {"seat_id": 2, "is_local": True, "life_total": 18, "commander_ids": [502]},
        ],
        "battlefield": [nissa, attacker, aerid, forest, commander_hobbit, token_hobbit, titan],
        "hand": [],
        "stack": [],
        "decision_context": {
            "type": "declare_blockers",
            "legal_blocker_ids": [442, 502, 510],
            "legal_blockers": ["Forest", "The Notary Hobbits #1", "The Notary Hobbits #2"],
            "raw_blockers": [
                {"blockerInstanceId": 442, "attackerInstanceIds": [486]},
                {"blockerInstanceId": 502, "attackerInstanceIds": [486]},
                {"blockerInstanceId": 510, "attackerInstanceIds": [486]},
            ],
        },
    }


def _match5_t15_state(*, aerid_tapped: bool = False) -> dict:
    return {
        "match_id": "639f4fba-d111-4c9b-9aa7-597926ce3daf",
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "_bridge_connected": True,
        "_bridge_request_type": "DeclareAttackers",
        "_bridge_request_class": "DeclareAttackersRequest",
        "turn": {
            "turn_number": 15,
            "active_player": 2,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 18, "commander_ids": [676]},
            {"seat_id": 2, "is_local": True, "life_total": 18, "commander_ids": [502]},
        ],
        "battlefield": [
            {
                "instance_id": 442,
                "name": "Forest",
                "type_line": "Basic Land Creature — Forest Elemental",
                "card_types": ["Creature", "Land"],
                "power": 3,
                "toughness": 3,
                "keywords": ["vigilance", "haste"],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
            },
            {
                "instance_id": 502,
                "name": "The Notary Hobbits",
                "type_line": "Legendary Creature — Halfling Advisor",
                "card_types": ["Creature"],
                "power": 1,
                "toughness": 1,
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 9,
            },
            {
                "instance_id": 510,
                "name": "The Notary Hobbits",
                "type_line": "Creature — Halfling Advisor",
                "card_types": ["Creature"],
                "power": 1,
                "toughness": 1,
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "is_token": True,
                "turn_entered_battlefield": 9,
            },
            {
                "instance_id": 533,
                "name": "Titan of Industry",
                "type_line": "Creature — Elemental",
                "card_types": ["Creature"],
                "power": 7,
                "toughness": 7,
                "keywords": ["reach", "trample"],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 11,
            },
            {
                "instance_id": 486,
                "name": "Studious First-Year",
                "type_line": "Creature — Human",
                "card_types": ["Creature"],
                "power": 6,
                "toughness": 6,
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": True,
                "turn_entered_battlefield": 6,
            },
            {
                "instance_id": 659,
                "name": "Heartwood Crafter",
                "type_line": "Creature — Elf",
                "card_types": ["Creature"],
                "power": 1,
                "toughness": 1,
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": True,
                "turn_entered_battlefield": 12,
            },
            {
                "instance_id": 676,
                "name": "Aerid Konstrari",
                "type_line": "Legendary Creature — Angel",
                "card_types": ["Creature"],
                "power": 7,
                "toughness": 6,
                "keywords": ["flying"],
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": aerid_tapped,
                "turn_entered_battlefield": 14,
            },
            {
                "instance_id": 692,
                "name": "Scavenging Ooze",
                "type_line": "Creature — Ooze",
                "card_types": ["Creature"],
                "power": 2,
                "toughness": 2,
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 14,
            },
        ],
        "hand": [],
        "stack": [],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attackers": [
                "Forest",
                "The Notary Hobbits #1",
                "The Notary Hobbits #2",
                "Titan of Industry",
            ],
            "legal_attacker_ids": [442, 502, 510, 533],
            "raw_attackers": [
                {
                    "attackerInstanceId": iid,
                    "legalDamageRecipients": [{"type": "Player", "playerSystemSeatId": 1}],
                }
                for iid in (442, 502, 510, 533)
            ],
        },
    }


def test_planeswalker_attack_target_shown_and_not_counted_as_face_damage():
    state = _match5_t14_state(target_id=664)
    coach = CoachEngine.__new__(CoachEngine)
    local = state["players"][1]
    yours = [c for c in state["battlefield"] if c["controller_seat_id"] == 2]
    opps = [c for c in state["battlefield"] if c["controller_seat_id"] == 1]

    combat_lines = coach._format_block_combat(
        yours, opps, local, 14, "Phase_Combat", set(), state["decision_context"], state
    )
    joined = "\n".join(combat_lines)
    assert "Studious First-Year 6/6 [-> Nissa, Who Shakes the World (6 loyalty)]" in joined
    assert "No blocks → 0 dmg to you (18 life)" in joined
    assert "Nissa, Who Shakes the World (6 loyalty) faces 6 unblocked dmg — DIES if unblocked!" in joined
    assert "Recommended planeswalker-saving blocks:" in joined
    assert "12 life remaining" not in joined

    detail_lines = coach._format_block_decision_details(state, state["decision_context"])
    assert any("Nissa, Who Shakes the World (6 loyalty)" in line for line in detail_lines)

    # Control: when the same 6/6 attacks the player instead of Nissa, report face damage normally.
    face_state = _match5_t14_state(target_id=2)
    face_yours = [c for c in face_state["battlefield"] if c["controller_seat_id"] == 2]
    face_opps = [c for c in face_state["battlefield"] if c["controller_seat_id"] == 1]
    face_joined = "\n".join(
        coach._format_block_combat(
            face_yours,
            face_opps,
            local,
            14,
            "Phase_Combat",
            set(),
            face_state["decision_context"],
            face_state,
        )
    )
    assert "No blocks → take 6 dmg → 12 life remaining" in face_joined
    assert "Planeswalker under attack" not in face_joined


def test_walker_saving_block_guard_chumps_with_token_not_commander_and_respects_controls():
    state = _match5_t14_state(target_id=664)
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._ensure_walker_saving_blocks(plan, state, state["decision_context"])
    assert plan.actions[0].action_type == ActionType.DECLARE_BLOCKERS
    # Prefers token copy (510) over commander (502) on equal material.
    assert plan.actions[0].blocker_instance_assignments == {510: 486}

    # Control 1: attacking player (not Nissa) -> guard does not override no-blocks.
    face_state = _match5_t14_state(target_id=2)
    face_plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._ensure_walker_saving_blocks(face_plan, face_state, face_state["decision_context"])
    assert face_plan.actions[0].action_type == ActionType.CLICK_BUTTON

    # Control 2: unblocked menace attacker targeting Nissa -> must not add an illegal single blocker.
    menace_state = _match5_t14_state(target_id=664, menace=True)
    cards = {c["instance_id"]: c for c in menace_state["battlefield"]}
    added = walker_saving_blocks(
        [cards[486]],
        [cards[442], cards[502], cards[510]],
        {},
        {442: {486}, 502: {486}, 510: {486}},
        cards,
        2,
        own_commanders={502},
    )
    assert added == {}


def test_check_block_survival_does_not_count_planeswalker_damage_as_lethal_to_player():
    state = _match5_t14_state(target_id=664)
    state["players"][1]["life_total"] = 6  # Would look lethal if walker damage counted as face damage.
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._check_block_survival(plan, state, state["decision_context"])
    assert plan.actions[0].action_type == ActionType.CLICK_BUTTON


def test_stale_game_plan_clears_attack_intent_so_explicit_hold_is_honored():
    t14_basis_state = _match5_t15_state(aerid_tapped=False)
    t14_basis_state["turn"]["turn_number"] = 14
    t14_basis_state["battlefield"] = [
        c for c in t14_basis_state["battlefield"] if c["name"] not in ("Aerid Konstrari", "Scavenging Ooze")
    ]
    t15_state = _match5_t15_state(aerid_tapped=False)

    mgr = GamePlanManager(None)
    mgr._match_id = t15_state["match_id"]
    mgr._plan = GamePlan(
        turn_formed=14,
        role="aggressor",
        win_conditions=["Swing with Titan and animated Forest"],
        turn_plan=[
            {
                "turn": 15,
                "land": "Mutavault",
                "cast": [],
                "attack": "Forest, The Notary Hobbits, Titan of Industry",
                "hold": "",
            }
        ],
        basis=board_basis(t14_basis_state),
    )

    step = mgr.current_step(t15_state)
    assert step is not None
    assert step["land"] == "Mutavault"
    assert step["attack"] == ""

    planner = ActionPlanner(backend=None)
    planner.set_game_plan_source(mgr.strategy_block, step_source=mgr.current_step)
    planner.sync_turn_plan_from_game_plan(t15_state)
    assert planner.has_pending_attack_intent() is False

    pilot = _BridgeSubmitMixin()
    pilot._planner = planner
    done = GameAction(ActionType.CLICK_BUTTON, card_name="done", model_choice=True)
    assert pilot._explicit_no_attack(done) is True

    # Control: when the board has not changed since the plan was formed, attack intent stands.
    fresh_mgr = GamePlanManager(None)
    fresh_mgr._match_id = t15_state["match_id"]
    fresh_mgr._plan = GamePlan(
        turn_formed=15,
        role="aggressor",
        win_conditions=["Swing"],
        turn_plan=[{"turn": 15, "land": "Mutavault", "cast": [], "attack": "Titan of Industry", "hold": ""}],
        basis=board_basis(t15_state),
    )
    planner.set_game_plan_source(fresh_mgr.strategy_block, step_source=fresh_mgr.current_step)
    planner.sync_turn_plan_from_game_plan(t15_state)
    assert planner.has_pending_attack_intent() is True


def test_combat_choice_holds_titan_into_untapped_commander_aerid_but_attacks_when_tapped():
    untapped = _match5_t15_state(aerid_tapped=False)
    choice = combat_choice(untapped)
    assert choice is not None
    assert choice.assignments == {}

    # Control: if Aerid Konstrari is tapped, Titan attacks for 7.
    tapped = _match5_t15_state(aerid_tapped=True)
    ctrl_choice = combat_choice(tapped)
    assert ctrl_choice is not None
    assert 533 in ctrl_choice.assignments


def test_protection_instants_not_classified_as_proactive_lifegain_or_selection():
    safekeeping = {
        "instance_id": 101,
        "name": "Tamiyo's Safekeeping",
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "Target permanent you control gains hexproof and indestructible until end of turn. You gain 2 life.",
    }
    lorans_escape = {
        "instance_id": 102,
        "name": "Loran's Escape",
        "type_line": "Instant",
        "mana_cost": "{W}",
        "oracle_text": "Target artifact or creature gains hexproof and indestructible until end of turn. Scry 1.",
    }
    pure_lifegain = {
        "instance_id": 103,
        "name": "Life Goes On",
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "You gain 4 life. If a creature died this turn, you gain 8 life instead.",
    }
    pure_selection = {
        "instance_id": 104,
        "name": "Index",
        "type_line": "Instant",
        "mana_cost": "{U}",
        "oracle_text": "Scry 2.",
    }

    assert card_role(safekeeping) == "other"
    assert card_role(lorans_escape) == "other"
    assert card_role(pure_lifegain) == "lifegain"
    assert card_role(pure_selection) == "selection"

    # Reported Match 3 T2 board: Forest in play + Tamiyo's Safekeeping in hand vs 1/1 Ashnod.
    state = {
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "turn": {"turn_number": 2, "active_player": 2, "phase": "Phase_Main1", "step": "Step_None"},
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 25},
            {"seat_id": 2, "is_local": True, "life_total": 25, "lands_played": 1},
        ],
        "battlefield": [
            {
                "instance_id": 452,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "card_types": ["Land"],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "color_production": ["Green"],
            },
            {
                "instance_id": 447,
                "name": "Ashnod, Flesh Mechanist",
                "type_line": "Legendary Creature — Human Artificer",
                "card_types": ["Creature"],
                "power": 1,
                "toughness": 1,
                "keywords": ["deathtouch"],
                "controller_seat_id": 1,
                "owner_seat_id": 1,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
            },
        ],
        "hand": [{**copy.deepcopy(safekeeping), "controller_seat_id": 2, "owner_seat_id": 2}],
        "stack": [],
    }
    assessment = assess(state)
    assert assessment is not None
    block = assessment.prompt_block()
    assert "best Tamiyo's Safekeeping" not in block


def test_protection_plus_pump_stays_reactive_pump_and_protection_spell_value_is_zero():
    from arenamcp.board_assessment import _Spell, _spell_value
    from arenamcp.line_search_moves import unmodelled_effect

    snakeskin = {
        "instance_id": 105,
        "name": "Snakeskin Veil",
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "Put a +1/+1 counter on target creature you control. It gains hexproof until end of turn.",
    }
    blossoming = {
        "instance_id": 106,
        "name": "Blossoming Defense",
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "Target creature you control gets +2/+2 and gains hexproof until end of turn.",
    }
    safekeeping = {
        "instance_id": 101,
        "name": "Tamiyo's Safekeeping",
        "type_line": "Instant",
        "mana_cost": "{G}",
        "oracle_text": "Target permanent you control gains hexproof and indestructible until end of turn. You gain 2 life.",
    }
    lorans_escape = {
        "instance_id": 102,
        "name": "Loran's Escape",
        "type_line": "Instant",
        "mana_cost": "{W}",
        "oracle_text": "Target artifact or creature gains hexproof and indestructible until end of turn. Scry 1.",
    }
    assert card_role(snakeskin) == "pump"
    assert card_role(blossoming) == "pump"
    assert unmodelled_effect(snakeskin) == "a combat trick"
    assert (
        _spell_value(_Spell(snakeskin, snakeskin["name"], "pump", 1, (), False), survival=False, theirs=[])
        == 0.0
    )
    assert (
        _spell_value(_Spell(blossoming, blossoming["name"], "pump", 1, (), False), survival=False, theirs=[])
        == 0.0
    )
    assert (
        _spell_value(
            _Spell(safekeeping, safekeeping["name"], "other", 1, (), False), survival=False, theirs=[]
        )
        == 0.0
    )
    assert (
        _spell_value(
            _Spell(lorans_escape, lorans_escape["name"], "other", 1, (), False), survival=False, theirs=[]
        )
        == 0.0
    )

    # Main-1 assess with a creature on board must not recommend casting Blossoming Defense proactively.
    state = {
        "local_seat_id": 2,
        "opponent_seat_id": 1,
        "turn": {"turn_number": 2, "active_player": 2, "phase": "Phase_Main1", "step": "Step_None"},
        "players": [
            {"seat_id": 1, "is_local": False, "life_total": 20},
            {"seat_id": 2, "is_local": True, "life_total": 20, "lands_played": 1},
        ],
        "battlefield": [
            {
                "instance_id": 201,
                "name": "Forest",
                "type_line": "Basic Land — Forest",
                "card_types": ["Land"],
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "color_production": ["Green"],
            },
            {
                "instance_id": 202,
                "name": "Llanowar Elves",
                "type_line": "Creature — Elf Druid",
                "card_types": ["Creature"],
                "power": 1,
                "toughness": 1,
                "controller_seat_id": 2,
                "owner_seat_id": 2,
                "is_tapped": False,
                "turn_entered_battlefield": 1,
            },
        ],
        "hand": [{**blossoming, "controller_seat_id": 2, "owner_seat_id": 2}],
        "stack": [],
    }
    assessment = assess(state)
    assert assessment is not None
    block = assessment.prompt_block()
    assert "cast Blossoming Defense" not in block
    assert "best Blossoming Defense" not in block


def test_check_block_survival_blocks_face_lethal_even_when_second_attacker_targets_walker():
    state = _match5_t14_state(target_id=664)
    state["players"][1]["life_total"] = 6
    # Keep only one 1/1 blocker and add a second 6/6 attacking our face.
    state["battlefield"] = [c for c in state["battlefield"] if c["instance_id"] not in (442, 502)]
    face_attacker = {
        "instance_id": 999,
        "name": "Face Crusher",
        "type_line": "Creature — Beast",
        "card_types": ["Creature"],
        "power": 6,
        "toughness": 6,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": True,
        "is_attacking": True,
        "attack_target_id": 2,
    }
    state["battlefield"].append(face_attacker)
    state["decision_context"] = {
        "type": "declare_blockers",
        "legal_blocker_ids": [510],
        "legal_blockers": ["The Notary Hobbits"],
        "raw_blockers": [{"blockerInstanceId": 510, "attackerInstanceIds": [486, 999]}],
    }
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._check_block_survival(plan, state, state["decision_context"])
    assert plan.actions[0].action_type == ActionType.DECLARE_BLOCKERS
    assert plan.actions[0].blocker_instance_assignments == {510: 999}


def test_format_block_combat_fallback_respects_flying_and_ignores_player_hit_bonus_on_walker():
    # 1. Flying attacker aimed at Nissa with empty raw_blockers and only ground blockers:
    # must NOT recommend an illegal ground block.
    state = _match5_t14_state(target_id=664)
    for c in state["battlefield"]:
        if c["instance_id"] == 486:
            c["keywords"] = ["flying"]
    state["decision_context"] = {}
    coach = CoachEngine.__new__(CoachEngine)
    local = state["players"][1]
    yours = [c for c in state["battlefield"] if c["controller_seat_id"] == 2]
    opps = [c for c in state["battlefield"] if c["controller_seat_id"] == 1]
    joined = "\n".join(coach._format_block_combat(yours, opps, local, 14, "Phase_Combat", set(), {}, state))
    assert "Recommended planeswalker-saving blocks:" not in joined

    # 2. 3/3 with a player combat-damage drain trigger attacking 6-loyalty Nissa:
    # deals only 3 damage to Nissa (does not die, does not trigger walker-saving block).
    drain_state = _match5_t14_state(target_id=664)
    for c in drain_state["battlefield"]:
        if c["instance_id"] == 486:
            c["power"] = 3
            c["toughness"] = 3
            c["oracle_text"] = (
                "Whenever Studious First-Year deals combat damage to a player, that player loses 3 life."
            )
    d_yours = [c for c in drain_state["battlefield"] if c["controller_seat_id"] == 2]
    d_opps = [c for c in drain_state["battlefield"] if c["controller_seat_id"] == 1]
    d_joined = "\n".join(
        coach._format_block_combat(
            d_yours, d_opps, local, 14, "Phase_Combat", set(), drain_state["decision_context"], drain_state
        )
    )
    assert "faces 3 unblocked dmg → 3 loyalty remaining" in d_joined
    assert "DIES if unblocked" not in d_joined
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._ensure_walker_saving_blocks(plan, drain_state, drain_state["decision_context"])
    assert plan.actions[0].action_type == ActionType.CLICK_BUTTON


def test_walker_saving_blocks_respects_max_blockers_and_unknown_blocker_stats():
    # 1. max_blockers: 6/6 trample with "can't be blocked by more than one creature"
    # already blocked by a 1/1 against 5-loyalty Nissa must not get a second blocker.
    state = _match5_t14_state(target_id=664)
    for c in state["battlefield"]:
        if c["instance_id"] == 664:
            c["counters"] = {"Loyalty": 5}
        elif c["instance_id"] == 486:
            c["keywords"] = ["trample"]
            c["oracle_text"] = "Trample\nStudious First-Year can't be blocked by more than one creature."
    cards = {c["instance_id"]: c for c in state["battlefield"]}
    added = walker_saving_blocks(
        [cards[486]],
        [cards[442], cards[502], cards[510]],
        {510: 486},
        {442: {486}, 502: {486}, 510: {486}},
        cards,
        2,
        own_commanders={502},
    )
    assert added == {}

    # 2. Unknown-stat blocker (power=None, toughness=None) is never drafted as a chump.
    unk_state = _match5_t14_state(target_id=664)
    unk_state["battlefield"] = [
        c for c in unk_state["battlefield"] if c["instance_id"] not in (442, 502, 510)
    ]
    unk_blocker = {
        "instance_id": 777,
        "name": "Mystery Body",
        "type_line": "Creature — Shapeshifter",
        "card_types": ["Creature"],
        "power": None,
        "toughness": None,
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
    }
    unk_state["battlefield"].append(unk_blocker)
    unk_state["decision_context"] = {
        "type": "declare_blockers",
        "legal_blocker_ids": [777],
        "legal_blockers": ["Mystery Body"],
        "raw_blockers": [{"blockerInstanceId": 777, "attackerInstanceIds": [486]}],
    }
    planner = ActionPlanner(backend=None)
    plan = ActionPlan(actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done")])
    planner._ensure_walker_saving_blocks(plan, unk_state, unk_state["decision_context"])
    assert plan.actions[0].action_type == ActionType.CLICK_BUTTON


def _ghostly_prison_t10_state(*, forest_vigilance: bool = True, include_dorks: bool = True) -> dict:
    forest = {
        "instance_id": 444,
        "name": "Forest",
        "type_line": "Basic Land — Forest",
        "card_types": ["Creature", "Land"],
        "power": 3,
        "toughness": 3,
        "keywords": ["vigilance", "haste"] if forest_vigilance else ["haste"],
        "oracle_text": "({T}: Add {G}.)",
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 1,
    }
    nissa = {
        "instance_id": 799,
        "name": "Nissa, Who Shakes the World",
        "type_line": "Legendary Planeswalker — Nissa",
        "card_types": ["Planeswalker"],
        "oracle_text": (
            "Whenever you tap a Forest for mana, add an additional {oG}.\n"
            "[+1]: Put three +1/+1 counters on up to one target noncreature land you control."
        ),
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "counters": {"Loyalty": 5},
    }
    ghostly_prison = {
        "instance_id": 792,
        "name": "Ghostly Prison",
        "type_line": "Enchantment",
        "card_types": ["Enchantment"],
        "oracle_text": (
            "Creatures can't attack you unless their controller pays {o2} for each "
            "creature they control that's attacking you."
        ),
        "controller_seat_id": 2,
        "owner_seat_id": 2,
        "is_tapped": False,
    }
    battlefield = [forest, nissa, ghostly_prison]
    raw_attackers = [
        {
            "attackerInstanceId": 444,
            "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
        }
    ]
    if include_dorks:
        halfling = {
            "instance_id": 661,
            "name": "Delighted Halfling",
            "type_line": "Creature — Halfling Citizen",
            "card_types": ["Creature"],
            "power": 1,
            "toughness": 2,
            "oracle_text": (
                "{oT}: Add {oC}.\n"
                "{oT}: Add one mana of any color. Spend this mana only to cast a legendary spell, "
                "and that spell can't be countered."
            ),
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": False,
            "turn_entered_battlefield": 2,
        }
        spawn = {
            "instance_id": 465,
            "name": "Eldrazi Spawn",
            "type_line": "Token Creature — Eldrazi Spawn",
            "card_types": ["Creature"],
            "power": 0,
            "toughness": 1,
            "oracle_text": "Sacrifice this token: Add {oC}.",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": False,
            "summoning_sickness": True,
            "turn_entered_battlefield": 10,
        }
        battlefield.extend([halfling, spawn])
        raw_attackers.extend(
            [
                {
                    "attackerInstanceId": 661,
                    "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
                },
                {
                    "attackerInstanceId": 465,
                    "legalDamageRecipients": [{"type": "DamageRecType_Player", "playerSystemSeatId": 2}],
                },
            ]
        )
    return {
        "local_seat_id": 1,
        "opponent_seat_id": 2,
        "turn": {
            "turn_number": 10,
            "active_player": 1,
            "phase": "Phase_Combat",
            "step": "Step_DeclareAttack",
        },
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 25},
            {"seat_id": 2, "is_local": False, "life_total": 25},
        ],
        "battlefield": battlefield,
        "hand": [],
        "stack": [],
        "decision_context": {
            "type": "declare_attackers",
            "raw_attackers": raw_attackers,
        },
    }


def test_ghostly_prison_nissa_vigilant_forest_halfling_and_spawn_budget_and_controls():
    from arenamcp.attack_tax import AttackBudget
    from arenamcp.combat_keywords import is_creature_now

    # Reported case (bug_20261122_103713.json T10): animated Forest + Nissa + Halfling + Spawn.
    state = _ghostly_prison_t10_state(forest_vigilance=True, include_dorks=True)
    forest_card = state["battlefield"][0]
    assert is_creature_now(forest_card) is True
    assert (
        is_creature_now({"card_types": ["Enchantment"], "type_line": "Legendary Enchantment Creature — God"})
        is False
    )

    budget = AttackBudget(state)
    source_map = {iid: amt for iid, amt, _ in budget.sources}
    assert source_map == {444: 2, 661: 1, 465: 1}
    # Forest (vigilant) pays its own {2} Ghostly Prison tax under Nissa.
    pay_forest = budget.payment({444: {"playerSystemSeatId": 2}})
    assert pay_forest is not None and pay_forest.cost == 2
    # Forest + Halfling costs {4}, but non-vigilant Halfling cannot tap while attacking (only 2 + 1 = 3).
    assert budget.payment({444: {"playerSystemSeatId": 2}, 661: {"playerSystemSeatId": 2}}) is None
    # Solver attacks with the 3/3 Forest instead of holding everything.
    choice = combat_choice(state)
    assert choice is not None and 444 in choice.assignments

    # Control 1: without vigilance and without other mana, Forest cannot pay its own {2} tax.
    no_vig = _ghostly_prison_t10_state(forest_vigilance=False, include_dorks=False)
    assert AttackBudget(no_vig).payment({444: {"playerSystemSeatId": 2}}) is None
    no_vig_choice = combat_choice(no_vig)
    assert no_vig_choice is not None and no_vig_choice.assignments == {}

    # Control 2: Eldrazi Spawn (no-tap sacrifice) pays even while summoning-sick/tapped,
    # but never self-pays when Spawn itself is declared as an attacker.
    spawn_only = _ghostly_prison_t10_state(forest_vigilance=False, include_dorks=True)
    spawn_only["battlefield"] = [c for c in spawn_only["battlefield"] if c["instance_id"] in (465, 661, 792)]
    spawn_only["battlefield"][0]["is_tapped"] = True  # tapped, summoning-sick Eldrazi Spawn
    sb = AttackBudget(spawn_only)
    assert {iid: amt for iid, amt, _ in sb.sources} == {661: 1, 465: 1}
    # Spawn attacking alone cannot sacrifice itself to pay its own {2} tax (only Halfling's 1 available).
    assert sb.payment({465: {"playerSystemSeatId": 2}}) is None

    # Control 3: a single restricted-only mana ability still produces 0 supported attack-tax mana.
    restricted_only = _ghostly_prison_t10_state(forest_vigilance=False, include_dorks=False)
    restricted_only["battlefield"] = [
        restricted_only["battlefield"][2],  # Ghostly Prison
        {
            "instance_id": 901,
            "name": "Restricted Forest",
            "type_line": "Basic Land — Forest",
            "card_types": ["Land"],
            "oracle_text": "{T}: Add {G}{G}. Spend this mana only to cast creature spells.",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "is_tapped": False,
        },
    ]
    assert AttackBudget(restricted_only).sources == []


def test_ghostly_prison_stale_held_attackers_withdraws_and_redeclares_affordable_subset(monkeypatch):
    from unittest.mock import Mock

    import arenamcp.autopilot_bridge as bridge_module
    from arenamcp.autopilot import AutopilotEngine

    opp = {"type": "DamageRecType_Player", "playerSystemSeatId": 2}
    state = _ghostly_prison_t10_state(forest_vigilance=False, include_dorks=False)
    # Turn 12 setup: 1 untapped non-creature Forest + Nissa (= 2 mana -> 1 attacker),
    # plus Emrakul (12/12 flying trample) and World Breaker (3/3 reach), both held stale from a cancelled PayCosts.
    state["turn"]["turn_number"] = 12
    state["battlefield"][0].update(card_types=["Land"], keywords=[], power=None, toughness=None)
    emrakul = {
        "instance_id": 805,
        "name": "Emrakul, the Exigent Doom",
        "type_line": "Legendary Creature — Eldrazi",
        "card_types": ["Creature"],
        "power": 12,
        "toughness": 12,
        "keywords": ["flying", "trample"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 10,
    }
    world_breaker = {
        "instance_id": 811,
        "name": "World Breaker",
        "type_line": "Creature — Eldrazi",
        "card_types": ["Creature"],
        "power": 3,
        "toughness": 3,
        "keywords": ["reach"],
        "controller_seat_id": 1,
        "owner_seat_id": 1,
        "is_tapped": False,
        "turn_entered_battlefield": 10,
    }
    state["battlefield"].extend([emrakul, world_breaker])
    raw_held = [
        {"attackerInstanceId": 805, "legalDamageRecipients": [opp], "selectedDamageRecipient": opp},
        {"attackerInstanceId": 811, "legalDamageRecipients": [opp], "selectedDamageRecipient": opp},
    ]
    state["decision_context"]["raw_attackers"] = raw_held

    class StaleHeldBridge:
        def __init__(self):
            self.calls = []
            self._pending = {
                "has_pending": True,
                "request_class": "DeclareAttackerRequest",
                "bridge_runtime": "il2cpp-macos",
                "game_state_id": 339,
                "msg_id": 10,
                "can_submit": True,
                "attackers": list(raw_held),
            }

        def connect(self):
            return True

        def get_pending_actions(self):
            return dict(self._pending)

        def submit_attackers_raw(self, entries, **kwargs):
            self.calls.append({"entries": list(entries), **kwargs})
            if kwargs.get("withdraw"):
                self._pending = {
                    **self._pending,
                    "msg_id": 11,
                    "attackers": [{**a, "selectedDamageRecipient": None} for a in self._pending["attackers"]],
                }
                return {"ok": True, "submitted_type": "DeclareAttackersWithdraw", "needs_finalize": True}
            if kwargs.get("finalize_only"):
                return {"ok": True, "submitted_type": "DeclareAttackersSubmit"}
            selected_map = {e["attackerInstanceId"]: e["damageRecipient"] for e in entries}
            self._pending = {
                **self._pending,
                "msg_id": 12,
                "attackers": [
                    {**a, "selectedDamageRecipient": selected_map.get(a["attackerInstanceId"])}
                    for a in self._pending["attackers"]
                ],
            }
            return {"ok": True, "submitted_type": "DeclareAttackersUpdate", "needs_finalize": True}

    bridge = StaleHeldBridge()
    monkeypatch.setattr(bridge_module, "_progress_bridge_for", lambda *args: bridge)
    engine = object.__new__(AutopilotEngine)
    engine._get_game_state = lambda: state
    engine._all_in_attack_action = lambda *args: None
    engine._attack_override = lambda *args: None
    engine._explicit_no_attack = lambda *args: False
    engine._log_execution_path = lambda *args, **kwargs: None
    engine._attack_entries = lambda action, names, gs, pend: [
        {"attackerInstanceId": 805, "damageRecipient": opp},
        {"attackerInstanceId": 811, "damageRecipient": opp},
    ]
    engine._withdraw_taxed_attack = Mock(side_effect=AssertionError("should re-declare affordable subset"))

    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS,
        attacker_names=["Emrakul, the Exigent Doom", "World Breaker"],
        line_push=True,
    )
    result = engine._try_bridge_declare_attackers_inner(action)
    assert result is not None and result.success
    # 1. Withdraw stale 2-attacker selection -> 2. Update with affordable Emrakul -> 3. Finalize Emrakul.
    assert len(bridge.calls) == 3
    assert bridge.calls[0] == {"entries": [], "withdraw": True}
    assert bridge.calls[1]["entries"] == [{"attackerInstanceId": 805, "damageRecipient": opp}]
    assert bridge.calls[2]["finalize_only"] is True
    assert bridge.calls[2]["expected_request_id"] == (339, 12)
    assert bridge.calls[2]["entries"] == [{"attackerInstanceId": 805, "damageRecipient": opp}]

    # Control: when 0 mana is available (empty affordable subset), withdraw and finalize 0 attackers.
    state_no_mana = copy.deepcopy(state)
    state_no_mana["battlefield"][0]["is_tapped"] = True
    bridge2 = StaleHeldBridge()
    monkeypatch.setattr(bridge_module, "_progress_bridge_for", lambda *args: bridge2)
    engine._get_game_state = lambda: state_no_mana
    engine._withdraw_taxed_attack = Mock(return_value="withdrawn")
    assert engine._try_bridge_declare_attackers_inner(action) == "withdrawn"
    engine._withdraw_taxed_attack.assert_called_once_with(bridge2, state_no_mana)
    assert bridge2.calls == []
