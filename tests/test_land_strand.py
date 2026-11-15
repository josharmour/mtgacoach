"""Land choices projected from the November 8 land tutor and two-colour land-drop decisions.

The original captured menus are replayed privately. These fixtures retain the relevant
mana production, legal alternatives and costs with synthetic identities.
"""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from arenamcp.action_planner import ActionPlanner, _lethal_this_turn
from arenamcp.card_facts.client import facts_for, hash_for_card
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.land_strand import filter_land_choices, guard_land_sacrifice


def card(iid, name, kind, rules="", cost=""):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": kind,
        "oracle_text": rules,
        "mana_cost": cost,
        "controller_seat_id": 1,
        "owner_seat_id": 1,
    }


def land(iid, color):
    return card(iid, "Source " + str(iid), "Land", "{T}: Add {" + color + "}.")


def state(hand, board, *, stack=()):
    return {
        "local_seat_id": 1,
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20, "lands_played": 0, "mana_pool": {}},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {"active_player": 1, "turn_number": 5, "phase": "Phase_Main1"},
        "battlefield": board,
        "own_battlefield": board,
        "command": [],
        "stack": list(stack),
        "hand": hand,
    }


def menu(kind, cards):
    opts = tuple(
        DecisionOption(
            "pick:" + str(c["instance_id"]),
            c["name"],
            meta={"instanceId": c["instance_id"], "card": c, "actionType": "ActionType_Play"},
        )
        for c in cards
    )
    if kind == "ActionsAvailable":
        opts += (DecisionOption("pass", "Pass"),)
    return PendingDecision(
        request_id=(1, 2),
        request_type=kind,
        prompt_text="Sacrifice a land." if kind == "SelectN" else "",
        options=opts,
    )


def drop():
    red = land(1, "R")
    green = land(2, "G")
    board = [land(3, "R"), land(4, "C")]
    return menu("ActionsAvailable", [red, green]), state(
        [red, green, card(5, "One-drop", "Creature", cost="{G}")], board
    )


def tutor():
    two = card(1, "Double source", "Land", "{T}: Add {C}{C}. This land deals 2 damage to you.")
    dual = card(2, "Dual source", "Land", "{T}: Add {R} or {G}. This land deals 1 damage to you.")
    resolving = card(
        3,
        "Land tutor",
        "Instant",
        "Search your library for a land card, put it onto the battlefield, then shuffle.",
    )
    return menu("Search", [two, dual]), state(
        [card(4, "Coloured spell", "Sorcery", cost="{R}")], [], stack=[resolving]
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr("arenamcp.card_facts.client.facts_for", lambda cards, **kwargs: {})
    monkeypatch.setattr("arenamcp.line_search_moves.facts_for", lambda cards, **kwargs: {})


@pytest.mark.parametrize("case", [drop, tutor])
def test_colour_stranding_choices_filtered_before_model_and_after_rewrites(monkeypatch, case):
    decision, board = case()
    planner = ActionPlanner(Mock())
    seen = []

    def choose(legal, _state):
        seen.append(legal.option_ids())
        return ["pick:2"]

    monkeypatch.setattr(planner, "_llm_decision_options", choose)
    monkeypatch.setattr("arenamcp.land_sequence.choose_land_sequence", lambda *a, **kw: (None, None))
    monkeypatch.setattr(
        "arenamcp.decision_consistency.consistent_action",
        lambda legal, *_a: ("pick:1", "stale") if legal.find("pick:1") else None,
    )
    assert planner.plan_decision_options(decision, board) == ["pick:2"]
    assert seen and all(legal == decision.option_ids() - {"pick:1"} for legal in seen)
    assert any("WITHHELD" in note for note in planner._land_strand_notes)
    assert "pick:1" in decision.option_ids()


@pytest.mark.parametrize("case", [drop, tutor])
@pytest.mark.parametrize("verdict", [True, None])
def test_winning_or_unassessed_position_keeps_all_choices(case, verdict):
    decision, board = case()
    filtered, _ = filter_land_choices(decision, board, lethal=lambda: verdict)
    assert filtered is decision


def test_noncombat_winning_land_activation_survives_real_board_assessment():
    decision, board = drop()
    activation = "{T}, Sacrifice this land: This land deals 2 damage to any target."
    source = land(8, "R")
    source["oracle_text"] += "\n" + activation
    board["battlefield"] = board["own_battlefield"] = [source]
    board["hand"] = [card(9, "Red spell", "Sorcery", cost="{R}")]
    board["players"][1]["life_total"] = 1
    ability = DecisionOption(
        "act",
        "Activate",
        meta={"instanceId": 8, "actionType": "ActionType_Activate", "ability_text": activation},
    )
    decision = replace(decision, options=(ability, DecisionOption("pass", "Pass")))
    filtered, notes = filter_land_choices(decision, board, lethal=lambda: _lethal_this_turn(board))
    assert filtered.option_ids() == {"pass", "act"}
    assert any("only R" in note and "life total" in note for note in notes)
    unpayable = replace(decision, options=(replace(ability, payable=False), decision.options[1]))
    assert filter_land_choices(unpayable, board)[0].option_ids() == {"pass"}


@pytest.mark.parametrize("cost", ["{2}", "{U/R}"])
def test_tutor_keeps_alternative_capacity_or_unknown_cost(cost):
    decision, board = tutor()
    board["hand"].append(card(7, "Other payoff", "Instant", cost=cost))
    assert filter_land_choices(decision, board)[0] == decision


def test_mixed_destination_search_does_not_grant_untapped_land():
    decision, board = tutor()
    board["stack"][0]["oracle_text"] = "Put one of them onto the battlefield and the rest into your hand."
    assert filter_land_choices(decision, board)[0] == decision


def test_a_fresh_record_conflicting_with_printed_mana_is_uncertain(monkeypatch):
    decision, board = drop()
    source = board["hand"][1]
    digest = hash_for_card(source)
    assert digest
    monkeypatch.setattr(
        "arenamcp.card_facts.client.facts_for",
        lambda cards, **kwargs: {
            digest: {
                "schema_version": 1,
                "rules_hash": digest,
                "name": source["name"],
                "confidence": "high",
                "type_line": source["type_line"],
                "mana": [{"mana": "{U}", "condition": ""}],
            }
        },
    )
    assert filter_land_choices(decision, board)[0] == decision


def test_multi_sacrifice_preserves_count_and_checks_real_legal_payment():
    green1, green2, red = land(1, "G"), land(2, "G"), land(3, "R")
    board = state([card(5, "Green payoff", "Instant", cost="{G}")], [green1, green2, red])
    request = replace(menu("SelectN", [green1, green2, red]), min_select=1, max_select=2)
    selected = ["pick:1", "pick:2"]
    result, why = guard_land_sacrifice(request, board, selected)
    assert len(result) == 2 and "pick:3" in result and request.selection_is_valid(result)
    assert "only G" in why
    invalid = replace(request, options=(*request.options[:2], replace(request.options[2], payable=False)))
    assert guard_land_sacrifice(invalid, board, selected) == (selected, "")
    assert guard_land_sacrifice(request, board, selected, lethal=True) == (selected, "")


def test_only_legal_sacrifice_kept():
    green = land(1, "G")
    decision = menu("SelectN", [green])
    filtered, notes = filter_land_choices(
        decision, state([card(2, "Payoff", "Instant", cost="{G}")], [green])
    )
    assert filtered is decision and any("only G" in note for note in notes)


def test_private_land_lookup_uses_only_fresh_prefetched_memory(tmp_path, monkeypatch):
    from arenamcp.card_facts import client

    source = land(6, "G")
    digest = hash_for_card(source)
    store = client.CardFactsClient(cache_dir=tmp_path, clock=lambda: 5000000)
    monkeypatch.setattr(client, "shared_client", lambda: store)
    monkeypatch.setattr(store, "_read_disk", lambda *_: pytest.fail("private lookup touched disk"))
    monkeypatch.setattr(store, "queue_hashes", lambda *_: pytest.fail("private lookup queued card hashes"))
    assert facts_for([source], memory_only=True) == {}
    record = {"rules_hash": digest}
    for stamp in (5000001, 0, float("nan"), True):
        store._memory[digest] = (record, stamp)
        assert facts_for([source], memory_only=True) == {}
    store._memory[digest] = (record, 5000000)
    assert facts_for([source], memory_only=True) == {digest: record}
    assert store._worker is None


def test_fetch_followup_requires_real_payment_and_a_matching_unused_target():
    from arenamcp.land_strand import _followups

    start = "{T}, Pay 1 life, Sacrifice this land: "
    search = "Search your library for a Forest or Mountain card, put it onto the battlefield, then shuffle."
    fetch = card(4, "Fetch", "Land", start + search)
    target = card(6, "Target", "Land — Forest")
    _, board = drop()
    targets = _followups(fetch, [], board, [target])
    assert [(c["instance_id"], c["_played_land_instance"]) for c in targets] == [(6, 4)]
    assert all(c["instance_id"] != 6 for c in _followups(fetch, [target], board, [target]))
    costly = dict(fetch, oracle_text="{2}, " + start + search)
    assert all(c["instance_id"] != 6 for c in _followups(costly, [], board, [target]))


@pytest.mark.parametrize(
    "opponent_life, extra, kept",
    [
        (1, None, True),
        (20, card(8, "Public target", "Creature"), True),
        (20, None, False),
    ],
)
def test_damage_land_drop_retains_decisive_payoff_before_activation(opponent_life, extra, kept):
    request, board = drop()
    source = board["hand"][0]
    source["oracle_text"] += "\n{T}, Sacrifice this land: This land deals 2 damage to any target."
    board["players"][1]["life_total"] = opponent_life
    if extra:
        board["battlefield"].append(dict(extra, controller_seat_id=2, owner_seat_id=2))
    result, _ = filter_land_choices(request, board, lethal=lambda: _lethal_this_turn(board))
    assert ("pick:1" in result.option_ids()) is kept


def test_a_damage_spell_in_hand_keeps_a_land_that_can_finish_the_kill():
    request, board = drop()
    board["players"][1]["life_total"] = 4
    board["hand"][0]["oracle_text"] += "\n{T}, Sacrifice this land: This land deals 2 damage to any target."
    board["hand"].append(card(9, "Other burn", "Instant", "Other burn deals 2 damage to any target.", "{R}"))
    assert "pick:1" in filter_land_choices(request, board)[0].option_ids()


@pytest.mark.parametrize(
    "target,rules",
    [
        (0, "{T}: Add {R}.\nCreatures you control have haste."),
        (0, "When this land enters, target creature can't block this turn.\n{T}: Add {R}."),
        (1, "{T}: Add {G}. Activate only if you control an artifact."),
        (1, "{T}: Add {G}.\nActivate only if you control an artifact."),
        (1, "{T}: Add {G}. Spend this mana only to cast a legendary spell."),
        (1, "{T}: Add {G}. You lose the game at the beginning of the next end step."),
        (1, "{T}: Add {G}. This land deals 1 damage to any target."),
        (1, "As this land enters, sacrifice a creature or it enters tapped.\n{T}: Add {G}."),
        (1, "As this land enters, you may pay 8 life. If you don't, it enters tapped.\n{T}: Add {G}."),
        (1, "{T}, Pay 8 life: Add {G}."),
        (1, "{T}: Add {G}. An enchantment deals 1 damage to you."),
    ],
)
def test_uncertain_rider_or_static_value_keeps_both_normal_menu_options(monkeypatch, target, rules):
    request, board = drop()
    board["players"][0]["life_total"] = 6
    board["hand"][target]["oracle_text"] = rules
    planner = ActionPlanner(Mock())
    monkeypatch.setattr("arenamcp.land_sequence.choose_land_sequence", lambda *a, **kw: (None, None))
    seen = []

    def choose(legal, state):
        seen.append(legal.option_ids())
        return ["pick:1"]

    monkeypatch.setattr(planner, "_llm_decision_options", choose)
    assert planner.plan_decision_options(request, board) == ["pick:1"]
    assert seen == [request.option_ids()]


def test_unknown_board_land_type_grant_is_not_applied_unconditionally():
    req, board = drop()
    conditional = card(
        12,
        "Conditional engine",
        "Land",
        "As long as you control an artifact, each land is a Forest in addition to its other land types.\n{T}: Add {C}.",
    )
    board["battlefield"].append(dict(conditional, owner_seat_id=2, controller_seat_id=2))
    assert filter_land_choices(req, board)[0] is req
