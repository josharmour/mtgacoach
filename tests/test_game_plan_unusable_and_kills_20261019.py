"""Game-plan answers that can't be used, and kill claims (2026-10-19 Standard Ranked, Mono-Red Burn).

#58: "game-plan answer had no usable plan (keeping prior plan)" 15 times in the
session, the answer never logged (10:56:57 match 15e0eb20: 30.7 s at high
effort, 4233 reasoning tokens; 11:11:47 match 115944ce: the turn-6 plan, so T6
ran on a plan written before Anthem of Champions), and nothing in the decision
prompts said the kept plan had not been refreshed.
#70: the 11:14:41 plan (match 115944ce) said "T8 Iroh's kills Trostani":
Iroh's Demonstration deals 4 to one creature, Trostani, Three Whispers was a
7/7 (4/4 + Anthem of Champions + an aura). At 11:15:07 the model read
"dies_to_damage 7" and followed the plan anyway.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy

import pytest

from arenamcp import board_assessment as ba
from arenamcp.board_assessment import assess
from arenamcp.game_plan import (
    GamePlan,
    GamePlanManager,
    compose_strategy_block,
    validate_plan,
)

US, THEM = 2, 1

CARDS = {
    "Mountain": {"type_line": "Basic Land — Mountain", "oracle_text": "({T}: Add {R}.)",
                 "card_types": ["CardType_Land"]},
    "Plains": {"type_line": "Basic Land — Plains", "oracle_text": "({T}: Add {W}.)",
               "card_types": ["CardType_Land"]},
    "Iroh's Demonstration": {
        "type_line": "Sorcery — Lesson", "mana_cost": "{1}{R}", "card_types": ["CardType_Sorcery"],
        "oracle_text": "Choose one — \n•<indent=4%>Iroh's Demonstration deals 1 damage to each creature your "
                       "opponents control. </indent>\n•<indent=4%>Iroh's Demonstration deals 4 damage to target "
                       "creature.</indent>"},
    "Shock": {"type_line": "Instant", "mana_cost": "{R}", "oracle_text": "Shock deals 2 damage to any target.",
              "card_types": ["CardType_Instant"]},
    "Burst Lightning": {"type_line": "Instant", "mana_cost": "{R}",
                        "oracle_text": "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell "
                        "was kicked, it deals 4 damage instead.", "card_types": ["CardType_Instant"]},
    "Slickshot Show-Off": {"type_line": "Creature — Bird Wizard", "mana_cost": "{1}{R}", "power": 1,
                           "toughness": 2, "oracle_text": "Flying, haste\nWhenever you cast a noncreature "
                           "spell, this creature gets +2/+0 until end of turn.",
                           "card_types": ["CardType_Creature"]},
    "Emberheart Challenger": {"type_line": "Creature — Mouse Warrior", "mana_cost": "{1}{R}", "power": 2,
                              "toughness": 2, "oracle_text": "Haste\nProwess", "card_types": ["CardType_Creature"]},
    # 4/4 printed; the snapshot carries the anthem and aura buffs (7/7 trample, lifelink).
    "Trostani, Three Whispers": {
        "type_line": "Legendary Creature — Dryad", "mana_cost": "{G}{G/W}{W}", "power": 7, "toughness": 7,
        "keywords": ["trample", "lifelink"], "card_types": ["CardType_Creature"],
        "oracle_text": "{1}{G}: Target creature gains deathtouch until end of turn.\n{G/W}: Target creature "
                       "gains vigilance until end of turn.\n{2}{W}: Target creature gains double strike until "
                       "end of turn."},
    "Anthem of Champions": {"type_line": "Enchantment", "mana_cost": "{G}{W}",
                            "oracle_text": "Creatures you control get +1/+1.",
                            "card_types": ["CardType_Enchantment"]},
}  # fmt: skip


def card(iid: int, name: str, seat: int, **extra) -> dict:
    return {"instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
            **deepcopy(CARDS[name]), **extra}  # fmt: skip


def lands(first: int, count: int, seat: int, name: str = "Mountain") -> list[dict]:
    return [card(first + k, name, seat, turn_entered_battlefield=1) for k in range(count)]


def board(turn: int, active: int, battlefield: list[dict], hand: list[dict], *, life=None) -> dict:
    life = life or {US: 13, THEM: 15}
    return {
        "match_id": "115944ce-5273-4596-a336-a43a9f70db4e",
        "local_seat_id": US,
        "opponent_seat_id": THEM,
        "turn": {"turn_number": turn, "active_player": active, "priority_player": active,
                 "phase": "Phase_Main1", "step": ""},
        "players": [
            {"seat_id": THEM, "is_local": False, "life_total": life[THEM], "lands_played": 0, "hand_size": 3},
            {"seat_id": US, "is_local": True, "life_total": life[US], "lands_played": 0, "hand_size": len(hand)},
        ],
        "battlefield": battlefield,
        "hand": hand,
        "graveyard": [],
        "exile": [],
        "stack": [],
        "zones": {"library_count": 30, "library_count_source": "log_zone_membership",
                  "opponent_hand_count": 3},
    }  # fmt: skip


@pytest.fixture(autouse=True)
def _fresh_assessments():
    ba._CACHE.clear()
    yield
    ba._CACHE.clear()


def _trostani_board(hand: list[str] = ("Iroh's Demonstration",), toughness: int = 7) -> dict:
    """115944ce T8 (ours): two Slickshots and Emberheart against the 7/7 Trostani."""
    battlefield = [
        card(301, "Slickshot Show-Off", US, turn_entered_battlefield=4),
        card(302, "Slickshot Show-Off", US, turn_entered_battlefield=6),
        card(289, "Emberheart Challenger", US, turn_entered_battlefield=5), *lands(400, 5, US),
        card(299, "Trostani, Three Whispers", THEM, turn_entered_battlefield=3, power=toughness,
             toughness=toughness),
        card(310, "Anthem of Champions", THEM, turn_entered_battlefield=5), *lands(500, 5, THEM, "Plains"),
    ]  # fmt: skip
    return board(8, US, battlefield, [card(219 + k, name, US) for k, name in enumerate(hand)])


def _validated(state: dict, plan: dict) -> GamePlan:
    parsed = GamePlanManager._parse(json.dumps(plan), state["turn"]["turn_number"])
    return validate_plan(parsed, assess(state), state)


# The 11:14:41 plan, as logged.
PLAN_1114 = {
    "role": "race", "role_reason": "flyers race while burn removes the lifelinker",
    "win_conditions": ["Flyer damage plus burn reach after killing Trostani",
                       "Burn-only fallback (~30 damage suite) if creatures die"],
    "path": "T8 Iroh's kills Trostani; fly every turn; burn reaches from ~11 life",
    "turns": [{"turn": "T", "cast": ["Iroh's Demonstration"],
               "attack": "Slickshot Show-Off #1, Slickshot Show-Off #2, Emberheart Challenger",
               "hold": "Mode 2 (4 damage) on Trostani pre-combat so it cannot block or gain life"}],
}  # fmt: skip


# --- #70: kill claims ------------------------------------------------------------------------


def test_the_false_iroh_kills_trostani_claim_is_annotated():
    plan = _validated(_trostani_board(), deepcopy(PLAN_1114))
    assert plan.path == (
        "T8 Iroh's kills Trostani [checked: NOT a kill — Iroh's Demonstration deals 4; "
        "Trostani, Three Whispers dies to 7]; fly every turn; burn reaches from ~11 life"
    )
    assert (
        "path: plan claim corrected: no kill: Iroh's Demonstration deals 4; Trostani, Three Whispers dies to 7"
        in plan.issues
    )
    # "after killing Trostani" names no burn: not judged.
    assert plan.win_conditions[0] == "Flyer damage plus burn reach after killing Trostani"


def test_a_combined_kill_with_the_steps_other_burn_stands():
    # Shock + Iroh's on a 5/5 (the T6 kill the ledger's #64 missed): 2 + 4 >= 5.
    state = _trostani_board(["Iroh's Demonstration", "Shock"], toughness=5)
    plan = _validated(state, {
        "role": "race", "role_reason": "flyers race while burn removes the lifelinker",
        "win_conditions": ["flyers"], "path": "T8 Iroh's kills Trostani, then fly every turn",
        "turns": [{"turn": "T", "cast": ["Shock", "Iroh's Demonstration"], "attack": "none", "hold": ""}],
    })  # fmt: skip
    assert plan.path == "T8 Iroh's kills Trostani, then fly every turn"
    named = _validated(state, {"role": "race", "role_reason": "flyers race while burn removes the lifelinker",
                               "win_conditions": ["flyers"], "path": "Shock + Iroh's kill Trostani",
                               "turns": []})  # fmt: skip
    assert named.path == "Shock + Iroh's kill Trostani"
    assert not [issue for issue in plan.issues + named.issues if "no kill" in issue]
    # Against the 7/7 even both together fall short.
    seven = _validated(_trostani_board(["Iroh's Demonstration", "Shock"]), {
        "role": "race", "role_reason": "flyers race while burn removes the lifelinker",
        "win_conditions": ["flyers"], "path": "T8 Iroh's kills Trostani, then fly every turn",
        "turns": [{"turn": "T", "cast": ["Shock", "Iroh's Demonstration"], "attack": "none", "hold": ""}],
    })  # fmt: skip
    assert seven.path == (
        "T8 Iroh's kills Trostani [checked: NOT a kill — Iroh's Demonstration 4 + Shock 2 deal 6; "
        "Trostani, Three Whispers dies to 7], then fly every turn"
    )


def test_true_unjudged_and_negated_kill_claims_are_left_alone():
    state = _trostani_board(["Iroh's Demonstration", "Burst Lightning"], toughness=4)
    texts = [
        "T8 Iroh's kills Trostani",  # 4 damage, a 4/4: true
        "kicked Burst Lightning kills Trostani",  # kicked: 4
        "Slickshot attacks and Iroh's kills Trostani after blocks",  # combat may add damage
        "Burst Lightning can't kill Trostani",  # negated
        "first drawn burn kills Trostani",  # names no burn in hand
    ]
    plan = _validated(state, {"role": "race", "role_reason": "flyers race while burn removes the lifelinker",
                              "win_conditions": ["flyers"], "path": " | ".join(texts), "turns": []})  # fmt: skip
    assert plan.path == " | ".join(texts)
    assert not [issue for issue in plan.issues if "no kill" in issue]
    seven = _trostani_board(["Burst Lightning"], toughness=7)
    plan = _validated(seven, {"role": "race", "role_reason": "flyers race while burn removes the lifelinker",
                              "win_conditions": ["flyers"], "path": "kicked Burst Lightning kills Trostani",
                              "turns": []})  # fmt: skip
    assert "NOT a kill — Burst Lightning deals 4; Trostani, Three Whispers dies to 7" in plan.path


def test_the_annotated_claim_reaches_the_decision_prompt():
    state = _trostani_board()
    plan = _validated(state, deepcopy(PLAN_1114))
    block = compose_strategy_block(assess(state), plan, state=state)
    assert "T8 Iroh's kills Trostani [checked: NOT a kill" in block


# --- #58: unusable answers -------------------------------------------------------------------


class Backend:
    """Scripted answers; records each call's effort and user message."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, system, user, max_tokens, **kwargs):
        self.calls.append({"user": user, "effort": kwargs.get("reasoning_effort")})
        return self.responses.pop(0) if self.responses else "{}"


GOOD = json.dumps({"role": "race", "role_reason": "flyers race", "win_conditions": ["flyers"],
                   "path": "fly every turn", "turns": []})  # fmt: skip
# A reasoning-model answer whose prose before the JSON quotes mana symbols.
PROSE = "Plan: hold {1}{R} for Shock.\n" + "x" * 400


def _manager(responses) -> tuple[GamePlanManager, Backend]:
    backend = Backend(responses)
    mgr = GamePlanManager(backend)
    mgr._plan = GamePlan(win_conditions=["old"], path="old path", turn_formed=4, role="race")
    return mgr, backend


def test_an_unusable_answer_is_logged_and_retried_once_at_low_effort(caplog):
    mgr, backend = _manager([PROSE, GOOD])
    state = _trostani_board()
    with caplog.at_level(logging.INFO, logger="arenamcp.game_plan"):
        plan = mgr._reform(state, 8)
    assert plan is not None and plan.path == "fly every turn" and plan.effort == "low"
    assert [call["effort"] for call in backend.calls] == ["high", "low"]
    assert backend.calls[1]["user"].startswith(backend.calls[0]["user"])
    assert "NOT A USABLE GAME PLAN" in backend.calls[1]["user"]
    unusable = [r.getMessage() for r in caplog.records if "no usable plan" in r.getMessage()]
    assert len(unusable) == 1
    # The answer's head (300 characters) is in the log.
    assert "'Plan: hold {1}{R} for Shock.\\nxxx" in unusable[0] and f"{len(PROSE)} chars" in unusable[0]
    assert "x" * 300 not in unusable[0]


def test_a_second_unusable_answer_keeps_the_prior_plan_without_a_third_call():
    mgr, backend = _manager(["no json at all", "still {not json}", GOOD])
    plan = mgr.maybe_reform(_trostani_board(), force=True)
    assert len(backend.calls) == 2
    assert plan is mgr.current and plan.path == "old path"
    assert mgr.plan_status()["last_outcome"] == "no usable plan from the model"


def test_no_retry_once_cancelled_or_after_a_call_failure():
    mgr, backend = _manager(["[BACKEND ERROR] LLM streaming time budget exhausted", GOOD])
    assert mgr._reform(_trostani_board(), 8) is None
    assert len(backend.calls) == 1

    class Cancelling(Backend):
        def complete(self, system, user, max_tokens, **kwargs):
            kwargs["cancel_event"].set()
            return super().complete(system, user, max_tokens, **kwargs)

    mgr, backend = _manager([])
    backend = mgr._backend = Cancelling(["no json", GOOD])
    import threading

    assert mgr._reform(_trostani_board(), 8, cancel=threading.Event()) is None
    assert len(backend.calls) == 1


def test_json_wrapped_in_prose_or_with_trailing_commas_is_used():
    wrapped = "Mana: {1}{R} open.\n" + GOOD + "\nNote: {T} untaps next turn."
    plan = GamePlanManager._parse(wrapped, 8)
    assert plan is not None and plan.path == "fly every turn"
    trailing = '{\n "path": "fly every turn",\n "win_conditions": ["flyers",\n ],\n}'
    plan = GamePlanManager._parse(trailing, 8)
    assert plan is not None and plan.win_conditions == ["flyers"]
    # A turn step nested in a broken outer object is not mistaken for the plan.
    assert GamePlanManager._parse('{"turns": [{"turn": "T", "land": "Mountain"}], "path": oops}', 8) is None


def test_a_kept_plan_is_flagged_as_not_refreshed_in_decision_prompts():
    mgr, _backend = _manager(["no json", "no json either", GOOD])
    state = _trostani_board()
    mgr.maybe_reform(state, force=True)
    block = mgr.strategy_block(state)
    note = (
        "PLAN NOT REFRESHED: the re-plan on turn 8 failed (no usable plan from the model), so this game plan "
        "is still the one formed on turn 4"
    )
    assert note in block
    assert note in mgr.plan_text()
    # A usable plan clears the flag.
    mgr.maybe_reform(state, force=True)
    assert mgr.current.path == "fly every turn"
    assert "PLAN NOT REFRESHED" not in mgr.strategy_block(state)
    assert "PLAN NOT REFRESHED" not in mgr.plan_text()


def test_the_step_tag_says_the_plan_was_not_refreshed():
    state = _trostani_board()
    plan = _validated(state, deepcopy(PLAN_1114))
    block = compose_strategy_block(assess(state), plan, state=state, refresh_failed=(8, "call failed"))
    assert "[game plan T8, not refreshed (re-plan on turn 8 failed)]" in block
    assert "PLAN NOT REFRESHED: the re-plan on turn 8 failed (call failed)" in block
