"""Public-board replay of b717d4e1 T9; no hidden cards or live commands."""

import json
from pathlib import Path

from arenamcp.combat_solver import _resolve_attacker
from arenamcp.combat_strategy import combat_choice, losing_attackers


def incident():
    return json.loads((Path(__file__).parent / "fixtures/combat/b717d4e1-shang-public.json").read_text())


def test_public_first_strike_outcome_and_current_solver():
    state = incident()
    cards = {c["instance_id"]: c for c in state["battlefield"]}
    outcome = _resolve_attacker(cards[449], [cards[478]])
    assert outcome.attacker_died and not outcome.blockers_died
    assert outcome.damage_through == 0 and outcome.damage_deltas == {}
    assert combat_choice(state).assignments == {}


def test_restricted_mana_does_not_make_safekeeping_a_saving_trick():
    state = incident()
    losing = losing_attackers(state, [449], strategy_text="they likely won't trade a 4/4 for a 2/2")
    assert 449 in losing
    assert "Black Widow" in losing[449]


def test_bridge_preparation_removes_explicit_player_attack_without_commands():
    from arenamcp.action_planner import ActionType, GameAction
    from arenamcp.autopilot import AutopilotEngine

    state = incident()
    name = next(c["name"] for c in state["battlefield"] if c["instance_id"] == 449)
    action = GameAction(
        action_type=ActionType.DECLARE_ATTACKERS, attacker_names=[name], attacker_targets={name: "Opponent"}
    )
    entries = [{"attackerInstanceId": 449, "damageRecipient": {"type": "Player", "playerSystemSeatId": 1}}]
    submitted, entries = object.__new__(AutopilotEngine)._prepare_attack_submission(
        action, [name], entries, state, {"attackers": state["decision_context"]["raw_attackers"]}
    )
    assert entries == [] and submitted.attacker_instance_ids == []


def test_available_green_protection_preserves_choice():
    state = incident()
    next(c for c in state["battlefield"] if c["instance_id"] == 501)["is_tapped"] = False
    assert losing_attackers(state, [449]) == {}


def test_forced_attack_and_real_attack_payoff_remain_legal():
    state = incident()
    state["decision_context"]["raw_attackers"][0]["mustAttack"] = True
    assert losing_attackers(state, [449]) == {}
    state = incident()
    next(c for c in state["battlefield"] if c["instance_id"] == 449)["oracle_text"] += (
        "\nWhenever this creature attacks, draw a card."
    )
    assert losing_attackers(state, [449]) == {}


def test_free_alternative_and_unknown_payments_preserve_choice():
    state = incident()
    state["hand"][0]["oracle_text"] += " You may exile a green card rather than pay this spell's mana cost."
    assert losing_attackers(state, [449]) == {}
    state = incident()
    next(c for c in state["battlefield"] if c["instance_id"] == 467).pop("oracle_text")
    assert losing_attackers(state, [449]) == {}


def test_deathtouch_alone_does_not_survive_first_strike_but_real_trade_is_preserved():
    state = incident()
    state["hand"] = []
    shang = next(c for c in state["battlefield"] if c["instance_id"] == 449)
    widow = next(c for c in state["battlefield"] if c["instance_id"] == 478)
    shang["keywords"] = ["deathtouch"]
    assert 449 in losing_attackers(state, [449])
    shang["keywords"].append("first strike")
    out = _resolve_attacker(shang, [widow])
    assert out.attacker_died and [c["instance_id"] for c in out.blockers_died] == [478]
    # Equal-size first-strike deathtouch survives Captain, trades with Widow.
    shang["power"] = shang["toughness"] = 4
    assert losing_attackers(state, [449]) == {}
