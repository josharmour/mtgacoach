"""#210 recurrence: taxes must constrain projections and declarations before payment fails."""

import copy
import json
from pathlib import Path

import pytest

from arenamcp.attack_tax import AttackBudget, affordable_entries, taxes
from arenamcp.combat_solver import optimal_attacks
from arenamcp.combat_strategy import combat_choice

PRISON = "Creatures can't attack you unless their controller pays {o2} for each creature they control that's attacking you."
DAIN = "As long as you have an enduring story, creatures can't attack you unless their controller pays {1} for each of those creatures."
ARCHANGEL = "As long as Archangel of Tithes is untapped, creatures can't attack you or planeswalkers you control unless their controller pays {1} for each of those creatures."


def creature(i, power=4, **kw):
    return dict(
        instance_id=i,
        name=f"C{i}",
        type_line="Creature",
        power=power,
        toughness=4,
        owner_seat_id=1,
        controller_seat_id=1,
        turn_entered_battlefield=1,
        **kw,
    )


def land(i, **kw):
    return dict(
        instance_id=i,
        name=f"L{i}",
        type_line="Basic Land — Forest",
        oracle_text="{T}: Add {G}.",
        owner_seat_id=1,
        controller_seat_id=1,
        **kw,
    )


def state(cost=PRISON, mana=2):
    cards = [
        creature(10),
        creature(11, 6),
        {
            "instance_id": 90,
            "name": "Prison",
            "type_line": "Enchantment",
            "owner_seat_id": 2,
            "controller_seat_id": 2,
            "oracle_text": cost,
        },
        *[land(i) for i in range(100, 100 + mana)],
    ]
    return {
        "turn": {"turn_number": 5, "active_player": 1, "phase": "Combat", "step": "DeclareAttack"},
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "battlefield": cards,
        "hand": [],
        "stack": [],
        "decision_context": {
            "type": "declare_attackers",
            "legal_attacker_ids": [10, 11],
            "raw_attackers": [
                {"attackerInstanceId": i, "legalDamageRecipients": [{"playerSystemSeatId": 2}]}
                for i in [10, 11]
            ],
        },
    }


def entries(s):
    return [
        {"attackerInstanceId": e["attackerInstanceId"], "damageRecipient": e["legalDamageRecipients"][0]}
        for e in s["decision_context"]["raw_attackers"]
    ]


def fixture():
    return json.loads((Path(__file__).parent / "fixtures/attack_tax_210_finale_public.json").read_text())


def test_observed_finale_selection_unpayable_but_public_subset_retained():
    s = fixture()
    before = copy.deepcopy(s)
    b = AttackBudget(s)
    proposed = entries(s)
    assert b.cost({e["attackerInstanceId"]: e["damageRecipient"] for e in proposed}) == 4
    assert b.payment({e["attackerInstanceId"]: e["damageRecipient"] for e in proposed}) is None
    # Actual payment offered only Field of Ruin after all four were selected.
    assert [i for i, n, c in b.sources if i not in {453, 467, 500, 506}] == [457]
    selected = affordable_entries(s, proposed)
    assert 0 < len(selected) < 4
    assert b.payment({e["attackerInstanceId"]: e["damageRecipient"] for e in selected}) is not None
    assert s == before


def test_supported_creature_mana_bonus_requires_holding_that_creature_back():
    s = fixture()
    b = AttackBudget(s)
    assert (506, 2, True) in b.sources  # Mutavault plus visible Badgermole Cub trigger
    assert b.payment({i: {"playerSystemSeatId": 1} for i in (453, 467, 500)}) is not None
    assert b.payment({i: {"playerSystemSeatId": 1} for i in (453, 467, 500, 506)}) is None


def test_valid_intended_subset_is_not_replaced():
    s = state()
    e = entries(s)[:1]
    assert affordable_entries(s, e) is e


def test_solver_selects_strongest_payable_attack_and_recomputes_mana_changes():
    s = state()
    assert set(combat_choice(s).assignments) == {11}
    s["battlefield"] += [land(102), land(103)]
    assert set(combat_choice(s).assignments) == {10, 11}
    for c in s["battlefield"]:
        if c["instance_id"] >= 100:
            c["is_tapped"] = True
    assert combat_choice(s).assignments == {}


def test_player_only_solver_filters_unpayable_subsets():
    s = state()
    ours = s["battlefield"][:2]
    p = optimal_attacks(ours, [], 20, 20, [], [], attack_budget=AttackBudget(s))
    assert set(p.attacker_ids) == {11}


def test_tax_sources_stack_and_tapped_unconditional_source_still_taxes():
    s = state()
    s["battlefield"][2]["is_tapped"] = True
    s["battlefield"].append({**s["battlefield"][2], "instance_id": 91, "name": "Second tax"})
    assert AttackBudget(s).cost({10: {"playerSystemSeatId": 2}}) == 4
    assert affordable_entries(s, entries(s)) == []


@pytest.mark.parametrize("active,expected", [(True, 1), (False, 0), (None, None)])
def test_enduring_story_condition_is_public_not_assumed(active, expected):
    s = state(DAIN)
    s["players"][1]["has_enduring_story"] = active
    assert AttackBudget(s).cost({10: {"playerSystemSeatId": 2}}) == expected


def test_tapped_archangel_does_not_tax_but_untapped_one_covers_walkers():
    s = state(ARCHANGEL)
    s["battlefield"][2]["name"] = "Archangel of Tithes"
    s["battlefield"].append({"instance_id": 99, "type_line": "Planeswalker", "controller_seat_id": 2})
    assert AttackBudget(s).cost({10: {"planeswalkerInstanceId": 99}}) == 1
    s["battlefield"][2]["is_tapped"] = True
    assert not taxes(s)


def test_prison_does_not_tax_planeswalker_or_battle_attacks():
    s = state(mana=0)
    assert (
        AttackBudget(s).payment({10: {"planeswalkerInstanceId": 99}, 11: {"battleInstanceId": 98}})
        is not None
    )


def test_control_change_and_phasing_change_tax():
    s = state()
    s["battlefield"][2]["controller_seat_id"] = 1
    assert AttackBudget(s).cost({10: {"playerSystemSeatId": 2}}) == 0
    s["battlefield"][2]["is_phased_out"] = True
    assert not taxes(s)


@pytest.mark.parametrize(
    "cost",
    [
        PRISON.replace("{o2}", "{X}"),
        PRISON.replace("{o2}", "{W}"),
        "If you control an artifact, " + PRISON.lower(),
    ],
)
def test_unknown_cost_or_condition_does_not_become_free_attack(cost):
    s = state(cost, mana=10)
    assert AttackBudget(s).payment({10: {"playerSystemSeatId": 2}}) is None
    assert affordable_entries(s, entries(s)) == []


def test_main_phase_floating_mana_is_not_reserved_through_combat():
    s = state(mana=0)
    s["turn"]["phase"] = "Main1"
    s["turn"]["step"] = "None"
    s["players"][0]["mana_pool"] = {"G": 10}
    assert affordable_entries(s, entries(s)) == []


def test_restricted_sick_tapped_and_attacking_sources_do_not_pay_twice():
    s = state(mana=0)
    s["battlefield"] += [
        dict(creature(20, oracle_text="{T}: Add {G}{G}."), turn_entered_battlefield=5),
        land(100, is_tapped=True),
        dict(land(101), oracle_text="{T}: Add {G}{G}. Spend this mana only to cast creature spells."),
    ]
    assert not AttackBudget(s).sources
    s["battlefield"][1]["oracle_text"] = "{T}: Add {G}{G}."
    b = AttackBudget(s)
    assert b.payment({10: {"playerSystemSeatId": 2}}).tapped_creatures == frozenset({11})
    assert b.payment({10: {"playerSystemSeatId": 2}, 11: {"playerSystemSeatId": 2}}) is None


def test_must_attack_does_not_require_payment_but_preserves_free_recipient():
    s = state(mana=0)
    s["decision_context"]["raw_attackers"][0]["mustAttack"] = True
    assert combat_choice(s).assignments == {}
    s["battlefield"].append(
        {
            "instance_id": 99,
            "name": "Walker",
            "type_line": "Planeswalker",
            "controller_seat_id": 2,
            "counters": {"Loyalty": 3},
        }
    )
    s["decision_context"]["raw_attackers"][0]["legalDamageRecipients"].append({"planeswalkerInstanceId": 99})
    assert combat_choice(s).assignments[10] == {"planeswalkerInstanceId": 99}


def test_storied_annotation_survives_player_updates_and_checkpoint():
    from arenamcp.gamestate import GameState

    s = GameState()
    s._update_player({"systemSeatNumber": 2, "lifeTotal": 20})
    ann = {
        "type": ["AnnotationType_AbilityWordActive"],
        "affectorId": 2,
        "affectedIds": [494],
        "details": [
            {"key": "AbilityWordName", "valueString": ["Storied"]},
            {"key": "Enduring Story", "valueInt32": [1]},
        ],
    }
    s._process_annotations([ann])
    s._update_player({"systemSeatNumber": 2, "lifeTotal": 19})
    assert s.players[2].to_dict()["has_enduring_story"] is True
    restored = GameState()
    assert restored.restore_checkpoint(s.export_checkpoint())
    assert restored.players[2].has_enduring_story is True


def test_bridge_submission_thins_even_line_push_before_any_game_command(monkeypatch):
    from arenamcp.action_planner import ActionType, GameAction
    from arenamcp.autopilot import AutopilotEngine

    s = state()
    e = entries(s)
    engine = object.__new__(AutopilotEngine)
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["C10", "C11"], line_push=True
    )
    submitted, chosen = engine._prepare_attack_submission(
        action, action.attacker_names, e, s, {"attackers": s["decision_context"]["raw_attackers"]}
    )
    assert submitted.attacker_instance_ids == [11]
    assert chosen == e[1:]
    assert "payable subset" in submitted.reasoning


def test_multi_turn_search_and_disabled_fallback_never_claim_free_lethal(monkeypatch):
    from arenamcp import board_assessment as ba
    from arenamcp.board_model import build_board_model
    from arenamcp.line_search import search_lines

    s = state(mana=0)
    s["players"][1]["life_total"] = 1
    result = search_lines(build_board_model(s), survival=False, lethal_now=True)
    assert result.unsupported and "attack-tax" in result.prompt_line()
    assert not result.first_action and not result.to_projections()
    monkeypatch.setattr(ba, "_run_line_search", lambda *a, **k: None)
    assessment = ba._assess(s)
    assert not assessment.lethal_now and not assessment.lethal_next_turn
    assert assessment.dead_in is None and not assessment.lookahead


def test_tax_activation_invalidates_assessment_cache():
    from arenamcp.board_assessment import _signature

    s = state(DAIN)
    before = _signature(s)
    s["players"][1]["has_enduring_story"] = True
    assert before != _signature(s)


def test_tapped_conditional_tax_is_not_assumed_inactive_on_all_future_turns():
    from arenamcp.board_model import build_board_model

    s = state(ARCHANGEL)
    s["battlefield"][2].update(name="Archangel of Tithes", is_tapped=True)
    assert not taxes(s)
    assert build_board_model(s).attack_tax_notes


def test_tactical_main_phase_branch_does_not_publish_unpayable_lethal():
    from arenamcp.mcts_evaluator import MCTSEvaluator

    s = state(mana=0)
    s["turn"].update(phase="Phase_Main1", step="")
    s["players"][1]["life_total"] = 1
    tree = MCTSEvaluator.evaluate(s, force=True)
    for branch in tree.branches:
        if branch.action_type == "attack":
            assert branch.details.get("damage_through", 0) == 0
            assert "Hold Blockers" in branch.action


def test_tax_thinning_does_not_finalize_stale_selected_attackers(monkeypatch):
    from unittest.mock import Mock

    import arenamcp.autopilot_bridge as bridge_module
    from arenamcp.action_planner import ActionType, GameAction
    from arenamcp.autopilot import AutopilotEngine

    s = state(mana=0)
    e = entries(s)
    raw = [
        {**c, "selectedDamageRecipient": c["legalDamageRecipients"][0]}
        for c in s["decision_context"]["raw_attackers"]
    ]
    bridge = Mock()
    bridge.connect.return_value = True
    bridge.get_pending_actions.return_value = {
        "has_pending": True,
        "request_class": "DeclareAttackerRequest",
        "attackers": raw,
    }
    monkeypatch.setattr(bridge_module, "_progress_bridge_for", lambda *args: bridge)
    engine = object.__new__(AutopilotEngine)
    engine._get_game_state = lambda: s
    engine._all_in_attack_action = lambda *args: None
    engine._attack_override = lambda *args: None
    engine._attack_entries = lambda *args: e
    engine._withdraw_taxed_attack = Mock(return_value="withdrawn")
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=["C10", "C11"], line_push=True
    )
    assert engine._try_bridge_declare_attackers_inner(action) == "withdrawn"
    engine._withdraw_taxed_attack.assert_called_once_with(bridge, s)
    bridge.submit_attackers_raw.assert_not_called()


def test_coach_does_not_label_raw_unpayable_power_lethal():
    from arenamcp.coach import CoachEngine

    s = state(mana=0)
    s["players"][1]["life_total"] = 1
    coach = object.__new__(CoachEngine)
    ours, tax = s["battlefield"][:2], s["battlefield"][2:]
    lines = coach._format_attack_combat(ours, tax, s["players"][0], s["players"][1], 5, ours, s)
    prompt = "\n".join(lines)
    assert "vs LETHAL" not in prompt
    assert "0 damage to player" in prompt


def test_submission_keeps_mandatory_free_walker_attack_when_paid_player_attack_fails():
    s = state(mana=0)
    s["battlefield"].append(
        {
            "instance_id": 99,
            "name": "Walker",
            "type_line": "Planeswalker",
            "controller_seat_id": 2,
            "counters": {"Loyalty": 3},
        }
    )
    raw = s["decision_context"]["raw_attackers"][0]
    raw["mustAttack"] = True
    raw["legalDamageRecipients"].append({"planeswalkerInstanceId": 99})
    chosen = affordable_entries(s, entries(s))
    assert chosen == [{"attackerInstanceId": 10, "damageRecipient": {"planeswalkerInstanceId": 99}}]


def test_unknown_global_attack_cost_does_not_allow_a_free_walker_bypass():
    s = state("Creatures can't attack unless their controller pays {X} for each of those creatures.", mana=10)
    assert AttackBudget(s).payment({10: {"planeswalkerInstanceId": 99}}) is None
