"""Ledger #186: "choose a creature type" is a SelectN over subtype ids, not "Option N".

Match 4d934402 (2026-10-19 16:08:20, Player.log msgId 469): Herald's Horn ("As this
artifact enters, choose a creature type.") sent ``selectNReq`` with ``listType
SelectionListType_StaticSubset`` / ``staticList StaticList_SubTypes``, 353 ids
(SubType enum values: 1 Angel, 2 Archer, ..., 232 Rabbit) and prompt CardId 561 (the
Horn). The typed decision showed "Option 1" ... "Option 133" (the Mac bridge snapshot
stopped at 100 ids); the model wrote "Name Rabbit" and picked sel:1, and the client sent
``selectNResp.ids [1]``: Angel.
"""

from __future__ import annotations

import sqlite3

import pytest
from tests.fakes import planner_with
from tests.mac_bridge_fakes import FakeGame, adapter_for, enum

from arenamcp import creature_type_choice, decisions
from arenamcp.creature_type_choice import pick_creature_type, subtype_selection_ids
from arenamcp.decisions import build_pending_decision, creature_type_pick, select_n_note
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message

# The first 100 of the 353 ids Arena offered, in order (Player.log msgId 469: what the bridge
# snapshot kept), plus the types of this deck further down the list.
OFFERED = [
    1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 32, 34, 35,
    36, 37, 39, 40, 41, 42, 45, 46, 47, 48, 50, 51, 52, 53, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 66, 67,
    68, 71, 72, 74, 75, 76, 77, 78, 79, 80, 81, 84, 85, 86, 87, 88, 89, 90, 91, 92, 95, 97, 98, 102, 103,
    104, 106, 107, 108, 112, 113, 114, 115, 116, 118, 119, 120, 122, 124, 127, 128, 129, 130, 131, 132, 133,
    207, 215, 232, 273, 365,
]  # fmt: skip
NAMES = {
    1: "Angel",
    2: "Archer",
    3: "Archon",
    207: "Mutant",
    215: "Ninja",
    232: "Rabbit",
    273: "Turtle",
    365: "Noble",
}
HORN_TEXT = (
    "As this artifact enters, choose a creature type.\nCreature spells you cast of the chosen type cost {o1} "
    "less to cast.\nAt the beginning of your upkeep, look at the top card of your library. If it's a creature "
    "card of the chosen type, you may reveal it and put it into your hand."
)
HARE_TEXT = (
    "When this creature enters, create a number of 1/1 white Rabbit creature tokens equal to the number of "
    "other creatures you control named Hare Apparent.\nA deck can have any number of cards named Hare Apparent."
)
CARDS = {
    93728: {"name": "Hare Apparent", "type_line": "Creature — Rabbit Noble", "oracle_text": HARE_TEXT},
    100471: {"name": "Leonardo, Big Brother", "type_line": "Legendary Creature — Mutant Ninja Turtle"},
    72531: {"name": "Herald's Horn", "type_line": "Artifact", "oracle_text": HORN_TEXT},
    75556: {"name": "Plains", "type_line": "Basic Land — Plains"},
}


@pytest.fixture(autouse=True)
def card_database(monkeypatch):
    monkeypatch.setattr(creature_type_choice, "_db_subtype_names", lambda: tuple(sorted(NAMES.items())))
    monkeypatch.setattr(creature_type_choice, "_resolve_card", lambda grp: CARDS.get(grp, {}))
    monkeypatch.setattr(decisions, "_live_game_state", lambda: None)


def horn_request(ids, count=None):
    return {
        "$c": "GreClient.Rules.SelectNRequest",
        "$h": 41,
        "OptionContext": enum("Resolution", 1),
        "ListType": enum("StaticSubset", 3),
        "Ids": {
            "$c": "System.Collections.Generic.List<UInt32>",
            "$h": 901,
            "$n": count or len(ids),
            "$items": ids,
        },
        "ZoneIds": {"$c": "List", "$h": 902, "$n": 0, "$items": []},
        "IdType": enum("None", 0),
        "StaticList": enum("SubTypes", 7),
        "ValidationType": enum("NonRepeatable", 1),
        "Weights": {"$c": "List", "$h": 903, "$n": 0, "$items": []},
        "Context": enum("Resolution", 1),
        "MinSel": 1,
        "MaxSel": 1,
        "MinWeight": -(2**31),
        "MaxWeight": 2**31 - 1,
        "SourceId": 9011,
        "ShouldCancel": False,
    }


def horn_poll(ids=None, count=None, game=None):
    game = game or FakeGame(
        horn_request(ids or OFFERED, count), {"Type": enum("SelectN", 9)}, gsid=349, msg=469
    )
    poll = adapter_for(game).handle({"action": "get_pending_actions"})
    poll["request_payload"] = {
        "requestType": "SelectN",
        "requestClass": "SelectNRequest",
        "prompt": {
            "promptId": 23,
            "parameters": [{"parameterName": "CardId", "type": "Number", "value": 561}],
        },
        "sourceId": 9011,
    }
    return poll


def horn(iid=561):
    return {"instance_id": iid, "name": "Herald's Horn", "controller_seat_id": 2, "type_line": "Artifact",
            "oracle_text": HORN_TEXT}  # fmt: skip


def hare_state():
    rabbits = [
        {"instance_id": i, "name": "Rabbit", "controller_seat_id": 2, "type_line": "Token Creature — Rabbit"}
        for i in (529, 530, 531)
    ]
    hares = [
        {
            "instance_id": i,
            "name": "Hare Apparent",
            "controller_seat_id": 2,
            "type_line": "Creature — Rabbit Noble",
        }
        for i in (445, 485, 519, 525)
    ]
    return {
        "local_seat_id": 2,
        "players": [{"seat_id": 1}, {"seat_id": 2, "is_local": True}],
        "deck_cards": [93728] * 60 + [100471, 72531] + [75556] * 37,
        "battlefield": [horn(), *rabbits, *hares],
        "hand": [],
    }


def test_the_subtype_list_is_recognised():
    assert subtype_selection_ids(
        list_type="SelectionListType_StaticSubset", static_list="StaticList_SubTypes", ids=[1, 232]
    ) == [1, 232]
    assert subtype_selection_ids(list_type="Dynamic", static_list="None", ids=[445]) is None
    assert subtype_selection_ids(list_type="StaticSubset", static_list="Colors", ids=[3]) is None


def test_the_options_are_named_creature_types_and_the_horn_asks():
    decision = build_pending_decision(
        horn_poll(), resolve_instance=lambda iid: "Herald's Horn" if iid == 561 else ""
    )
    assert decision.request_type == "SelectN"
    assert len(decision.options) == len(OFFERED)
    assert decision.find("sel:1").label == "Angel"
    assert decision.find("sel:232").label == "Rabbit"
    assert decision.find("sel:232").meta == {
        "choice": "creature_type",
        "subtype_id": 232,
        "identity_known": True,
    }
    assert decision.find("sel:4").label == "Creature type #4"
    assert decision.source_label == "Herald's Horn"
    assert not any(option.label.startswith("Option ") for option in decision.options)


def test_the_snapshot_keeps_the_whole_list_and_flags_a_cut():
    game = FakeGame(horn_request(OFFERED), {"Type": enum("SelectN", 9)})
    poll = adapter_for(game).handle({"action": "get_pending_actions"})
    [pending] = [op for op in game.batches[0] if op["op"] == "pending"]
    assert pending["max_items"] >= 353
    assert "select_n_ids_truncated" not in poll
    cut = adapter_for(FakeGame(horn_request(OFFERED[:100], count=353))).handle(
        {"action": "get_pending_actions"}
    )
    assert cut["select_n_ids_truncated"] is True


def test_a_cut_bridge_list_takes_the_logs_ids_for_the_same_request(monkeypatch):
    class Live:
        decision_context = {
            "request_id": [349, 469],
            "list_type": "SelectionListType_StaticSubset",
            "static_list": "StaticList_SubTypes",
            "option_ids": list(OFFERED),
            "prompt_card_ids": [561],
        }

    monkeypatch.setattr(decisions, "_live_game_state", lambda: Live())
    poll = horn_poll(ids=OFFERED[:100], count=353)
    poll.pop("request_payload")
    decision = build_pending_decision(
        poll, resolve_instance=lambda iid: "Herald's Horn" if iid == 561 else ""
    )
    assert "sel:232" in decision.option_ids()
    assert decision.source_label == "Herald's Horn"
    Live.decision_context["request_id"] = [348, 460]  # another request: the bridge's own ids only
    assert "sel:232" not in build_pending_decision(poll).option_ids()


def test_our_deck_names_rabbit():
    decision = build_pending_decision(
        horn_poll(), resolve_instance=lambda iid: "Herald's Horn" if iid == 561 else ""
    )
    pick = creature_type_pick(decision, hare_state())
    assert (pick.option_id, pick.name, pick.obvious) == ("sel:232", "Rabbit", True)
    assert pick.reason.startswith("Rabbit: our deck's main creature type (Rabbit")


def test_a_type_choice_against_creatures_is_not_obvious():
    decision = build_pending_decision(horn_poll())
    pick = pick_creature_type(decision, hare_state(), source_text="Destroy all creatures of the chosen type.")
    assert pick is not None and pick.obvious is False


def test_the_planner_names_rabbit_without_the_model(monkeypatch):
    decision = build_pending_decision(
        horn_poll(), resolve_instance=lambda iid: "Herald's Horn" if iid == 561 else ""
    )
    planner = planner_with("{}")

    def no_model(*_a, **_k):
        raise AssertionError("the model must not be asked for an obvious creature type")

    monkeypatch.setattr(planner, "_llm_decision_options", no_model)
    assert planner.plan_decision_options(decision, hare_state()) == ["sel:232"]
    assert planner._last_decision_trace["policy"] == "creature_type"


def test_the_model_down_fallback_never_names_angel(monkeypatch):
    decision = build_pending_decision(horn_poll())  # unnamed source: not obvious, the model is asked
    planner = planner_with("{}")

    def broken(*_a, **_k):
        raise RuntimeError("model down")

    monkeypatch.setattr(planner, "_llm_decision_options", broken)
    state = hare_state()
    state["battlefield"] = state["battlefield"][1:]  # the Horn's text unknown
    assert planner.plan_decision_options(decision, state) == ["sel:232"]


def test_the_prompt_note_says_these_are_types():
    decision = build_pending_decision(
        horn_poll(), resolve_instance=lambda iid: "Herald's Horn" if iid == 561 else ""
    )
    note = select_n_note(decision, hare_state())
    assert note.startswith("CREATURE TYPE CHOICE for Herald's Horn: these options are creature types")
    assert "Rabbit (" in note and "Noble (" in note


LOG_SELECT_N_REQ = {
    "type": "GREMessageType_SelectNReq",
    "systemSeatIds": [2],
    "msgId": 469,
    "gameStateId": 349,
    "prompt": {
        "promptId": 23,
        "parameters": [{"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 561}],
    },
    "selectNReq": {
        "minSel": 1,
        "maxSel": 1,
        "context": "SelectionContext_Resolution",
        "optionContext": "OptionContext_Resolution",
        "listType": "SelectionListType_StaticSubset",
        "staticList": "StaticList_SubTypes",
        "ids": OFFERED,
        "prompt": {},
        "sourceId": 9011,
        "validationType": "SelectionValidationType_NonRepeatable",
        "minWeight": -2147483648,
        "maxWeight": 2147483647,
    },
    "allowCancel": "AllowCancel_No",
}


def test_the_log_names_the_types_and_the_card_asking():
    state = GameState()
    state.set_local_seat_id(2, source=2)
    _handle_decision_message(state, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    context = state.decision_context
    assert context["option_cards"][:2] == ["Angel", "Archer"]
    assert context["option_cards"][OFFERED.index(232)] == "Rabbit"
    assert context["option_ids"] == OFFERED
    assert context["prompt_card_ids"] == [561]
    assert context["request_id"] == [349, 469]


def test_the_card_database_reads_the_subtype_enum(tmp_path):
    from arenamcp.mtgadb import MTGADatabase

    path = tmp_path / "Raw_CardDatabase_test.mtga"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE Enums (Type TEXT, Value INT, LocId INT);
        CREATE TABLE Localizations_enUS (LocId INT, Formatted INT, Loc TEXT);
        INSERT INTO Enums VALUES ('SubType', 1, 10), ('SubType', 232, 11), ('CardType', 2, 12);
        INSERT INTO Localizations_enUS VALUES (10, 1, 'Angel'), (11, 1, 'Rabbit'), (11, 0, 'rabbit'), (12, 1, 'Creature');
        """
    )
    connection.commit()
    connection.close()
    assert MTGADatabase(db_path=path).get_subtype_names() == {1: "Angel", 232: "Rabbit"}
