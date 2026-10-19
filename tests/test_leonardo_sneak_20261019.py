"""Sneak, Hare Apparent's tokens and Leonardo's scaling (ledger #156, #157, #160; match be1a945f).

2026-10-19 14:13-14:18, Brawl_Ladder with Leonardo, Big Brother. The coach knew
nothing of Sneak ("Sneak {W}: cast it in the declare-blockers step by returning
an unblocked attacker; it enters tapped and attacking"):

- T6: the declare-blockers "Cast Leonardo, Big Brother" (Arena's action carries
  alternativeGrpId 203047, "Sneak {oW}") read as an ordinary cast, and the
  model paid the "Pay non-mana cost" (prompt 8580, "Return an unblocked
  attacking creature you control to its owner's hand.") as if it were a tap.
- T12: six mana, two Hare Apparents in hand, Leonardo in the command zone
  (tax {2}). Both Hares were cast (4 mana) and the attack took them 17 -> 5;
  keeping {2}{W} for a Sneak Leonardo after blocks was 26 or more.
- Every Hare Apparent was "its tokens, not modelled" (``token_specs`` None) and
  Leonardo's +1/+0 per other creature was a frozen snapshot number.

Boards: ``tests/leonardo_boards_20261019.py`` (replayed from Player.log).
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from tests import leonardo_boards_20261019 as lb

from arenamcp import decisions, sneak
from arenamcp.board_model import _action_cost, build_board_model, our_commanders
from arenamcp.decision_memory import lethal_reach
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.line_search import WIN, search_lines
from arenamcp.line_search_moves import named_token_specs, token_specs, unmodelled_effect

SNEAK_ABILITY = 203047
# The T6 Sneak cast as Player.log logged our PerformActionResp (L353015).
T6_SNEAK_ACTION = {
    "actionType": "ActionType_Cast", "grpId": 100471, "instanceId": 241, "facetId": 241, "abilityGrpId": 115,
    "sourceId": 241, "alternativeGrpId": SNEAK_ABILITY,
    "manaCost": [{"color": ["ManaColor_White"], "count": 1, "abilityGrpId": SNEAK_ABILITY}],
    "autoTapSolution": {"autoTapActions": [{"instanceId": 749, "abilityGrpId": 1001}]},
}  # fmt: skip


@pytest.fixture
def sneak_text(monkeypatch):
    """Arena's ability text for the Sneak id without the MTGA card database."""
    monkeypatch.setattr(
        decisions,
        "_default_ability_description",
        lambda grp: ("", "Sneak {oW}") if int(grp) == SNEAK_ABILITY else ("", ""),
    )


# --- reading the card ------------------------------------------------------------------------


def test_sneak_costs_and_the_commander_tax():
    leonardo = lb.card(821, "Leonardo, Big Brother", lb.LOCAL)
    assert sneak.sneak_cost(leonardo) == "{W}"
    scryfall = {
        "oracle_text": "Sneak {W} (You may cast this spell for {W} if you also return an unblocked ...)"
    }
    assert sneak.sneak_cost(scryfall) == "{W}"
    assert sneak.sneak_cost(lb.card(835, "Hare Apparent", lb.LOCAL)) == ""
    assert sneak.with_tax("{W}", 1) == "{2}{W}" and sneak.with_tax("{W}", 0) == "{W}"
    assert sneak.is_sneak_ability("Sneak {oW}") and not sneak.is_sneak_ability("Kicker {o4}")
    assert sneak.per_creature_bonus(leonardo) == (1, 0, True)
    # 1/3 printed; 15 other creatures -> 16 power (the T10 snapshot's 16/3).
    assert sneak.entry_power(leonardo, 16) == 16


def test_the_commander_in_the_command_zone_has_its_sneak_and_its_normal_cost_apart():
    state = lb.t12_declare_blockers()
    # In the declare-blockers step Arena's only cast action for Leonardo is the Sneak one:
    # {W} + tax {2}. It is not the commander's cost ({2}{W} + {2}).
    state["legal_actions_raw"] = [
        {**T6_SNEAK_ACTION, "instanceId": 821, "manaCost": [{"color": ["ManaColor_Generic"], "count": 2},
                                                             {"color": ["ManaColor_White"], "count": 1}]}
    ]  # fmt: skip
    (commander,) = our_commanders(state)
    assert (commander.cost, commander.casts, commander.sneak_cost) == ("{4}{W}", 1, "{2}{W}")
    (cast,) = sneak.sneak_casts(state)
    assert (cast.name, cast.cost, cast.zone, cast.mana_value) == (
        "Leonardo, Big Brother",
        "{2}{W}",
        "command",
        3,
    )


def test_the_mac_bridges_bare_colour_names_keep_their_pips():
    # The Mac bridge's ActionsAvailable writes '[ "White" ]': Leonardo's {4}{W} read "{5}".
    assert (
        _action_cost([{"color": '[ "Generic" ]', "count": 4}, {"color": '[ "White" ]', "count": 1}])
        == "{4}{W}"
    )
    assert _action_cost([{"color": '[ "ManaColor_Red" ]', "count": 1}]) == "{R}"


# --- the decision the model sees -------------------------------------------------------------


def test_the_sneak_cast_option_says_what_it_is(sneak_text):
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "actions": [T6_SNEAK_ACTION],
        "can_pass": True,
    }
    decision = decisions.build_pending_decision(poll, resolve_name=lambda grp: "Leonardo, Big Brother")
    option = decision.find("idx:0")
    assert option.label.startswith("Cast Leonardo, Big Brother [via Sneak {W}: return an unblocked attacker")
    assert "enters tapped and attacking" in option.label
    assert option.meta["alternativeGrpId"] == SNEAK_ABILITY and option.meta["alternative"] == "Sneak"
    assert sneak.is_sneak_option(option.meta)
    # The "Cast X" name parsers strip the [tag]: the card name stays the card's.
    from arenamcp.autopilot import AutopilotEngine

    assert AutopilotEngine._play_name(option.label) == ("ActionType_Cast", "leonardo, big brother")


def test_an_ordinary_cast_keeps_its_plain_label(sneak_text):
    plain = {key: value for key, value in T6_SNEAK_ACTION.items() if key != "alternativeGrpId"}
    poll = {"has_pending": True, "request_type": "ActionsAvailable", "actions": [plain], "can_pass": True}
    option = decisions.build_pending_decision(poll, resolve_name=lambda grp: "Leonardo, Big Brother").find(
        "idx:0"
    )
    assert option.label == "Cast Leonardo, Big Brother"
    assert "alternative" not in option.meta and "alternativeGrpId" not in option.meta


# --- declare blockers: the lethal Sneak and what it returns ----------------------------------


def _sneak_menu() -> PendingDecision:
    meta = {"actionType": "ActionType_Cast", "grpId": 100471, "instanceId": 821, "alternative": "Sneak",
            "alternativeGrpId": SNEAK_ABILITY}  # fmt: skip
    return PendingDecision(
        (316, 420), "ActionsAvailable",
        (DecisionOption("idx:0", "Cast Leonardo, Big Brother [via Sneak {2}{W}]", True, meta),
         DecisionOption("pass", "Pass")),
        can_pass=True,
    )  # fmt: skip


def test_after_blocks_the_sneak_is_counted_and_cast_when_lethal():
    state = lb.t12_declare_blockers(sneak_mana=3)
    found = sneak.sneak_now(state)
    # Eleven unblocked attackers (12 damage); Leonardo comes in with fifteen creatures around.
    assert (found.through, found.power, found.lethal) == (12, 15, True)
    picked, why = sneak.sneak_cast_pick(_sneak_menu(), state)
    assert picked == ["idx:0"] and why.startswith("Lethal: Sneak Leonardo, Big Brother")
    reach = lethal_reach(_sneak_menu(), state)
    assert reach is not None and reach.sneak and "Sneak Leonardo, Big Brother after blocks" in reach.text()


def test_a_sneak_that_is_not_lethal_is_left_to_the_model():
    state = lb.t12_declare_blockers()
    state["players"][1]["life_total"] = 40
    assert sneak.sneak_cast_pick(_sneak_menu(), state) == ([], "")


def test_without_blocks_known_nothing_is_assumed_unblocked():
    state = lb.t12_declare_blockers()
    for card in state["battlefield"]:
        card.pop("is_blocked", None)
    assert sneak.unblocked_attackers(state) is None
    assert sneak.sneak_now(state) is None


def _return_request(ids) -> PendingDecision:
    return PendingDecision(
        (317, 421), "SelectN", tuple(DecisionOption(f"sel:{i}", "x") for i in ids), min_select=1, max_select=1,
        can_cancel=True, source_label="Pay non-mana cost",
        prompt_text="Return an unblocked attacking creature you control to its owner's hand.",
    )  # fmt: skip


def test_the_sneak_cost_returns_a_card_to_recast_over_a_token():
    state = lb.t12_declare_blockers()
    state["stack"] = [lb.card(821, "Leonardo, Big Brother", lb.LOCAL)]
    # Lethal either way: the Hare Apparent goes back to hand (it makes its Rabbits again).
    picked, why = sneak.return_pick(_return_request([798, 761, 767]), state)
    assert picked == ["sel:798"] and "back to hand to cast again" in why


def test_the_sneak_cost_keeps_lethal_before_card_advantage():
    state = lb.t12_declare_blockers()
    state["stack"] = [lb.card(821, "Leonardo, Big Brother", lb.LOCAL)]
    # 12 unblocked - 2 (a Hare) + 15 = 25; - 1 (a Rabbit) + 15 = 26: at 26 life only the Rabbit keeps it.
    state["players"][1]["life_total"] = 26
    picked, _why = sneak.return_pick(_return_request([798, 761]), state)
    assert picked == ["sel:761"]


def test_the_return_request_is_recognised_by_arenas_prompt_only():
    assert sneak.is_sneak_return(_return_request([798]))
    other = PendingDecision(
        (1, 2), "SelectN", (DecisionOption("sel:798", "x"),), source_label="Pay non-mana cost"
    )
    assert not sneak.is_sneak_return(other)


# --- main 1: keep the Sneak mana -------------------------------------------------------------


def _t12_menu(*, hares: int = 2) -> PendingDecision:
    hare_cost = [{"color": '[ "Generic" ]', "count": 1}, {"color": '[ "White" ]', "count": 1}]
    leo_cost = [{"color": '[ "Generic" ]', "count": 4}, {"color": '[ "White" ]', "count": 1}]
    options = [
        DecisionOption(f"idx:{i}", "Cast Hare Apparent", True,
                       {"actionType": "ActionType_Cast", "grpId": 93728, "instanceId": iid, "manaCost": hare_cost})
        for i, iid in enumerate((756, 835)[:hares])
    ]  # fmt: skip
    options.append(
        DecisionOption("idx:8", "Cast Leonardo, Big Brother", True,
                       {"actionType": "ActionType_Cast", "grpId": 100471, "instanceId": 821, "manaCost": leo_cost})
    )  # fmt: skip
    options.append(
        DecisionOption(
            "idx:9", "Play land: Plains", None, {"actionType": "ActionType_Play", "instanceId": 790}
        )
    )
    options.append(DecisionOption("pass", "Pass"))
    return PendingDecision((297, 392), "ActionsAvailable", tuple(options), can_pass=True)


def test_main_one_keeps_the_sneak_mana_when_the_sneak_line_is_lethal():
    state = lb.t12_main1()
    hold = sneak.sneak_main_hold(state)
    # Their four untapped blockers take four attackers: 12 through, +15 Leonardo - 1 Rabbit = 26.
    assert (hold.through, hold.power, hold.total, hold.keep) == (12, 15, 26, 3)
    # Six mana (five Plains and the land drop): one Hare leaves 4 >= 3, the normal cast does not.
    kept, why = sneak.hold_filter(_t12_menu(), state)
    assert {o.option_id for o in kept.options} == {"idx:0", "idx:1", "idx:9", "pass"}
    assert why.startswith("keep {2}{W} open for the Sneak") and "Leonardo, Big Brother" in why
    # After the first Hare: three Plains untapped and the land drop: the second Hare is withheld.
    state["hand"] = [h for h in state["hand"] if h["instance_id"] != 756]
    for land in [c for c in state["battlefield"] if c["name"] == "Plains"][:2]:
        land["is_tapped"] = True
    kept, _why = sneak.hold_filter(_t12_menu(hares=1), state)
    assert {o.option_id for o in kept.options} == {"idx:9", "pass"}


def test_no_hold_when_the_attack_is_lethal_without_it_or_not_lethal_with_it():
    state = lb.t12_main1()
    state["players"][1]["life_total"] = 30
    assert sneak.sneak_main_hold(state) is None
    state["players"][1]["life_total"] = 10
    assert sneak.sneak_main_hold(state) is None  # 12 through already kills


def test_the_planner_applies_the_hold_before_asking_the_model():
    from arenamcp.action_planner import ActionPlanner

    planner = ActionPlanner.__new__(ActionPlanner)
    planner._sneak_notes = []
    kept, picked = planner._sneak_policy(_t12_menu(), lb.t12_main1())
    assert picked == [] and "idx:8" not in kept.option_ids()
    assert planner._sneak_notes and planner._sneak_notes[0].startswith("SNEAK HOLD: keep {2}{W} open")
    planner._sneak_notes = []
    _kept, picked = planner._sneak_policy(_sneak_menu(), lb.t12_declare_blockers())
    assert picked == ["idx:0"] and planner._last_decision_trace["policy"] == "sneak_lethal"


# --- the line search (#157) ------------------------------------------------------------------


def test_hare_apparent_tokens_are_counted_on_the_board():
    hare = lb.card(835, "Hare Apparent", lb.LOCAL)
    from arenamcp.board_assessment import _cast_text

    text = _cast_text(hare)
    assert token_specs(text) == []
    assert named_token_specs(text, "Hare Apparent") == ((1, 1, (), "Rabbit token", "Hare Apparent", True),)
    assert unmodelled_effect(hare) == ""
    assert unmodelled_effect(lb.card(821, "Leonardo, Big Brother", lb.LOCAL)) == ""


def test_t12_the_best_line_is_a_hare_then_the_sneak_for_lethal():
    result = search_lines(build_board_model(lb.t12_main1()), survival=False, lethal_now=False, hard_ms=1e9)
    best = result.best
    assert (best.cls, best.win_at) == (WIN, 1), best.summary(200)
    step = best.steps[0]
    assert step.casts == ("Hare Apparent", "Leonardo, Big Brother")
    assert dict(step.modes)["Leonardo, Big Brother"].startswith("Sneak after blocks")
    assert result.posture == "lethal"
    # The greedy line (both Hares) does not win this turn.
    assert result.baseline.cls != WIN or result.baseline.win_at != 1


def test_the_hare_makes_one_rabbit_per_other_hare_and_grows_leonardo():
    state = lb.t12_main1()
    model = build_board_model(state)
    from arenamcp.line_search import _Search

    search = _Search(model, survival=False, lethal_now=False, hard_ms=1e9, groups=16, width=2, k_next=4,
                     opponent=None)  # fmt: skip
    search.free = True
    root = search.roots()[0][1]
    land = search.land_options(root)[0]
    _base, sources = search.sources(root, land)
    hare_sneak = next(
        casts for casts in search.cast_sets(root, sources, instant_only=False)
        if sorted(search.spells[v.spell].name + v.kind for v, _t in casts)
        == ["Hare Apparentcast", "Leonardo, Big Brothersneak"]
    )  # fmt: skip
    mid = search.begin(root, land, hare_sneak)
    # Five Hares on the board: the sixth makes five Rabbits (21 creatures before the attack).
    assert len(mid.ours) == 21
    attack = next(choice for choice in search.attack_choices(mid)[0] if choice[0] == "all")
    node = search.finish(mid, attack)
    leonardo = next(b for b in node.ours if b["name"] == "Leonardo, Big Brother")
    # In for a Rabbit after blocks; 21 creatures (Leonardo among them) at damage: 21 power.
    assert node.win_at == 1 and node.history[0].opp_life_after <= 17 - 21
    assert leonardo["power"] >= 1 + (len(node.ours) - 1)


def test_leonardo_on_the_battlefield_follows_the_creature_count():
    state = lb.t10_declare_attackers()
    model = build_board_model(state)
    from arenamcp.line_search import _Search

    search = _Search(model, survival=False, lethal_now=False, hard_ms=1e9, groups=16, width=2, k_next=4,
                     opponent=None)  # fmt: skip
    leonardo = next(b for b in model.ours if b["name"] == "Leonardo, Big Brother")
    assert search._scale_base[leonardo["instance_id"]][:2] == (1, 3)
    fewer = search.rescale([b for b in model.ours if b["name"] != "Rabbit"])
    assert next(b for b in fewer if b["name"] == "Leonardo, Big Brother")["power"] == 1 + 5


# --- #160: the PayCosts prompt -----------------------------------------------------------------


def test_the_mac_bridge_carries_the_non_mana_cost_prompt(monkeypatch):
    from tests.mac_bridge_fakes import FakeGame, adapter_for, enum, listing

    import arenamcp.arena_prompts as prompts

    monkeypatch.setattr(
        prompts, "_lookup_template",
        lambda pid: "Return an unblocked attacking creature you control to its owner's hand." if pid == 8580 else "",
    )  # fmt: skip
    prompt = {
        "$c": "Wotc.Mtgo.Gre.External.Messaging.Prompt", "$h": 73, "promptId_": 8580,
        "parameters_": listing({"$c": "PromptParameter", "$h": 74, "parameterName_": "CardId",
                                "type_": enum("Number", 1), "numberValue_": 750}),
    }  # fmt: skip
    selection = {
        "$c": "GreClient.Rules.SelectNRequest", "$h": 72, "Ids": listing(545), "Weights": listing(1),
        "IdType": enum("InstanceId", 1), "MinSel": 1, "MaxSel": 1, "MinWeight": -2147483648,
        "MaxWeight": 2147483647, "Prompt": prompt,
    }  # fmt: skip
    request = {
        "$c": "GreClient.Rules.PayCostsRequest", "$h": 5, "Prompt": prompt, "SourceId": 750,
        "EffectCost": {"$c": "GreClient.Rules.EffectCostRequest", "$h": 71, "CostSelection": selection},
    }  # fmt: skip
    response = adapter_for(FakeGame(request, {"Type": enum("PayCostsReq", 1)})).handle(
        {"action": "get_pending_actions"}
    )
    assert response["prompt"]["promptId"] == 8580
    assert response["source_card"] == (
        "Pay non-mana cost: Return an unblocked attacking creature you control to its owner's hand."
    )
    assert response["decision_context"]["source_id"] == 750
    decision = decisions.build_pending_decision(response, resolve_instance=lambda iid: "")
    assert sneak.is_sneak_return(decision)


# --- the log: attackers' blocked state -----------------------------------------------------------


def test_player_log_block_states_mark_unblocked_attackers():
    from arenamcp.gamestate import GameState

    state = GameState()
    rabbit = {
        "instanceId": 805, "grpId": 94160, "type": "GameObjectType_Token", "zoneId": 28,
        "visibility": "Visibility_Public", "ownerSeatId": 1, "controllerSeatId": 1,
        "cardTypes": ["CardType_Creature"], "subtypes": ["SubType_Rabbit"], "power": {"value": 1},
        "toughness": {"value": 1}, "attackState": "AttackState_Attacking", "attackInfo": {"targetId": 2},
    }  # fmt: skip
    hare = {**rabbit, "instanceId": 744, "grpId": 93728, "type": "GameObjectType_Card", "power": {"value": 2},
            "toughness": {"value": 2}}  # fmt: skip
    state.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 316, "gameObjects": [deepcopy(rabbit), deepcopy(hare)]}
    )
    assert state.game_objects[805].is_blocked is None  # blocks not declared yet
    state.update_from_message(
        {
            "type": "GameStateType_Diff",
            "gameStateId": 322,
            "gameObjects": [
                {**rabbit, "blockState": "BlockState_Unblocked"},
                {**hare, "blockState": "BlockState_Blocked"},
            ],
        }
    )
    assert state.game_objects[805].to_dict()["is_blocked"] is False
    assert state.game_objects[744].to_dict()["is_blocked"] is True
