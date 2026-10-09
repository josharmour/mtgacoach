"""Two deterministic guards from the 2026-10-08 game-1 review (match 07c043d8).

17:10:23, their T12: Icy Reception was cast to "counter the resolving Fblthp
(opponent has only ~2 open mana, can't pay the {3})" with four of their six
lands untapped; they paid. The counter-unless-pay guard withholds the cast
(and the counter mode in the casting menu) when the spell's controller has
the tax open, and leaves it alone when they are short or their board is
unknown.

17:08:39, our T9: Jace went from 5 to 2 loyalty for a card into Tetsuko
Umezawa's board — Tetsuko, Geist and Traxos, three power, every one of them
unblockable — and was attacked down. The loyalty floor withholds a minus
that leaves the walker at or below the evasive power they can attack it with
next turn; the plus stays, as does the minus when the walker is moot (dead to
their next attack whatever we do).

Boards: ``tests/fra_quickdraft_states`` (replayed from Player.log).
"""

from __future__ import annotations

import logging

from tests.fra_quickdraft_states import state

from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.play_safety import (
    counter_tax_payable,
    counter_tax_payable_wasted,
    evasive_power_next_turn,
    filter_play_options,
    loyalty_minus_exposes_walker,
    opponent_open_mana,
    unsafe_play_reason,
)

ICY_COUNTER = "Counter target creature or legendary spell unless its controller pays {o3}."
ICY_SHRINK = "Target creature gets -5/-0 until end of turn."


def _icy(window: dict) -> dict:
    return next(card for card in window["hand"] if card["name"] == "Icy Reception")


def _priority_window(window: dict) -> PendingDecision:
    """The 17:10:20 ActionsAvailable request: Icy Reception payable, Twinned Vision not."""
    return PendingDecision(
        request_id=(291, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:0",
                "Cast Icy Reception",
                payable=True,
                meta={
                    "actionType": "ActionType_Cast",
                    "instanceId": 122,
                    "grpId": 106256,
                    "hasAutoTap": True,
                },
            ),
            DecisionOption(
                "idx:1",
                "Cast Twinned Vision (cannot auto-pay)",
                payable=False,
                meta={"actionType": "ActionType_Cast", "instanceId": 208, "grpId": 106300},
            ),
            DecisionOption("pass", "Pass", meta={"actionType": "ActionType_Pass"}),
        ),
        can_pass=True,
    )


def _casting_menu() -> PendingDecision:
    """The 17:10:23 CastingTimeOptions request for Icy Reception's two modes."""
    return PendingDecision(
        request_id=(292, 1),
        request_type="CastingTimeOptions",
        options=(
            DecisionOption("idx:0", f"Mode 1: {ICY_COUNTER}", meta={"choiceKind": "modal", "childIndex": 0}),
            DecisionOption("idx:1", f"Mode 2: {ICY_SHRINK}", meta={"choiceKind": "modal", "childIndex": 0}),
        ),
        can_cancel=True,
    )


def _their_lands(window: dict) -> list[dict]:
    return [c for c in window["battlefield"] if c["controller_seat_id"] == 2 and "Land" in c["card_types"]]


# --- counter unless its controller pays {N} ----------------------------------------


def test_the_logged_board_has_four_of_their_lands_open_for_the_three_tax():
    window = state("G1_T12_THEIR_MAIN1")
    assert [c["instance_id"] for c in _their_lands(window) if not c["is_tapped"]] == [298, 274, 242, 201]
    assert opponent_open_mana(window) == 4
    assert window["stack"][0]["controller_seat_id"] == 2  # Fblthp is their spell


def test_icy_reception_is_withheld_when_they_can_pay(caplog):
    window = state("G1_T12_THEIR_MAIN1")
    reason = counter_tax_payable_wasted(_icy(window), window)
    assert "pays {3}" in reason and "4 mana open" in reason
    assert "-N/-0" in reason  # the other mode is dead outside combat too
    assert unsafe_play_reason(window, _icy(window), "ActionType_Cast") == reason
    with caplog.at_level(logging.INFO, logger="arenamcp.play_safety"):
        filtered = filter_play_options(_priority_window(window), window)
    assert [o.option_id for o in filtered.options] == ["pass"]
    assert any(
        "Withholding Cast Icy Reception" in r.message and "4 mana open" in r.message for r in caplog.records
    )


def test_the_counter_mode_is_withheld_in_the_casting_menu_and_the_shrink_too(caplog):
    window = state("G1_T12_THEIR_MAIN1")
    assert counter_tax_payable(ICY_COUNTER, window)
    with caplog.at_level(logging.INFO, logger="arenamcp.play_safety"):
        filtered = filter_play_options(_casting_menu(), window)
    # Nothing left: the planner declines and the cast is cancelled.
    assert filtered.options == ()
    assert any("Mode 1" in r.message and "4 mana open" in r.message for r in caplog.records)


def test_the_counter_stays_when_they_are_short():
    window = state("G1_T12_THEIR_MAIN1")
    for land in _their_lands(window):
        land["is_tapped"] = land["instance_id"] != 298  # one Island open
    assert opponent_open_mana(window) == 1
    assert counter_tax_payable_wasted(_icy(window), window) == ""
    assert [o.option_id for o in filter_play_options(_priority_window(window), window).options] == [
        "idx:0",
        "pass",
    ]
    # The counter mode stays; the -5/-0 mode is still dead outside combat (the older rule).
    assert [o.option_id for o in filter_play_options(_casting_menu(), window).options] == ["idx:0"]


def test_the_counter_stays_when_their_board_is_unknown():
    window = state("G1_T12_THEIR_MAIN1")
    window["battlefield"] = [c for c in window["battlefield"] if c not in _their_lands(window)]
    assert opponent_open_mana(window) is None
    assert counter_tax_payable_wasted(_icy(window), window) == ""


def test_the_counter_stays_without_an_opposing_spell_on_the_stack():
    window = state("G1_T12_THEIR_MAIN1")
    window["stack"] = []
    assert counter_tax_payable(ICY_COUNTER, window) == ""


def test_their_floating_mana_counts_and_restricted_mana_does_not():
    window = state("G1_T12_THEIR_MAIN1")
    for land in _their_lands(window):
        land["is_tapped"] = True
    assert opponent_open_mana(window) == 0
    next(p for p in window["players"] if p["seat_id"] == 2)["mana_pool"] = {"Blue": 3}
    assert opponent_open_mana(window) == 3
    assert counter_tax_payable(ICY_COUNTER, window)


def test_a_card_whose_other_mode_still_matters_is_left_to_the_model():
    window = state("G1_T12_THEIR_MAIN1")
    card = {
        **_icy(window),
        "oracle_text": "Choose one —\n•Counter target spell unless its controller pays {o2}.\n•Draw two cards.",
    }
    assert counter_tax_payable_wasted(card, window) == ""


# --- the planeswalker loyalty floor ---------------------------------------------------


def _jace(window: dict) -> dict:
    return next(card for card in window["battlefield"] if card["name"] == "Jace")


def _jace_window(window: dict) -> PendingDecision:
    """The 17:08:38 request after autopilot labelled Jace's two abilities."""
    return PendingDecision(
        request_id=(222, 1),
        request_type="ActionsAvailable",
        options=(
            DecisionOption(
                "idx:5",
                "Activate: Jace [+1: Surveil 1.]",
                meta={
                    "actionType": "ActionType_Activate",
                    "instanceId": 236,
                    "grpId": 106555,
                    "abilityGrpId": 208424,
                },
            ),
            DecisionOption(
                "idx:6",
                "Activate: Jace [-3: Draw a card.]",
                meta={
                    "actionType": "ActionType_Activate",
                    "instanceId": 236,
                    "grpId": 106555,
                    "abilityGrpId": 208425,
                },
            ),
            DecisionOption("pass", "Pass", meta={"actionType": "ActionType_Pass"}),
        ),
        can_pass=True,
    )


def test_tetsuko_makes_their_three_one_power_creatures_unblockable():
    window = state("G1_T9_MAIN1")
    power, names = evasive_power_next_turn(window)
    assert power == 3
    assert set(names) == {"Traxos, Academy Guardian", "Tetsuko Umezawa, Fugitive", "Geist of Saint Thalia"}
    assert all("cant_be_blocked" not in c for c in window["battlefield"])  # the snapshot is not marked


def test_jace_minus_three_to_two_loyalty_is_withheld_the_plus_stays(caplog):
    window = state("G1_T9_MAIN1")
    jace = _jace(window)
    assert jace["counters"]["Loyalty"] == 5
    reason = loyalty_minus_exposes_walker("Activate: Jace [-3: Draw a card.]", jace, window)
    assert "Jace at 5 loyalty: -3 leaves 2" in reason and "3 evasive power" in reason
    assert loyalty_minus_exposes_walker("Activate: Jace [+1: Surveil 1.]", jace, window) == ""
    with caplog.at_level(logging.INFO, logger="arenamcp.play_safety"):
        filtered = filter_play_options(_jace_window(window), window)
    assert [o.option_id for o in filtered.options] == ["idx:5", "pass"]
    assert any("Withholding Activate: Jace [-3" in r.message for r in caplog.records)


def test_the_minus_stays_when_loyalty_stays_above_their_evasive_power():
    window = state("G1_T9_MAIN1")
    _jace(window)["counters"]["Loyalty"] = 7  # -3 leaves 4 > 3
    assert loyalty_minus_exposes_walker("Activate: Jace [-3: Draw a card.]", _jace(window), window) == ""


def test_the_minus_stays_when_their_power_can_be_blocked():
    window = state("G1_T9_MAIN1")
    window["battlefield"] = [c for c in window["battlefield"] if c["name"] != "Tetsuko Umezawa, Fugitive"]
    # Without Tetsuko only Geist and Traxos fly, and our Fatehold Chronologist-less board has no
    # flyer: two evasive power, so 5 - 3 = 2 is still within reach ...
    assert evasive_power_next_turn(window)[0] == 2
    assert loyalty_minus_exposes_walker("Activate: Jace [-3: Draw a card.]", _jace(window), window)
    # ... until a flying blocker of ours covers the air.
    window["battlefield"].append(
        {
            "instance_id": 900,
            "name": "Fatehold Chronologist",
            "controller_seat_id": 1,
            "owner_seat_id": 1,
            "type_line": "Creature — Bird Wizard",
            "card_types": ["Creature"],
            "oracle_text": "Flying",
            "keywords": ["flying"],
            "power": 1,
            "toughness": 2,
        }
    )
    assert evasive_power_next_turn(window)[0] == 0
    assert loyalty_minus_exposes_walker("Activate: Jace [-3: Draw a card.]", _jace(window), window) == ""


def test_the_minus_stays_on_their_turn_and_when_we_are_dead_anyway():
    window = state("G1_T9_MAIN1")
    window["turn"]["active_player"] = 2
    assert loyalty_minus_exposes_walker("Activate: Jace [-3: Draw a card.]", _jace(window), window) == ""
    window = state("G1_T9_MAIN1")
    next(p for p in window["players"] if p["seat_id"] == 1)["life_total"] = 2  # dead to the three unblockable
    assert loyalty_minus_exposes_walker("Activate: Jace [-3: Draw a card.]", _jace(window), window) == ""
