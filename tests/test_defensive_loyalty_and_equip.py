from types import SimpleNamespace

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.loyalty_removal import choose_removal
from arenamcp.play_safety import filter_play_options


def body(i, name, seat, power, toughness, *, text="", types=()):
    return {
        "instance_id": i,
        "name": name,
        "controller_seat_id": seat,
        "type_line": "Creature",
        "power": power,
        "toughness": toughness,
        "oracle_text": text,
        "subtypes": list(types),
    }


def test_removing_a_support_creature_saves_the_walker_and_guides_both_requests():
    walker = {
        "instance_id": 20,
        "name": "Walker",
        "type_line": "Legendary Planeswalker",
        "controller_seat_id": 1,
        "counters": {"Loyalty": 5},
    }
    food = body(22, "Token", 1, 4, 4, types=["Hamster"])
    lord = body(30, "Lord", 2, 1, 1, text="Thopters you control get +2/+0.")
    fliers = [body(i, "Flier", 2, 2, 2, text="Flying", types=["Thopter"]) for i in (31, 32, 33)]
    state = {
        "match_id": "sample",
        "turn": {"turn_number": 12, "active_player": 1, "phase": "Main1"},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True, "life_total": 18}, {"seat_id": 2, "life_total": 18}],
        "battlefield": [walker, food, lord, *fliers],
    }
    ability = DecisionOption(
        "idx:1",
        "Activate: Walker [-2: Sacrifice a creature. When you do, this deals X damage to any target, where X is that creature's power. If the sacrificed creature was a Hamster, draw X cards.]",
        meta={"actionType": "ActionType_Activate", "instanceId": 20},
    )
    menu = PendingDecision((375, 561), "ActionsAvailable", (ability, DecisionOption("pass", "Pass")))
    pick, pending = choose_removal(filter_play_options(menu, state), state, None)
    assert pick[0] == "idx:1" and "6 to 0" in pick[1]
    sac = PendingDecision(
        (377, 563),
        "SelectN",
        (DecisionOption("sel:22", "Token"),),
        source_label="Walker ability",
        prompt_text="Sacrifice a creature.",
    )
    pick, pending = choose_removal(sac, state, pending)
    assert pick[0] == "sel:22"
    targets = PendingDecision(
        (378, 564), "SelectTargets", (DecisionOption("tgt:30", "Lord"),), source_label="Walker ability"
    )
    assert choose_removal(targets, state, pending)[0][0] == "tgt:30"
    assert choose_removal(targets, dict(state, match_id="other"), pending)[0] is None
    lord["oracle_text"] += "\nWard—Pay 7 life."
    assert choose_removal(menu, state, None)[0] is None


def test_hold_only_defers_attack_equip_if_added_attacker_cannot_be_lethal(monkeypatch):
    from arenamcp import board_assessment, card_db, combat_strategy
    from arenamcp.equipment_opportunities import defer_attack_equip

    board = SimpleNamespace(posture="hold", all_in=False, lethal_now=False, opp_life=18)
    monkeypatch.setattr(board_assessment, "assess", lambda _s: board)
    db = SimpleNamespace(
        grp_ids_by_name=lambda n: [5],
        get_card=lambda i: SimpleNamespace(power="6", oracle_text="Trample\nHaste"),
    )
    monkeypatch.setattr(card_db, "get_card_database", lambda: SimpleNamespace(get_raw_mtgadb=lambda: db))
    monkeypatch.setattr(combat_strategy, "able_attackers", lambda s: [body(22, "Token", 1, 4, 4)])
    gear = {
        "oracle_text": 'Equipped creature has "Whenever this creature attacks, conjure a card named Runner onto the battlefield tapped and attacking."\nEquip—Pay 3 life.'
    }
    equip = DecisionOption("idx:4", "Activate: Gear [Equip—Pay 3 life.]")
    assert defer_attack_equip(equip, gear, {})
    board.opp_life = 10
    assert not defer_attack_equip(equip, gear, {})


def test_current_plan_reserves_loyalty_and_rechecks_after_combat():
    from arenamcp.game_plan import GamePlan, GamePlanManager, postcombat_sacrifice_step
    from arenamcp.loyalty_removal import postcombat_activation, reserve_postcombat_loyalty

    walker = {
        "instance_id": 10,
        "name": "Walker",
        "type_line": "Legendary Planeswalker",
        "controller_seat_id": 1,
        "counters": {"Loyalty": 4},
    }
    attacker = body(11, "Runner", 1, 4, 4, text="Haste", types=["Hamster"])
    blocker = body(12, "Blocker", 2, 2, 3)
    state = {
        "match_id": "match",
        "turn": {"turn_number": 7, "active_player": 1, "phase": "Main1"},
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True, "life_total": 23}, {"seat_id": 2, "life_total": 25}],
        "battlefield": [walker, attacker, blocker],
    }
    minus = DecisionOption(
        "idx:0",
        "Activate: Walker [-2: Sacrifice a creature. When you do, this deals X damage to any target, where X is that creature's power. If the sacrificed creature was a Hamster, draw X cards.]",
        meta={"actionType": "ActionType_Activate", "instanceId": 10},
    )
    plus = DecisionOption(
        "idx:1",
        "Activate: Walker [+1: Draw a card.]",
        meta={"actionType": "ActionType_Activate", "instanceId": 10},
    )
    walker["oracle_text"] = minus.label.split("Walker ", 1)[1].replace("-2:", "-2]:", 1)
    from arenamcp.loyalty_removal import loyalty_strategy_context

    assert "after combat for up to 4 damage" in loyalty_strategy_context(state)
    menu = PendingDecision((165, 226), "ActionsAvailable", (minus, plus, DecisionOption("pass", "Pass")))
    intent = {"postcombat_sacrifice": {"source": "Walker", "attacker": "Runner", "cost": -2}}
    step = {"turn": 7, "attack": "Runner", "hold": "", **postcombat_sacrifice_step(intent, "Runner")}
    assert step.get("postcombat_sacrifice") == intent["postcombat_sacrifice"]
    assert not postcombat_sacrifice_step(intent, "no attacks")
    manager = GamePlanManager(None)
    manager.observe(state)
    manager._plan = GamePlan(turn_plan=[step])
    assert reserve_postcombat_loyalty(menu, state, manager.current_step(state)).option_ids() == {"pass"}
    assert manager.current_step(dict(state, match_id="other")) is None
    blocker["power"] = 4
    assert reserve_postcombat_loyalty(menu, state, step) == menu
    state["turn"]["phase"] = "Main2"
    attacker["is_tapped"] = True
    assert postcombat_activation(menu, state, step)[0] == "idx:0"
    state["battlefield"].remove(attacker)
    assert postcombat_activation(menu, state, step) is None


def test_mana_engine_arrival_refreshes_plan_and_prices_removal(monkeypatch):
    from arenamcp.decisions import DecisionOption, PendingDecision
    from arenamcp.game_plan import board_basis, board_changes
    from arenamcp.loyalty_removal import mana_engine_context

    rules = "[-2]: Sacrifice a creature. When you do, this deals X damage to any target, where X is that creature's power. If the sacrificed creature was a Hamster, draw X cards."
    walker = {
        "instance_id": 10,
        "name": "Walker",
        "controller_seat_id": 1,
        "type_line": "Legendary Planeswalker",
        "counters": {"Loyalty": 3},
        "oracle_text": rules,
    }
    food = body(11, "Runner", 1, 4, 4, types=["Hamster"])
    artifacts = [
        {"instance_id": i, "name": f"Rock {i}", "type_line": "Artifact", "controller_seat_id": 2}
        for i in (20, 21)
    ]
    source = body(30, "Tinkerer", 2, 1, 4, text="Tap an untapped artifact you control: Add {U}.")
    state = {
        "match_id": "game",
        "local_seat_id": 1,
        "players": [{"seat_id": 1, "is_local": True, "life_total": 21}, {"seat_id": 2, "life_total": 23}],
        "turn": {"turn_number": 11, "active_player": 1, "phase": "Main1"},
        "battlefield": [walker, food, *artifacts],
    }
    before = board_basis(state)
    state["battlefield"].append(source)
    assert "Tinkerer" in " ".join(board_changes(before, board_basis(state), threats_only=True))
    assert "4 damage and 4 cards" in mana_engine_context(state)
    ability = DecisionOption(
        "idx:1",
        "Activate: Walker [" + rules[1:].replace("]:", ":", 1) + "]",
        meta={"actionType": "ActionType_Activate", "instanceId": 10},
    )
    d = PendingDecision((339, 470), "ActionsAvailable", (ability, DecisionOption("pass", "Pass")))
    assert choose_removal(d, state, None)[0][0] == "idx:1"
    from arenamcp import gre_action_matcher
    from arenamcp.game_plan import GamePlan, removal_board_changed
    from arenamcp.loyalty_removal import align_offered_removal

    plan = GamePlan(turn_plan=[{"turn": 11, "attack": "Runner", "hold": "Hold loyalty."}], path="Attack")
    align_offered_removal(plan, state)
    assert plan.path == "Attack"
    monkeypatch.setattr(
        gre_action_matcher,
        "describe_ability",
        lambda gid: ("+1", "Gain life.") if gid == 51 else ("-2", rules.split("]: ", 1)[1]),
    )
    state["legal_actions_raw"] = [
        {"actionType": "ActionType_Activate", "instanceId": 10, "abilityGrpId": gid} for gid in (51, 52)
    ]
    align_offered_removal(plan, state)
    assert "4 damage to Tinkerer" in plan.develop_next and plan.turn_plan[0]["attack"] == ""
    assert not removal_board_changed(plan, state)
    assert removal_board_changed(plan, {**state, "battlefield": [walker, food, *artifacts]})
    source["oracle_text"] += "\nHexproof"
    assert "sacrifice Runner" not in mana_engine_context(state)
    assert choose_removal(d, state, None)[0] is None
