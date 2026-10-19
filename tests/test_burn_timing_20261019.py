"""Burn timing: the #80 hold re-scoped and its release at their end step (ledger #80 #136 #137 #139 #140).

Mono-Red Burn, Standard Ranked, 2026-10-19 12:43-13:45 (b720163). Boards are
read from the matches' Player.log (scratchpad audit ge91/matches, gd9a) and
the coach log; card text is the MTGA database's (tests/race_boards_20261019).

* #137: five opponent end-step windows offered a held instant and all five
  passed: 56fc394b T7/T9/T11 (13:05-13:08, the model down: "outside our main
  phase only a land drop or a pass is safe") and 59064e41 T2 (13:11:06, the
  model: "hold it as removal for the creature their Lavaspur Boots
  telegraph"). Instant burn at the face fell from 63% to 12% of casts.
* #136: e917434f T1 13:18:48, Tectonic Hazard at an empty board, tagged
  "[LINE best]" because it beat holding by its own 1 face damage.
* #139: d9a33232 T4 13:28:25, Boltwave on the stack, Shock at a 3-loyalty Jace
  with the mana the line kept for Hired Claw.
* #140: d9a33232 T6 13:28:43, Shock cast in our upkeep.
"""

from __future__ import annotations

import logging

import pytest
from tests.race_boards_20261019 import CARDS
from tests.test_decision_layer_20261019 import _Model, _planner, _reply
from tests.test_llm_down_fallback import _Backend

from arenamcp.action_planner import ActionPlanner
from arenamcp.backends.proxy import BackendError
from arenamcp.board_assessment import assess
from arenamcp.burn_timing import (
    HoldFates,
    end_step_release,
    hold_overrides,
    line_gain_is_face_only,
    own_stack_line_waits,
    two_turn_reach,
)
from arenamcp.decision_memory import burn_kills_nothing
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot, build_pending_decision
from arenamcp.line_guard import option_note

IROH = {
    "type_line": "Sorcery — Lesson",
    "mana_cost": "{1}{R}",
    "card_types": ["CardType_Sorcery"],
    "oracle_text": "Choose one —\n• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "• Iroh's Demonstration deals 4 damage to target creature.",
}
CHANNELER = {
    "type_line": "Creature — Bat Cleric",
    "mana_cost": "{1}{W}",
    "card_types": ["CardType_Creature"],
    "power": 2,
    "toughness": 1,
    "oracle_text": "As long as you've lost life this turn, this creature has flying and vigilance.\n"
    "Whenever you gain life, put a +1/+1 counter on this creature.\n"
    "When this creature dies, put its counters on target creature you control.",
}
JACE = {
    "type_line": "Legendary Planeswalker — Jace",
    "card_types": ["CardType_Planeswalker"],
    "oracle_text": "+1: Draw a card, then discard a card.\n−2: Return target creature to its owner's hand.",
}
BOOTS = {
    "type_line": "Artifact — Equipment",
    "card_types": ["CardType_Artifact"],
    "oracle_text": "Equipped creature gets +1/+0 and has haste and ward {1}.\nEquip {1}",
}
ENCHANTMENT = {"type_line": "Enchantment", "card_types": ["CardType_Enchantment"], "oracle_text": ""}
LAND = {
    "type_line": "Basic Land — Mountain",
    "card_types": ["CardType_Land"],
    "oracle_text": "({T}: Add {R}.)",
}


def _card(iid, name, seat, **fields):
    base = dict(CARDS.get(name, {}))
    base.update(fields)
    return {"instance_id": iid, "grp_id": 90000 + iid, "name": name, "controller_seat_id": seat,
            "owner_seat_id": seat, **base}  # fmt: skip


def _land(iid, seat, tapped=False, name="Mountain"):
    return _card(iid, name, seat, is_tapped=tapped, **LAND)


def _board(
    local, turn, active, phase, step, *, ours, theirs, hand, life, their_life, lands_played=0, stack=()
):
    opp = 2 if local == 1 else 1
    return {
        "match_id": "ladder-20261019",
        "local_seat_id": local,
        "turn": {
            "turn_number": turn,
            "active_player": active,
            "priority_player": local,
            "phase": phase,
            "step": step,
        },  # fmt: skip
        "players": [
            {"seat_id": local, "life_total": life, "is_local": True, "lands_played": lands_played},
            {"seat_id": opp, "life_total": their_life, "is_local": False},
        ],
        "battlefield": [*ours, *theirs],
        "hand": list(hand),
        "graveyard": [],
        "exile": [],
        "stack": list(stack),
    }


def _menu(state, *casts, special=()):
    """Arena's ActionsAvailableReq for these hand cards: (instance id, payable)."""
    by_id = {card["instance_id"]: card for card in state["hand"]}
    actions = [
        {
            "actionType": "ActionType_Cast",
            "grpId": by_id[iid]["grp_id"],
            "instanceId": iid,
            "hasAutoTap": payable,
        }
        for iid, payable in casts
    ]
    actions += [{"actionType": "ActionType_Special", "grpId": by_id[iid]["grp_id"], "instanceId": iid,
                 "abilityGrpId": 328} for iid in special]  # fmt: skip
    actions += [
        {"actionType": "ActionType_Activate_Mana", "instanceId": 1},
        {"actionType": "ActionType_Pass"},
    ]
    names = {card["grp_id"]: card["name"] for card in state["hand"]}
    return build_pending_decision(
        {"has_pending": True, "request_type": "ActionsAvailable", "can_pass": True, "actions": actions},
        resolve_name=lambda grp: names.get(grp, ""),
    )


def _option(decision, name_or_iid):
    return next(
        o.option_id
        for o in decision.options
        if o.meta.get("instanceId") == name_or_iid or o.label == f"Cast {name_or_iid}"
    )


def _targets(source, ids):
    return PendingDecision(
        request_id=(301, 402),
        request_type="SelectTargets",
        options=tuple(
            DecisionOption(f"tgt:{iid}", f"Target #{iid}", meta={"instanceId": iid}) for iid in ids
        ),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label=source,
        slots=(TargetSlot(1, 1, 1, 0, tuple(ids)),),
    )


def _down() -> ActionPlanner:
    """The model server is down: every call fails at once (13:05-13:08 Cloudflare outage)."""
    return ActionPlanner(_Backend(error=BackendError("Request timed out.")), timeout=5, land_drop_first=False)


# --- #137: 59064e41 (we are seat 1). T1: Mountain, Shock held. T2 end step: their Island and Lavaspur
# Boots, no creature; one Mountain open; Shock castable, Lightning Strike not.


def _59064e41_hand():
    return [_card(168, "Mountain", 1, **LAND), _card(167, "Iroh's Demonstration", 1, **IROH),
            _card(166, "Mountain", 1, **LAND), _card(165, "Lightning Strike", 1), _card(164, "Iroh's Demonstration", 1, **IROH),
            _card(162, "Shock", 1)]  # fmt: skip


def _59064e41_t1():
    return _board(1, 1, 1, "Phase_Main1", "", ours=[_land(282, 1)], theirs=[], hand=_59064e41_hand(),
                  life=20, their_life=20, lands_played=1)  # fmt: skip


def _59064e41_t2_end():
    theirs = [_card(285, "Lavaspur Boots", 2, **BOOTS), _land(284, 2, True, "Island")]
    return _board(1, 2, 2, "Phase_Ending", "Step_End", ours=[_land(282, 1)], theirs=theirs,
                  hand=_59064e41_hand(), life=20, their_life=20)  # fmt: skip


@pytest.mark.parametrize("model_down", [False, True], ids=["model-passes", "model-down"])
def test_59064e41_the_held_shock_goes_at_their_face_at_their_end_step(model_down, caplog):
    if model_down:
        planner = _down()
        backend = planner._backend
    else:
        model = _Model(_reply("pass", "No targets worth Shock right now; hold it as removal."))
        planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp"):
        t1 = _59064e41_t1()
        menu = _menu(t1, (162, True), (165, False), (167, False), (164, False))
        assert planner.plan_decision_options(menu, t1) == ["pass"]  # T1: the #80 hold, no model
        end = _59064e41_t2_end()
        menu = _menu(end, (162, True), (165, False))
        assert planner.plan_decision_options(menu, end) == [_option(menu, 162)]
        assert planner.get_decision_reasoning([_option(menu, 162)]).startswith(
            "End-step release: Shock at their face"
        )
        end["stack"] = [_card(301, "Shock", 1)]
        assert planner.plan_decision_options(_targets("Shock", [1, 2]), end) == ["tgt:2"]
    if model_down:
        assert backend.calls == []
    else:
        assert model.efforts == []  # neither window asked the model
    assert (
        "hold-fate: Shock (held T1: face damage only in our main phase) -> released at end step at face"
        in (caplog.text)
    )


# --- #137: 56fc394b T7 end step (13:06:17, model down): one Mountain open, Burst Lightning and
# Tectonic Hazard in hand; their Essence Channeler is 3/2 (a +1/+1 counter). The line for our T8:
# "Burst Lightning (on Essence Channeler) + Tectonic Hazard".


def _56fc394b_t7_end(**channeler):
    ours = [_land(375, 1), _land(350, 1, True), _land(342, 1, True), _card(343, "Hired Claw", 1,
            turn_entered_battlefield=2)]  # fmt: skip
    theirs = [
        _card(366, "Essence Channeler", 2, **{**CHANNELER, "power": 3, "toughness": 2, "counters": {"P1P1": 1},
              **channeler}),
        _card(380, "Trapped in the Screen", 2, **ENCHANTMENT),
        _land(361, 2, True, "Abandoned Air Temple"), _land(346, 2, True, "Plains"), _land(340, 2, True, "Plains"),
    ]  # fmt: skip
    hand = [_card(285, "Tectonic Hazard", 1), _card(284, "Burst Lightning", 1)]
    return _board(1, 7, 2, "Phase_Ending", "Step_End", ours=ours, theirs=theirs, hand=hand, life=20,
                  their_life=17)  # fmt: skip


def test_56fc394b_burst_kills_the_channeler_the_line_aims_it_at_with_the_model_down(caplog):
    planner = _down()
    state = _56fc394b_t7_end()
    menu = _menu(state, (284, True))
    with caplog.at_level(logging.INFO, logger="arenamcp"):
        assert planner.plan_decision_options(menu, state) == [_option(menu, 284)]
        state["stack"] = [_card(376, "Burst Lightning", 1)]
        assert planner.plan_decision_options(_targets("Burst Lightning", [1, 2, 343, 366]), state) == [
            "tgt:366"
        ]
    assert planner._backend.calls == []
    assert "End-step release: casting Burst Lightning at Essence Channeler" in caplog.text


def test_burn_the_line_aims_at_a_creature_it_does_not_kill_now_stays_in_hand():
    # A 3/3 Channeler (two counters): Burst's 2 kills it only with Hazard's 1 on our turn.
    state = _56fc394b_t7_end(power=4, toughness=3, counters={"P1P1": 2})
    plan = end_step_release(_menu(state, (284, True)), state)
    # The line: "Burst Lightning (on Essence Channeler) + Tectonic Hazard" on our T8.
    assert plan is not None and plan.pick is None
    assert [(name, why) for _oid, name, why in plan.kept] == [
        ("Burst Lightning", "the searched line aims it at Essence Channeler on our next turn")
    ]
    planner = _down()
    assert planner.plan_decision_options(_menu(state, (284, True)), state) == ["pass"]
    assert planner.get_decision_reasoning(["pass"]).startswith("Keeping Burst Lightning")


def test_a_pump_creature_does_not_keep_the_release_and_lethal_face_damage_beats_every_keep():
    # Slickshot Show-Off on our side: a keep for its pump would wait at every end step while it lives.
    ours = [_land(1, 2), _land(2, 2), _card(10, "Slickshot Show-Off", 2, turn_entered_battlefield=3)]
    state = _board(2, 4, 1, "Phase_Ending", "Step_End", ours=ours, theirs=[_land(50, 1, True, "Plains")],
                   hand=[_card(20, "Shock", 2)], life=20, their_life=15)  # fmt: skip
    plan = end_step_release(_menu(state, (20, True)), state)
    assert plan.pick is not None and (plan.pick.name, plan.pick.target, plan.kept) == (
        "Shock",
        "opponent",
        [],
    )
    # Face damage that is lethal now goes, whatever the line keeps the spell for (a 1-toughness kill here).
    state["battlefield"].append(_card(60, "Grizzly Bears", 1, power=1, toughness=1))
    assert end_step_release(_menu(state, (20, True)), state).pick.target == "Grizzly Bears"  # the line's kill
    state["players"][1]["life_total"] = 2
    plan = end_step_release(_menu(state, (20, True)), state)
    assert (plan.pick.target, plan.pick.why) == ("opponent", "2 face damage now is lethal (they are at 2)")


def test_the_most_face_damage_the_mana_pays_for_goes_first():
    hand = [_card(20, "Shock", 2), _card(21, "Lightning Strike", 2), _card(22, "Burst Lightning", 2)]
    state = _board(2, 6, 1, "Phase_Ending", "Step_End", ours=[_land(1, 2), _land(2, 2)],
                   theirs=[_land(50, 1, True, "Plains")], hand=hand, life=20, their_life=16)  # fmt: skip
    menu = _menu(state, (20, True), (21, True), (22, True))
    plan = end_step_release(menu, state)
    # Two mana: Shock + Burst (4) beats Lightning Strike (3).
    assert plan.pick.name in ("Shock", "Burst Lightning") and plan.face_total == 4


def test_no_release_outside_their_end_step_or_with_a_stack():
    state = _59064e41_t2_end()
    menu = _menu(state, (162, True))
    assert end_step_release(menu, state).pick.name == "Shock"
    for change in (
        {"phase": "Phase_Main2", "step": ""},
        {"phase": "Phase_Combat", "step": "Step_BeginCombat"},
    ):
        moved = _59064e41_t2_end()
        moved["turn"].update(change)
        assert end_step_release(menu, moved) is None
    stacked = _59064e41_t2_end()
    stacked["stack"] = [_card(400, "Opt", 2, type_line="Instant", card_types=["CardType_Instant"])]
    assert end_step_release(menu, stacked) is None
    ours = _59064e41_t2_end()
    ours["turn"]["active_player"] = 1  # our own end step
    assert end_step_release(menu, ours) is None
    # Arena's autotap must confirm the cast.
    assert end_step_release(_menu(state, (162, False)), state) is None


# --- hold fates --------------------------------------------------------------------------------------


def test_a_hold_that_their_end_step_never_releases_is_logged_stranded(caplog):
    # 7cfcbde6: Burst Lightning held T4 and T6, never cast; Arena offered no end-step window.
    planner = _down()
    t1 = _59064e41_t1()
    with caplog.at_level(logging.INFO, logger="arenamcp"):
        planner.plan_decision_options(_menu(t1, (162, True)), t1)
        t3 = _59064e41_t1()
        t3["turn"]["turn_number"] = 3
        t3["battlefield"].append(_land(288, 1, True))
        planner.plan_decision_options(_menu(t3, (162, True)), t3)
    assert "hold-fate: Shock (held T1: face damage only in our main phase) -> stranded (our turn 3 began" in (
        caplog.text
    )


def test_a_held_spell_cast_later_at_a_creature_is_used_as_removal(caplog):
    fates = HoldFates()
    state = _59064e41_t1()
    fates.held(state, ["Shock"], "face damage only in our main phase")
    later = _59064e41_t1()
    later["battlefield"].append(_card(307, "Iron Lad, Diverging Destiny", 2, type_line="Creature — Human",
                                      card_types=["CardType_Creature"], power=2, toughness=2))  # fmt: skip
    menu = _menu(later, (162, True))
    with caplog.at_level(logging.INFO, logger="arenamcp.burn_timing"):
        fates.observe(menu, later, [_option(menu, 162)])
        fates.observe(_targets("Shock", [2, 307]), later, ["tgt:307"])
    assert "-> used as removal on Iron Lad, Diverging Destiny on T1 (our main1)" in caplog.text


# --- #80 re-scoped ----------------------------------------------------------------------------------


def _strike_main(*, ours=(), hand=(), lands=2, their_life=17, **turn):
    board = _board(2, 6, 2, turn.pop("phase", "Phase_Main1"), turn.pop("step", ""),
                   ours=[*(_land(300 + i, 2) for i in range(lands)), *ours],
                   theirs=[_land(401, 1, True, "Swamp")],
                   hand=[_card(281, "Lightning Strike", 2), _card(295, "Shock", 2), *hand],
                   life=20, their_life=their_life, lands_played=1)  # fmt: skip
    return board


def test_after_combat_a_pump_creature_no_longer_stops_the_hold():
    # Slickshot already attacked (tapped) in main 2: the +2/+0 buys nothing now.
    slick = _card(10, "Slickshot Show-Off", 2, is_tapped=True, turn_entered_battlefield=4)
    state = _strike_main(ours=[slick], phase="Phase_Main2")
    menu = _menu(state, (281, True), (295, True))
    planner = _down()
    assert planner.plan_decision_options(menu, state) == ["pass"]
    assert planner.get_decision_reasoning(["pass"]).startswith(
        "Holding Lightning Strike, Shock: instant burn"
    )
    # Before combat it pumps the attack: no hold (the model decides).
    untapped = _strike_main(ours=[{**slick, "is_tapped": False}])
    assert hold_overrides(menu, untapped, []).startswith("a cast now pumps Slickshot Show-Off")


def test_their_life_within_reach_over_two_turns_stops_the_hold():
    claw = _card(10, "Hired Claw", 2, is_tapped=True, turn_entered_battlefield=2)
    state = _strike_main(ours=[claw], phase="Phase_Main2", their_life=6)
    assert two_turn_reach(state).startswith("up to 6 damage this turn and next")
    model = _Model(_reply("idx:0", "Strike face: they are at 6."))
    planner = _planner(model)
    planner.plan_decision_options(_menu(state, (281, True), (295, True)), state)
    assert model.efforts  # asked, with the burn on the menu
    assert "Cast Lightning Strike" in model.prompts[0][model.prompts[0].index("OPTIONS:") :]
    assert two_turn_reach(_strike_main(ours=[claw], phase="Phase_Main2", their_life=7)) == ""


def test_more_mana_than_the_end_step_can_spend_stops_the_hold():
    # Four Mountains open, Shock the only spell: casting it now still leaves three for their turn.
    state = _board(2, 8, 2, "Phase_Main1", "", ours=[_land(300 + i, 2) for i in range(4)],
                   theirs=[_land(401, 1, True, "Swamp")], hand=[_card(295, "Shock", 2)],
                   life=20, their_life=17, lands_played=1)  # fmt: skip
    menu = _menu(state, (295, True))
    held = [(menu.options[0], state["hand"][0])]
    assert hold_overrides(menu, state, held).startswith("we have more mana than the end step can spend")
    one_land = _board(2, 8, 2, "Phase_Main1", "", ours=[_land(300, 2)], theirs=[_land(401, 1, True, "Swamp")],
                      hand=[_card(295, "Shock", 2)], life=20, their_life=17, lands_played=1)  # fmt: skip
    assert hold_overrides(menu, one_land, held) == ""


# --- #136: e917434f T1 (we are seat 2, on the play): one Mountain, an empty board. --------------------


def _e917434f_t1(**changes):
    hand = [_card(225, "Burst Lightning", 2), _card(224, "Mountain", 2, **LAND), _card(223, "Lightning Strike", 2),
            _card(222, "Tectonic Hazard", 2), _card(221, "Shock", 2), _card(219, "Slickshot Show-Off", 2)]  # fmt: skip
    state = _board(2, 1, 2, "Phase_Main1", "", ours=[_land(279, 2)], theirs=[], hand=hand, life=20,
                   their_life=20, lands_played=1)  # fmt: skip
    state.update(changes)
    return state


def _e917434f_menu(state):
    return _menu(state, (219, False), (221, True), (222, True), (223, False), (225, True), special=(219,))


def test_e917434f_hazard_waits_with_the_instants_and_the_prompt_says_why():
    state = _e917434f_t1()
    menu = _e917434f_menu(state)
    model = _Model(_reply("pass", "Nothing to do with one Mountain."))
    planner = _planner(model)
    assert planner.plan_decision_options(menu, state) == ["pass"]
    prompt = model.prompts[0]
    options = prompt[prompt.index("OPTIONS:") : prompt.index("GAME STATE:")]
    assert "Tectonic Hazard" not in options and "Cast Shock" not in options and "Burst" not in options
    assert "SWEEPER TIMING: Tectonic Hazard are held this window" in prompt
    assert "INSTANT TIMING: Shock, Burst Lightning are held this window" in prompt


def test_hazard_that_pumps_an_attacker_now_is_not_held():
    slick = _card(10, "Slickshot Show-Off", 2, turn_entered_battlefield=1)
    state = _e917434f_t1()
    state["battlefield"].append(slick)
    model = _Model(_reply("idx:2", "Hazard pumps Slickshot +2."))
    planner = _planner(model)
    planner.plan_decision_options(_e917434f_menu(state), state)
    assert "Cast Tectonic Hazard" in model.prompts[0][model.prompts[0].index("OPTIONS:") :]


def test_a_line_best_tag_won_only_by_the_spells_own_face_damage_keeps_the_face_burn_concern(monkeypatch):
    state = _e917434f_t1()
    result = assess(state).line_search
    menu = _e917434f_menu(state)
    hazard = _option(menu, 222)
    assert option_note(result, menu.find(hazard), state).startswith("[LINE best")
    # The best line ends with them at 9, holding everything at 10: Hazard's own 1 face damage.
    assert line_gain_is_face_only(result, 1)
    planner = _planner(_Model("{}"))
    from arenamcp import action_planner as planner_module

    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda s: False)
    tags = {hazard: option_note(result, menu.find(hazard), state)}
    concerns = planner._second_look_concerns(menu, state, [hazard], "Chip them for 1.", result, tags)
    assert [kind for kind, _ in concerns] == ["face_burn"]
    assert "rates it best only by its own 1 face damage over holding it" in concerns[0][1]


# --- #139: d9a33232 T4 (we are seat 2): Boltwave on the stack, one Mountain open, Shock x2 castable,
# Hired Claw and Slickshot Show-Off in hand; their Jace at 3 loyalty.


def _d9a33232_t4(loyalty=3):
    theirs = [_card(357, "Jace", 1, **JACE, counters={"Loyalty": loyalty}),
              _card(353, "Way of the Pyromancer", 1, **ENCHANTMENT), _land(352, 1, True), _land(342, 1, True)]  # fmt: skip
    hand = [_card(359, "Shock", 2), _card(343, "Mountain", 2, **LAND), _card(227, "Hired Claw", 2),
            _card(224, "Slickshot Show-Off", 2), _card(223, "Shock", 2)]  # fmt: skip
    return _board(2, 4, 2, "Phase_Main1", "", ours=[_land(360, 2), _land(344, 2, True)], theirs=theirs,
                  hand=hand, life=20, their_life=20, lands_played=1,
                  stack=[_card(361, "Boltwave", 2)])  # fmt: skip


def test_burn_short_of_a_walkers_loyalty_kills_nothing():
    shock = CARDS["Shock"]["oracle_text"]
    assert burn_kills_nothing(shock, _d9a33232_t4(3), castable=_card(223, "Shock", 2)) is True
    assert burn_kills_nothing(shock, _d9a33232_t4(2), castable=_card(223, "Shock", 2)) is False
    unknown = _d9a33232_t4()
    jace = next(card for card in unknown["battlefield"] if card["name"] == "Jace")
    jace["counters"] = {}  # unknown loyalty: it may die, as before
    assert burn_kills_nothing(shock, unknown, castable=_card(223, "Shock", 2)) is False


@pytest.mark.parametrize("loyalty", [3, 2], ids=["kills-nothing", "kills-jace"])
def test_d9a33232_with_our_boltwave_on_the_stack_the_line_casts_hired_claw_next(loyalty, caplog):
    state = _d9a33232_t4(loyalty)
    wait = own_stack_line_waits(state)
    assert (
        wait is not None
        and wait.casts == ("Hired Claw",)
        and wait.need == 1
        and wait.pending == ("Boltwave",)
    )
    menu = _menu(state, (223, True), (359, True))
    result = assess(state).line_search
    assert option_note(result, menu.find("pass"), state).startswith("[LINE best")
    assert option_note(result, menu.find(_option(menu, 223)), state) == ""
    model = _Model(_reply(_option(menu, 223), "Chip Jace down to 1 loyalty."))
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(menu, state) == ["pass"]
    assert model.efforts == []
    if loyalty == 2:  # Shock kills Jace, so no #80 hold: the line's Hired Claw keeps it
        assert planner.get_decision_reasoning(["pass"]) == (
            "Holding Shock: once our Boltwave resolves, the searched line casts Hired Claw, which it would "
            "leave unpaid."
        )
        assert "Stack timing: holding Shock until our Boltwave resolves" in caplog.text


# --- #140: d9a33232 T6 upkeep (13:28:43): two Mountains open, Shock castable, Jace at 2. The coach's
# snapshot had cleared the stack at our untap ("Cleared 1 stale stack entries (forced)").


def _d9a33232_t6_upkeep(**changes):
    theirs = [_card(357, "Jace", 1, **JACE, counters={"Loyalty": 2}),
              _card(353, "Way of the Pyromancer", 1, **ENCHANTMENT), _land(368, 1), _land(352, 1),
              _land(342, 1, True)]  # fmt: skip
    hand = [_card(359, "Shock", 2), _card(343, "Mountain", 2, **LAND), _card(227, "Hired Claw", 2),
            _card(224, "Slickshot Show-Off", 2)]  # fmt: skip
    state = _board(2, 6, 2, "Phase_Beginning", "Step_Upkeep", ours=[_land(360, 2), _land(344, 2)],
                   theirs=theirs, hand=hand, life=18, their_life=17)  # fmt: skip
    state.update(changes)
    return state


@pytest.mark.parametrize("step", ["Step_Upkeep", "Step_Draw"])
def test_d9a33232_shock_waits_for_our_main_phase_without_the_model(step):
    state = _d9a33232_t6_upkeep()
    state["turn"]["step"] = step
    planner = _down()
    assert planner.plan_decision_options(_menu(state, (359, True)), state) == ["pass"]
    assert planner._backend.calls == []
    assert planner.get_decision_reasoning(["pass"]).startswith("Holding Shock for our main phase")


def test_in_our_upkeep_their_stack_or_lethal_burn_is_not_held():
    trigger = {"instance_id": 376, "name": "Command the Stage", "controller_seat_id": 1, "owner_seat_id": 1,
               "type_line": "Ability"}  # fmt: skip
    for state in (_d9a33232_t6_upkeep(stack=[trigger]), _d9a33232_t6_upkeep()):
        if not state["stack"]:
            state["players"][1]["life_total"] = 2
        model = _Model(_reply("pass", "Let it resolve."))
        planner = _planner(model)
        planner.plan_decision_options(_menu(state, (359, True)), state)
        assert model.efforts, state["stack"]
        assert "Cast Shock" in model.prompts[0][model.prompts[0].index("OPTIONS:") :]


# --- #153: f5bc17dc T4 (we are seat 2). Their end step 14:02:30: one Mountain open, Burst Lightning x2
# and Shock held since T1/T3. The model had passed at their main 1 ("saving the single red mana for my
# own curve-out next turn"), and the reversal check re-asked the end-step Burst into another pass.


def _f5bc17dc_t4(phase="Phase_Ending", step="Step_End"):
    theirs = [_card(296, "Doom Reigns Supreme", 1, **ENCHANTMENT), _land(295, 1, True, "Island"),
              _land(288, 1, True, "Swamp")]  # fmt: skip
    hand = [_card(289, "Mountain", 2, **LAND), _card(227, "Burst Lightning", 2), _card(226, "Shock", 2),
            _card(223, "Burst Lightning", 2)]  # fmt: skip
    return _board(2, 4, 1, phase, step, ours=[_land(290, 2), _land(283, 2, True)], theirs=theirs, hand=hand,
                  life=20, their_life=14)  # fmt: skip


def test_f5bc17dc_the_end_step_release_never_reaches_the_reversal_check():
    model = _Model(_reply("pass", "Board unchanged; saving the single red mana for my curve-out next turn."))
    planner = _planner(model)
    main = _f5bc17dc_t4("Phase_Main1", "")
    menu = _menu(main, (223, True), (226, True), (227, True))
    assert planner.plan_decision_options(menu, main) == ["pass"]  # their main 1: the model's pass
    end = _f5bc17dc_t4()
    menu = _menu(end, (223, True), (226, True), (227, True))
    picked = planner.plan_decision_options(menu, end)
    assert picked in ([_option(menu, 223)], [_option(menu, 226)], [_option(menu, 227)])
    assert len(model.efforts) == 1  # the end step asked nothing
    assert "reversal_check" not in planner.get_last_decision_trace()


def test_an_instant_at_their_end_step_never_reverses_an_earlier_pass():
    from arenamcp.decision_memory import DecisionMemory

    memory = DecisionMemory()
    combat = _f5bc17dc_t4("Phase_Combat", "Step_BeginCombat")
    menu = _menu(combat, (223, True), (226, True))
    memory.record(combat, menu, ["pass"], "saving the single red mana for my own curve-out next turn")
    end = _f5bc17dc_t4()
    assert memory.reversal(end, _menu(end, (223, True), (226, True)), [_option(menu, 223)]) is None
    # Earlier in their turn the same cast still reverses the pass (nothing changed).
    later = _f5bc17dc_t4("Phase_Main2", "")
    assert memory.reversal(later, _menu(later, (223, True), (226, True)), [_option(menu, 223)]) is not None


def test_the_reversal_check_says_mana_is_never_saved_for_later():
    # A reversal that does reach the model (their main 2 here) states the rule the 14:02:30 re-ask broke.
    model = _Model([_reply("pass", "Saving the red mana for next turn."), _reply("idx:0", "Shock face.")])
    planner = _planner(model)
    main = _f5bc17dc_t4("Phase_Main1", "")
    planner.plan_decision_options(_menu(main, (226, True)), main)
    later = _f5bc17dc_t4("Phase_Main2", "")
    planner.plan_decision_options(_menu(later, (226, True)), later)
    assert any(
        "Mana is never saved for later: unused mana empties at the end of each step" in prompt
        for prompt in model.prompts
    )


# --- #140 in our combat: 633f56bd T7 (we are seat 1), our declare-blockers step 13:58:43. Emberheart
# Challenger attacks; their only creature, Leonin Vanguard (2/1 with Sheltered by Ghosts: ward {2}), is
# tapped; two Mountains open, Lightning Strike in hand (held at 13:58:33). Strike went face at 13:59:03.

SHELTERED = {
    "type_line": "Enchantment — Aura",
    "card_types": ["CardType_Enchantment"],
    "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an "
    "opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has "
    "lifelink and ward {2}.",
}
VANGUARD = {
    "type_line": "Creature — Cat Soldier",
    "card_types": ["CardType_Creature"],
    "mana_cost": "{W}",
    "power": 2,
    "toughness": 1,
    "oracle_text": "At the beginning of combat on your turn, if you control three or more creatures, this "
    "creature gets +1/+1 until end of turn and you gain 1 life.",
}


def _633f56bd_t7(step="Step_DeclareBlock", **vanguard):
    ours = [_card(321, "Emberheart Challenger", 1, is_tapped=True, is_attacking=True, turn_entered_battlefield=7),
            _land(320, 1), _land(302, 1), _land(290, 1, True), _land(281, 1, True)]  # fmt: skip
    theirs = [_card(296, "Leonin Vanguard", 2, **{**VANGUARD, "is_tapped": True, **vanguard}),
              _card(314, "Sheltered by Ghosts", 2, attached_to_id=296, **SHELTERED),
              _card(280, "Leyline of Hope", 2, **ENCHANTMENT), _land(295, 2, True, "Plains"),
              _land(286, 2, True, "Plains")]  # fmt: skip
    return _board(1, 7, 1, "Phase_Combat", step, ours=ours, theirs=theirs,
                  hand=[_card(301, "Lightning Strike", 1)], life=17, their_life=18)  # fmt: skip


def test_633f56bd_strike_waits_for_their_end_step_in_our_combat(caplog):
    state = _633f56bd_t7()
    model = _Model(_reply("idx:0", "Lightning Strike to the face for 3 is pure clock progress."))
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_menu(state, (301, True)), state) == ["pass"]
    assert model.efforts == []
    assert planner.get_decision_reasoning(["pass"]).startswith(
        "Holding Lightning Strike: it kills no creature that matters in this combat"
    )
    assert "Instant timing: holding Lightning Strike for the opponent's end step (our combat" in caplog.text


@pytest.mark.parametrize(
    "case",
    ["a blocker it kills", "a potential blocker before blocks", "lethal", "their trick on the stack"],
)
def test_in_our_combat_burn_that_matters_now_is_not_held(case):
    if case == "a blocker it kills":
        state = _633f56bd_t7(is_tapped=False, is_blocking=True)
        state["battlefield"] = [c for c in state["battlefield"] if c["name"] != "Sheltered by Ghosts"]
    elif case == "a potential blocker before blocks":
        state = _633f56bd_t7("Step_DeclareAttack", is_tapped=False)
        state["battlefield"] = [c for c in state["battlefield"] if c["name"] != "Sheltered by Ghosts"]
    elif case == "lethal":
        state = _633f56bd_t7()
        state["players"][1]["life_total"] = 6  # Emberheart 2 + prowess 1 + Strike 3
    else:
        state = _633f56bd_t7()
        state["stack"] = [
            _card(500, "Feat of Resistance", 2, type_line="Instant", card_types=["CardType_Instant"])
        ]
    model = _Model(_reply("pass", "Pass."), _reply("pass", "Pass."))
    planner = _planner(model)
    planner.plan_decision_options(_menu(state, (301, True)), state)
    assert model.efforts, case
    assert "Cast Lightning Strike" in model.prompts[0][model.prompts[0].index("OPTIONS:") :]
