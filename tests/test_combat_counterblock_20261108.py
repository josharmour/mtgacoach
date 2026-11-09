"""Match 22bea7bf: Boo cannot demand a trade while Colossus can block."""

import pytest
from tests.test_losing_attack_guard import AttackBackend, creature, incident

from arenamcp.action_planner import ActionPlanner
from arenamcp.combat_strategy import losing_attack_prompt


@pytest.fixture(autouse=True)
def offline_cards(monkeypatch):
    # All combat facts are in the fixture; prompt enrichment must not fetch.
    monkeypatch.setattr("arenamcp.scryfall.ScryfallCache.get_card_by_name", lambda *args: None)


def board():
    state = incident(
        attackers=[creature(470, "Boo", 2, 7, 7, "Trample, haste")],
        blockers=[
            creature(600, "Vaultborn Tyrant", 1, 6, 6, "Trample"),
            creature(601, "Darksteel Colossus", 1, 11, 11, "Trample, indestructible"),
        ],
        opponent_life=31,
    )
    state["hand"] = []
    state["graveyard"] = []
    return state


def test_colossus_counterblock_is_present_before_model_choice():
    state = board()
    planner = ActionPlanner(AttackBackend(), timeout=1, land_drop_first=False)
    prompt = planner._build_action_prompt(
        state, "decision_required", ["Attack with: Boo (7/7)"], state["decision_context"]
    )
    assert "COMPUTED COUNTER-BLOCKS" in prompt
    assert "Darksteel Colossus 11/11 can block Boo 7/7" in prompt
    assert "defending player chooses blockers" in prompt
    assert "not legality restrictions" in prompt


def test_tapped_colossus_does_not_create_false_warning():
    state = board()
    next(c for c in state["battlefield"] if c["instance_id"] == 601)["is_tapped"] = True
    assert losing_attack_prompt(state, state["decision_context"]) == ""


def test_priority_and_unknown_attacker_menu_do_not_warn():
    state = board()
    assert losing_attack_prompt(state, {"type": "priority"}) == ""
    assert losing_attack_prompt(state, {"type": "declare_attackers"}) == ""


def test_raw_attacker_ids_are_supported():
    state = board()
    context = dict(state["decision_context"])
    del context["legal_attacker_ids"]
    assert "Darksteel Colossus" in losing_attack_prompt(state, context)
