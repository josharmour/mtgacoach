"""#254 live 21b5c71a req241/313: three-cost activation, two paying sources.

Minimal factual regression from monitoring/21b5c71a-paycost-{candidate,request}.json.
Payment request242/315 offers349 once and418 twice as alternative abilities;
source403 must tap to activate and is not one of those payment sources.
"""

from copy import deepcopy

import pytest
from tests.test_tactical_timing_backlog import card

from arenamcp.activation_payment import activation_payment_reason


def live_case():
    source = card(
        403,
        "Dalkovan Encampment",
        "{T}: Add {R}.\n{2}{W}, {T}: Whenever you attack this turn, create two 1/1 red Warrior creature tokens that are tapped and attacking. Sacrifice them at the beginning of the next end step.",
        "Land",
    )
    meta = {
        "actionType": "ActionType_Activate",
        "instanceId": 403,
        "abilityGrpId": 188998,
        "manaCost": [{"color": '[ "Generic" ]', "count": 2}, {"color": '[ "White" ]', "count": 1}],
        "hasAutoTap": False,
        "autoTapActions": [],
    }
    state = {
        "local_seat_id": 1,
        "turn": {"turn_number": 10},
        "players": [{"seat_id": 1, "is_local": True}],
        "battlefield": [
            source,
            card(349, "Inspiring Vantage", "{T}: Add {R} or {W}.", "Land"),
            card(
                418,
                "Sunbillow Verge",
                "{T}: Add {W}.\n{T}: Add {R}. Activate only if you control a Mountain or a Plains.",
                "Land",
            ),
            card(379, "Plains", "{T}: Add {W}.", "Basic Land — Plains", is_tapped=True),
            card(358, "Mountain", "{T}: Add {R}.", "Basic Land — Mountain", is_tapped=True),
        ],
        "_bridge_actions": [
            {"instanceId": 349, "actionType": "ActionType_Activate_Mana", "abilityGrpId": 4247},
            {"instanceId": 418, "actionType": "ActionType_Activate_Mana", "abilityGrpId": 1001},
            {"instanceId": 418, "actionType": "ActionType_Activate_Mana", "abilityGrpId": 176678},
        ],
    }
    return state, source, meta


def test_recorded_activation_reserves_source_and_counts_verge_once():
    state, source, meta = live_case()
    original = deepcopy(state)
    reason = activation_payment_reason(state, source, meta)
    assert "{2}{W}" in reason and "only 2 mana" in reason and "{T} cost" in reason
    assert state == original


def test_real_autotap_solution_and_manual_pay_with_enough_sources_are_allowed():
    state, source, meta = live_case()
    assert not activation_payment_reason(state, source, {**meta, "hasAutoTap": True})
    assert not activation_payment_reason(state, source, {**meta, "autoTapSolution": {}})
    state["battlefield"][3]["is_tapped"] = False
    assert not activation_payment_reason(state, source, meta)  # null payable can have valid manual payment


def test_floating_mana_can_pay_after_source_is_reserved():
    state, source, meta = live_case()
    state["floating_mana"] = {"W": 1}
    assert not activation_payment_reason(state, source, meta)


def test_explicit_ability_text_reserves_only_its_own_tap_cost():
    state, source, meta = live_case()
    source["oracle_text"] += "\n{2}{W}: Draw a card."
    # With two non-mana abilities, the unknown tap cost cannot be assumed.
    assert not activation_payment_reason(state, source, meta)
    meta["ability_text"] = "{2}{W}, {T}: Create two tokens."
    assert "only 2 mana" in activation_payment_reason(state, source, meta)
    meta["ability_text"] = "{2}{W}: Draw a card."
    assert not activation_payment_reason(state, source, meta)


def test_printed_activation_cost_is_used_when_exact_metadata_is_missing():
    state, source, meta = live_case()
    meta.pop("manaCost")
    assert "only 2 mana" in activation_payment_reason(state, source, meta)
    # The land's printed casting cost is never the activation price.
    source["mana_cost"] = "{0}"
    assert "{2}{W}" in activation_payment_reason(state, source, meta)


@pytest.mark.parametrize(
    "text",
    [
        "{T}: Add an amount of {G} equal to your devotion to green.",
        "Activated abilities you activate cost {1} less to activate.",
        "{T}: Add {C}. Spend this mana only to activate abilities.",
    ],
)
def test_unknown_variable_reducer_or_restricted_mana_does_not_prove_shortfall(text):
    state, source, meta = live_case()
    state["battlefield"].append(card(500, "Complicated mana", text, "Artifact"))
    assert not activation_payment_reason(state, source, meta)


def test_actual_metadata_price_wins_over_printed_cost():
    state, source, meta = live_case()
    meta["manaCost"] = [
        {"color": ["ManaColor_Generic"], "count": 1},
        {"color": ["ManaColor_White"], "count": 1},
    ]
    assert not activation_payment_reason(state, source, meta)


def test_variable_or_unknown_payment_cost_abstains():
    state, source, meta = live_case()
    for raw in (
        [{"color": ["X"], "count": 1}],
        [{"color": ["Unrecognized"], "count": 4}],
        [{"color": ["White"], "count": "3"}],
    ):
        assert not activation_payment_reason(state, source, {**meta, "manaCost": raw})


def test_typed_prefilter_withholds_before_payment_but_keeps_affordable_manual_activation():
    from tests.test_tactical_timing_backlog import menu

    from arenamcp.decisions import DecisionOption
    from arenamcp.play_safety import filter_play_options, unsafe_play_reason

    state, source, meta = live_case()
    decision = menu(DecisionOption("idx:1", "Activate Dalkovan", None, meta))
    assert "only 2 mana" in unsafe_play_reason(state, source, "ActionType_Activate", meta)
    assert [o.option_id for o in filter_play_options(decision, state).options] == ["pass"]
    state["battlefield"][3]["is_tapped"] = False
    assert [o.option_id for o in filter_play_options(decision, state).options] == ["idx:1", "pass"]
