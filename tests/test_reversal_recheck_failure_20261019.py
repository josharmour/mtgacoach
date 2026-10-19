"""A failed reversal re-check keeps the earlier pass (ledger #151, 2026-10-19).

Match 633f56bd (Mono-Red Burn vs W lifegain), standalone.log 13:58:33-13:59:05; the boards
are the match's Player.log (gsid 187, request [187, 257]):

- T7 Main 1, 13:58:38: Emberheart Challenger cast, Lightning Strike in hand with two Mountains
  open; they are at 18 with Leonin Vanguard 2/1 tapped under Sheltered by Ghosts (lifelink, ward
  {2}). The low answer passed: "Leonin Vanguard's Ward {2} counters a Strike aimed at it and face
  damage doesn't advance the plan now; keep Lightning Strike as instant-speed interaction".
- T7 declare-blockers step, 13:58:44: Emberheart attacking, the same board. The low answer cast
  Lightning Strike ("[LINE: -7 vs best; 15 after T8, ...]") at the face. "reverses an earlier
  pass this turn ...; asking again at high effort" (13:58:48); the high-effort call ran out its
  15 s budget beside a 3,162-token game-plan reform ("LLM streaming time budget exhausted",
  13:59:03) and "reversal re-check failed ...; keeping the first answer": the cast stood.

Since 6d97e0f (#140) the burn-timing hold answers this very window before the model ("our
combat: kills no blocker, not lethal"). The re-check tests below take the hold out
(``reaches_model``), as at b720163, to exercise the reversal path any other window can reach.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from copy import deepcopy

import pytest
from tests import race_boards_20261019 as rb

from arenamcp import action_planner as planner_module
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision

# Arena's card text (the match's card database), as the replayed snapshot carries it.
CARDS = {
    "Emberheart Challenger": {
        "type_line": "Creature — Mouse Warrior", "mana_cost": "{1}{R}", "power": 2, "toughness": 2,
        "keywords": ["haste"], "card_types": ["CardType_Creature"], "subtypes": ["Mouse", "Warrior"],
        "oracle_text": "Haste\nProwess\n<i>Valiant</i> — Whenever this creature becomes the target of a spell "
        "or ability you control for the first time each turn, exile the top card of your library. Until end "
        "of turn, you may play that card.",
    },
    "Leonin Vanguard": {
        "type_line": "Creature — Cat Soldier", "mana_cost": "{W}", "power": 2, "toughness": 1,
        "keywords": ["lifelink"], "card_types": ["CardType_Creature"], "subtypes": ["Cat", "Soldier"],
        "oracle_text": "At the beginning of combat on your turn, if you control three or more creatures, this "
        "creature gets +1/+1 until end of turn and you gain 1 life.",
    },
    "Sheltered by Ghosts": {
        "type_line": "Enchantment — Aura", "mana_cost": "{1}{W}", "card_types": ["CardType_Enchantment"],
        "subtypes": ["Aura"],
        "oracle_text": "Enchant creature you control\nWhen this Aura enters, exile target nonland permanent an "
        "opponent controls until this Aura leaves the battlefield.\nEnchanted creature gets +1/+0 and has "
        "lifelink and ward {o2}.",
    },
    "Leyline of Hope": {
        "type_line": "Enchantment", "mana_cost": "{2}{W}{W}", "card_types": ["CardType_Enchantment"],
        "oracle_text": "If this card is in your opening hand, you may begin the game with it on the "
        "battlefield.\nIf you would gain life, you gain that much life plus 1 instead.\nAs long as you have "
        "at least 7 life more than your starting life total, creatures you control get +2/+2.",
    },
    "Lightning Strike": deepcopy(rb.CARDS["Lightning Strike"]),
}  # fmt: skip

EARLIER_PASS = (
    "Leonin Vanguard's Ward {2} counters a Strike aimed at it and face damage doesn't advance the plan now; "
    "keep Lightning Strike as instant-speed interaction per my earlier hold."
)
STRIKE_FACE = (
    "Their creatures are all tapped so no blocks or tricks matter this combat; Lightning Strike to the face "
    "for 3 is pure clock progress and the warded Vanguard isn't a valid target anyway."
)


def _card(iid: int, name: str, seat: int, **extra) -> dict:
    return {
        "instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
        **deepcopy(CARDS[name]), **extra,
    }  # fmt: skip


def _t7(*, step: str = "Step_DeclareBlock", their_life: int = 18) -> dict:
    """633f56bd T7 (local seat 1). Default: gsid 187, our declare-blockers step, Emberheart attacking."""
    combat = step != ""
    state = rb.board(
        turn=7, life=17, their_life=their_life, lands_played=1, opponent_hand=3,
        ours=[(281, "Mountain", True, 1), (290, "Mountain", True, 3), (302, "Mountain", False, 5),
              (320, "Mountain", False, 7)],
        theirs=[(286, "Plains", True, 2), (295, "Plains", True, 4)],
        hand=[], phase="Phase_Combat" if combat else "Phase_Main1", step=step,
    )  # fmt: skip
    state["match_id"] = "633f56bd-2155-4233-a6b0-4369ce49f87e"
    state["battlefield"] += [
        _card(321, "Emberheart Challenger", rb.LOCAL, is_tapped=combat, is_attacking=combat,
              turn_entered_battlefield=7),
        _card(296, "Leonin Vanguard", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=4),
        _card(314, "Sheltered by Ghosts", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=6,
              attached_to_id=296),
        _card(280, "Leyline of Hope", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=1),
    ]  # fmt: skip
    state["hand"] = [_card(301, "Lightning Strike", rb.LOCAL)]
    state["exile"] = [_card(318, "Emberheart Challenger", rb.LOCAL)]  # under Sheltered by Ghosts
    return state


def _menu(request_id: tuple[int, int] = (187, 257)) -> PendingDecision:
    """The recorded menu: Cast Lightning Strike (Arena-payable), Pass."""
    meta = {
        "actionType": "ActionType_Cast", "grpId": 97423, "instanceId": 301,
        "manaCost": [{"color": '[ "Generic" ]', "count": 1}, {"color": '[ "Red" ]', "count": 1}],
        "autoTapActions": [{"instanceId": 302, "manaId": 0}, {"instanceId": 320, "manaId": 0}],
        "hasAutoTap": True,
    }  # fmt: skip
    options = (
        DecisionOption("idx:0", "Cast Lightning Strike", payable=True, meta=meta),
        DecisionOption("pass", "Pass"),
    )
    return PendingDecision(request_id, "ActionsAvailable", options, can_pass=True)


def _reply(option_id: str, reasoning: str) -> str:
    return json.dumps({"option_ids": [option_id], "reasoning": reasoning})


class _Model:
    """Low effort casts Strike at once; high effort answers ``high`` or, with None, runs out its budget."""

    def __init__(self, high: str | None = None):
        self.high = high
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
        if effort != "high":
            return _reply("idx:0", STRIKE_FACE)
        if self.high is None:
            raise TimeoutError("LLM streaming time budget exhausted")
        return self.high


def _planner(model: _Model) -> ActionPlanner:
    planner = ActionPlanner(model, timeout=5, land_drop_first=False)
    # 13:58:38: the Main 1 pass with Strike on the menu.
    planner._memory().record(_t7(step=""), _menu((183, 251)), ["pass"], EARLIER_PASS)
    return planner


@pytest.fixture(autouse=True)
def _short_budget(monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 0.3)


@pytest.fixture
def reaches_model(monkeypatch):
    """The window goes to the model, as it did live (no #140 combat hold at b720163)."""
    monkeypatch.setattr(ActionPlanner, "_hold_instant_burn", lambda self, decision, state: (decision, []))


def test_today_the_combat_hold_answers_this_window_before_the_model(caplog):
    model = _Model(high=None)
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_menu(), _t7()) == ["pass"]
    assert model.efforts == []
    assert "Instant timing: holding Lightning Strike for the opponent's end step (our combat" in caplog.text


def test_the_t7_board_reproduces_the_recorded_tag_and_the_reversal():
    state, menu = _t7(), _menu()
    source, text, unmodelled = ActionPlanner._line_prompt_context(menu, state)
    tag = ActionPlanner._line_tag(source, menu.find("idx:0"), state, unmodelled)
    # Live (b720163): "[LINE: -7 vs best; 15 after T8, ...]", best line "attack with Emberheart
    # Challenger; then Lightning Strike (on Leo…" with the opponent at 14; today's search ends it at 13.
    gap = re.match(r"\[LINE: -(\d+) vs best; 15 after T8, 15 after T10, 15 after T12\]", tag)
    assert gap is not None and int(gap.group(1)) >= 5, tag
    assert text.startswith(
        "LINES (2-turn search, greedy 3rd; their new cards/tricks not modelled): best attack with Emberheart "
        "Challenger; then Lightning Strike (on Leo"
    )
    planner = _planner(_Model())
    record = planner._memory().reversal(state, menu, ["idx:0"])
    assert record is not None and record.reasoning == EARLIER_PASS
    assert planner._memory().stated_plan_followed(record, state, menu, ["idx:0"]) == ""


def test_a_failed_recheck_keeps_the_earlier_pass_not_the_reversing_cast(reaches_model, caplog):
    model = _Model(high=None)
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_menu(), _t7()) == ["pass"]
    assert model.efforts == [None, "high"]  # the low answer, then the re-check that got nothing
    assert "REVERSAL CHECK" in model.prompts[1]
    assert "reverses an earlier pass this turn" in caplog.text
    assert (
        "reversal re-check failed (LLM streaming time budget exhausted); keeping the earlier pass, not "
        "Cast Lightning Strike [LINE: -" in caplog.text
    )
    assert "keeping the first answer" not in caplog.text
    trace = planner.get_last_decision_trace()
    assert trace["reversal_check"]["kept"] == "earlier pass"
    assert trace["reversal_check"]["set_aside"] == ["idx:0"]
    assert trace["reversal_check"]["final"] == ["pass"]
    assert trace["selected_ids"] == ["pass"]
    reasoning = planner.get_decision_reasoning(["pass"])
    assert reasoning.startswith("Keeping the earlier pass this turn: Leonin Vanguard's Ward {2} counters")
    # The memory now holds a second pass, not the cast.
    assert [r.chosen for r in planner._memory().this_turn(_t7())] == [["pass"], ["pass"]]


def test_a_failed_recheck_keeps_a_line_best_pick(reaches_model, monkeypatch, caplog):
    monkeypatch.setattr(
        ActionPlanner,
        "_line_tag",
        staticmethod(
            lambda source, option, state, unmodelled: (
                "[LINE best: 15 after T8]" if option.option_id == "idx:0" else ""
            )
        ),
    )
    planner = _planner(_Model(high=None))
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_menu(), _t7()) == ["idx:0"]
    assert "keeping the first answer (the line search rates it best [LINE best: 15 after T8])" in caplog.text
    assert planner.get_last_decision_trace()["reversal_check"]["final"] == ["idx:0"]


def test_a_failed_recheck_keeps_a_lethal_pick(reaches_model, monkeypatch, caplog):
    monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda state: True)
    planner = _planner(_Model(high=None))
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_menu(), _t7()) == ["idx:0"]
    assert "keeping the first answer (the board math has lethal this turn)" in caplog.text


def test_an_answered_recheck_still_decides(reaches_model):
    planner = _planner(_Model(high=_reply("idx:0", "Emberheart is attacking now; Strike face is fine.")))
    assert planner.plan_decision_options(_menu(), _t7()) == ["idx:0"]
    assert planner.get_last_decision_trace()["reversal_check"]["final"] == ["idx:0"]
    planner = _planner(_Model(high=_reply("pass", "Keep Strike for a creature.")))
    assert planner.plan_decision_options(_menu(), _t7()) == ["pass"]
