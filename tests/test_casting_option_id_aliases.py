"""Ledger #121 recurrence: bare ['1'] cancelled Iroh instead of choosing Mode 2.

2026-10-31, 4e124219 T10 at 12:22:15: the log records the bare ID and
the intention to remove Doorkeeper Thrull. The completed packet's T12
control preserves the same spell's mode metadata and succeeds with idx:1.
Replay that menu/answer through the real planner and a recording bridge;
this tests execution, not whether the alternative line wins the match.
"""

import copy
import json
from types import SimpleNamespace

import pytest
from tests.fakes import TypedBridge, make_engine, planner_with, target_state

from arenamcp.action_planner import DECLINE_DECISION
from arenamcp.decisions import DecisionOption, PendingDecision, build_pending_decision, submit_option

REASON = (
    "Mode 2 kills the flying Doorkeeper Thrull, unblocking Slickshot for the lethal alpha strike; "
    "the 1-damage sweeper kills nothing."
)
MODE_TEXT = {
    87912: "CARDNAME deals 1 damage to each creature your opponents control.",
    21773: "CARDNAME deals 4 damage to target creature.",
}


@pytest.fixture
def modal_poll(monkeypatch):
    monkeypatch.setattr("arenamcp.server.get_card_info", lambda *_: {})
    monkeypatch.setattr(
        "arenamcp.card_db.get_card_database",
        lambda: SimpleNamespace(get_ability_text=lambda grp: MODE_TEXT.get(grp, "")),
    )
    return {
        "has_pending": True,
        "request_type": "CastingTimeOptions",
        "game_state_id": 337,
        "msg_id": 474,
        "can_cancel": True,
        "actions": [
            {
                "actionType": "CastingTimeOption",
                "choiceKind": "modal",
                "requestClass": "CastingTimeOption_ModalRequest",
                "childIndex": 0,
                "label": f"Mode {index + 1}",
                "optionIndex": index,
                "grpId": grp,
                "sourceId": 407,
                "abilityGrpId": 192719,
                "min": 1,
                "max": 1,
            }
            for index, grp in enumerate(MODE_TEXT)
        ],
    }


def modal_state():
    state = target_state()
    state.update(
        pending_decision="Casting Option",
        decision_context={"type": "casting_time_options"},
        _bridge_request_type="CastingTimeOptions",
        battlefield=[
            {
                "instance_id": 350,
                "name": "Doorkeeper Thrull",
                "type_line": "Creature — Thrull",
                "card_types": ["CardType_Creature"],
                "oracle_text": "Flying",
                "power": 3,
                "toughness": 4,
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "is_tapped": True,
            }
        ],
    )
    return state


class CastingBridge(TypedBridge):
    def submit_action_by_index(self, index, expected=None):
        self.submitted.append(("action", index, expected))
        return True

    def submit_casting_options(self, indices, expected=None):
        self.submitted.append(("modes", indices, expected))
        return True

    def cancel_action(self):
        self.submitted.append(("cancel",))
        return True


@pytest.mark.parametrize("answer", [1, "1", "idx:1"])
def test_iroh_mode_two_reaches_executor_without_cancelling(modal_poll, answer):
    bridge = CastingBridge(modal_poll)
    planner = planner_with(json.dumps({"option_ids": [answer], "reasoning": REASON}))
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    assert engine._try_typed_decision_path(modal_state(), "decision_required")
    expected = {**modal_poll["actions"][1], "gameStateId": 337, "msgId": 474}
    assert bridge.submitted == [("action", 1, expected)]
    assert planner.get_decision_reasoning(["idx:1"]) == REASON
    assert planner._last_decision_trace["selected_ids"] == ["idx:1"]


@pytest.mark.parametrize(
    ("options", "answer", "expected"),
    [
        (("idx:3", "idx:4"), 4, "idx:4"),
        (("idx:3", "idx:4"), "0", "0"),  # not a position in a filtered menu
        (("idx:3", "idx:4"), "99", "99"),
        (("idx:3", "idx:4"), "04", "04"),
        (("idx:3", "idx:4"), "Mode 2", "Mode 2"),
        (("3", "idx:3"), "3", "3"),  # an advertised literal takes precedence
        (("idx:3", "idx:4"), DECLINE_DECISION, DECLINE_DECISION),
    ],
)
def test_only_exact_advertised_index_aliases_are_repaired(options, answer, expected):
    decision = PendingDecision(
        (1, 2), "CastingTimeOptions", tuple(DecisionOption(oid, "Choice") for oid in options)
    )
    planner = planner_with(json.dumps({"option_ids": [answer]}))
    assert planner._llm_decision_options(decision, target_state()) == [expected]


@pytest.mark.parametrize(
    "meta",
    [
        {"choiceKind": "done", "childIndex": 0},
        {"choiceKind": "kicker", "childIndex": 1},
        {"choiceKind": "modal", "childIndex": 2, "optionIndex": 0},
        {"choiceKind": "numeric_input", "childIndex": 2, "numericValue": 7},
    ],
)
def test_casting_alias_means_advertised_id_not_child_index_or_x_value(meta):
    decision = PendingDecision(
        (1, 2),
        "CastingTimeOptions",
        (DecisionOption("idx:3", "First choice"), DecisionOption("idx:4", "Second choice", meta=meta)),
    )
    planner = planner_with(json.dumps({"option_ids": [4]}))
    chosen = planner._llm_decision_options(decision, target_state())
    assert chosen == ["idx:4"]
    bridge = CastingBridge({})
    assert submit_option(bridge, decision, chosen)
    assert bridge.submitted == [("action", 4, {**meta, "gameStateId": 1, "msgId": 2})]


def test_choose_two_modes_remain_an_atomic_selection(modal_poll):
    for action in modal_poll["actions"]:
        action.update(min=2, max=2)
    decision = build_pending_decision(modal_poll)
    planner = planner_with(json.dumps({"option_ids": [0, "1"]}))
    chosen = planner.plan_decision_options(decision, modal_state())
    assert chosen == ["idx:0", "idx:1"]
    bridge = CastingBridge(modal_poll)
    assert submit_option(bridge, decision, chosen)
    assert bridge.submitted == [
        ("modes", [0, 1], [{**a, "gameStateId": 337, "msgId": 474} for a in modal_poll["actions"]])
    ]


@pytest.mark.parametrize("ids", [[0, "0"], [0], [0, 1, 2]])
def test_numeric_aliases_do_not_relax_mode_count_or_distinctness(modal_poll, ids):
    for action in modal_poll["actions"]:
        action.update(min=2, max=2)
    decision = build_pending_decision(modal_poll)
    planner = planner_with(json.dumps({"option_ids": ids}))
    chosen = planner._llm_decision_options(decision, modal_state())
    bridge = CastingBridge(modal_poll)
    assert not decision.selection_is_valid(chosen)
    assert not submit_option(bridge, decision, chosen)
    assert bridge.submitted == []


def test_numeric_aliases_cannot_combine_different_casting_children(modal_poll):
    for index, action in enumerate(modal_poll["actions"]):
        action.update(min=1, max=2, childIndex=index)
    decision = build_pending_decision(modal_poll)
    planner = planner_with(json.dumps({"option_ids": [0, 1]}))
    chosen = planner._llm_decision_options(decision, modal_state())
    assert chosen == ["idx:0", "idx:1"]
    bridge = CastingBridge(modal_poll)
    assert not submit_option(bridge, decision, chosen)
    assert bridge.submitted == []


def test_explicit_decline_still_cancels(modal_poll):
    bridge = CastingBridge(modal_poll)
    planner = planner_with(json.dumps({"option_ids": [DECLINE_DECISION]}))
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    assert engine._try_typed_decision_path(modal_state(), "decision_required")
    assert bridge.submitted == [("cancel",)]


@pytest.mark.parametrize("change", ["window", "options", "closed"])
def test_numeric_mode_is_not_submitted_to_a_changed_request(modal_poll, change):
    bridge = CastingBridge(modal_poll)
    fresh = copy.deepcopy(modal_poll)
    if change == "window":
        fresh["msg_id"] += 1
    elif change == "options":
        fresh["actions"][1]["grpId"] = 999
    else:
        fresh = {"has_pending": False}
    planner = planner_with("")

    def answer(*args, **kwargs):
        bridge.poll_resp = fresh
        return json.dumps({"option_ids": [1], "reasoning": REASON})

    planner._backend.complete = answer
    engine = make_engine(bridge, planner)
    engine._game_plan_mgr = None
    assert engine._try_typed_decision_path(modal_state(), "decision_required")
    assert bridge.submitted == []
