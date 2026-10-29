"""The line-gap second look and the target step's line agreement (ledger #116, #118; 2026-10-19).

Match 2c67eed8 (vs Boros Angels), standalone.log 12:47-12:52; boards from the match's Player.log.

- #116, T8 12:49:36: three untapped Mountains, Iroh's Demonstration, Giant Cindermaw and two
  Mountains in hand at 19 life; they are at 11 with an untapped Dazzling Angel 2/3 flying, all four
  lands tapped and two cards in hand. The prompt tagged Iroh's "[LINE: -8 vs best]" and the best
  line was "Mountain + Giant Cindermaw; then Mountain + Iroh's Demonstration". The low answer cast
  Iroh's at 9.9 s ("Kill the Dazzling Angel now"), the high leg was cancelled 3 s later, and the pick
  stood; Iroh's was gone when Herald of Faith arrived (it dealt 8).
- #118, T6 12:48:55: the line was "Burst Lightning (on Helpful Hunter)"; the target step's low answer
  picked Helpful Hunter at 2.1 s and the hedge waited 3 s more for a high answer that never came.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from copy import deepcopy

import pytest
from tests import race_boards_20261019 as rb

from arenamcp import action_planner as planner_module
from arenamcp.action_planner import ActionPlanner
from arenamcp.decisions import DecisionOption, PendingDecision, TargetSlot

IROH_TEXT = (
    "Choose one —\n• Iroh's Demonstration deals 1 damage to each creature your opponents control.\n"
    "• Iroh's Demonstration deals 4 damage to target creature."
)
CARDS = {
    "Iroh's Demonstration": {
        "grp_id": 97417, "type_line": "Sorcery — Lesson", "mana_cost": "{1}{R}",
        "card_types": ["CardType_Sorcery"], "oracle_text": IROH_TEXT,
    },
    "Giant Cindermaw": {
        "grp_id": 94052, "type_line": "Creature — Dinosaur Beast", "mana_cost": "{2}{R}", "power": 4,
        "toughness": 3, "keywords": ["trample"], "card_types": ["CardType_Creature"],
        "oracle_text": "Trample\nPlayers can't gain life.",
    },
    "Dazzling Angel": {
        "grp_id": 93722, "type_line": "Creature — Angel", "mana_cost": "{2}{W}", "power": 2, "toughness": 3,
        "keywords": ["flying"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying\nWhenever another creature you control enters, you gain 1 life.",
    },
    "Burst Lightning": {
        "grp_id": 93905, "type_line": "Instant", "mana_cost": "{R}", "card_types": ["CardType_Instant"],
        "oracle_text": "Kicker {4}\nBurst Lightning deals 2 damage to any target. If this spell was kicked, "
        "it deals 4 damage instead.",
    },
    "Helpful Hunter": {
        "grp_id": 93800, "type_line": "Creature — Cat", "mana_cost": "{1}{W}", "power": 1, "toughness": 1,
        "card_types": ["CardType_Creature"], "oracle_text": "When this creature enters, draw a card.",
    },
    "Giada, Font of Hope": {
        "grp_id": 93854, "type_line": "Legendary Creature — Angel", "mana_cost": "{1}{W}", "power": 2,
        "toughness": 2, "keywords": ["flying", "vigilance"], "card_types": ["CardType_Creature"],
        "oracle_text": "Flying, vigilance\nEach other Angel you control enters with an additional +1/+1 "
        "counter on it for each Angel you already control.\n{T}: Add {W}. Spend this mana only to cast an "
        "Angel spell.",
    },
    "Emberheart Challenger": deepcopy(rb.CARDS["Emberheart Challenger"]),
}  # fmt: skip


def _card(iid: int, name: str, seat: int, **extra) -> dict:
    return {
        "instance_id": iid, "name": name, "owner_seat_id": seat, "controller_seat_id": seat,
        **deepcopy(CARDS[name]), **extra,
    }  # fmt: skip


def _t8() -> dict:
    """2c67eed8 T8, gsid 188: our Main 1, no land played yet."""
    state = rb.board(
        turn=8, life=19, their_life=11, lands_played=0, opponent_hand=2,
        ours=[(281, "Mountain", False, 2), (293, "Mountain", False, 4), (303, "Mountain", False, 6)],
        theirs=[(279, "Mountain", True, 1), (286, "Plains", True, 3), (298, "Mountain", True, 5),
                (318, "Mountain", True, 7)],
        hand=[],
    )  # fmt: skip
    state["battlefield"].append(
        _card(319, "Dazzling Angel", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=7)
    )
    state["hand"] = [
        _card(221, "Iroh's Demonstration", rb.LOCAL),
        _card(327, "Giant Cindermaw", rb.LOCAL),
        rb.card(292, "Mountain", rb.LOCAL),
        rb.card(302, "Mountain", rb.LOCAL),
    ]
    return state


def _t8_menu(*, cindermaw_payable: bool | None = True) -> PendingDecision:
    """The recorded menu (request [188, 257])."""

    def cast(option_id, iid, grp, name, payable):
        meta = {"actionType": "ActionType_Cast", "instanceId": iid, "grpId": grp}
        return DecisionOption(option_id, f"Cast {name}", payable=payable, meta=meta)

    def land(option_id, iid):
        meta = {"actionType": "ActionType_Play", "instanceId": iid, "grpId": 65385}
        return DecisionOption(option_id, "Play land: Mountain", payable=None, meta=meta)

    options = (
        cast("idx:0", 221, 97417, "Iroh's Demonstration", True),
        cast("idx:1", 327, 94052, "Giant Cindermaw", cindermaw_payable),
        land("idx:2", 292),
        land("idx:3", 302),
        DecisionOption("pass", "Pass"),
    )
    return PendingDecision((188, 257), "ActionsAvailable", options, can_pass=True)


IROH_NOW = (
    "Kill the Dazzling Angel now with Iroh's mode 2 — it's their only clock and our only answer; the land "
    "drop remains afterward."
)


def _reply(option_id: str, reasoning: str) -> str:
    return json.dumps({"option_ids": [option_id], "reasoning": reasoning})


class _Model:
    """A scripted model: the low leg answers at once; the high leg answers ``high`` after ``high_delay``
    (None: it never does), unless cancelled."""

    def __init__(self, low: str, high: str | None = None, *, high_delay: float = 5.0):
        self.low, self.high, self.high_delay = low, high, high_delay
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
            return self.low
        cancel = kwargs.get("cancel_event")
        end = time.monotonic() + self.high_delay
        while time.monotonic() < end:
            if cancel is not None and cancel.is_set():
                raise TimeoutError("request cancelled")
            time.sleep(0.005)
        if self.high is None:
            raise TimeoutError("LLM streaming time budget exhausted")
        return self.high


def _planner(model: _Model) -> ActionPlanner:
    return ActionPlanner(model, timeout=5, land_drop_first=False)


@pytest.fixture
def fast_hedge(monkeypatch):
    """The hedge's clocks, shortened: a high leg that runs past 0.3 s (+0.1 s grace) has no answer."""
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 0.3)
    monkeypatch.setattr(ActionPlanner, "_HEDGE_GRACE_S", 0.1)
    monkeypatch.setattr(ActionPlanner, "_HIGH_AFTER_LOW_S", 0.05)
    # These tests exercise the guard itself; the default is 'on' since 2026-10-20 (see below).
    monkeypatch.setenv("ARENAMCP_LINE_GAP", "on")


# --- #116: the recorded board, tags and concern -------------------------------------------------


def test_the_t8_board_reproduces_the_recorded_line_tags():
    state, menu = _t8(), _t8_menu()
    source, text, unmodelled = ActionPlanner._line_prompt_context(menu, state)
    assert text.startswith(
        "LINES (2-turn search, greedy 3rd; their new cards/tricks not modelled): best Mountain + Giant "
        "Cindermaw; then Mountain + Iroh's Demonstration"
    )
    tags = {o.option_id: ActionPlanner._line_tag(source, o, state, unmodelled) for o in menu.options}
    assert tags["idx:0"].startswith("[LINE: -8 vs best;")
    assert tags["idx:1"].startswith("[LINE best:")


def test_a_cast_eight_below_the_best_line_is_doubted_and_names_the_line(fast_hedge):
    state, menu = _t8(), _t8_menu()
    source, _text, unmodelled = ActionPlanner._line_prompt_context(menu, state)
    tags = {o.option_id: ActionPlanner._line_tag(source, o, state, unmodelled) for o in menu.options}
    planner = _planner(_Model("{}"))
    planner._last_decision_trace = {}
    concerns = planner._second_look_concerns(menu, state, ["idx:0"], IROH_NOW, source, tags)
    assert [kind for kind, _ in concerns] == ["line_gap"]
    text = concerns[0][1]
    assert text.startswith(
        "The line search rates Iroh's Demonstration 8 below its best line (Mountain + Giant"
    )
    assert "nothing is lethal this turn" in text
    assert "Play the best line (Play land: Mountain first)" in text
    assert planner._last_decision_trace["line_gap"] == {
        "chosen": "idx:0", "card": "Iroh's Demonstration", "gap": 8, "replacement": "idx:2",
        "with": "Play land: Mountain", "mode": "on",
    }  # fmt: skip
    # The best line's own plays raise nothing.
    for option_id in ("idx:1", "idx:2"):
        assert planner._second_look_concerns(menu, state, [option_id], "", source, tags) == []


@pytest.mark.parametrize("case", ["small gap", "lethal", "best line not castable", "their turn", "off"])
def test_no_line_gap_concern_without_a_large_gap_and_a_playable_best_line(fast_hedge, monkeypatch, case):
    state, menu = _t8(), _t8_menu()
    source, _text, unmodelled = ActionPlanner._line_prompt_context(menu, state)
    tags = {o.option_id: ActionPlanner._line_tag(source, o, state, unmodelled) for o in menu.options}
    if case == "small gap":
        tags["idx:0"] = tags["idx:0"].replace("-8 vs best", "-4 vs best")
    elif case == "lethal":
        monkeypatch.setattr(planner_module, "_lethal_this_turn", lambda s: True)
    elif case == "best line not castable":
        menu = _t8_menu(cindermaw_payable=False)
    elif case == "their turn":
        state["turn"]["active_player"] = rb.OPPONENT
    elif case == "off":
        monkeypatch.setenv("ARENAMCP_LINE_GAP", "off")
    planner = _planner(_Model("{}"))
    assert planner._second_look_concerns(menu, state, ["idx:0"], IROH_NOW, source, tags) == []


# --- #116: the decision, end to end ------------------------------------------------------------------


def test_without_a_high_answer_the_doubted_low_cast_gives_way_to_the_line(fast_hedge, caplog):
    model = _Model(_reply("idx:0", IROH_NOW), high=None)
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_t8_menu(), _t8()) == ["idx:2"]
    assert model.efforts.count(None) == 1 and "high" in model.efforts
    assert "line_gap: Iroh's Demonstration is 8 below the best line" in caplog.text
    assert "the low answer ['idx:0'] waits for high effort (line_gap)" in caplog.text
    assert "line_gap: did replace Iroh's Demonstration with Play land: Mountain" in caplog.text
    trace = planner.get_last_decision_trace()
    assert trace["line_gap"]["applied"] == ["idx:2"]
    assert trace["hedge"]["winner"] == "none"
    assert planner.get_decision_reasoning(["idx:2"]).startswith("The model's pick was set aside;")


def test_a_high_answer_still_decides(fast_hedge, monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 3.0)
    model = _Model(
        _reply("idx:0", IROH_NOW),
        high=_reply("idx:1", "Cindermaw first; Iroh's waits for a bigger angel."),
        high_delay=0.3,
    )
    planner = _planner(model)
    assert planner.plan_decision_options(_t8_menu(), _t8()) == ["idx:1"]
    assert planner.get_last_decision_trace()["hedge"]["winner"] == "high"


def test_in_shadow_mode_the_low_pick_stands_and_the_log_says_what_would_change(
    fast_hedge, monkeypatch, caplog
):
    monkeypatch.setenv("ARENAMCP_LINE_GAP", "shadow")
    model = _Model(_reply("idx:0", IROH_NOW), high=None)
    planner = _planner(model)
    with caplog.at_level(logging.INFO, logger="arenamcp.action_planner"):
        assert planner.plan_decision_options(_t8_menu(), _t8()) == ["idx:0"]
    assert (
        "line_gap [shadow]: would replace Iroh's Demonstration with Play land: Mountain (8 below the best line"
        in caplog.text
    )
    assert caplog.text.count("line_gap [shadow]") == 1
    gap = planner.get_last_decision_trace()["line_gap"]
    assert gap["mode"] == "shadow" and "applied" not in gap


# --- #118: the target step's low answer on the line's own target -------------------------------------


def _t6_targets() -> tuple[dict, PendingDecision]:
    """2c67eed8 T6, 12:48:55: Burst Lightning (unkicked) on the stack, choosing its target."""
    state = rb.board(
        turn=6, life=19, their_life=15, lands_played=1, opponent_hand=4,
        ours=[(281, "Mountain", True, 2), (293, "Mountain", False, 4), (303, "Mountain", False, 6)],
        theirs=[(279, "Mountain", False, 1), (286, "Plains", False, 3), (298, "Mountain", False, 5)],
        hand=[],
    )  # fmt: skip
    state["battlefield"] += [
        _card(294, "Emberheart Challenger", rb.LOCAL, is_tapped=False, turn_entered_battlefield=4),
        _card(287, "Helpful Hunter", rb.OPPONENT, is_tapped=True, turn_entered_battlefield=3),
        _card(299, "Giada, Font of Hope", rb.OPPONENT, is_tapped=False, turn_entered_battlefield=5),
    ]
    state["stack"] = [_card(304, "Burst Lightning", rb.LOCAL)]
    ids = (rb.OPPONENT, rb.LOCAL, 287, 294, 299)
    decision = PendingDecision(
        request_id=(122, 190),
        request_type="SelectTargets",
        options=tuple(DecisionOption(f"tgt:{i}", f"Target #{i}", meta={"instanceId": i}) for i in ids),
        min_select=1,
        max_select=1,
        can_cancel=True,
        source_label="Burst Lightning",
        slots=(TargetSlot(1, 1, 1, 0, ids),),
    )
    return state, decision


def _intent(planner: ActionPlanner) -> None:
    """What the T6 cast step remembered (``_remember_cast_intent``): the line's Helpful Hunter."""
    planner._cast_intent = {
        "card": "Burst Lightning",
        "said": "Burst Lightning kills Helpful Hunter",
        "line_target": "Helpful Hunter",
        "line_target_id": 287,
        "at": time.monotonic(),
    }


def test_the_lines_own_target_agrees():
    state, decision = _t6_targets()
    planner = _planner(_Model("{}"))
    _intent(planner)
    assert (
        planner._line_target_agreement(decision, state, ["tgt:287"])
        == "the searched line aims Burst Lightning at Helpful Hunter"
    )
    assert planner._line_target_agreement(decision, state, ["tgt:299"]) == ""
    assert planner._line_target_agreement(decision, state, ["tgt:287", "tgt:299"]) == ""


def test_without_a_remembered_cast_the_searched_line_is_asked(monkeypatch):
    state, decision = _t6_targets()
    asked = []

    def line_target(game_state, name):
        asked.append(name)
        return "Giada, Font of Hope", 299

    monkeypatch.setattr(ActionPlanner, "_searched_line_target", staticmethod(line_target))
    planner = _planner(_Model("{}"))
    assert planner._line_target_agreement(decision, state, ["tgt:299"]) == (
        "the searched line aims Burst Lightning at Giada, Font of Hope"
    )
    assert asked == ["Burst Lightning"]
    assert planner._line_target_agreement(decision, state, ["tgt:287"]) == ""


def test_a_low_target_on_the_line_does_not_wait_for_high(fast_hedge, monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 8.0)
    monkeypatch.setattr(ActionPlanner, "_HIGH_AFTER_LOW_S", 3.0)
    monkeypatch.setattr(
        planner_module, "critical_decision", lambda *a, **k: "removal target: Burst Lightning"
    )
    state, decision = _t6_targets()
    hunter = _reply("tgt:287", "Unpaid Burst Lightning deals 2, killing Helpful Hunter.")
    model = _Model(hunter, high=hunter, high_delay=6.0)
    planner = _planner(model)
    _intent(planner)
    started = time.monotonic()
    assert planner._llm_decision_options(decision, state) == ["tgt:287"]
    assert time.monotonic() - started < 3.0  # not the 3 s grace, let alone the 6 s high leg
    hedge = planner.get_last_decision_trace()["hedge"]
    assert hedge["winner"] == "low"
    assert hedge["why"].endswith("the searched line aims Burst Lightning at Helpful Hunter")


def test_a_low_target_without_a_concrete_concern_does_not_wait(fast_hedge, monkeypatch):
    monkeypatch.setattr(ActionPlanner, "_CRITICAL_DECISION_BUDGET_S", 8.0)
    monkeypatch.setattr(
        planner_module, "critical_decision", lambda *a, **k: "removal target: Burst Lightning"
    )
    state, decision = _t6_targets()
    model = _Model(_reply("tgt:299", "Giada is the flying threat."), high=None, high_delay=6.0)
    planner = _planner(model)
    _intent(planner)
    assert planner._llm_decision_options(decision, state) == ["tgt:299"]
    assert planner.get_last_decision_trace()["hedge"]["why"] == "usable low answer; high not waited for"
    assert planner.get_last_decision_trace()["hedge"]["wait_after_low_ms"] == 0


def test_the_line_gap_guard_is_on_by_default(monkeypatch):
    """It shipped in shadow; the user turned it on by default on 2026-10-20 (GUARD_DEFAULTS)."""
    from arenamcp.line_guard import line_gap_mode

    monkeypatch.delenv("ARENAMCP_LINE_GAP", raising=False)
    assert line_gap_mode() == "on"
    monkeypatch.setenv("ARENAMCP_LINE_GAP", "shadow")
    assert line_gap_mode() == "shadow"
