"""Burn at "any target" and "each creature": stated kills checked, targets kept on the plan (2026-10-19).

Mono-Red Burn, standalone.log and match packets:

* 09:01:42 (match 41753907 T5): Tectonic Hazard ("1 damage to each opponent
  and each creature they control") was cast because it "kills their only
  creature, the flying Dawnwing Marshal" — a 2/2. The false-kill guard only
  read "target creature" damage (``play_safety._VARIABLE_DAMAGE`` listed "any
  target"), so neither Hazard nor Shock / Lightning Strike / Burst Lightning
  was ever checked. Again 10:30:16 (match 10a78106): Hazard "kills the growing
  Bristly Bill" (2/2) while the line cast Impractical Joke on Bill.
* 09:34:14-23 (match 92c09c7a T10, model down; packet decisions[27:29]): the
  board-math line was "Lightning Strike (on opponent) … opponent at 6"; the
  target request went to the power-sorted fallback, which aimed the Strike
  at a tapped 7/8 Sazh's Chocobo (Player.log: 3 damage to #402).
* 10:13:45-47 (match 1e4ccb0c): "Shock to face advances the burn race" and the
  line's "Shock (on opponent)", then the target step's model picked a 3/4
  Battlesong Berserker ("Killing the 3/4 Berserker clears the only blocker").
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from arenamcp import action_planner as planner_module
from arenamcp import board_assessment
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.play_safety import claimed_kills, damage_effect, damage_removal_kills_nothing

LOCAL, OPP = 2, 1
SHOCK = "Shock deals 2 damage to any target."
STRIKE = "cardname deals 3 damage to any target."
BURST = (
    "Kicker {4} (You may pay an additional {4} as you cast this spell.)\n"
    "Burst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage instead."
)
HAZARD = "Tectonic Hazard deals 1 damage to each opponent and each creature they control."


def _creature(iid, name, seat, power, toughness, oracle="", **extra):
    return {
        "instance_id": iid,
        "name": name,
        "type_line": "Creature — Bird",
        "card_types": ["CardType_Creature"],
        "oracle_text": oracle,
        "power": power,
        "toughness": toughness,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
        "is_tapped": False,
        "turn_entered_battlefield": 3,
        **extra,
    }


def _mountain(iid, tapped=False):
    return {
        "instance_id": iid,
        "name": "Mountain",
        "type_line": "Basic Land — Mountain",
        "oracle_text": "({T}: Add {R}.)",
        "card_types": ["CardType_Land"],
        "controller_seat_id": LOCAL,
        "owner_seat_id": LOCAL,
        "is_tapped": tapped,
    }


def _spell(iid, name, type_line, cost, oracle, seat=LOCAL):
    return {
        "instance_id": iid,
        "grp_id": 90000 + iid,
        "name": name,
        "type_line": type_line,
        "mana_cost": cost,
        "oracle_text": oracle,
        "controller_seat_id": seat,
        "owner_seat_id": seat,
    }


def _state(battlefield, *, hand=(), stack=(), opp_life=11, life=10, source=0):
    state = {
        "match_id": "92c09c7a",
        "local_seat_id": LOCAL,
        "turn": {"turn_number": 10, "active_player": LOCAL, "priority_player": LOCAL, "phase": "Phase_Main1"},
        "players": [
            {"seat_id": OPP, "life_total": opp_life, "is_local": False},
            {"seat_id": LOCAL, "life_total": life, "is_local": True},
        ],
        "battlefield": list(battlefield),
        "hand": list(hand),
        "graveyard": [],
        "exile": [],
        "stack": list(stack),
    }
    if source:
        state["_bridge_request_payload"] = {"sourceId": source}
        state["decision_context"] = {"type": "target_selection", "source_id": source}
    return state


def _targets(ids, source_label):
    return PendingDecision(
        request_id=(276, 376),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{iid}", f"Target #{iid}", meta={"instanceId": iid}) for iid in ids),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label=source_label,
        slots=(TargetSlot(1, 1, 1, 0, tuple(ids)),),
    )


def _cast_menu(*casts):
    options = [
        DecisionOption(
            f"idx:{index}",
            f"Cast {name}",
            payable=True,
            meta={"actionType": "ActionType_Cast", "instanceId": iid, "grpId": 90000 + iid},
        )
        for index, (iid, name) in enumerate(casts)
    ]
    options.append(DecisionOption("pass", "Pass"))
    return PendingDecision((274, 371), "ActionsAvailable", tuple(options), can_pass=True)


class _Backend:
    def __init__(self, reply="", *, available=True, error=None):
        self.reply = reply
        self._available = available
        self.error = error
        self.prompts: list[str] = []
        self.efforts: list = []

    def available(self):
        return self._available

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" in user:
            self.prompts.append(user)
            self.efforts.append(kwargs.get("reasoning_effort"))
        if self.error is not None:
            raise self.error
        return self.reply


def _planner(reply="", **kwargs):
    return ActionPlanner(_Backend(reply, **kwargs), timeout=5, land_drop_first=False)


def _down():
    return _planner(available=False)


# --- parsing --------------------------------------------------------------------------------


def test_damage_effect_reads_any_target_each_creature_and_kicker():
    assert damage_effect({"oracle_text": SHOCK, "type_line": "Instant"}) == (2, "any")
    assert damage_effect({"oracle_text": STRIKE}) == (3, "any")
    assert damage_effect({"oracle_text": HAZARD, "type_line": "Sorcery"}) == (1, "each")
    assert damage_effect({"oracle_text": "~ deals 4 damage to target creature."}) == (4, "target")
    assert damage_effect({"oracle_text": "Boltwave deals 3 damage to each opponent."}) is None
    assert damage_effect({"oracle_text": "~ deals X damage to any target."}) is None
    burst = {"oracle_text": BURST, "mana_cost": "{R}", "type_line": "Instant"}
    assert damage_effect(burst) == (4, "any")  # no state: the larger reach
    assert damage_effect(burst, kicked=False) == (2, "any")
    three_lands = _state([_mountain(1), _mountain(2), _mountain(3)])
    five_lands = _state([_mountain(i) for i in range(1, 6)])
    assert damage_effect(burst, three_lands) == (2, "any")
    assert damage_effect(burst, five_lands) == (4, "any")
    # Burn that can go face is never withheld as removal that "kills nothing".
    tapped_giant = _creature(402, "Sazh's Chocobo", OPP, 7, 8, is_tapped=True)
    shock = _spell(570, "Shock", "Instant", "{R}", SHOCK)
    assert damage_removal_kills_nothing(shock, _state([tapped_giant, _mountain(1)])) == ""


def test_a_kill_claim_through_an_appositive_is_read():
    marshal = _creature(300, "Dawnwing Marshal", OPP, 2, 2, "Flying")
    said = (
        "Tectonic Hazard for {R} kills their only creature, the flying Dawnwing Marshal, and pings them "
        "for 1 — removing their clock while keeping the land drop and remaining mana for further plays."
    )
    assert claimed_kills(said, [marshal]) == [marshal]
    # A later clause naming another creature is not part of the kill.
    assert claimed_kills("Strike kills the Elves, then Marshal attacks into nothing", [marshal]) == []


# --- #16: the cast step's stated kill ------------------------------------------------------


def _hazard_window(marshal_toughness=2):
    marshal = _creature(300, "Dawnwing Marshal", OPP, 2, marshal_toughness, "Flying")
    hazard = _spell(310, "Tectonic Hazard", "Sorcery", "{R}", HAZARD)
    state = _state([marshal, _mountain(1), _mountain(2)], hand=[hazard], opp_life=20, life=20)
    state["match_id"] = "41753907"
    return state, _cast_menu((310, "Tectonic Hazard"))


HAZARD_SAID = (
    "Tectonic Hazard for {R} kills their only creature, the flying Dawnwing Marshal, and pings them for 1 "
    "— removing their clock while keeping the land drop and remaining mana for further plays."
)


def test_hazard_claiming_to_kill_a_two_toughness_marshal_is_rejected(caplog):
    state, menu = _hazard_window()
    planner = _planner(json.dumps({"option_ids": ["idx:0"], "reasoning": HAZARD_SAID}))
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner.plan_decision_options(menu, state)
    assert "claims kills Dawnwing Marshal (2/2) but 1 damage of Tectonic Hazard cannot kill it" in caplog.text
    assert planner.get_last_decision_trace()["false_kill_claim"] == ["Dawnwing Marshal"]


def test_hazard_claiming_to_kill_bristly_bill_is_rejected(caplog):
    """10:30:16 (match 10a78106, packet decision 10): Hazard over the line's Joke on Bill."""
    bill = _creature(330, "Bristly Bill, Spine Sower", OPP, 2, 2, "Landfall — put a +1/+1 counter.")
    hazard = _spell(219, "Tectonic Hazard", "Sorcery", "{R}", HAZARD)
    state = _state([bill, _mountain(279)], hand=[hazard], opp_life=20, life=20)
    said = (
        "Tectonic Hazard kills the growing Bristly Bill before it snowballs via landfall and pings the "
        "opponent for 1."
    )
    planner = _planner(json.dumps({"option_ids": ["idx:0"], "reasoning": said}))
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner.plan_decision_options(_cast_menu((219, "Tectonic Hazard")), state)
    assert planner.get_last_decision_trace()["false_kill_claim"] == ["Bristly Bill, Spine Sower"]
    assert "1 damage of Tectonic Hazard cannot kill it" in caplog.text


def test_hazard_on_a_one_toughness_creature_is_kept():
    state, menu = _hazard_window(marshal_toughness=1)
    planner = _planner(json.dumps({"option_ids": ["idx:0"], "reasoning": HAZARD_SAID}))
    assert planner.plan_decision_options(menu, state) == ["idx:0"]


def _strike_window(lands=3):
    marshal = _creature(300, "Dawnwing Marshal", OPP, 4, 4, "Flying\nLifelink")
    strike = _spell(320, "Lightning Strike", "Instant", "{1}{R}", STRIKE.replace("cardname", "Lightning Strike"))
    burst = _spell(321, "Burst Lightning", "Instant", "{R}", BURST)
    state = _state([marshal, *[_mountain(i) for i in range(1, lands + 1)]], hand=[strike, burst], opp_life=20)
    return state, _cast_menu((320, "Lightning Strike"), (321, "Burst Lightning"))


@pytest.mark.parametrize(
    "said, kept",
    [
        ("Lightning Strike kills the 4/4 Dawnwing Marshal before it gains more life.", False),
        ("Lightning Strike now, then Burst finishes off the 4/4 Dawnwing Marshal: 5 damage.", True),
    ],
    ids=["strike-alone", "strike-plus-named-burst"],
)
def test_a_two_spell_kill_is_no_false_claim(said, kept):
    state, menu = _strike_window()
    planner = _planner(json.dumps({"option_ids": ["idx:0"], "reasoning": said}))
    planner.plan_decision_options(menu, state)
    claim = planner.get_last_decision_trace().get("false_kill_claim")
    assert (claim is None) is kept


def test_kicked_burst_counts_only_when_the_kicker_is_payable():
    said = "Kicked Burst Lightning kills the 4/4 Dawnwing Marshal."
    state, menu = _strike_window(lands=5)
    planner = _planner(json.dumps({"option_ids": ["idx:1"], "reasoning": said}))
    planner.plan_decision_options(menu, state)
    assert "false_kill_claim" not in planner.get_last_decision_trace()
    state, menu = _strike_window(lands=3)
    planner = _planner(json.dumps({"option_ids": ["idx:1"], "reasoning": said}))
    planner.plan_decision_options(menu, state)
    assert planner.get_last_decision_trace()["false_kill_claim"] == ["Dawnwing Marshal"]


# --- #26: the 92c09c7a T10 target request -------------------------------------------------------


def _strike_targets_state(opp_life=11):
    chocobo = _creature(402, "Sazh's Chocobo", OPP, 7, 8, "Landfall — +1/+1 counter.", is_tapped=True)
    elves = _creature(501, "Llanowar Elves", OPP, 1, 1, "{T}: Add {G}.")
    slickshot = _creature(491, "Slickshot Show-Off", LOCAL, 1, 2, "Flying, haste")
    strike = _spell(572, "Lightning Strike", "Instant", "{1}{R}", STRIKE)
    state = _state(
        [chocobo, elves, slickshot, _mountain(405, True), _mountain(418, True)],
        stack=[strike],
        opp_life=opp_life,
        source=572,
    )
    return state, _targets([1, 2, 402, 491, 501], "Lightning Strike")


def _line_says(monkeypatch, card, target):
    """The searched best line casts ``card`` at ``target`` this turn."""
    monkeypatch.setattr(
        ActionPlanner,
        "_searched_line_target",
        staticmethod(lambda state, name: (target, 0) if name == card else ("", 0)),
    )


def test_the_line_target_is_read_from_the_best_lines_first_step(monkeypatch):
    elves = {"instance_id": 501, "name": "Llanowar Elves"}
    step = SimpleNamespace(
        targets=(("Lightning Strike", "Llanowar Elves"), ("Shock", "opponent")),
        plays=("", ((object(), elves), (object(), None)), ()),
    )
    result = SimpleNamespace(best=SimpleNamespace(steps=[step]))
    monkeypatch.setattr(board_assessment, "assess", lambda state: SimpleNamespace(line_search=result))
    assert ActionPlanner._searched_line_target({}, "Lightning Strike") == ("Llanowar Elves", 501)
    assert ActionPlanner._searched_line_target({}, "Shock") == ("opponent", 0)
    assert ActionPlanner._searched_line_target({}, "Burst Lightning") == ("", 0)
    monkeypatch.setattr(board_assessment, "assess", lambda state: None)
    assert ActionPlanner._searched_line_target({}, "Shock") == ("", 0)


def test_fallback_never_aims_any_target_burn_at_an_unkillable_creature():
    state, decision = _strike_targets_state()
    # Without the line: the creature it kills, not the tapped 7/8 (the old pick).
    assert _down().plan_decision_options(decision, state) == ["tgt:501"]
    # The bridge's safe default uses the same choice.
    assert ActionPlanner.__new__(ActionPlanner).targeting_fallback_choice(decision, state) == ["tgt:501"]


def test_fallback_goes_face_when_nothing_dies_or_face_is_lethal():
    state, decision = _strike_targets_state()
    state["battlefield"] = [card for card in state["battlefield"] if card["instance_id"] != 501]
    no_elves = _targets([1, 2, 402, 491], "Lightning Strike")
    planner = _down()
    assert planner.plan_decision_options(no_elves, state) == ["tgt:1"]
    assert "to the opponent's face" in planner.get_decision_reasoning(["tgt:1"])
    lethal, decision = _strike_targets_state(opp_life=3)
    assert _down().plan_decision_options(decision, lethal) == ["tgt:1"]


def test_the_board_math_cast_keeps_its_line_target(monkeypatch):
    """packet decisions[27:29]: the cast came from the line, so its target does too."""
    strike = _spell(570, "Lightning Strike", "Instant", "{1}{R}", STRIKE)
    before, _ = _strike_targets_state()
    before = {**before, "stack": [], "hand": [strike]}
    before.pop("decision_context")
    before.pop("_bridge_request_payload")
    cast = _cast_menu((570, "Lightning Strike"))
    monkeypatch.setattr(
        planner_module,
        "board_math_option_pick",
        lambda decision, state: (["idx:0"], "the board-math line casts Lightning Strike"),
    )
    monkeypatch.setattr(planner_module, "line_fallback_option_pick", lambda *args: None)
    _line_says(monkeypatch, "Lightning Strike", "opponent")
    planner = _down()
    assert planner.plan_decision_options(cast, before) == ["idx:0"]
    state, decision = _strike_targets_state()
    assert planner.plan_decision_options(decision, state) == ["tgt:1"]
    assert (
        planner.get_decision_reasoning(["tgt:1"])
        == "The board-math line aims Lightning Strike at the opponent's face."
    )
    # Another priority choice first: the intent is gone, the kill rule applies.
    planner.plan_decision_options(_cast_menu(), before)
    assert planner.plan_decision_options(decision, state) == ["tgt:501"]


# --- 1e4ccb0c: the model's target pick ---------------------------------------------------------


def _shock_targets_state(*, berserker_toughness=4, **berserker):
    berserker_card = _creature(324, "Battlesong Berserker", OPP, 3, berserker_toughness, "Menace", **berserker)
    effigy = _creature(320, "Fleeting Effigy", LOCAL, 2, 2, "Haste")
    shock = _spell(329, "Shock", "Instant", "{R}", SHOCK)
    strike = _spell(330, "Lightning Strike", "Instant", "{1}{R}", STRIKE.replace("cardname", "Lightning Strike"))
    state = _state(
        [berserker_card, effigy, _mountain(279, True), _mountain(286, True), _mountain(295)],
        hand=[strike],
        stack=[shock],
        opp_life=14,
        source=329,
    )
    state["match_id"] = "1e4ccb0c"
    return state, _targets([1, 2, 324, 320], "Shock")


MODEL_10_13_47 = json.dumps(
    {
        "option_ids": ["tgt:324"],
        "reasoning": "Killing the 3/4 Berserker clears the only blocker so Effigy connects; "
        "2 face damage doesn't advance the race enough.",
        "target_controllers": {"tgt:324": "opponent"},
    }
)


def test_a_model_pick_the_damage_cannot_kill_goes_face(caplog):
    state, decision = _shock_targets_state()
    planner = _planner(MODEL_10_13_47)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == ["tgt:1"]
    assert "does not kill Battlesong Berserker" in planner.get_decision_reasoning(["tgt:1"])
    assert "Retargeting" in caplog.text


@pytest.mark.parametrize(
    "berserker, reasoning",
    [
        ({"berserker_toughness": 2}, "Killing the 3/2 Berserker clears the blocker."),
        ({"is_blocking": True}, "Shock the blocking Berserker so combat damage finishes it."),
        ({}, "Shock the Berserker now; Lightning Strike finishes it off after."),
    ],
    ids=["dies-to-it", "in-combat", "named-second-burn"],
)
def test_model_picks_that_can_still_kill_stand(berserker, reasoning):
    state, decision = _shock_targets_state(**berserker)
    reply = json.dumps(
        {"option_ids": ["tgt:324"], "reasoning": reasoning, "target_controllers": {"tgt:324": "opponent"}}
    )
    assert _planner(reply).plan_decision_options(decision, state) == ["tgt:324"]


def test_the_target_prompt_carries_the_cast_intent_and_the_line(monkeypatch):
    shock = _spell(328, "Shock", "Instant", "{R}", SHOCK)
    state, decision = _shock_targets_state()
    before = {**state, "stack": [], "hand": [shock, *state["hand"]]}
    before.pop("decision_context")
    before.pop("_bridge_request_payload")
    _line_says(monkeypatch, "Shock", "opponent")
    planner = _planner(
        json.dumps({"option_ids": ["idx:0"], "reasoning": "Shock to face advances the burn race; Effigy attacks."})
    )
    # Since ledger #80 this face-only Shock is held in our main phase for the opponent's
    # end step (tests/test_decision_layer_20261019.py). This test is about the cast
    # intent reaching the target prompt once Shock is cast, so the hold stays out of it.
    monkeypatch.setattr(planner, "_hold_instant_burn", lambda decision, state: (decision, []))
    assert planner.plan_decision_options(_cast_menu((328, "Shock")), before) == ["idx:0"]
    planner._backend.reply = MODEL_10_13_47
    assert planner.plan_decision_options(decision, state) == ["tgt:1"]
    prompt = planner._backend.prompts[-1]
    assert 'CAST INTENT: this window you cast Shock saying: "Shock to face advances the burn race' in prompt
    assert "The best searched line aims Shock at the opponent's face." in prompt
    assert "2 damage kills none of their creatures" in prompt
    assert prompt.index("CAST INTENT") < prompt.index("OPTIONS:")

