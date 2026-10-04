"""Every strategic entry point sends the same complete deck and live inventory."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from arenamcp.action_planner import ActionPlanner
from arenamcp.coach import CoachEngine
from arenamcp.decisions import build_pending_decision
from arenamcp.game_plan import GamePlanManager


class Backend:
    def __init__(self, reply='{"option_ids":["pass"]}'):
        self.reply = reply
        self.calls = []

    def complete(self, system, user, *args, **kwargs):
        self.calls.append((system, user))
        return self.reply


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(
        "arenamcp.match_context._local_card",
        lambda gid, epoch: {
            "name": f"Card {gid}",
            "type_line": "Instant",
            "mana_cost": "{G}",
            "cmc": 1,
            "oracle_text": f"Distinct rules for {gid}.",
        },
    )
    return {
        "match_id": "context-test",
        "deck_cards": list(range(100, 140)),
        "players": [
            {"is_local": True, "seat_id": 1, "life_total": 20},
            {"is_local": False, "seat_id": 2, "life_total": 20},
        ],
        "local_seat_id": 1,
        "turn": {"turn_number": 4, "active_player": 1, "phase": "Phase_Main1"},
        "hand": [
            {
                "instance_id": 1,
                "grp_id": 100,
                "name": "Card 100",
                "owner_seat_id": 1,
                "type_line": "Instant",
                "mana_cost": "{G}",
                "oracle_text": "Distinct rules for 100.",
            }
        ],
        "battlefield": [],
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"library_count": 39, "opponent_hand_count": 5},
    }


def assert_full_context(prompt):
    assert prompt.startswith("DECK REFERENCE")
    assert "Distinct rules for 139." in prompt
    library = prompt.split("MY LIBRARY", 1)[1]
    assert "1x Card 139" in library
    assert "1x Card 100" not in library


def test_typed_and_legacy_prompts_share_complete_stable_deck(state):
    backend = Backend()
    planner = ActionPlanner(
        backend, deck_strategy_fn=lambda: "Win through an engine; hold removal as needed."
    )
    decision = build_pending_decision(
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "can_pass": True,
            "actions": [{"actionType": "ActionType_Pass"}],
        }
    )
    planner._llm_decision_options(decision, state)
    first = backend.calls[-1][1]
    assert_full_context(first)
    assert "For tutors" in backend.calls[-1][0]
    changed = deepcopy(state)
    changed["players"][0]["life_total"] = 5
    changed["turn"]["turn_number"] = 5
    planner._llm_decision_options(decision, changed)
    second = backend.calls[-1][1]
    assert first.split("PENDING DECISION:")[0] == second.split("PENDING DECISION:")[0]
    assert first != second
    assert_full_context(planner._build_action_prompt(state, "new_turn", legal_actions=["Pass"]))


def test_coach_dynamic_strategy_does_not_invalidate_deck_prefix(state, monkeypatch):
    backend = Backend("Develop the engine while keeping removal available.")
    coach = CoachEngine(backend=backend)
    monkeypatch.setattr(coach, "_ensure_game_plan_mgr", lambda: None)
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    coach._deck_strategy = "Establish the engine."
    coach.get_advice(state, question="What is our plan?")
    system, first = backend.calls[-1]
    assert_full_context(first)
    coach._deck_strategy = "Remove the new threat before developing."
    coach.get_advice(state, question="What is our plan?")
    next_system, second = backend.calls[-1]
    assert system == next_system
    assert first.split("DECK STRATEGY:")[0] == second.split("DECK STRATEGY:")[0]
    assert "Remove the new threat" in second


def test_background_strategy_receives_all_draws_and_deck_rules(state):
    backend = Backend(
        '{"win_conditions":["Engine advantage"],"path":"Develop then protect",'
        '"threat":"Opposing engine","develop_next":"Hold removal"}'
    )
    manager = GamePlanManager(backend)
    manager.seed("Build the engine, preserve interaction.")
    assert manager.maybe_reform(state) is not None
    assert_full_context(backend.calls[-1][1])


def test_win_plan_does_not_repeat_shared_deck_passed_by_hotkey(state):
    from arenamcp.match_context import prepare_match_context, with_deck_reference

    backend = Backend("Deploy the engine, then protect it.")
    coach = CoachEngine(backend=backend)
    prepared = prepare_match_context(state)
    legacy_summary = with_deck_reference(prepared["library_summary"], prepared)
    coach.get_win_plan(state, 3, legacy_summary)
    prompt = backend.calls[-1][1]
    assert prompt.count("DECK REFERENCE (") == 1
    assert prompt.count("MY LIBRARY (") == 1


def test_background_plan_change_is_announced_on_next_advice(state, monkeypatch):
    backend = Backend("Develop the engine.")
    coach = CoachEngine(backend=backend)
    coach._rules_db = SimpleNamespace(get_rules_for_situation=lambda *args, **kwargs: [])
    intro = "First plan"
    manager = SimpleNamespace(
        observe=lambda state: None,
        seed=lambda seed: None,
        request_reform=lambda state: None,
        coach_intro=lambda: intro,
        plan_text=lambda: intro,
    )
    monkeypatch.setattr(coach, "_ensure_game_plan_mgr", lambda: manager)
    changed = []
    monkeypatch.setattr(
        coach,
        "_plan_framing_instruction",
        lambda block, **kwargs: changed.append(kwargs["plan_changed"]) or block,
    )
    coach.get_advice(state, question="What now?")
    coach.get_advice(state, question="What now?")
    intro = "Respond to the new threat"
    coach.get_advice(state, question="What now?")
    assert changed == [True, False, True]
