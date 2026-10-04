"""Recovery is a priced continuation of the entire combat, not a token override."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from arenamcp.combat_recovery import (
    CombatRecovery,
    _payable,
    entry_resources,
    improve_block_recovery,
    mana_options,
)
from arenamcp.combat_solver import blocker_allowed_attackers_map, optimal_blocks


@pytest.fixture
def report():
    state = json.loads((Path(__file__).parent / "fixtures/commander_block_20261004_103338.json").read_text())
    # Printed facts normally come from the analyzed deck's Oracle catalog.
    state["deck_catalog"] = {103511: {"power": 1, "toughness": 1}}
    return state


def solve(state, recovery=True):
    ctx = state["decision_context"]
    forecast = CombatRecovery(state)
    return optimal_blocks(
        [c for c in state["battlefield"] if c.get("is_attacking")],
        [c for c in state["battlefield"] if c["instance_id"] in ctx["legal_blocker_ids"]],
        next(p["life_total"] for p in state["players"] if p["is_local"]),
        blocker_allowed_attackers=blocker_allowed_attackers_map(ctx["raw_blockers"]),
        recovery_credit=forecast.credit if recovery else None,
    )


def test_recorded_board_prices_commander_replay_and_preserves_both_old_tokens(report):
    forecast = CombatRecovery(report)
    result = forecast.forecasts(frozenset({1034, 1057}))[0]
    assert result.payable
    assert {1042, 1043} <= result.surviving_ids
    assert not {1034, 1057} & result.surviving_ids
    assert "mana after untap = 12" in result.explanation
    assert "tax = 9" in result.explanation
    assert "2 survivors + 1 original + 2 NEW tokens = 5 creatures" in result.explanation
    assert "once all are ready = 33" in result.explanation  # 25C + eight lands
    plan = solve(report)
    assert plan.assignments == {1034: 1046, 1057: 849}
    assert plan.damage_through == 0
    assert plan.blockers_lost_ids == {1034, 1057}
    assert plan.recovery_credit > 0


def test_all_combat_deaths_are_removed_before_counting_recast_mana(report):
    forecast = CombatRecovery(report)
    # Losing the other producers too leaves eight lands, short of nine.
    result = forecast.forecasts(frozenset({1034, 1042, 1043, 1057}))[0]
    assert not result.payable
    assert result.credit == 0
    assert "mana after untap = 8" in result.explanation


@pytest.mark.parametrize("casts", [None, 8, True, -1])
def test_unknown_or_unaffordable_tax_never_earns_recovery_credit(report, casts):
    report["commander_casts"] = {"103511": casts}
    result = CombatRecovery(report).forecasts(frozenset({1034}))[0]
    assert not result.payable and result.credit == 0
    assert 1034 not in solve(report).assignments


def test_recovery_is_not_credited_when_player_dies(report):
    report["players"][0]["life_total"] = 1
    # Forbid blocking Atraxa. A later cast cannot recover from lethal now.
    for entry in report["decision_context"]["raw_blockers"]:
        entry["attackerInstanceIds"] = [1046]
    plan = solve(report)
    assert plan.damage_through >= 1
    assert plan.recovery_credit == 0


def test_color_requirements_survive_large_colorless_mana_output(report):
    for card in report["battlefield"]:
        if card.get("controller_seat_id") == 2 and "Land" in card["type_line"]:
            card["oracle_text"] = "{T}: Add {C}."
    result = CombatRecovery(report).forecasts(frozenset({1034}))[0]
    assert not result.payable and result.credit == 0


@pytest.mark.parametrize("change", ["counter", "attached", "copy", "stolen", "token", "not_commander"])
def test_investments_and_identity_prevent_speculative_recovery_credit(report, change):
    card = next(c for c in report["battlefield"] if c["instance_id"] == 1034)
    if change == "counter":
        card["counters"] = {"+1/+1": 10}
    elif change == "attached":
        card["attachments"] = [1111]
    elif change == "copy":
        card["is_copy"] = True
    elif change == "stolen":
        card["owner_seat_id"] = 1
    elif change == "token":
        card["object_kind"] = "TOKEN"
    else:
        report["players"][0]["commander_ids"] = []
    assert CombatRecovery(report).credit(frozenset({1034})) == 0


@pytest.mark.parametrize(
    "oracle",
    [
        "Creatures entering the battlefield don't cause abilities to trigger.",
        "Creature spells cost {2} more to cast.",
        "Creatures your opponents control lose all abilities.",
        "If a creature would die, exile it instead.",
        "Whenever a creature dies, its controller loses 5 life.",
    ],
)
def test_visible_interference_disables_numerical_credit(report, oracle):
    report["battlefield"].append({"instance_id": 2000, "controller_seat_id": 1, "oracle_text": oracle})
    assert CombatRecovery(report).credit(frozenset({1034})) == 0


def test_search_does_not_reuse_cached_scores_after_tax_changes(report):
    baseline = solve(report, recovery=False)
    assert solve(report).assignments.get(1034) == 1046
    expensive = deepcopy(report)
    expensive["commander_casts"]["103511"] = 12
    assert 1034 not in solve(expensive).assignments
    assert solve(report, recovery=False) == baseline


def test_recovery_is_derived_from_rules_for_other_commanders(report):
    for card in report["battlefield"]:
        if card.get("grp_id") == 103511:
            card.update(
                name="Visiting Captain",
                mana_cost="{1}{G}",
                oracle_text=(
                    "When Visiting Captain enters the battlefield, create three 1/1 white Soldier creature tokens."
                ),
            )
    forecast = CombatRecovery(report).forecasts(frozenset({1034, 1057}))[0]
    assert forecast.payable and forecast.credit > 0
    assert "2 survivors + 1 original + 3 NEW tokens = 6 creatures" in forecast.explanation
    assert solve(report).assignments.get(1034) == 1046


def test_ongoing_commander_engine_without_replay_payoff_gets_no_credit(report):
    card = next(c for c in report["battlefield"] if c["instance_id"] == 1034)
    card["oracle_text"] = "At the beginning of your upkeep, draw two cards.\n{T}: Add {G}{G}{G}."
    assert CombatRecovery(report).forecasts(frozenset({1034})) == ()
    assert 1034 not in solve(report).assignments


@pytest.mark.parametrize(
    "text",
    [
        "Whenever another creature enters, draw two cards.",
        "When Scholar enters, if you control an artifact, draw two cards.",
        "When you cast Scholar, draw two cards.",
        "When Scholar enters, create two tokens that are copies of it.",
        "{3}: Create two 1/1 white Soldier creature tokens.",
    ],
)
def test_unknown_conditions_cast_triggers_and_recursive_copies_are_not_etb_payoffs(text):
    assert not entry_resources({"name": "Scholar", "oracle_text": text}).evidence


def test_draw_entry_is_a_replay_resource_but_not_a_token():
    effect = entry_resources({"name": "Scholar", "oracle_text": "When Scholar enters, draw two cards."})
    assert effect.cards == 2 and effect.copies == 0 and not effect.tokens


def test_scaling_counts_only_actual_producers_and_the_current_survivors():
    elf = {"type_line": "Creature — Elf Druid", "oracle_text": "{oT}: Add {oG} for each Elf you control."}
    soldier = {"type_line": "Creature — Soldier", "oracle_text": ""}
    ordinary_elf = {"type_line": "Creature — Elf Warrior", "oracle_text": ""}
    board = [elf, soldier, ordinary_elf]
    assert mana_options(elf, board) == ((0, 0, 0, 0, 2, 0),)
    assert mana_options(ordinary_elf, board) == ()


def test_multimana_color_choice_and_expensive_activations():
    dual = mana_options({"oracle_text": "{T}: Add {G} or {U}."}, [])
    assert not _payable([dual], (2, (0, 1, 0, 0, 1, 0)))
    assert _payable([dual, dual], (2, (0, 1, 0, 0, 1, 0)))
    rock = mana_options({"oracle_text": "{T}: Add {C}{C}."}, [])
    assert _payable([rock], (2, (0, 0, 0, 0, 0, 0)))
    assert not mana_options({"oracle_text": "{2}, {T}: Add {G}{G}{G}."}, [])
    assert not mana_options({"oracle_text": "{T}: Add {G}. Spend this mana only to activate abilities."}, [])
    assert not mana_options(
        {"oracle_text": "{T}: Add {G} or {U}. Spend this mana only to activate abilities."}, []
    )
    assert not mana_options({"oracle_text": "{T}: Add {G}.\nActivate only if you control a Dragon."}, [])


def test_missing_untap_and_stun_counter_are_not_next_turn_sources():
    assert not mana_options(
        {"oracle_text": "{T}: Add {G}.\nThis creature doesn't untap during your untap step."}, []
    )
    assert not mana_options({"oracle_text": "{T}: Add {G}.", "counters": {"Stun": 1}}, [])


def test_entry_forecast_does_not_drop_an_unmodeled_downside():
    assert not entry_resources(
        {
            "name": "Captain",
            "oracle_text": (
                "When Captain enters, create two 1/1 white Soldier creature tokens.\n"
                "When Captain enters, sacrifice three creatures."
            ),
        }
    ).evidence


def test_current_pumped_stats_are_not_inherited_by_recast_or_new_copies(report):
    commander = next(c for c in report["battlefield"] if c["instance_id"] == 1034)
    baseline = CombatRecovery(report).credit(frozenset({1034, 1057}))
    commander["power"] = commander["toughness"] = 20
    assert CombatRecovery(report).credit(frozenset({1034, 1057})) == baseline
    report.pop("deck_catalog")
    assert CombatRecovery(report).credit(frozenset({1034, 1057})) == 0


def test_attachment_from_a_separate_battlefield_object_preserves_investment(report):
    report["battlefield"].append(
        {"instance_id": 2000, "type_line": "Enchantment — Aura", "parent_instance_id": 1034}
    )
    assert CombatRecovery(report).credit(frozenset({1034, 1057})) == 0


def test_planner_checks_the_trade_and_updates_identity_narration_and_training_origin(report, monkeypatch):
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner, plan_fallback_reason

    monkeypatch.setattr("arenamcp.match_context._local_card", lambda *args: {})
    backend = Mock()
    backend.complete.return_value = json.dumps(
        {
            "actions": [
                {
                    "action_type": "declare_blockers",
                    "blocker_assignments": {
                        "*The Notary Hobbits [id:1042]": "*Samurai [id:1046]",
                        "*Bird [id:1057]": "Atraxa, Praetors' Voice [id:849]",
                    },
                    "reasoning": "Preserve the commander and sacrifice a token.",
                }
            ],
            "voice_advice": "Block with the token.",
            "overall_strategy": "Preserve the original.",
        }
    )
    planner = ActionPlanner(backend)
    plan = planner.plan_actions(
        report, "combat_blockers", report["legal_actions"], report["decision_context"]
    )
    assert plan.actions[0].blocker_instance_assignments == {1034: 1046, 1057: 849}
    assert "The Notary Hobbits [id:1034]" in plan.actions[0].blocker_assignments
    assert "[id:1042]" not in plan.voice_advice
    assert "[id:1034]" in plan.voice_advice
    assert "recast" in plan.voice_advice and "I'm" not in plan.voice_advice
    assert "tax = 9" in plan.actions[0].reasoning
    assert plan_fallback_reason(plan) == "planner_combat_recovery"
    backend.complete.assert_called_once()
    assert planner._committed_commander_return(report, {"recipient_ids": [1034]})
    assert not planner._committed_commander_return(report, {"recipient_ids": [1042]})


@pytest.mark.parametrize(
    "change",
    [
        "hand",
        "unknown_hand",
        "tax",
        "counter",
        "illegal",
        "other_damage",
        "missing_stats",
        "missing_legality",
        "not_dying",
        "stack",
    ],
)
def test_trade_check_keeps_planner_choice_when_assumptions_fail(report, change):
    commander = next(c for c in report["battlefield"] if c["instance_id"] == 1034)
    if change == "hand":
        report["hand"] = [{"name": "Another plan", "mana_cost": "{5}{G}{G}"}]
    elif change == "unknown_hand":
        report.pop("hand")
    elif change == "tax":
        report["commander_casts"] = {}
    elif change == "counter":
        commander["counters"] = {"+1/+1": 1}
    elif change == "illegal":
        report["decision_context"]["raw_blockers"][0]["attackerInstanceIds"] = []
    elif change == "other_damage":
        next(c for c in report["battlefield"] if c["instance_id"] == 1046)["oracle_text"] += "\nTrample"
        commander["toughness"] = 0
    elif change == "missing_stats":
        commander["toughness"] = None
    elif change == "missing_legality":
        report["decision_context"]["raw_blockers"] = []
    elif change == "not_dying":
        commander["oracle_text"] += "\nIndestructible"
    else:
        report["stack"] = [{"name": "Removal"}]
    assert improve_block_recovery(report, report["decision_context"], {1042: 1046, 1057: 849}) is None


def test_trade_check_supports_regular_creature_entry_payoffs_and_nontoken_substitutes(report):
    for c in report["battlefield"]:
        if c.get("grp_id") == 103511:
            c.update(
                name="Visiting Captain",
                mana_cost="{1}{G}",
                oracle_text="When Visiting Captain enters, create three 1/1 white Soldier creature tokens.",
            )
        if c.get("instance_id") == 1042:
            c.update(
                name="Ordinary Soldier", grp_id=12345, object_kind="CARD", is_token=False, oracle_text=""
            )
    improved = improve_block_recovery(report, report["decision_context"], {1042: 1046, 1057: 849})
    assert improved is not None and improved[0] == {1034: 1046, 1057: 849}


@pytest.mark.parametrize("changed", ["match", "turn", "recipient", "ownership"])
def test_recovery_commitment_does_not_leak_to_other_games_or_creatures(report, changed):
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner(Mock())
    planner._planned_recovery = (report["match_id"], 17, 103511, 1034)
    context = {"recipient_ids": [2000]}
    report["graveyard"] = [{"instance_id": 2000, "grp_id": 103511, "owner_seat_id": 2}]
    assert planner._committed_commander_return(report, context)  # new zone instance
    if changed == "match":
        report["match_id"] = "other"
    elif changed == "turn":
        report["turn"]["turn_number"] += 1
    elif changed == "recipient":
        context["recipient_ids"] = [2001]
    else:
        report["graveyard"][0]["owner_seat_id"] = 1
    assert not planner._committed_commander_return(report, context)


def test_zone_choice_follows_priced_recovery_even_with_a_general_graveyard_policy(report):
    from unittest.mock import Mock

    from arenamcp.action_planner import ActionPlanner
    from arenamcp.decisions import build_pending_decision

    book = Mock()
    book.data = {"decision_rules": [{"decisions": ["commander_zone"]}]}
    backend = Mock()
    planner = ActionPlanner(backend, deck_playbook_fn=lambda: book)
    planner._planned_recovery = (report["match_id"], 17, 103511, 1034)
    report["decision_context"] = {
        "type": "optional_action",
        "commander_return": True,
        "recipient_ids": [1034],
        "recipient_names": ["The Notary Hobbits"],
        "raw": {"gameStateId": 300, "msgId": 400},
    }
    poll = {
        "has_pending": True,
        "request_type": "OptionalAction",
        "request_class": "OptionalActionMessageRequest",
        "game_state_id": 300,
        "msg_id": 400,
        "optional_recipients": [1034],
        "optional_mechanics": ["ZoneTransfer"],
    }
    decision = build_pending_decision(poll)
    assert planner.plan_decision_options(decision, report) == ["optional:accept"]
    backend.complete.assert_not_called()
