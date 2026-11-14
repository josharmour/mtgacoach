from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, SecondLookError
from arenamcp.decisions import DecisionOption, PendingDecision

REASON = "pings Griselbrand down to 3/7 and leaves a double-striking 3/3 body that blocks the flyer"


def fixture():
    fury = dict(
        instance_id=1,
        name="Fury",
        type_line="Creature — Elemental Incarnation",
        card_types=["CardType_Creature"],
        power=3,
        toughness=3,
        mana_cost="{3}{R}{R}",
        oracle_text="Double strike\nWhen this creature enters, it deals 4 damage divided as you choose among any number of target creatures and/or planeswalkers.",
        controller_seat_id=1,
        owner_seat_id=1,
    )
    grisel = dict(
        instance_id=2,
        name="Griselbrand",
        type_line="Legendary Creature — Demon",
        card_types=["CardType_Creature"],
        power=7,
        toughness=7,
        keywords=["Flying", "Lifelink"],
        oracle_text="Flying, lifelink",
        controller_seat_id=2,
        owner_seat_id=2,
    )
    state = dict(
        local_seat_id=1,
        hand=[fury],
        battlefield=[grisel],
        turn=dict(active_player=1, turn_number=5, phase="Phase_Main1"),
    )
    menu = PendingDecision(
        request_id=(1, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "cast", "Cast Fury", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": 1}
            ),
            DecisionOption("pass", "Pass", payable=True, meta={"actionType": "ActionType_Pass"}),
        ),
    )
    return state, menu


@pytest.mark.parametrize("high", [False, True])
def test_exact_fury_claim_is_reconsidered_before_selection(high):
    state, menu = fixture()
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}

    def concerns(ids, data):
        return planner._unevaluated_claim_concerns(menu, state, ids, data["reasoning"], {"cast": "unknown"})

    assert concerns(["cast"], {"reasoning": REASON})
    ask = Mock(return_value="revised")
    answer = planner._second_look(
        menu,
        state,
        {"reasoning": REASON},
        ["cast"],
        concerns,
        ask,
        lambda _: ({"reasoning": "Pass"}, ["pass"]),
        "",
        high,
    )
    assert answer[1] == ["pass"]
    assert ask.call_count == 1 and ask.call_args.kwargs["effort"] == "high"


def test_repeated_contradiction_does_not_submit_original_cast():
    state, menu = fixture()
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}

    def concerns(ids, data):
        return planner._unevaluated_claim_concerns(menu, state, ids, data["reasoning"], {"cast": "unknown"})

    with pytest.raises(SecondLookError):
        planner._second_look(
            menu,
            state,
            {"reasoning": REASON},
            ["cast"],
            concerns,
            Mock(return_value="same"),
            lambda _: ({"reasoning": REASON}, ["cast"]),
            "",
            False,
        )


@pytest.mark.parametrize(
    "yielded,degraded,remaining", [(False, False, 30), (True, True, 30), (True, False, 2)]
)
def test_unavailable_high_effort_outcome_check_does_not_send_model(monkeypatch, yielded, degraded, remaining):
    import time

    from arenamcp import action_planner

    planner = ActionPlanner(Mock())
    monkeypatch.setattr(action_planner, "llm_degraded", lambda _: degraded)
    with pytest.raises(SecondLookError):
        planner._require_outcome_reask(yielded=yielded, deadline=time.monotonic() + remaining)


def test_activation_opponent_turn_is_not_excluded_and_evaluated_action_is_untouched(monkeypatch):
    from arenamcp import game_plan

    state, menu = fixture()
    menu.options[0].meta["actionType"] = "ActionType_Activate"
    state["turn"]["active_player"] = 2
    checker = Mock(return_value=["Fury cannot produce the claimed outcome"])
    monkeypatch.setattr(game_plan, "decision_claim_notes", checker)
    planner = ActionPlanner(Mock())
    assert planner._unevaluated_claim_concerns(menu, state, ["cast"], REASON, {"cast": "unknown"})
    assert checker.call_args.kwargs["cast_ids"] == []
    assert not planner._unevaluated_claim_concerns(menu, state, ["cast"], REASON, {})


def test_exact_fury_reason_at_public_decision_boundary(monkeypatch):
    import json

    state, menu = fixture()
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["cast"], "reasoning": REASON}),
        json.dumps({"option_ids": ["pass"], "reasoning": "Hold the spell; it cannot supply that block."}),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=False, land_drop_first=False)
    monkeypatch.setattr(
        planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "effect not modelled"})
    )
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30.0)
    selected = planner.plan_decision_options(menu, state)
    assert selected == ["pass"]
    assert backend.complete.call_count == 2
    assert "SECOND LOOK" in backend.complete.call_args.args[1]
    assert backend.complete.call_args.kwargs["reasoning_effort"] == "high"


@pytest.mark.parametrize("change", ["flying", "reach", "conditional", "negated"])
def test_fury_block_claim_abstains_when_not_contradicted(change):
    state, menu = fixture()
    reason = REASON
    if change in ("flying", "reach"):
        state["hand"][0]["oracle_text"] += "\n" + change.capitalize()
        state["hand"][0]["keywords"] = [change.capitalize()]
    elif change == "conditional":
        reason = "If we give it flying, it " + REASON
    else:
        reason = "It is not true that it " + REASON
    planner = ActionPlanner(Mock())
    notes = planner._unevaluated_claim_concerns(menu, state, ["cast"], reason, {"cast": "unknown"})
    assert not any("claimed blocker" in text for _, text in notes)


def test_changed_revised_pick_with_outcome_contradiction_is_also_rejected():
    state, menu = fixture()
    planner = ActionPlanner(Mock())
    planner._last_decision_trace = {}

    def concerns(ids, data):
        return [("unevaluated_outcome", "contradicted outcome")]

    with pytest.raises(SecondLookError, match="remains contradicted"):
        planner._second_look(
            menu,
            state,
            {"reasoning": REASON},
            ["cast"],
            concerns,
            Mock(return_value="different"),
            lambda _: ({"reasoning": "still false"}, ["pass"]),
            "",
            False,
        )


def known_fury(monkeypatch):
    from arenamcp import line_search_moves
    from arenamcp.card_facts.schema import card_facts_from_dict

    record = card_facts_from_dict(
        {
            "name": "Fury",
            "effects": [
                {
                    "trigger": "ENTERS_THE_BATTLEFIELD",
                    "effect": {"effect_type": "Damage", "amount": 4, "target": "target creature"},
                }
            ],
            "modes": [
                {
                    "name": "evoke",
                    "cost_text": "Exile a red card from your hand.",
                    "cost": {"mana": ""},
                    "stays_on_battlefield": False,
                }
            ],
        }
    )
    monkeypatch.setattr(line_search_moves, "_known_record", lambda *a: record)


@pytest.mark.parametrize(
    "reason",
    [
        "Evoking Fury leaves a double-striking 3/3 body on our battlefield.",
        "Fury deals four damage and survives to attack next turn.",
        "Fury kills Griselbrand with its four damage.",
    ],
)
@pytest.mark.parametrize("blocked", [False, True])
def test_record_outcomes_reask_or_fallback_without_resubmitting(monkeypatch, reason, blocked):
    import json

    known_fury(monkeypatch)
    state, menu = fixture()
    menu.options[0].meta["alternativeCostText"] = "Evoke—Exile a red card from your hand."
    backend = Mock()
    backend.complete.side_effect = [
        json.dumps({"option_ids": ["cast"], "reasoning": reason}),
        json.dumps({"option_ids": ["pass"], "reasoning": "Pass safely."}),
    ]
    planner = ActionPlanner(backend, separate_strategy_planning=blocked, land_drop_first=False)
    monkeypatch.setattr(planner, "_line_prompt_context", lambda *a: (None, "", {"cast": "unknown"}))
    monkeypatch.setattr(planner, "_sized_budget", lambda *a: 30)
    assert planner.plan_decision_options(menu, state) == ["pass"]
    assert backend.complete.call_count == (1 if blocked else 2)


@pytest.mark.parametrize(
    "change", ["hard_cast", "negated", "conditional", "unknown_record", "return_engine", "marked_damage"]
)
def test_record_outcomes_conservative_abstention(monkeypatch, change):
    from arenamcp import line_search_moves
    from arenamcp.outcome_claims import record_outcome_notes

    known_fury(monkeypatch)
    state, menu = fixture()
    meta = {"alternativeCostText": "Evoke—Exile a red card from your hand."}
    reason = "Fury survives to attack next turn."
    if change == "hard_cast":
        meta = {}
    elif change == "negated":
        reason = "Fury does not survive to attack next turn."
    elif change == "conditional":
        reason = "If we return it, Fury survives to attack next turn."
    elif change == "unknown_record":
        monkeypatch.setattr(line_search_moves, "_known_record", lambda *a: None)
    elif change == "return_engine":
        state["battlefield"].append(
            {"name": "Return engine", "oracle_text": "When a creature dies, return it."}
        )
    else:
        reason = "Fury kills Griselbrand with its four damage."
        state["battlefield"][0]["damage_marked"] = 3
    assert record_outcome_notes(state["hand"][0], meta, state, reason) == []
