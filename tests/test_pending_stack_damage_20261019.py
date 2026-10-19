"""Our own pending damage on the stack, match 17a32d51 (2026-10-19 12:52:59-12:57:00).

Ledger #122: T5 12:54:13, our Shock (312) on the stack aimed at their tapped
Emberheart Challenger (307, 2/2). Lightning Strike was offered; the effort gate
("removal available: Lightning Strike, but only our own Shock is on the stack —
low effort") and the #80 instant hold still counted the Emberheart as Strike's
kill, so Strike went to the face (14 -> 11) instead of waiting for their end
step, and the lines offered "Iroh's Demonstration (on Emberheart Challenger)"
"before our pending Shock resolves". Player.log gsid 102 carried the target as
a TargetSpec persistent annotation (affectorId 312, affectedIds [307]) that
the parser never read (it looked for a "sourceId" detail).

Ledger #124: T5 12:54:10, the low-effort answer cast Shock at the Emberheart
while the best line killed it with Iroh's Demonstration (a sorcery) and kept
both instants.

Ledger #125: T3 12:53:43, Burst Lightning cast with "Done" (no kicker); the #56
redirect said "4 damage to the opponent's face is not lethal".

The boards are the game's own, from Player.log (ids, life, tapped state) and
the coach's snapshot (card text).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time

import pytest
from tests.test_decision_layer_20261019 import _Model

from arenamcp import action_planner as planner_module
from arenamcp import board_assessment
from arenamcp.action_planner import ActionPlanner
from arenamcp.board_model import build_board_model
from arenamcp.decision_memory import burn_kills_nothing, critical_decision
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot
from arenamcp.gamestate import GameState
from arenamcp.line_search import _after_our_pending, _deployment, _order
from arenamcp.play_safety import doomed_by_our_stack, our_stack_damage

ME, THEM = 1, 2

TEXT = {
    "Emberheart Challenger": (
        "Creature — Mouse Warrior", "{1}{R}",
        "Haste\nProwess\n<i>Valiant</i> — Whenever this creature becomes the target of a spell or ability you "
        "control for the first time each turn, exile the top card of your library. Until end of turn, you may "
        "play that card.",
    ),
    "Pompous Battlemage": ("Creature — Goblin Sorcerer", "{R}", "Prowess\nThis creature enters prepared."),
    "Shock": ("Instant", "{R}", "Shock deals 2 damage to any target."),
    "Lightning Strike": ("Instant", "{1}{R}", "Lightning Strike deals 3 damage to any target."),
    "Burst Lightning": (
        "Instant", "{R}",
        "Kicker {o4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, it deals 4 damage "
        "instead.",
    ),
    "Iroh's Demonstration": (
        "Sorcery — Lesson", "{1}{R}",
        "Choose one — \n•<indent=4%>Iroh's Demonstration deals 1 damage to each creature your opponents "
        "control. </indent>\n•<indent=4%>Iroh's Demonstration deals 4 damage to target creature.</indent>",
    ),
    "Mountain": ("Basic Land — Mountain", "", "({T}: Add {R}.)"),
}  # fmt: skip


def _card(iid, name, seat=ME, **extra):
    type_line, cost, text = TEXT[name]
    kind = type_line.split(" —")[0].split()[-1]
    card = {
        "instance_id": iid, "name": name, "type_line": type_line, "mana_cost": cost, "oracle_text": text,
        "controller_seat_id": seat, "owner_seat_id": seat, "card_types": [f"CardType_{kind}"],
        "is_tapped": False,
    }  # fmt: skip
    card.update(extra)
    return card


def _emberheart(**extra):
    return _card(307, "Emberheart Challenger", THEM, power=2, toughness=2, keywords=["haste"],
                 is_tapped=True, turn_entered_battlefield=4, **extra)  # fmt: skip


def _shock(targets=(307,), iid=312):
    return _card(iid, "Shock", targeting=list(targets))


def t5(*, stack=(), hand=("Lightning Strike", "Iroh's Demonstration"), theirs=None, their_life=14):
    """T5 at 12:54:13 (gsid 102): three Mountains (one tapped for the Shock), 18 vs 14 life."""
    ids = {"Lightning Strike": 167, "Iroh's Demonstration": 170, "Shock": 310}
    return {
        "match_id": "17a32d51-5a3e-44c5-9c06-84b937383526",
        "local_seat_id": ME,
        "turn": {"turn_number": 5, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": ME, "life_total": 18, "is_local": True, "lands_played": 1},
            {"seat_id": THEM, "life_total": their_life, "is_local": False, "lands_played": 0},
        ],
        "battlefield": [
            _card(287, "Mountain", is_tapped=bool(stack)), _card(297, "Mountain"), _card(311, "Mountain"),
            _card(292, "Mountain", THEM, is_tapped=True), _card(306, "Mountain", THEM, is_tapped=True),
            *([_emberheart()] if theirs is None else theirs),
        ],
        "hand": [_card(ids[name], name) for name in hand],
        "graveyard": [],
        "exile": [],
        "stack": list(stack),
    }  # fmt: skip


def _cast(option_id, iid):
    return DecisionOption(
        option_id, "Cast", payable=True, meta={"actionType": "ActionType_Cast", "instanceId": iid, "grpId": 0}
    )


STRIKE_MENU = PendingDecision(
    (102, 139), "ActionsAvailable", (_cast("idx:0", 167), DecisionOption("pass", "Pass")), can_pass=True
)


@pytest.fixture(autouse=True)
def _fresh_assessments():
    board_assessment._CACHE.clear()
    yield
    board_assessment._CACHE.clear()


# --- #122: what our pending Shock kills -----------------------------------------------------------


def test_our_shock_on_the_stack_dooms_the_emberheart_it_targets():
    damage = our_stack_damage(t5(stack=[_shock()]))
    assert damage.doomed == {307} and damage.settled == {312} and damage.face == 0
    # Its targets are what counts: untargeted (still choosing), or the spell itself left out.
    assert doomed_by_our_stack(t5(stack=[_shock(targets=())])) == frozenset()
    assert doomed_by_our_stack(t5(stack=[_shock()]), exclude=312) == frozenset()
    # At the face it kills nothing and deals its 2.
    damage = our_stack_damage(t5(stack=[_shock(targets=(THEM,))]))
    assert damage.doomed == frozenset() and damage.face == 2 and damage.settled == {312}


def test_strike_kills_nothing_while_our_shock_is_aimed_at_the_emberheart():
    strike = _card(167, "Lightning Strike")
    oracle = strike["oracle_text"]
    # The reviewer's offline repro: False with the Shock on the stack before the fix.
    assert burn_kills_nothing(oracle, t5(stack=[_shock()]), castable=strike)
    assert not burn_kills_nothing(oracle, t5(), castable=strike)
    skipped: list[str] = []
    assert critical_decision(STRIKE_MENU, t5(stack=[_shock()]), "", skipped) == ""
    assert skipped == ["Lightning Strike can kill nothing on their side"]
    assert critical_decision(STRIKE_MENU, t5(), "") == "removal available: Lightning Strike"


def test_damage_adds_up_marked_damage_counts_and_their_answers_keep_it_alive():
    giant = {**_emberheart(), "power": 3, "toughness": 3}
    # One Shock does not kill a 3/3; two do; one does when it already took 1 this turn.
    assert doomed_by_our_stack(t5(stack=[_shock()], theirs=[giant])) == frozenset()
    assert our_stack_damage(t5(stack=[_shock()], theirs=[giant])).settled == frozenset()
    two = [_shock(), _shock(iid=313)]
    assert doomed_by_our_stack(t5(stack=two, theirs=[giant])) == {307}
    hurt = {**giant, "damage": 1, "damaged_this_turn": True}
    assert doomed_by_our_stack(t5(stack=[_shock()], theirs=[hurt])) == {307}
    # Their ward trigger on the stack (the Shock may be countered), or their spell at it.
    ward = {"instance_id": 400, "name": "ward", "object_kind": "ABILITY", "controller_seat_id": THEM,
            "parent_instance_id": 307, "oracle_text": "Ward {2}"}  # fmt: skip
    assert doomed_by_our_stack(t5(stack=[_shock(), ward])) == frozenset()
    trick = {"instance_id": 401, "name": "Snakeskin Veil", "controller_seat_id": THEM, "targeting": [307],
             "type_line": "Instant"}  # fmt: skip
    assert doomed_by_our_stack(t5(stack=[_shock(), trick])) == frozenset()


def test_the_t5_strike_window_holds_the_instant_without_the_model(caplog):
    model = _Model(json.dumps({"option_ids": ["idx:0"], "reasoning": "Shock kills it; Strike face."}))
    planner = ActionPlanner(model, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(STRIKE_MENU, t5(stack=[_shock()])) == ["pass"]
    assert model.efforts == []  # only a pass was left: no model call
    assert "Instant timing: holding Lightning Strike for the opponent's end step" in caplog.text
    assert planner.get_decision_reasoning(["pass"]).startswith("Holding Lightning Strike")


def test_the_strike_target_step_is_not_the_shock_below_it():
    strike = _card(314, "Lightning Strike")
    decision = PendingDecision(
        (103, 141), "SelectTargets",
        tuple(DecisionOption(f"tgt:{i}", f"#{i}", meta={"instanceId": i}) for i in (1, 2, 307)),
        min_select=1, max_select=1, can_cancel=True, source_label="Lightning Strike",
        slots=(TargetSlot(1, 1, 1, 0, (1, 2, 307)),),
    )  # fmt: skip
    # 12:54:15 the log said "high reasoning effort (removal target: Shock)" for Strike's targets.
    state = t5(stack=[_shock(), strike], hand=("Iroh's Demonstration",))
    assert critical_decision(decision, state, "") == ""
    # The Shock's own target step (no target chosen yet) is removal.
    shock_step = dataclasses.replace(decision, source_label="Shock")
    assert critical_decision(shock_step, t5(stack=[_shock(targets=())]), "") == "removal target: Shock"


def test_the_line_search_starts_after_our_pending_shock_resolves():
    pending = board_assessment.assess(t5(stack=[_shock()]))
    text = pending.line_search.prompt_line(400, pending=pending.pending)
    assert "Emberheart" not in text and "before our pending Shock" not in text
    assert pending.pending == []
    assert not pending.line_search.best.steps[0].casts  # hold: nothing left to kill
    # Without the Shock the lines kill it with Iroh's Demonstration, as at 12:54:02.
    before = board_assessment.assess(t5(hand=("Lightning Strike", "Iroh's Demonstration", "Shock")))
    assert dict(before.line_search.best.steps[0].targets) == {"Iroh's Demonstration": "Emberheart Challenger"}


def test_the_root_resolution_is_the_board_after_the_shock_and_idempotent():
    model = build_board_model(t5(stack=[_shock()]))
    assert model.our_stack_doomed == {307} and [b["instance_id"] for b in model.theirs] == [307]
    after = _after_our_pending(model)
    assert after.theirs == () and after.our_stack_doomed == frozenset()
    assert all(c.get("instance_id") != 307 for c in after.battlefield)
    assert _after_our_pending(after) is after
    # At the face: their life drops by the 2, unless that alone ends the game.
    face = _after_our_pending(build_board_model(t5(stack=[_shock(targets=(THEM,))])))
    assert face.opp_life == 12 and face.our_stack_face == 0
    lethal = build_board_model(t5(stack=[_shock(targets=(THEM,))], their_life=2))
    assert _after_our_pending(lethal).opp_life == 2


def test_a_targetspec_from_the_log_gives_the_stack_spell_its_target():
    state = GameState()
    state.set_local_seat_id(ME, source=3)
    shock = {"instanceId": 312, "grpId": 75525, "type": "GameObjectType_Card", "zoneId": 27,
             "visibility": "Visibility_Public", "ownerSeatId": 1, "controllerSeatId": 1,
             "cardTypes": ["CardType_Instant"], "overlayGrpId": 75525,
             "uniqueAbilities": [{"id": 275, "grpId": 86613}]}  # fmt: skip
    # gsid 100 (cast) and 102 (targets submitted), as Player.log has them.
    state.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 100, "gameObjects": [shock],
         "zones": [{"zoneId": 27, "type": "ZoneType_Stack", "objectInstanceIds": [312]}]}
    )  # fmt: skip
    target_spec = {
        "id": 341, "affectorId": 312, "affectedIds": [307], "type": ["AnnotationType_TargetSpec"],
        "details": [
            {"key": "abilityGrpId", "type": "KeyValuePairValueType_int32", "valueInt32": [86613]},
            {"key": "index", "type": "KeyValuePairValueType_int32", "valueInt32": [1]},
            {"key": "promptParameters", "type": "KeyValuePairValueType_int32", "valueInt32": [312]},
            {"key": "promptId", "type": "KeyValuePairValueType_int32", "valueInt32": [11869]},
        ],
    }  # fmt: skip
    state.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 102, "persistentAnnotations": [target_spec]}
    )
    assert state.game_objects[312].targeting == [307]
    # A diff that re-sends the spell where it is keeps its target; leaving the stack drops it.
    state.update_from_message({"type": "GameStateType_Diff", "gameStateId": 103, "gameObjects": [shock]})
    assert state.game_objects[312].targeting == [307]
    state.update_from_message(
        {"type": "GameStateType_Diff", "gameStateId": 109, "gameObjects": [{**shock, "zoneId": 30}]}
    )
    assert state.game_objects[312].targeting == []


# --- #124: a sorcery-speed kill keeps the instant ------------------------------------------------


SHOCK_WINDOW = PendingDecision(
    (99, 133),
    "ActionsAvailable",
    (_cast("idx:0", 167), _cast("idx:1", 170), _cast("idx:2", 310), DecisionOption("pass", "Pass")),
    can_pass=True,
)


def _shock_window():
    """12:54:10 (gsid 99): Mountain played, Shock, Strike and Iroh's in hand, the stack empty."""
    return t5(hand=("Lightning Strike", "Iroh's Demonstration", "Shock"))


def test_a_tie_keeps_the_instant_and_spends_the_sorcery():
    lines = board_assessment.assess(_shock_window()).line_search.lines
    iroh = next(line for line in lines if line.steps[0].casts == ("Iroh's Demonstration",))
    shock = next(line for line in lines if line.steps[0].casts == ("Shock",))
    tied = dataclasses.replace(shock, v=iroh.v)
    assert _deployment(iroh)[2] == 0 and _deployment(tied)[2] == -1
    assert _deployment(tied)[3] > _deployment(iroh)[3]  # Shock leaves more mana open...
    assert _order(iroh) > _order(tied)  # ...but the sorcery's kill keeps both instants


def test_an_instant_pick_on_the_lines_sorcery_kill_is_doubted():
    state = _shock_window()
    result = board_assessment.assess(state).line_search
    doubt = ActionPlanner._instant_for_sorcery_kill
    assert doubt(SHOCK_WINDOW, state, ["idx:2"], result) == (
        "Shock kills Emberheart Challenger, which the best line kills with Iroh's Demonstration at sorcery speed"
    )
    assert doubt(SHOCK_WINDOW, state, ["idx:0"], result).startswith("Lightning Strike kills Emberheart")
    assert doubt(SHOCK_WINDOW, state, ["idx:1"], result) == ""  # the line's own sorcery
    assert doubt(SHOCK_WINDOW, state, ["pass"], result) == ""


def test_the_low_shock_waits_for_the_high_answer(monkeypatch, caplog):
    monkeypatch.setattr(ActionPlanner, "_HIGH_AFTER_LOW_S", 0.05)
    low = json.dumps({"option_ids": ["idx:2"], "reasoning": "Shock kills their only creature for {R}."})
    high = json.dumps(
        {"option_ids": ["idx:1"], "reasoning": "Iroh's kills it at sorcery speed; keep instants."}
    )
    model = _Model(low, high, high_delay=0.4)
    planner = ActionPlanner(model, timeout=5, land_drop_first=False)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(SHOCK_WINDOW, _shock_window()) == ["idx:1"]
    assert "waits for high effort (instant_for_sorcery_kill: Shock kills Emberheart Challenger" in caplog.text


# --- #125: the #56 redirect reads the kicker as it was chosen ------------------------------------


KICKER_MENU = PendingDecision(
    (49, 72),
    "CastingTimeOptions",
    (
        DecisionOption("idx:0", "Done", meta={
            "actionType": "CastingTimeOption", "choiceKind": "done", "requestClass": "CastingTimeOption_DoneRequest",
            "childIndex": 0, "label": "Done", "manaCost": [{"color": '[ "Red" ]', "count": 1}]}),
        DecisionOption("idx:1", "Kicker", meta={
            "actionType": "CastingTimeOption", "choiceKind": "kicker",
            "requestClass": "CastingTimeOption_KickerRequest", "childIndex": 1, "label": "Kicker", "grpId": 2852,
            "sourceId": 300, "manaCost": [{"color": '[ "Red" ]', "count": 1}, {"color": '[ "Generic" ]', "count": 4}]}),
    ),
    min_select=1, max_select=1, can_cancel=True,
)  # fmt: skip

BURST_TARGETS = PendingDecision(
    (50, 74), "SelectTargets",
    tuple(DecisionOption(f"tgt:{i}", f"#{i}", meta={"instanceId": i}) for i in (1, 2, 293)),
    min_select=1, max_select=1, can_cancel=True, source_label="Burst Lightning",
    slots=(TargetSlot(1, 1, 1, 0, (1, 2, 293)),),
)  # fmt: skip


def t3(their_life):
    """T3 at 12:53:41 (gsid 74): Burst Lightning (300) on the stack, their Pompous Battlemage 1/1."""
    state = {
        "match_id": "17a32d51-5a3e-44c5-9c06-84b937383526",
        "local_seat_id": ME,
        "turn": {"turn_number": 3, "active_player": ME, "priority_player": ME, "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": ME, "life_total": 20, "is_local": True, "lands_played": 1},
            {"seat_id": THEM, "life_total": their_life, "is_local": False},
        ],
        "battlefield": [
            _card(287, "Mountain", is_tapped=True), _card(297, "Mountain", is_tapped=True),
            _card(292, "Mountain", THEM),
            _card(293, "Pompous Battlemage", THEM, power=1, toughness=1, turn_entered_battlefield=2),
        ],
        "hand": [_card(170, "Iroh's Demonstration")],
        "graveyard": [],
        "exile": [],
        "stack": [_card(300, "Burst Lightning")],
        "_bridge_request_payload": {"sourceId": 300},
        "decision_context": {"type": "target_selection", "source_id": 300},
    }  # fmt: skip
    return state


FACE = json.dumps(
    {"option_ids": ["tgt:2"], "reasoning": "Unkicked Burst Lightning goes to the face as planned."}
)


def _burst_planner(monkeypatch, *, kicker_choice):
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)
    planner = ActionPlanner(_Model(FACE, FACE), timeout=5, land_drop_first=False)
    if kicker_choice is not None:
        planner._remember_cast_intent(KICKER_MENU, t3(3), [kicker_choice], "")
    planner._cast_intent = {
        "card": "Burst Lightning",
        "said": "unkicked Burst Lightning to the face",
        "line_target": "Pompous Battlemage",
        "line_target_id": 293,
        "at": time.monotonic(),
    }
    return planner


def test_an_unkicked_burst_is_two_damage_to_the_redirect(monkeypatch, caplog):
    planner = _burst_planner(monkeypatch, kicker_choice="idx:0")  # "Done": not kicked
    assert planner._recorded_kick(t3(3)) is False
    assert planner._recorded_kick({**t3(3), "match_id": "a later game"}) is None
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        # At 3 life, 2 to the face is not lethal: the line's Battlemage (the T3 redirect, at 17 then).
        assert planner.plan_decision_options(BURST_TARGETS, t3(3)) == ["tgt:293"]
    assert "2 damage to the opponent's face is not lethal (they are at 3)" in caplog.text


def test_a_kicked_or_unknown_burst_keeps_a_face_shot_that_may_be_lethal(monkeypatch):
    planner = _burst_planner(monkeypatch, kicker_choice="idx:1")  # kicked: 4 is lethal at 3
    assert planner._recorded_kick(t3(3)) is True
    assert planner.plan_decision_options(BURST_TARGETS, t3(3)) == ["tgt:2"]
    planner = _burst_planner(monkeypatch, kicker_choice=None)  # never seen: the kicked amount
    assert planner._recorded_kick(t3(3)) is None
    assert planner.plan_decision_options(BURST_TARGETS, t3(3)) == ["tgt:2"]
