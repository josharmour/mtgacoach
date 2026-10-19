"""A false kill claim on a 'choose one' pick, and the hedge's mode check (ledger #142, #144).

2026-10-19 match 970d9b41 T12 (standalone.log 13:33:50-13:33:56, Player.log
gameStateId 244/245): we were at 20 with five Mountains (two tapped for the
Emberheart Challenger just cast), they were at 19 with one creature, a
summoning-sick 1/2 flying Ravenhill Flock ("Whenever you draw a card, put a
+1/+1 counter on this creature"), four tapped lands and three cards in hand.

- 13:33:53.668 (#144): the line was "Iroh's Demonstration (deals 4 damage to
  target creature, on Ravenhill Flock)"; the low answer "Iroh's mode 1 sweeps
  Ravenhill Flock (the only blocker/threat) ..." named the line's target, so
  ``board_math_agreement`` counted it as the line's play and the high-effort
  leg was cancelled.
- 13:33:55.980 (#142): at the mode menu (request [245, 322]; MODES tagged
  mode 2 'LINE best') the low-effort answer chose mode 1: "Mode 1 kills
  Ravenhill Flock, the only blocker, opening an unblocked attack for Emberheart
  Challenger; mode 2's 4 damage is wasted on a 1-toughness bird." The 1 damage
  left the Flock at 1/2; it grew to 8/9 and dealt 23 of the 29 damage we took.
"""

from __future__ import annotations

import logging
from copy import deepcopy

import pytest
from tests import race_boards_20261019 as rb
from tests.strategic_states import actions_decision, cast, modal_decision

from arenamcp import action_planner
from arenamcp.action_planner import ActionPlanner, board_math_agreement
from arenamcp.line_search import compare_modes
from arenamcp.mode_claims import mode_kill_check, other_modes_named, read_menu

# Arena's card text (MTGA card database, grpIds 97417, 103426, 91668, 103570, 105163, 105175).
IROH_TEXT = (
    "Choose one — \n•<indent=4%>Iroh's Demonstration deals 1 damage to each creature your opponents "
    "control. </indent>\n•<indent=4%>Iroh's Demonstration deals 4 damage to target creature.</indent>"
)
CARDS = {
    **rb.CARDS,
    "Iroh's Demonstration": {
        "grp_id": 97417, "type_line": "Sorcery — Lesson", "mana_cost": "{1}{R}",
        "card_types": ["CardType_Sorcery"], "oracle_text": IROH_TEXT,
    },
    "Ravenhill Flock": {
        "grp_id": 103426, "type_line": "Creature — Bird", "mana_cost": "{1}{U}", "power": 1, "toughness": 2,
        "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\nWhenever you draw a card, put a +1/+1 counter on this creature.",
    },
    "Lake-town": {
        "grp_id": 103570, "type_line": "Land", "card_types": ["CardType_Land"],
        "oracle_text": "This land enters tapped.\n{T}: Add {W} or {U}.\n{2}{W}{U}, {T}, Sacrifice this land: "
        "Put two +1/+1 counters on target Human you control. Activate only as a sorcery.",
    },
    "Gleaming Bastion": {
        "grp_id": 105163, "type_line": "Land", "card_types": ["CardType_Land"],
        "oracle_text": "{T}: Add {C}.\n{T}: Add {W} or {U}. Activate only if this land entered this turn or if "
        "you control a basic land.",
    },
    "Island": {
        "grp_id": 105175, "type_line": "Basic Land — Island", "card_types": ["CardType_Land"],
        "oracle_text": "({T}: Add {U}.)",
    },
    "Soldier": {
        "type_line": "Token Creature — Human Soldier", "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"], "oracle_text": "", "object_kind": "TOKEN",
    },
}  # fmt: skip

LIVE_MODE_ANSWER = (
    "Mode 1 kills Ravenhill Flock, the only blocker, opening an unblocked attack for Emberheart Challenger; "
    "mode 2's 4 damage is wasted on a 1-toughness bird."
)
LIVE_CAST_ANSWER = (
    "Iroh's mode 1 sweeps Ravenhill Flock (the only blocker/threat) for 2 mana, leaving Emberheart "
    "Challenger to attack unblocked and finally start the stalled clock."
)

# The recorded mode menu (match packet decision 25, request [245, 322]).
MODE_MENU = [
    (
        "idx:0",
        "Mode 1: CARDNAME deals 1 damage to each creature your opponents control.",
        {
            "actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "label": "Mode 1", "optionIndex": 0, "grpId": 87912, "sourceId": 360,
            "abilityGrpId": 192719, "min": 1, "max": 1,
        },
    ),
    (
        "idx:1",
        "Mode 2: CARDNAME deals 4 damage to target creature.",
        {
            "actionType": "CastingTimeOption", "choiceKind": "modal", "requestClass": "CastingTimeOption_ModalRequest",
            "childIndex": 0, "label": "Mode 2", "optionIndex": 1, "grpId": 21773, "sourceId": 360,
            "abilityGrpId": 192719, "min": 1, "max": 1,
        },
    ),
]  # fmt: skip


def _card(instance_id: int, name: str, controller: int, **extra) -> dict:
    return {
        "instance_id": instance_id,
        "name": name,
        "owner_seat_id": controller,
        "controller_seat_id": controller,
        **deepcopy(CARDS[name]),
        **extra,
    }


def _t12(*, on_stack: bool, theirs: list[dict] | None = None) -> dict:
    """T12 13:33:50 (``on_stack`` False: Iroh's 325 in hand, request [244, 319]) or 13:33:53 (True:
    cast as 360, waiting for its mode, request [245, 322]). Mountains 284/294 paid for Emberheart."""
    source = {
        "match_id": "970d9b41-d8f4-47fc-a42f-8231f9281699",
        "local_seat_id": rb.LOCAL,
        "opponent_seat_id": rb.OPPONENT,
        "turn": {"turn_number": 12, "active_player": rb.LOCAL, "priority_player": rb.LOCAL,
                 "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": rb.LOCAL, "life_total": 20, "is_local": True, "lands_played": 0},
            {"seat_id": rb.OPPONENT, "life_total": 19, "is_local": False, "lands_played": 0},
        ],
        "battlefield": [
            *(_card(iid, "Mountain", rb.LOCAL, is_tapped=iid in (284, 294), turn_entered_battlefield=t)
              for iid, t in ((284, 2), (294, 4), (312, 6), (335, 8), (349, 10))),
            _card(357, "Emberheart Challenger", rb.LOCAL, turn_entered_battlefield=12),
            _card(282, "Lake-town", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=1),
            _card(289, "Gleaming Bastion", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=3),
            _card(324, "Island", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=7),
            _card(337, "Island", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=9),
            *(theirs if theirs is not None
              else [_card(351, "Ravenhill Flock", rb.OPPONENT, turn_entered_battlefield=11)]),
        ],
        "hand": [],
        "stack": [],
        "graveyard": [],
        "zones": {"library_count": 25, "library_count_source": "log_zone_membership", "opponent_hand_count": 3},
        "deck_cards": [],
    }  # fmt: skip
    if on_stack:
        source["stack"] = [_card(360, "Iroh's Demonstration", rb.LOCAL)]
    else:
        source["hand"] = [_card(325, "Iroh's Demonstration", rb.LOCAL)]
    return source


def _modes() -> object:
    return modal_decision(MODE_MENU, (245, 322))


def _planner(reasoning: str) -> ActionPlanner:
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = reasoning
    planner._last_decision_option_ids = ["idx:0"]
    planner._last_decision_trace = {"reasoning": reasoning, "selected_ids": ["idx:0"]}
    return planner


# --- #142: what each mode kills on the real board ---------------------------------------------


def test_the_real_t12_menu_reads_what_each_mode_kills():
    menu = read_menu(_t12(on_stack=True), _modes())
    assert menu is not None
    assert menu.index == {"idx:0": 0, "idx:1": 1}
    assert not menu.kills["idx:0"].kills(351)  # 1 damage to each creature: the 1/2 lives
    assert menu.kills["idx:1"].kills(351)  # 4 damage to target creature
    assert menu.mode("idx:0") == "mode 1 (deals 1 damage to each creature…)"
    assert menu.mode("idx:1") == "mode 2 (deals 4 damage to target creature)"
    # The line search's own per-mode comparison (#87) agrees, and ranks mode 2 first ('LINE best').
    comparison = compare_modes(_t12(on_stack=True), _modes(), hard_ms=1e9)
    assert comparison is not None
    assert comparison.kills == {"idx:0": (), "idx:1": ("Ravenhill Flock",)}
    assert comparison.lines["idx:1"].score > comparison.lines["idx:0"].score


def test_the_live_mode_answer_is_a_false_kill_claim_that_mode_2_makes():
    check = mode_kill_check(_t12(on_stack=True), _modes(), ["idx:0"], LIVE_MODE_ANSWER)
    assert check is not None
    assert check.claimed == ("Ravenhill Flock (1/2)",) and check.missed == ("Ravenhill Flock (1/2)",)
    assert check.replacement == "idx:1" and check.extra == ()
    assert check.applies


def test_the_planner_switches_the_live_pick_to_mode_2_and_keeps_the_models_words(caplog):
    planner = _planner(LIVE_MODE_ANSWER)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        chosen = planner._correct_mode_kill_claim(_modes(), _t12(on_stack=True), ["idx:0"])
    assert chosen == ["idx:1"]
    assert (
        "mode pick corrected: mode 1 (deals 1 damage to each creature…) claims kill Ravenhill Flock (1/2); "
        "mode 2 (deals 4 damage to target creature) kills it" in caplog.text
    )
    assert planner._last_decision_option_ids == ["idx:1"]
    assert planner._last_decision_reasoning.startswith(
        "Mode 2 (deals 4 damage to target creature) kills Ravenhill Flock (1/2); mode 1 (deals 1 damage"
    )
    record = planner._last_decision_trace["mode_kill_claim"]
    assert record["applied"] is True and record["replacement"] == "idx:1"
    assert record["model_reasoning"] == LIVE_MODE_ANSWER
    assert planner._last_decision_trace["reasoning"] == LIVE_MODE_ANSWER


def _answering(monkeypatch, planner: ActionPlanner, chosen: list[str], reasoning: str) -> ActionPlanner:
    """The model's answer, as ``_llm_decision_options`` leaves it (choice, reasoning, trace)."""

    def answer(decision, state):
        planner._last_decision_reasoning = reasoning
        planner._last_decision_option_ids = chosen
        planner._last_decision_trace = {"reasoning": reasoning, "selected_ids": chosen}
        return chosen

    monkeypatch.setattr(planner, "_llm_decision_options", answer)
    monkeypatch.setattr(planner, "_correct_false_lethal_claim", lambda state: None)
    return planner


def test_the_check_runs_in_the_typed_decision_path_and_is_always_on(monkeypatch):
    """The mode guard is shadow (and the line search may be off); the claim check still applies."""
    monkeypatch.setenv("ARENAMCP_MODE_GUARD", "shadow")
    planner = _answering(monkeypatch, _planner(""), ["idx:0"], LIVE_MODE_ANSWER)
    assert planner._plan_decision_options(_modes(), _t12(on_stack=True)) == ["idx:1"]
    assert planner.get_decision_reasoning(["idx:1"]).startswith("Mode 2 (deals 4 damage to target creature)")
    assert planner.get_last_decision_trace()["mode_kill_claim"]["model_reasoning"] == LIVE_MODE_ANSWER
    monkeypatch.setenv("ARENAMCP_LINE_SEARCH", "0")
    planner = _answering(monkeypatch, _planner(""), ["idx:0"], LIVE_MODE_ANSWER)
    assert planner._plan_decision_options(_modes(), _t12(on_stack=True)) == ["idx:1"]


def test_an_applied_mode_guard_is_not_second_guessed(monkeypatch):
    planner = _answering(monkeypatch, _planner(""), ["idx:0"], LIVE_MODE_ANSWER)
    monkeypatch.setattr(planner, "_apply_mode_guard", lambda decision, state, chosen: ["idx:1"])
    called = []
    monkeypatch.setattr(planner, "_correct_mode_kill_claim", lambda *args: called.append(args) or ["idx:0"])
    assert planner._plan_decision_options(_modes(), _t12(on_stack=True)) == ["idx:1"]
    assert called == []


# --- #142: negative cases ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "reasoning",
    [
        # Right about the Flock: mode 2 is chosen and kills it (the T10 answer, 13:33:27.109).
        "Mode 2 kills the Ravenhill Flock, their only evasive threat and draw engine, which mode 1's 1 damage "
        "cannot.",
        # No kill claimed.
        "Mode 2's 4 damage is overkill; I'd rather keep options open.",
        # The claim is negated.
        "Mode 1 doesn't kill Ravenhill Flock, but it pings it for the attack.",
    ],
)
def test_no_correction_without_a_false_claim_on_the_pick(reasoning):
    chosen = ["idx:1"] if reasoning.startswith("Mode 2 kills") else ["idx:0"]
    assert mode_kill_check(_t12(on_stack=True), _modes(), chosen, reasoning) is None
    planner = _planner(reasoning)
    assert planner._correct_mode_kill_claim(_modes(), _t12(on_stack=True), chosen) == chosen
    assert planner._last_decision_reasoning == reasoning
    assert "mode_kill_claim" not in planner._last_decision_trace


@pytest.mark.parametrize(
    "reasoning",
    [
        "Mode 2 would kill Ravenhill Flock, but the sweep gets both Soldier tokens.",
        "The 4-damage mode would kill Ravenhill Flock; mode 1 clears both tokens instead.",
    ],
)
def test_a_kill_claimed_for_the_other_mode_is_not_the_picks_claim(reasoning):
    soldiers = [_card(901, "Soldier", rb.OPPONENT, turn_entered_battlefield=11),
                _card(902, "Soldier", rb.OPPONENT, turn_entered_battlefield=11),
                _card(351, "Ravenhill Flock", rb.OPPONENT, turn_entered_battlefield=11)]  # fmt: skip
    assert mode_kill_check(_t12(on_stack=True, theirs=soldiers), _modes(), ["idx:0"], reasoning) is None


def test_a_sweep_that_kills_something_else_keeps_the_pick_and_fixes_the_words(caplog):
    # The sweep kills two Soldier tokens mode 2 would leave: a real choice the claim left out.
    theirs = [_card(351, "Ravenhill Flock", rb.OPPONENT, turn_entered_battlefield=11),
              _card(901, "Soldier", rb.OPPONENT, turn_entered_battlefield=11),
              _card(902, "Soldier", rb.OPPONENT, turn_entered_battlefield=11)]  # fmt: skip
    state = _t12(on_stack=True, theirs=theirs)
    check = mode_kill_check(state, _modes(), ["idx:0"], LIVE_MODE_ANSWER)
    assert check is not None and check.replacement == "idx:1"
    assert check.extra == ("Soldier (1/1)", "Soldier (1/1)") and not check.applies
    planner = _planner(LIVE_MODE_ANSWER)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner._correct_mode_kill_claim(_modes(), state, ["idx:0"]) == ["idx:0"]
    assert (
        "mode pick kept: mode 1 (deals 1 damage to each creature…) claims kill Ravenhill Flock" in caplog.text
    )
    assert "kills Ravenhill Flock" not in planner._last_decision_reasoning
    assert planner._last_decision_reasoning.endswith(
        "Board math: mode 1 (deals 1 damage to each creature…) does not kill Ravenhill Flock (1/2); mode 2 "
        "(deals 4 damage to target creature) kills it, but mode 1 (deals 1 damage to each creature…) also "
        "kills Soldier (1/1), Soldier (1/1)."
    )
    assert planner._last_decision_trace["mode_kill_claim"]["applied"] is False


def test_when_no_mode_kills_the_claimed_creature_the_pick_stands():
    # A 5/5 Flock (four draws later): neither 1 to each nor 4 to one kills it.
    big = [_card(351, "Ravenhill Flock", rb.OPPONENT, turn_entered_battlefield=11, power=5, toughness=5)]
    state = _t12(on_stack=True, theirs=big)
    for chosen, said in (
        (["idx:0"], "Mode 1 kills Ravenhill Flock."),
        (["idx:1"], "Mode 2 kills Ravenhill Flock."),
    ):
        check = mode_kill_check(state, _modes(), chosen, said)
        assert check is not None and check.replacement == "" and not check.applies
        planner = _planner(said)
        assert planner._correct_mode_kill_claim(_modes(), state, chosen) == chosen
        assert "no other mode kills it" in planner._last_decision_reasoning


def test_a_targeted_pick_that_can_kill_the_claimed_creature_is_no_false_claim():
    # Mode 2 can aim its 4 at either creature: a claim on either one is true, whichever target the
    # line search's best run of the mode happened to use (``ModeComparison.kills`` names only that one).
    theirs = [_card(351, "Ravenhill Flock", rb.OPPONENT, turn_entered_battlefield=11),
              _card(601, "Hill Giant", rb.OPPONENT, turn_entered_battlefield=9)]  # fmt: skip
    assert (
        mode_kill_check(
            _t12(on_stack=True, theirs=theirs), _modes(), ["idx:1"], "Mode 2 kills Ravenhill Flock."
        )
        is None
    )


def test_marked_damage_counts_for_the_targeted_mode():
    # A 4/5 with 3 damage marked (ledger #109): mode 2's 4 kills it, the sweep's 1 does not.
    wounded = [
        _card(601, "Hill Giant", rb.OPPONENT, turn_entered_battlefield=9, power=4, toughness=5, damage=3)
    ]
    state = _t12(on_stack=True, theirs=wounded)
    menu = read_menu(state, _modes())
    assert menu is not None and menu.kills["idx:1"].kills(601) and not menu.kills["idx:0"].kills(601)
    assert mode_kill_check(state, _modes(), ["idx:1"], "Mode 2 kills Hill Giant.") is None
    check = mode_kill_check(state, _modes(), ["idx:0"], "Mode 1 finishes off the Hill Giant.")
    assert check is not None and check.applies and check.replacement == "idx:1"
    # Undamaged, the 4 does not kill a 4/5 either: no mode does.
    whole = [_card(601, "Hill Giant", rb.OPPONENT, turn_entered_battlefield=9, power=4, toughness=5)]
    assert not read_menu(_t12(on_stack=True, theirs=whole), _modes()).kills["idx:1"].kills(601)
    # With 4 marked, the sweep's 1 finishes it too.
    finished = [
        _card(601, "Hill Giant", rb.OPPONENT, turn_entered_battlefield=9, power=4, toughness=5, damage=4)
    ]
    assert read_menu(_t12(on_stack=True, theirs=finished), _modes()).kills["idx:0"].kills(601)


def test_unknown_power_and_toughness_is_never_corrected():
    blank = [
        _card(351, "Ravenhill Flock", rb.OPPONENT, turn_entered_battlefield=11, power=None, toughness=None)
    ]
    assert mode_kill_check(_t12(on_stack=True, theirs=blank), _modes(), ["idx:0"], LIVE_MODE_ANSWER) is None


def test_other_decisions_are_untouched():
    planner = _planner(LIVE_MODE_ANSWER)
    menu = actions_decision([("idx:0", "Cast Iroh's Demonstration", cast(325, 97417), True)])
    assert planner._correct_mode_kill_claim(menu, _t12(on_stack=False), ["idx:0"]) == ["idx:0"]
    assert planner._correct_mode_kill_claim(_modes(), _t12(on_stack=True), ["idx:0", "idx:1"]) == [
        "idx:0",
        "idx:1",
    ]


# --- #144: the hedge and the line's mode ------------------------------------------------------


def test_the_t12_line_casts_mode_2_on_the_flock():
    state = _t12(on_stack=False)
    assert (
        ActionPlanner._searched_line_mode(state, "Iroh's Demonstration")
        == "deals 4 damage to target creature"
    )
    assert ActionPlanner._searched_line_target(state, "Iroh's Demonstration") == ("Ravenhill Flock", 351)


def _cast_menu() -> object:
    # The recorded menu (match packet decision 24, request [244, 319]).
    return actions_decision(
        [("idx:0", "Cast Iroh's Demonstration", cast(325, 97417), True), ("pass", "Pass", None, None)]
    )


def test_the_live_low_answer_naming_mode_1_is_not_the_lines_play(caplog):
    state = _t12(on_stack=False)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert board_math_agreement(_cast_menu(), state, ["idx:0"], LIVE_CAST_ANSWER) == ""
    assert "names mode 1 (deals 1 damage to each creature…), not the line's mode" in caplog.text
    # The same cast described as the line's mode, or with no mode, is the line's play.
    for said in (
        "Iroh's 4 damage kills Ravenhill Flock so Emberheart attacks unblocked.",
        "Iroh's mode 2 kills Ravenhill Flock, the only blocker.",
        "Iroh's Demonstration kills Ravenhill Flock, the only blocker.",
        "Iroh's single-target mode on Ravenhill Flock over the 1-damage sweep.",
    ):
        assert board_math_agreement(_cast_menu(), state, ["idx:0"], said), said


@pytest.mark.parametrize(
    "said",
    [
        "Iroh's sweep clears Ravenhill Flock.",
        "Iroh's 1 damage to each creature finishes Ravenhill Flock.",
        "Iroh's first mode kills Ravenhill Flock.",
        "Cast Iroh's on Ravenhill Flock; mode 1 hits each creature they control.",
    ],
)
def test_a_low_answer_naming_the_other_mode_waits_for_high(said, caplog):
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert board_math_agreement(_cast_menu(), _t12(on_stack=False), ["idx:0"], said) == ""
    # Rejected for the mode: each one names the line's target.
    assert "names mode 1 (deals 1 damage to each creature…), not the line's mode" in caplog.text
    assert "but not at its target" not in caplog.text


def test_other_modes_named_matches_the_lines_mode_text():
    iroh = CARDS["Iroh's Demonstration"] | {"name": "Iroh's Demonstration"}
    target_mode, sweep_mode = "deals 4 damage to target creature", "deals 1 damage to each creature…"
    assert other_modes_named(LIVE_CAST_ANSWER, iroh, target_mode) == [
        "mode 1 (deals 1 damage to each creature…)"
    ]
    assert other_modes_named(LIVE_CAST_ANSWER, iroh, sweep_mode) == []
    assert other_modes_named("Iroh's 4-damage mode kills the Queen.", iroh, sweep_mode) == [
        "mode 2 (deals 4 damage to target creature)"
    ]
    # Not the card's mode, or no modes: nothing to compare.
    assert other_modes_named(LIVE_CAST_ANSWER, iroh, "gain 4 life") == []
    assert other_modes_named(LIVE_CAST_ANSWER, CARDS["Shock"] | {"name": "Shock"}, target_mode) == []


def test_a_line_without_a_mode_keeps_the_old_agreement(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_searched_line_mode", staticmethod(lambda state, name: ""))
    assert board_math_agreement(_cast_menu(), _t12(on_stack=False), ["idx:0"], LIVE_CAST_ANSWER)
    # A card without modes is never checked, whatever the reasoning says.
    monkeypatch.undo()
    monkeypatch.setattr(action_planner, "find_source", lambda state, meta: {"name": "Iroh's Demonstration"})
    monkeypatch.setattr(
        ActionPlanner,
        "_searched_line_mode",
        staticmethod(lambda state, name: pytest.fail("not a modal card")),
    )
    assert board_math_agreement(_cast_menu(), _t12(on_stack=False), ["idx:0"], LIVE_CAST_ANSWER)
