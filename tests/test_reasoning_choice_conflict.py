"""The reasoning-vs-choice override keeps a pick the reasoning actually describes (2026-10-19).

Mono-Red Burn under autopilot: ``reasoning_choice_conflict`` counted the chosen
card as named only by its full name, then took the FIRST other card the lead
clause mentioned. Four correct picks were replaced by the burn spell the
sentence sequenced after them (standalone.log 07:50:46, 07:53:33, 08:18:10,
09:12:14). The 2026-10-06 Tam's Resistance case and the 07:42:29 Mountain case
(the model answered idx:4, Tectonic Hazard, while reasoning about playing the
Mountain) must still be corrected.
"""

from __future__ import annotations

import dataclasses

import pytest

from arenamcp import decisions as decisions_module
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import build_pending_decision, reasoning_choice_conflict

# grpIds as the 2026-10-19 coach log submitted them (Shock's is a stand-in).
NAMES = {
    93905: "Burst Lightning",
    90492: "Slickshot Show-Off",
    91676: "Hired Claw",
    93792: "Boltwave",
    102578: "Impractical Joke",
    97423: "Lightning Strike",
    66263: "Lightning Strike",
    95628: "Fleeting Effigy",
    87323: "Tectonic Hazard",
    91668: "Emberheart Challenger",
    90001: "Shock",
    65385: "Mountain",
}


def _cast(grp_id: int, iid: int, payable: bool = True) -> dict:
    action = {"actionType": "ActionType_Cast", "grpId": grp_id, "instanceId": iid}
    if payable:
        action["hasAutoTap"] = True
    return action


def _menu(*actions: dict):
    tail = [
        {"actionType": "ActionType_Activate_Mana", "grpId": 65385, "instanceId": 279},
        {"actionType": "ActionType_Pass"},
        {"actionType": "ActionType_FloatMana"},
    ]
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [*actions, *tail],
    }
    return build_pending_decision(poll, resolve_name=NAMES.get, resolve_zone=lambda iid: "Hand")


def slickshot_window():
    # 07:50:40 menu: Cast Burst Lightning [OK], Cast Slickshot Show-Off [OK].
    return _menu(_cast(93905, 221), _cast(90492, 223))


def hired_claw_window():
    # 07:53:30 menu after the land drop.
    return _menu(
        _cast(91676, 220),
        _cast(93792, 224),
        _cast(102578, 222),
        _cast(93792, 226),
        _cast(97423, 225, payable=False),
    )


def effigy_window():
    # 08:18:04 menu after the third Mountain.
    return _menu(_cast(87323, 291), _cast(102578, 292), _cast(66263, 289), _cast(95628, 290))


def claw_before_burst_window():
    # 09:12:05 menu after the third Mountain.
    return _menu(_cast(97423, 221), _cast(91676, 223), _cast(93905, 222), _cast(91668, 224))


def mountain_window():
    # 07:42:25 menu: idx:4 Tectonic Hazard, idx:5 Play Land: Mountain.
    return _menu(
        _cast(90001, 220),
        _cast(97423, 221, payable=False),
        _cast(91668, 223, payable=False),
        _cast(102578, 222),
        _cast(87323, 225),
        {"actionType": "ActionType_Play", "grpId": 65385, "instanceId": 224},
    )


@pytest.mark.parametrize(
    ("window", "chosen", "reasoning"),
    [
        (
            slickshot_window,
            "idx:1",
            "Cast Slickshot first so the following Burst Lightning pumps it to a 3/2 flying haster alongside "
            "2 face damage, matching the best line.",
        ),
        (
            hired_claw_window,
            "idx:0",
            "Cast the creature first so remaining mana can still fire Impractical Joke at Spyglass Siren "
            "afterward, developing a body and future attack trigger.",
        ),
        (
            effigy_window,
            "idx:3",
            "Cast the hasty Effigy first so it can attack this turn alongside a follow-up Lightning Strike "
            "(5 total damage), establishing the renewable R-per-turn attacker while their board is empty.",
        ),
        (
            claw_before_burst_window,
            "idx:1",
            "Cast the creature first so the following Burst Lightning pumps Slickshot before combat, adding "
            "a second Lizard ping engine per the planned line.",
        ),
    ],
)
def test_a_pick_the_reasoning_casts_first_is_kept(window, chosen, reasoning):
    decision = window()
    assert reasoning_choice_conflict(decision, [chosen], reasoning, NAMES.get) == []


def test_reasoning_that_plays_the_mountain_still_corrects_a_tectonic_hazard_pick():
    reasoning = (
        "Playing the second Mountain enables casting Emberheart Challenger this turn for a hasty prowess "
        "attacker, developing the board per the aggro curve."
    )
    assert reasoning_choice_conflict(mountain_window(), ["idx:4"], reasoning, NAMES.get) == ["idx:5"]


def test_the_tams_resistance_case_still_follows_the_reasoning():
    names = {106272: "Undulating Witness", 106394: "Tam's Resistance", 106399: "Keeper of the Quiet Hour"}
    landcycle = {"actionType": "ActionType_Activate", "grpId": 106272, "abilityGrpId": 1}
    poll = {
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "can_pass": True,
        "actions": [
            {"actionType": "ActionType_Cast", "grpId": 106272, "instanceId": 240},
            {"actionType": "ActionType_Cast", "grpId": 106394, "instanceId": 242, "hasAutoTap": True},
            {"actionType": "ActionType_Cast", "grpId": 106399, "instanceId": 293},
            {**landcycle, "instanceId": 240},
            {**landcycle, "instanceId": 364},
            {"actionType": "ActionType_Pass"},
        ],
    }
    decision = build_pending_decision(poll, resolve_name=names.get, resolve_zone=lambda iid: "Hand")
    reasoning = (
        "Cycling Undulating Witness for {2} digs for a land drop and Arni, matching the "
        "stalled-development plan; Tam's Resistance has no creature to buff."
    )
    assert reasoning_choice_conflict(decision, ["idx:1"], reasoning, names.get) == ["idx:3", "idx:4"]


@pytest.mark.parametrize(
    "reasoning",
    [
        # A distinctive word of the other card as the verb's object still overrides.
        "Cast Slickshot to start the pump engine before any burn.",
        # A possessive object names the card too.
        "Cast Slickshot's hasty flier body first, then burn.",
    ],
)
def test_a_distinctive_name_word_as_the_object_names_that_card(reasoning):
    decision = slickshot_window()
    assert reasoning_choice_conflict(decision, ["idx:0"], reasoning, NAMES.get) == ["idx:1"]


@pytest.mark.parametrize(
    "reasoning",
    [
        "Cast it now, then Burst Lightning after combat.",
        "Cast removal on their flier before Burst Lightning.",
        # "Lightning" is shared by Burst Lightning and Lightning Strike: not a name on its own.
        "Cast Lightning first, then the creature.",
    ],
)
def test_an_object_that_is_not_one_card_keeps_the_pick(reasoning):
    decision = claw_before_burst_window()
    assert reasoning_choice_conflict(decision, ["idx:1"], reasoning, NAMES.get) == []


def test_planner_keeps_slickshot_when_the_reasoning_casts_it_first(monkeypatch):
    """07:50:46 end to end: the answer idx:1 (Slickshot) is what gets submitted."""
    decision = slickshot_window()
    decision = dataclasses.replace(
        decision,
        options=tuple(dataclasses.replace(o, payable=True) if o.meta else o for o in decision.options),
    )
    monkeypatch.setattr(
        decisions_module, "_default_card_resolver", lambda grp_id: {"name": NAMES.get(grp_id, "")}
    )
    planner = ActionPlanner.__new__(ActionPlanner)

    def answer(decision, game_state):
        planner._last_decision_reasoning = (
            "Cast Slickshot first so the following Burst Lightning pumps it to a 3/2 flying haster "
            "alongside 2 face damage, matching the best line."
        )
        return ["idx:1"]

    monkeypatch.setattr(planner, "_llm_decision_options", answer, raising=False)
    monkeypatch.setattr("arenamcp.action_planner.filter_play_options", lambda d, s: d)
    assert planner.plan_decision_options(decision, {"turn": {}}) == ["idx:1"]
