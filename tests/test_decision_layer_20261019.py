"""Typed-decision effort, hedge and guards: 2026-10-19 Mono-Red Burn ladder, ledger #36 #37 #54 #56 #57
#59 #60 #64 #68 #77 #80 #83 (autopilot-improvements.md).

Boards are the games' own, read from the archived Player.log
(~/.arenamcp/log_archive/ladder-2026-10-19/) and the coach log; card text is
Scryfall's. Each test names its window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from tests.race_boards_20261019 import CARDS
from tests.test_burn_targeting_20261019 import _creature, _mountain, _spell, _state, _targets

from arenamcp import action_planner as planner_module
from arenamcp import board_assessment
from arenamcp.action_planner import (
    DECLINE_DECISION,
    ActionPlan,
    ActionPlanner,
    ActionType,
    GameAction,
    SecondLookError,
    _reasoning_names_target,
    board_math_agreement,
)
from arenamcp.decision_memory import (
    CANCELLED,
    DecisionMemory,
    _kicker_affordable,
    burn_combo_note,
    critical_decision,
    ward_tax,
)
from arenamcp.decisions import DecisionOption, PendingDecision

ME, THEM = 2, 1


def _card(iid, name, seat, **fields):
    base = dict(CARDS.get(name, {}))
    base.update(fields)
    return {"instance_id": iid, "name": name, "controller_seat_id": seat, "owner_seat_id": seat, **base}


def _land(iid, seat=ME, tapped=False, name="Mountain"):
    return _card(iid, name, seat, is_tapped=tapped)


def _board(*, ours, theirs, hand=(), stack=(), turn=6, life=20, their_life=17, lands_played=0, **turn_info):
    info = {
        "turn_number": turn,
        "active_player": ME,
        "priority_player": ME,
        "phase": "Phase_Main1",
        "step": "",
    }
    info.update(turn_info)
    return {
        "match_id": "ladder-20261019",
        "local_seat_id": ME,
        "turn": info,
        "players": [
            {"seat_id": ME, "life_total": life, "is_local": True, "lands_played": lands_played},
            {"seat_id": THEM, "life_total": their_life, "is_local": False},
        ],
        "battlefield": [*ours, *theirs],
        "hand": list(hand),
        "graveyard": [],
        "exile": [],
        "stack": list(stack),
    }


def _cast(option_id, card, payable=True):
    return DecisionOption(
        option_id,
        f"Cast {card['name']}",
        payable=payable,
        meta={"actionType": "ActionType_Cast", "instanceId": card["instance_id"], "grpId": 90000},
    )


def _menu(*options, can_pass=True):
    opts = list(options) + ([DecisionOption("pass", "Pass")] if can_pass else [])
    return PendingDecision((1, 1), "ActionsAvailable", tuple(opts), can_pass=can_pass)


class _Model:
    """A scripted model: answers per reasoning effort (a list is consumed in order), recording each call."""

    def __init__(self, low, high=None, *, high_delay=0.0):
        self.answers = {
            None: low if isinstance(low, list) else [low],
            "high": high if isinstance(high, list) else [high],
        }
        self.high_delay = high_delay
        self.efforts: list = []
        self.prompts: list[str] = []
        self.lock = threading.Lock()

    def complete(self, system, user, *args, **kwargs):
        if "PENDING DECISION" not in user:
            return "{}"
        effort = kwargs.get("reasoning_effort")
        with self.lock:
            self.efforts.append(effort)
            self.prompts.append(user)
            queue = self.answers[effort]
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if effort == "high" and self.high_delay:
            cancel = kwargs.get("cancel_event")
            end = time.monotonic() + self.high_delay
            while time.monotonic() < end:
                if cancel is not None and cancel.is_set():
                    raise TimeoutError("request cancelled")
                time.sleep(0.005)
        if answer is None:
            raise TimeoutError("LLM streaming time budget exhausted")
        return answer


def _planner(model) -> ActionPlanner:
    return ActionPlanner(model, timeout=5, land_drop_first=False)


def _reply(option_id, reasoning):
    return json.dumps({"option_ids": [option_id], "reasoning": reasoning})


# --- #64: burn that kills together --------------------------------------------------------------
# 115944ce T6, 11:11:29: two Mountains and three in hand (no land played yet), Shock and Iroh's
# Demonstration in hand, Trostani, Three Whispers 5/5 (the anthem already counted). The effort gate
# said "low effort, burn only goes face (Iroh's ... can kill nothing; Shock can kill nothing)".

IROH = {
    "type_line": "Sorcery — Lesson",
    "mana_cost": "{1}{R}",
    "card_types": ["CardType_Sorcery"],
    "oracle_text": "Choose one —\n• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "• Iroh's Demonstration deals 4 damage to target creature.",
}
TROSTANI = {
    "type_line": "Legendary Creature — Dryad",
    "card_types": ["CardType_Creature"],
    "power": 5,
    "toughness": 5,
    "oracle_text": "{1}{G}: Target creature gains deathtouch until end of turn.\n{G/W}: Target creature gains "
    "vigilance until end of turn.\n{2}{W}: Target creature gains double strike until end of turn.",
}
SHOCK_115 = _card(220, "Shock", ME)
IROH_115 = _card(219, "Iroh's Demonstration", ME, **IROH)


def _trostani_board(*, lands=2, lands_played=0, stack=()):
    hand = [SHOCK_115, IROH_115, _card(303, "Slickshot Show-Off", ME), _land(290), _land(281)]
    hand = [card for card in hand if card not in stack]
    return _board(
        ours=[*(_land(282 + 9 * i) for i in range(lands)), _card(283, "Hired Claw", ME, is_tapped=False)],
        theirs=[_card(299, "Trostani, Three Whispers", THEM, **TROSTANI), _land(298, THEM, True, "Forest")],
        hand=hand,
        stack=stack,
        lands_played=lands_played,
    )


TROSTANI_MENU = _menu(
    _cast("idx:0", IROH_115), _cast("idx:1", SHOCK_115), _cast("idx:2", _card(303, "Slickshot Show-Off", ME))
)


def test_burn_that_kills_together_is_removal_and_the_prompt_says_so():
    skipped: list[str] = []
    why = critical_decision(TROSTANI_MENU, _trostani_board(), "", skipped)
    assert why == "removal available: Iroh's Demonstration + Shock kill Trostani, Three Whispers"
    assert skipped == [
        "Iroh's Demonstration can kill nothing on their side",
        "Shock can kill nothing on their side",
    ]
    note = burn_combo_note(TROSTANI_MENU, _trostani_board())
    assert note.startswith(
        "BURN COMBO: Iroh's Demonstration (4) + Shock (2) = 6 damage kills Trostani, Three Whispers (5/5) "
        "this turn for 3 mana"
    )
    # 11:11:35, the land played: three untapped Mountains pay for both.
    assert critical_decision(TROSTANI_MENU, _trostani_board(lands=3, lands_played=1), "").startswith(
        "removal"
    )
    # Without the mana for both (land already played, two Mountains) it is face burn again.
    assert critical_decision(TROSTANI_MENU, _trostani_board(lands=2, lands_played=1), "") == ""
    assert burn_combo_note(TROSTANI_MENU, _trostani_board(lands=2, lands_played=1)) == ""


def test_the_target_step_of_a_combined_kill_is_removal_too():
    shock = dict(SHOCK_115)
    board = _trostani_board(lands=3, lands_played=1, stack=[shock])
    targets = PendingDecision(
        (1, 2), "SelectTargets", (DecisionOption("tgt:299", "Trostani"),), source_label="Shock"
    )
    assert critical_decision(targets, board, "") == (
        "removal target: Shock (Shock + Iroh's Demonstration kill Trostani, Three Whispers)"
    )
    assert "Aim this spell at Trostani, Three Whispers" in burn_combo_note(targets, board)
    # Shock's own cost is still owed while its targets are chosen: two lands pay for Shock alone.
    assert critical_decision(targets, _trostani_board(lands=2, lands_played=1, stack=[shock]), "") == ""


def test_the_combined_kill_runs_the_decision_at_high_effort_with_the_combo_in_the_prompt():
    model = _Model(_reply("idx:1", "low"), _reply("idx:1", "Shock first, then Iroh's 4 kills Trostani."))
    planner = _planner(model)
    planner._llm_decision_options(TROSTANI_MENU, _trostani_board())
    assert "high" in model.efforts
    assert "BURN COMBO: Iroh's Demonstration (4) + Shock (2)" in model.prompts[0]
    assert model.prompts[0].index("BURN COMBO") < model.prompts[0].index("OPTIONS:")


# --- #83: ward tax and Arena's mana dialect in the effort gate -----------------------------------
# a358cce5 T5 11:39:23: two Mountains, Shock vs Skyward Spider 2/2 with Ward {2} -> "removal available:
# Shock" (Shock plus ward needs three). T7 11:39:57: the Spider 4/3 also wore Sheltered by Ghosts
# (ward {2} more) and Burst Lightning qualified: Arena's "Kicker {o4}" read as one mana.

SPIDER = {
    "type_line": "Creature — Spider Human Hero",
    "card_types": ["CardType_Creature"],
    "oracle_text": "Ward {2} (Whenever this creature becomes the target of a spell or ability an opponent "
    "controls, counter it unless that player pays {2}.)\nThis creature has flying as long as it's modified. "
    "(Equipment, Auras you control, and counters are modifications.)",
}
SHELTERED = {
    "type_line": "Enchantment — Aura",
    "card_types": ["CardType_Enchantment"],
    "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an "
    "opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has lifelink "
    "and ward {2}.",
}
ARENA_BURST = {
    "type_line": "Instant",
    "mana_cost": "{oR}",
    "card_types": ["CardType_Instant"],
    "oracle_text": "Kicker {o4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, it "
    "deals 4 damage instead.",
}


def _spider_board(*, lands=2, power=2, toughness=2, aura=False):
    spider = _card(292, "Skyward Spider", THEM, power=power, toughness=toughness, is_tapped=True, **SPIDER)
    theirs = [spider, _land(291, THEM, True, "Plains"), _land(284, THEM, True, "Plains")]
    if aura:
        theirs.append(_card(301, "Sheltered by Ghosts", THEM, attached_to_id=292, **SHELTERED))
    return _board(
        ours=[*(_land(279 + 7 * i) for i in range(lands)), _card(296, "Slickshot Show-Off", ME)],
        theirs=theirs,
        hand=[
            _card(160, "Shock", ME),
            _card(285, "Burst Lightning", ME, **ARENA_BURST),
            _card(163, "Boltwave", ME),
        ],
        lands_played=1,
        turn=7,
    )


SPIDER_MENU = _menu(
    _cast("idx:0", _card(160, "Shock", ME)),
    _cast("idx:1", _card(163, "Boltwave", ME)),
    _cast("idx:2", _card(285, "Burst Lightning", ME)),
)


def test_a_warded_creature_counts_only_when_the_spell_and_its_ward_fit_our_mana():
    skipped: list[str] = []
    assert critical_decision(SPIDER_MENU, _spider_board(), "", skipped) == ""  # T5: 1 + 2 > 2 lands
    assert "Shock can kill nothing on their side" in skipped
    assert critical_decision(SPIDER_MENU, _spider_board(lands=3), "") == "removal available: Shock"


def test_aura_ward_adds_up_and_arena_kicker_costs_are_read():
    board = _spider_board(power=4, toughness=3, aura=True)
    spider = next(card for card in board["battlefield"] if card["name"] == "Skyward Spider")
    assert ward_tax(spider, board) == 4
    burst = _card(285, "Burst Lightning", ME, **ARENA_BURST)
    assert _kicker_affordable(burst, ARENA_BURST["oracle_text"], board) is False  # {oR} + {o4} is 5, not 2
    assert critical_decision(SPIDER_MENU, board, "") == ""  # T7: was "removal available: Burst Lightning"


# --- #56: the hedge's agreement needs the line's target; face burn follows the line ---------------
# 15e0eb20 T7 10:55:38: LINES best "Lightning Strike (on Nullpriest of Oblivion), attack with ...";
# the low answer cast Strike "pointing ... at face" and counted as the board-math play.

STRIKE_SAID = (
    "Opponent is at 10 and we're racing ahead; pointing Lightning Strike at face advances our clock while "
    "keeping the second Strike for blockers."
)


@pytest.mark.parametrize(
    "reasoning, target, agrees",
    [
        (STRIKE_SAID, "Nullpriest of Oblivion", False),
        (
            "Lightning Strike kills Nullpriest of Oblivion before its lifelink swings the race.",
            "Nullpriest of Oblivion",
            True,
        ),
        ("Kill the Nullpriest instead of going face.", "Nullpriest of Oblivion", True),
        ("Strike face rather than killing Nullpriest.", "Nullpriest of Oblivion", False),
        (STRIKE_SAID, "opponent", True),
        ("Lightning Strike kills Nullpriest of Oblivion.", "opponent", False),
        ("", "opponent", False),
    ],
)
def test_the_stated_target_must_be_the_lines(reasoning, target, agrees):
    assert _reasoning_names_target(reasoning, target) is agrees


def test_board_math_agreement_checks_the_target(monkeypatch):
    strike = _spell(
        162, "Lightning Strike", "Instant", "{1}{R}", "Lightning Strike deals 3 damage to any target.", seat=2
    )
    state = _state([_mountain(279), _mountain(288)], hand=[strike])
    menu = _menu(_cast("idx:0", strike))
    monkeypatch.setattr(
        planner_module,
        "board_math_option_pick",
        lambda d, s: (["idx:0"], "the board-math line casts Lightning Strike"),
    )
    monkeypatch.setattr(planner_module, "line_fallback_option_pick", lambda d, s, p: None)
    monkeypatch.setattr(
        ActionPlanner, "_searched_line_target", staticmethod(lambda s, name: ("Nullpriest of Oblivion", 312))
    )
    assert board_math_agreement(menu, state, ["idx:0"], STRIKE_SAID) == ""
    assert board_math_agreement(menu, state, ["idx:0"], "Strike kills Nullpriest of Oblivion.") == (
        "the board-math line casts Lightning Strike"
    )
    monkeypatch.setattr(ActionPlanner, "_searched_line_target", staticmethod(lambda s, name: ("", 0)))
    assert board_math_agreement(menu, state, ["idx:0"], STRIKE_SAID)  # no aimed target: the spell agrees


def _nullpriest_targets(opp_life=10):
    strike = _spell(
        319, "Lightning Strike", "Instant", "{1}{R}", "Lightning Strike deals 3 damage to any target."
    )
    state = _state(
        [
            _mountain(279),
            _mountain(288),
            _mountain(298),
            _creature(289, "Emberheart Challenger", 2, 3, 3),
            _creature(304, "Hired Claw", 2, 1, 2),
            _creature(312, "Nullpriest of Oblivion", 1, 2, 1, "Lifelink\nMenace"),
            _creature(315, "Bloodghast", 1, 2, 1, "This creature can't block."),
        ],
        stack=[strike],
        opp_life=opp_life,
        source=319,
    )
    state["turn"]["turn_number"] = 7
    return state, _targets([1, 2, 289, 304, 312, 315], "Lightning Strike")


TARGET_10_55_43 = json.dumps(
    {
        "option_ids": ["tgt:1"],
        "reasoning": "Consistent with my earlier decision this turn: opponent is at 10 and we're ahead in the race, "
        "so 3 to face advances the clock while their 2/1s only trade with my attackers anyway.",
        "target_controllers": {"tgt:1": "opponent"},
    }
)


def _with_line_intent(planner):
    planner._cast_intent = {
        "card": "Lightning Strike",
        "said": STRIKE_SAID,
        "line_target": "Nullpriest of Oblivion",
        "line_target_id": 312,
        "at": time.monotonic(),
    }


def test_face_burn_the_line_aims_at_a_creature_follows_the_line(monkeypatch, caplog):
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)
    state, decision = _nullpriest_targets()
    planner = _planner(_Model(TARGET_10_55_43, TARGET_10_55_43))
    _with_line_intent(planner)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(decision, state) == ["tgt:312"]
    assert (
        "the searched line aims Lightning Strike at Nullpriest of Oblivion"
        in planner.get_decision_reasoning(["tgt:312"]).replace("The searched", "the searched")
    )
    assert "[model: Consistent with my earlier decision" in caplog.text
    # Face stays when the 3 damage is lethal.
    state, decision = _nullpriest_targets(opp_life=3)
    planner = _planner(_Model(TARGET_10_55_43, TARGET_10_55_43))
    _with_line_intent(planner)
    assert planner.plan_decision_options(decision, state) == ["tgt:1"]


# --- #57: false lethal claims without the word "lethal" ------------------------------------------
# 15e0eb20 T9 10:56:13: "Lightning Strike to face puts opponent at 2, letting Emberheart Challenger's
# attack close the game this turn" while an untapped 4/4 Gwenom blocked the only attacker.

CLOSE_THE_GAME = (
    "Lightning Strike to face puts opponent at 2, letting Emberheart Challenger's attack close the game this "
    "turn; Impractical Joke on Bloodghast doesn't advance lethal."
)


@pytest.mark.parametrize(
    "text, claims",
    [
        (CLOSE_THE_GAME, True),
        (
            "Opponent is at 2 and Bloodghast can't block, so passing to attack with hasty Emberheart Challenger "
            "ends the game; Impractical Joke on Bloodghast accomplishes nothing.",
            True,
        ),
        ("Opponent is at 2 life so the Vaultborn attack should close the game.", True),
        ("Casting it at the opponent wins the game outright.", True),
        ("A huge flying body closes the game fastest.", False),
        ("Attacks plus Hired Claw's ping close the game two turns sooner than passing.", False),
        ("Tutor two creatures to build toward closing out the game.", False),
        ("A 9/9 trampler threatens to close out the game against the opponent's 12 life.", False),
    ],
)
def test_ending_the_game_now_counts_as_a_lethal_claim(text, claims):
    assert bool(ActionPlanner._lethal_claims(text)) is claims


def _joke_menu():
    strike = _card(318, "Lightning Strike", ME)
    joke = _card(
        336,
        "Impractical Joke",
        ME,
        type_line="Sorcery",
        mana_cost="{R}",
        card_types=["CardType_Sorcery"],
        oracle_text="Impractical Joke deals 3 damage to up to one target creature or planeswalker.",
    )
    state = _board(
        ours=[_land(279), _land(288), _land(298), _card(289, "Emberheart Challenger", ME)],
        theirs=[
            _card(
                330,
                "Gwenom, Remorseless",
                THEM,
                type_line="Legendary Creature — Symbiote Spider Hero",
                card_types=["CardType_Creature"],
                power=4,
                toughness=4,
                oracle_text="Deathtouch, lifelink",
            ),
            _card(
                315,
                "Bloodghast",
                THEM,
                type_line="Creature — Vampire Spirit",
                card_types=["CardType_Creature"],
                power=2,
                toughness=1,
                oracle_text="This creature can't block.",
            ),
        ],
        hand=[strike, joke],
        turn=9,
        their_life=5,
        lands_played=1,
    )
    return state, _menu(_cast("idx:0", strike), _cast("idx:1", joke))


def _no_lines(monkeypatch):
    monkeypatch.setattr(
        ActionPlanner, "_line_prompt_context", staticmethod(lambda decision, state: (None, "", {}))
    )


def test_a_low_pick_resting_on_a_false_win_claim_gets_a_second_look(monkeypatch):
    monkeypatch.setattr(planner_module, "critical_decision", lambda *a, **k: "")
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)
    _no_lines(monkeypatch)
    state, menu = _joke_menu()
    model = _Model(
        _reply("idx:0", CLOSE_THE_GAME), _reply("idx:1", "Joke kills Bloodghast; Gwenom blocks anyway.")
    )
    planner = _planner(model)
    assert planner._llm_decision_options(menu, state) == ["idx:1"]
    assert model.efforts == [None, "high"]
    assert "SECOND LOOK: you chose Cast Lightning Strike." in model.prompts[1]
    assert "the board math finds no lethal this turn (they are at 5)" in model.prompts[1]
    assert planner.get_last_decision_trace()["second_look"]["first"] == ["idx:0"]


def test_a_high_effort_pick_resting_on_a_false_win_claim_goes_to_the_board_math(monkeypatch):
    monkeypatch.setattr(
        planner_module, "critical_decision", lambda *a, **k: "removal available: Lightning Strike"
    )
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)
    monkeypatch.setattr(
        planner_module,
        "board_math_option_pick",
        lambda d, s: (["idx:1"], "the board-math line casts Impractical Joke"),
    )
    monkeypatch.setattr(planner_module, "line_fallback_option_pick", lambda d, s, p: None)
    _no_lines(monkeypatch)
    state, menu = _joke_menu()
    planner = _planner(_Model(_reply("idx:0", CLOSE_THE_GAME), _reply("idx:0", CLOSE_THE_GAME)))
    monkeypatch.setattr(planner, "_hold_instant_burn", lambda decision, s: (decision, []))
    assert planner.plan_decision_options(menu, state) == ["idx:1"]
    assert planner.get_decision_reasoning(["idx:1"]).startswith("The model's pick was set aside;")
    assert planner.last_llm_failure == "set_aside"


def test_the_close_the_game_claim_is_narrated_as_the_board_math(monkeypatch):
    facts = SimpleNamespace(lethal_now=False, posture="attack", lines=[], opp_life=5)
    monkeypatch.setattr(board_assessment, "assess", lambda state: facts)
    planner = ActionPlanner.__new__(ActionPlanner)
    planner._last_decision_reasoning = CLOSE_THE_GAME
    planner._last_decision_trace = {}
    planner._correct_false_lethal_claim({})
    assert "close the game" not in planner._last_decision_reasoning
    assert planner._last_decision_reasoning.endswith("Board math: not lethal this turn (they are at 5).")


# --- #59 / #77: face burn the line search does not rate, at low effort ---------------------------
# abde2a07 T6 11:30:12: Tectonic Hazard into an empty opposing board ("zero downside"), no LINE tag;
# held to T10 it would have killed their only reach creature. Three Mountains (two tapped), Hired Claw.

HAZARD_CARD = _card(162, "Tectonic Hazard", ME)


def _hazard_board(theirs=()):
    return _board(
        ours=[_land(305), _land(290, tapped=True), _land(281, tapped=True), _card(282, "Hired Claw", ME)],
        theirs=[_land(299, THEM, True, "Forest"), _land(285, THEM, True, "Forest"), *theirs],
        hand=[HAZARD_CARD, _card(160, "Emberheart Challenger", ME), _land(289)],
        lands_played=1,
        their_life=18,
    )


HAZARD_MENU = _menu(
    _cast("idx:0", _card(159, "Slickshot Show-Off", ME), payable=False),
    _cast("idx:1", _card(160, "Emberheart Challenger", ME), payable=False),
    _cast("idx:2", HAZARD_CARD),
)
ZERO_DOWNSIDE = (
    "With one red mana open and no creatures to protect, Tectonic Hazard chips the opponent for 1 toward "
    "lethal range at zero downside."
)


def test_sweeper_burn_into_an_empty_board_gets_a_second_look_at_high_effort(monkeypatch):
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)
    _no_lines(monkeypatch)
    model = _Model(_reply("idx:2", ZERO_DOWNSIDE), _reply("pass", "Hold Hazard for a creature it kills."))
    planner = _planner(model)
    assert planner.plan_decision_options(HAZARD_MENU, _hazard_board()) == ["pass"]
    assert model.efforts == [None, "high"]
    assert (
        "Tectonic Hazard only does face damage now: it hits no creature now (their board is empty), nothing is "
        "lethal this turn, and the line search does not rate it above holding it." in model.prompts[1]
    )


def test_face_burn_concerns_follow_the_line_tag(monkeypatch):
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: False)
    bear = _card(601, "Grizzly Bears", THEM, is_tapped=False)
    planner = _planner(_Model("{}"))
    board = _hazard_board([bear])  # a 2/2: Hazard's 1 kills nothing
    negative = {"idx:2": "[LINE: -3 vs best; 20 after T7, 20 after T9]"}
    concerns = planner._second_look_concerns(HAZARD_MENU, board, ["idx:2"], ZERO_DOWNSIDE, None, negative)
    assert [kind for kind, _ in concerns] == ["face_burn"]
    assert "rates it 3 below its best line" in concerns[0][1]
    best = {"idx:2": "[LINE best: 20 after T7, 20 after T9]"}
    assert planner._second_look_concerns(HAZARD_MENU, board, ["idx:2"], ZERO_DOWNSIDE, None, best) == []
    near = {"idx:2": "[LINE: ~best; 20 after T7, 20 after T9]"}
    assert planner._second_look_concerns(HAZARD_MENU, board, ["idx:2"], ZERO_DOWNSIDE, None, near) == []
    # Hazard that kills an X/1 is removal, not face burn.
    elf = _card(602, "Grizzly Bears", THEM, power=1, toughness=1)
    assert (
        planner._second_look_concerns(HAZARD_MENU, _hazard_board([elf]), ["idx:2"], "", None, negative) == []
    )
    # Their turn: no second look.
    their_turn = _hazard_board([bear])
    their_turn["turn"]["active_player"] = THEM
    assert planner._second_look_concerns(HAZARD_MENU, their_turn, ["idx:2"], "", None, negative) == []


def _hedge(planner, *, high, low, low_doubt):
    def ask(message, *, effort, budget, cancel_event=None):
        delay, answer = high if effort == "high" else low
        end = time.monotonic() + delay
        while time.monotonic() < end:
            if cancel_event is not None and cancel_event.is_set():
                raise TimeoutError("request cancelled")
            time.sleep(0.005)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def parse(text):
        data = json.loads(text)
        return data, [str(i) for i in data["option_ids"]]

    planner._last_decision_trace = {}
    return planner._hedged_decision(ask, parse, "PENDING DECISION", 2.0, None, low_doubt)


def test_a_doubted_low_answer_waits_for_high_and_never_fires_alone(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_HIGH_AFTER_LOW_S", 0.05)
    planner = ActionPlanner(backend=None)
    low, high = _reply("idx:2", "low: Hazard face"), _reply("pass", "high: hold")
    data, chosen = _hedge(planner, high=(0.4, high), low=(0.01, low), low_doubt=lambda ids, data: "face_burn")
    assert chosen == ["pass"]  # past the 0.05 s grace: the doubt waited for it
    data, chosen = _hedge(planner, high=(0.4, high), low=(0.01, low), low_doubt=lambda ids, data: "")
    assert chosen == ["idx:2"]  # undoubted: the grace ran out first, as before
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 0.2)
    with pytest.raises(SecondLookError):
        _hedge(
            planner,
            high=(0.05, TimeoutError("LLM streaming time budget exhausted")),
            low=(0.01, low),
            low_doubt=lambda ids, data: "face_burn",
        )


# --- #80: instant burn that only goes face waits for their end step --------------------------------
# 772233af T6 11:36:01: our Fleeting Effigy on the stack, an empty opposing board, Lightning Strike and
# Shock castable; Strike went face at low effort ("burn only goes face"). The plan: "hold: nothing worth
# holding". Held, it would have killed Vadmir (T7) or Baron Zemo (T9).


def _strike_board(*, theirs=(), ours=(), stack=None, their_life=12, **turn):
    effigy = _card(368, "Fleeting Effigy", ME)
    return _board(
        ours=[_land(345, tapped=True), _land(359), _land(373), *ours],
        theirs=[_land(401, THEM, True, "Swamp"), _land(402, THEM, False, "Swamp"), *theirs],
        hand=[_card(281, "Lightning Strike", ME), _card(295, "Shock", ME)],
        stack=[effigy] if stack is None else stack,
        their_life=their_life,
        lands_played=1,
        **turn,
    )


STRIKE_MENU = _menu(
    _cast("idx:0", _card(281, "Lightning Strike", ME)), _cast("idx:1", _card(295, "Shock", ME))
)


def test_face_only_instant_burn_is_held_in_our_main_phase(caplog):
    model = _Model(_reply("idx:0", "Empty opposing board makes face burn optimal."))
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(STRIKE_MENU, _strike_board()) == ["pass"]
    assert model.efforts == []  # nothing else to choose: no model call
    assert "Instant timing: holding Lightning Strike, Shock for the opponent's end step" in caplog.text
    assert planner.get_decision_reasoning(["pass"]).startswith(
        "Holding Lightning Strike, Shock: instant burn"
    )


def test_other_plays_are_still_asked_about_and_the_prompt_says_why_the_burn_waits():
    hired = _card(300, "Hired Claw", ME)
    menu = _menu(
        _cast("idx:0", _card(281, "Lightning Strike", ME)),
        _cast("idx:1", _card(295, "Shock", ME)),
        _cast("idx:2", hired),
    )
    board = _strike_board(stack=[])
    board["hand"].append(hired)
    model = _Model(_reply("idx:2", "Deploy Hired Claw."))
    planner = _planner(model)
    assert planner.plan_decision_options(menu, board) == ["idx:2"]
    prompt = model.prompts[0]
    assert "INSTANT TIMING: Lightning Strike, Shock are held this window" in prompt
    options = prompt[prompt.index("OPTIONS:") : prompt.index("GAME STATE:")]
    assert "idx:0" not in options and "idx:1" not in options and "idx:2" in options


@pytest.mark.parametrize(
    "case",
    ["prowess on board", "a kill", "lethal", "their spell on the stack"],
)
def test_burn_with_a_use_now_is_not_held(case):
    ours, theirs, stack, life = (), (), None, 12
    if case == "prowess on board":
        ours = [_card(296, "Slickshot Show-Off", ME)]
    elif case == "a kill":
        theirs = [_card(387, "Grizzly Bears", THEM)]
    elif case == "lethal":
        life = 3
    else:
        stack = [_card(450, "Grizzly Bears", THEM)]
    model = _Model(_reply("idx:0", "Strike now."), _reply("idx:0", "Strike now, high."))
    planner = _planner(model)
    planner.plan_decision_options(
        STRIKE_MENU, _strike_board(ours=ours, theirs=theirs, stack=stack, their_life=life)
    )
    assert model.efforts, case  # the model was asked, with the burn on the menu
    options = model.prompts[0][model.prompts[0].index("OPTIONS:") :]
    assert "idx:0: Cast Lightning Strike" in options


def test_at_their_end_step_the_burn_is_offered_and_the_prompt_says_it_is_the_last_window():
    board = _strike_board(
        stack=[], active_player=THEM, priority_player=ME, phase="Phase_Ending", step="Step_End"
    )
    model = _Model(_reply("idx:0", "End step: Strike face."))
    planner = _planner(model)
    assert planner.plan_decision_options(STRIKE_MENU, board) == ["idx:0"]
    assert "OPPONENT'S END STEP: the last window before we untap" in model.prompts[0]


# --- #37: an attack-only creature cast before combat whose attack the guard holds ----------------
# 6175f002 T10 10:04:21: Fleeting Effigy cast with the last Mountain ("a recurring haste attacker");
# Momo, Playful Pet 5/5 flying vigilance stood untapped, the losing-attack guard held the Effigy and it
# went back to hand at end step.

MOMO = {
    "type_line": "Legendary Creature — Lemur Bat Ally",
    "card_types": ["CardType_Creature"],
    "power": 5,
    "toughness": 5,
    "keywords": ["flying", "vigilance"],
    "oracle_text": "Flying, vigilance\nWhen Momo leaves the battlefield, choose one —\n• Create a Food token.\n"
    "• Put a +1/+1 counter on target creature you control.\n• Scry 2.",
}


def _effigy_board():
    effigy = _card(391, "Fleeting Effigy", ME)
    claw = _card(392, "Hired Claw", ME)
    board = _board(
        ours=[_land(374), _land(358, tapped=True), _land(344, tapped=True)],
        theirs=[
            _card(370, "Momo, Playful Pet", THEM, **MOMO),
            *(_land(i, THEM, True, "Plains") for i in (340, 351, 369, 384, 404)),
        ],
        hand=[effigy, claw],
        turn=10,
        life=12,
        their_life=20,
        lands_played=1,
    )
    return board, _menu(_cast("idx:0", effigy), _cast("idx:1", claw))


def test_an_attack_only_creature_the_guard_would_hold_gets_a_second_look(monkeypatch):
    monkeypatch.setattr(planner_module, "critical_decision", lambda *a, **k: "")
    _no_lines(monkeypatch)
    board, menu = _effigy_board()
    model = _Model(
        _reply("idx:0", "Fleeting Effigy gives a recurring haste attacker that returns each end step."),
        _reply("idx:1", "Hired Claw stays on the board."),
    )
    planner = _planner(model)
    assert planner._llm_decision_options(menu, board) == ["idx:1"]
    assert model.efforts == [None, "high"]
    second = model.prompts[1]
    assert "Fleeting Effigy only matters if it attacks this turn (it leaves at end step)" in second
    assert "Momo, Playful Pet 5/5 can block Fleeting Effigy 2/2, kill it and survive" in second
    # A creature that stays on the battlefield is no attack-only cast.
    concerns = planner._second_look_concerns(menu, board, ["idx:1"], "", None, {})
    assert [kind for kind, _ in concerns] == []


# --- #54: the model's no-attack against a tapped-out opponent when the line attacks ----------------
# a9cd5a2e T4 10:35:26: Emberheart Challenger 2/2 haste vs one untapped 2/2 Skyward Spider, every land
# of theirs tapped; the line attacked, the model kept it home "to grow with burn spells".


def _tapped_out_attack_window():
    challenger = _card(292, "Emberheart Challenger", ME, turn_entered_battlefield=4)
    spider = _card(287, "Skyward Spider", THEM, power=2, toughness=2, **SPIDER)
    state = _board(
        ours=[_land(281, tapped=True), _land(291, tapped=True), challenger],
        theirs=[spider, _land(279, THEM, True, "Plains"), _land(286, THEM, True, "Plains")],
        turn=4,
        their_life=18,
        phase="Phase_Combat",
        step="Step_DeclareAttack",
    )
    context = {
        "type": "declare_attackers",
        "legal_attackers": ["Emberheart Challenger"],
        "legal_attacker_ids": [292],
        "raw_attackers": [
            {"attackerInstanceId": 292, "legalDamageRecipients": [{"type": "DamageRecType_Player"}]}
        ],
    }
    return state, context


def _line_attacks(monkeypatch, posture="attack"):
    step = SimpleNamespace(
        attack=("Emberheart Challenger",), plays=(None, (), frozenset({292})), at_walkers=()
    )
    best = SimpleNamespace(steps=[step], summary=lambda: "attack with Emberheart Challenger — survives")
    result = SimpleNamespace(posture=posture, best=best)
    assessment = SimpleNamespace(line_search=result, our_turn=True)
    monkeypatch.setattr(board_assessment, "assess", lambda state: assessment)


def test_a_no_attack_into_a_tapped_out_opponent_follows_the_lines_attack(monkeypatch, caplog):
    _line_attacks(monkeypatch)
    state, context = _tapped_out_attack_window()
    plan = ActionPlan(
        actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done", reasoning="Keep it back.")],
        overall_strategy="Keep Emberheart Challenger back to grow with burn spells; race via burn reach.",
    )
    planner = _planner(_Model("{}"))
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        planner._follow_line_attack(plan, state, context)
    attack = plan.actions[0]
    assert attack.action_type == ActionType.DECLARE_ATTACKERS
    assert attack.attacker_names == ["Emberheart Challenger"] and attack.attacker_instance_ids == [292]
    assert plan.fallback_reason == "planner_line_attack"
    assert "Keep Emberheart Challenger back to grow" in attack.model_reasoning
    assert "[model planned: Keep it back. | Keep Emberheart Challenger back" in caplog.text


@pytest.mark.parametrize("change", ["their land untapped", "posture either", "the plan attacks"])
def test_the_line_attack_needs_a_tapped_out_opponent_an_attacking_line_and_an_empty_plan(monkeypatch, change):
    _line_attacks(monkeypatch, posture="either" if change == "posture either" else "attack")
    state, context = _tapped_out_attack_window()
    if change == "their land untapped":
        next(c for c in state["battlefield"] if c["instance_id"] == 279)["is_tapped"] = False
    action = GameAction(ActionType.CLICK_BUTTON, card_name="done")
    if change == "the plan attacks":
        action = GameAction(ActionType.DECLARE_ATTACKERS, attacker_names=["Emberheart Challenger"])
    plan = ActionPlan(actions=[action])
    _planner(_Model("{}"))._follow_line_attack(plan, state, context)
    assert plan.actions == [action] and plan.fallback_reason == ""


# --- #36 / #60: a guard keeps the model's reasoning and the decision memory records the hold -------


def test_the_losing_attack_guard_logs_the_models_reason_and_remembers_the_hold(caplog):
    from tests.test_losing_attack_guard import MENU, AttackBackend, incident

    state = incident()
    planner = ActionPlanner(AttackBackend(), timeout=1, land_drop_first=False)
    with caplog.at_level(logging.WARNING, logger="arenamcp.action_planner"):
        plan = planner.plan_actions(state, "decision_required", MENU, state["decision_context"])
    assert "[model planned: Remove the planeswalker | Trade Fblthp into Jace" in caplog.text
    assert plan.actions[0].model_reasoning.startswith("Remove the planeswalker")
    block = planner._memory().prompt_block(state)
    assert "DeclareAttackers (guard): attack held by guard: Fblthp, Impossibly Lost not attacking" in block
    assert "Keeper of the Quiet Hour 3/2" in block
    planner.plan_actions(state, "decision_required", MENU, state["decision_context"])  # re-planned window
    assert planner._memory().prompt_block(state).count("attack held by guard") == 1


def test_a_lethal_block_guard_logs_the_models_reason():
    planner = _planner(_Model("{}"))
    state = {
        "players": [{"seat_id": 1, "life_total": 4, "is_local": True}, {"seat_id": 2, "life_total": 20}],
        "battlefield": [
            {"instance_id": 10, "name": "Bear", "power": 2, "toughness": 2, "controller_seat_id": 1},
            {
                "instance_id": 20,
                "name": "Ogre",
                "power": 5,
                "toughness": 5,
                "controller_seat_id": 2,
                "is_attacking": True,
            },
        ],
    }
    plan = ActionPlan(
        actions=[GameAction(ActionType.CLICK_BUTTON, card_name="done", reasoning="Preserve blockers!")],
        overall_strategy="No blocks.",
    )
    planner._check_block_survival(plan, state, {"type": "declare_blockers", "legal_blocker_ids": [10]})
    assert plan.actions[0].action_type == ActionType.DECLARE_BLOCKERS
    assert plan.actions[0].model_reasoning == "Preserve blockers! | No blocks."


# --- #68: a declined target cancels the cast; memory says so --------------------------------------
# 115944ce T8 11:13: Iroh's Demonstration was cast, its 4-damage target declined (cast cancelled) and
# recast six times; the memory listed every cast and target, and the model went on "two prior
# 4-damage hits already landed".


def test_a_declined_target_marks_the_cast_cancelled_in_memory():
    memory = DecisionMemory()
    board = _trostani_board()
    iroh = _cast("idx:0", IROH_115)
    memory.record(board, _menu(iroh), ["idx:0"], "Iroh's 4 damage cripples Trostani before attacking.")
    mode = PendingDecision(
        (1, 2),
        "CastingTimeOptions",
        (DecisionOption("idx:1", "Mode 2"),),
        source_label="Iroh's Demonstration",
    )
    memory.record(board, mode, ["idx:1"], "4 damage to the anthemed Trostani.")
    targets = PendingDecision(
        (1, 3), "SelectTargets", (DecisionOption("tgt:299", "Trostani"),), source_label="Iroh's Demonstration"
    )
    memory.record(board, targets, [DECLINE_DECISION], "4 damage kills no legal opposing target (Trostani)")
    lines = memory.prompt_block(board).splitlines()
    assert lines[1].endswith(f"[{CANCELLED}]") and "Cast Iroh's Demonstration" in lines[1]
    assert lines[2].endswith(f"[{CANCELLED}]")
    assert f"SelectTargets (Iroh's Demonstration): {CANCELLED} — 4 damage kills no legal" in lines[3]
    # The next cast is a new chain, not marked.
    memory.record(board, _menu(iroh), ["idx:0"], "Cast it again.")
    assert not memory.prompt_block(board).splitlines()[4].endswith("]")


def test_the_planner_remembers_why_it_declined(monkeypatch):
    planner = _planner(_Model("{}"))

    def declined(decision, state):
        planner._decline_reason = (
            "4 damage kills no legal opposing target (Trostani, Three Whispers); spell kept in hand"
        )
        planner._last_decision_reasoning = (
            "Dealing 4 to the tapped 7/7 Trostani weakens my only real blocker."
        )
        return [DECLINE_DECISION]

    monkeypatch.setattr(planner, "_plan_decision_options", declined)
    targets = PendingDecision(
        (1, 3), "SelectTargets", (DecisionOption("tgt:299", "Trostani"),), source_label="Iroh's Demonstration"
    )
    planner.plan_decision_options(targets, _trostani_board())
    line = planner.decisions_for_turn(6)[-1]
    assert CANCELLED in line and "spell kept in hand" in line and "weakens" not in line


# --- #100: a window within lethal reach runs at high effort --------------------------------------
# 18b1601a T9 12:05:02 (Player.log): they were at 7; ours Slickshot Show-Off x2 (one just cast, haste)
# and Emberheart Challenger, one untapped Mountain, Tectonic Hazard and Fleeting Effigy in hand. Hazard
# (1) pumps both airborne Slickshots +2/+0: exactly 7. The window ran at low effort and cast Effigy,
# which the line search (blind to the pumps, #94) tagged "LINE best".


def _lethal_window(their_life=7, **turn):
    theirs = [
        _card(442, "Traveling Chocobo", THEM, type_line="Creature — Bird", card_types=["CardType_Creature"],
              power=3, toughness=2, oracle_text=""),
        _card(426, "Tifa Lockhart", THEM, type_line="Legendary Creature — Human Monk",
              card_types=["CardType_Creature"], power=1, toughness=2, oracle_text="Trample", is_tapped=True),
        _card(346, "Sazh's Chocobo", THEM, power=4, toughness=5, is_tapped=True),
        *(_land(i, THEM, True, "Forest") for i in (345, 358, 439)),
    ]  # fmt: skip
    ours = [
        _land(411),
        _land(349, tapped=True),
        _land(340, tapped=True),
        _card(350, "Slickshot Show-Off", ME, turn_entered_battlefield=5),
        _card(430, "Emberheart Challenger", ME, turn_entered_battlefield=7),
        _card(447, "Slickshot Show-Off", ME, turn_entered_battlefield=9),
    ]
    hand = [_card(446, "Tectonic Hazard", ME), _card(436, "Fleeting Effigy", ME)]
    return _board(
        ours=ours, theirs=theirs, hand=hand, turn=9, life=9, their_life=their_life, lands_played=1, **turn
    )


LETHAL_MENU = _menu(
    _cast("idx:0", _card(436, "Fleeting Effigy", ME)), _cast("idx:1", _card(446, "Tectonic Hazard", ME))
)


def test_their_life_within_our_reach_makes_the_window_critical():
    why = critical_decision(LETHAL_MENU, _lethal_window(), "")
    assert why.startswith(
        "within lethal reach: up to 10 damage this turn (attackers 4, burn 1, cast pumps +5"
    )
    assert why.endswith("against their 7 life")
    assert critical_decision(LETHAL_MENU, _lethal_window(their_life=11), "") == ""
    # Their turn: our creatures do not attack, so Hazard's 1 is all.
    assert critical_decision(LETHAL_MENU, _lethal_window(active_player=THEM), "") == ""


def test_a_lethal_reach_window_waits_for_high_effort_and_says_why(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_HIGH_AFTER_LOW_S", 0.05)
    model = _Model(
        _reply("idx:0", "Casting Fleeting Effigy adds a third hasty attacker per the best line."),
        _reply("idx:1", "Hazard pumps both Slickshots to 3/2 flying: 1 + 3 + 3 is exactly 7."),
        high_delay=0.4,
    )
    planner = _planner(model)
    assert planner._llm_decision_options(LETHAL_MENU, _lethal_window()) == ["idx:1"]
    assert "high" in model.efforts
    assert "LETHAL REACH: up to 10 damage this turn" in model.prompts[0]
    assert planner.get_last_decision_trace()["hedge"]["winner"] == "high"
