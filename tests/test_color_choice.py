"""Room of Refuge's "choose a color" is a SelectN over colour ids, not cards.

Shapes come from the live match of 2026-10-07 18:39 (Player.log msgId 27):
``selectNReq`` with ``listType SelectionListType_Static``,
``staticList StaticList_Colors``, no ids, min/max 1, prompt 118 with
``CardId 240`` (Room of Refuge); the user's manual answer was
``selectNResp.ids [3]`` (Black). The coach saw "Select Items" with no options,
the bridge found no ids, the planner answered ``select_target -> Blue`` and
the stale "Play Land: Swamp" menu was re-planned three times.
"""

from __future__ import annotations

import pytest

from arenamcp.action_planner import ActionPlanner, ActionType
from arenamcp.color_choice import color_ids_for_names, color_selection_ids, pick_color
from arenamcp.decisions import build_pending_decision
from arenamcp.gamestate import GameState
from arenamcp.gamestate_decisions import _handle_decision_message
from arenamcp.gre_bridge import enrich_snapshot_from_pending_response
from arenamcp.rules_engine import RulesEngine
from test_mac_bridge_adapter import FakeGame, adapter_for, enum, listing, submitted_uint_values
from test_typed_decision_path import _engine, _planner_with, _TypedBridge

# --- the live shapes ------------------------------------------------------------

LOG_SELECT_N_REQ = {
    "type": "GREMessageType_SelectNReq",
    "systemSeatIds": [2],
    "msgId": 27,
    "gameStateId": 9,
    "prompt": {
        "promptId": 118,
        "parameters": [{"parameterName": "CardId", "type": "ParameterType_Number", "numberValue": 240}],
    },
    "selectNReq": {
        "minSel": 1,
        "maxSel": 1,
        "context": "SelectionContext_Resolution",
        "optionContext": "OptionContext_Resolution",
        "listType": "SelectionListType_Static",
        "staticList": "StaticList_Colors",
        "prompt": {},
        "sourceId": 9004,
        "validationType": "SelectionValidationType_NonRepeatable",
        "minWeight": -2147483648,
        "maxWeight": 2147483647,
    },
    "allowCancel": "AllowCancel_No",
}

STALE_PRIORITY_MENU = ["Play Land: Swamp", "Play Land: Swamp", "Play Land: Room of Refuge", "Pass"]
STALE_PRIORITY_RAW = [
    {"actionType": "ActionType_Play", "grpId": 106531, "instanceId": 159},
    {"actionType": "ActionType_Play", "grpId": 106531, "instanceId": 160},
    {"actionType": "ActionType_Play", "grpId": 106433, "instanceId": 164},
    {"actionType": "ActionType_Pass"},
]


def _card(name, mana_cost="", instance_id=0, **extra):
    return {"instance_id": instance_id, "name": name, "mana_cost": mana_cost, **extra}


def live_hand():
    """Swamp, Swamp, {2}{B}, {2}{U}, {1}{U/B}, {1}{U}: two and a half blue pips, Swamps cover black."""
    return [
        _card("Swamp", instance_id=159, card_types=["Land"], oracle_text="({T}: Add {B}.)"),
        _card("Swamp", instance_id=160, card_types=["Land"], oracle_text="({T}: Add {B}.)"),
        _card("Screeching Soulbreaker", "{2}{B}", 161, card_types=["Creature"]),
        _card("Divining Duelist", "{2}{U}", 162, card_types=["Creature"]),
        _card("Paradox Shaper", "{1}{U/B}", 163, card_types=["Creature"]),
        _card("Fblthp, Impossibly Lost", "{1}{U}", 165, card_types=["Creature"]),
    ]


def live_state():
    return {
        "turn": {"turn_number": 1, "phase": "Phase_Main1", "active_player": 2},
        "players": [{"seat_id": 1}, {"seat_id": 2, "is_local": True}],
        "local_seat_id": 2,
        "_bridge_connected": True,
        "_bridge_request_type": "SelectN",
        "pending_decision": "Select Items",
        "hand": live_hand(),
        "battlefield": [
            _card(
                "Room of Refuge",
                instance_id=240,
                card_types=["Land"],
                controller_seat_id=2,
                color_production=[],
            )
        ],
    }


def color_request(handle=31, list_type="Static", static_list="Colors", ids=(), min_sel=1, max_sel=1):
    return {
        "$c": "GreClient.Rules.SelectNRequest",
        "$h": handle,
        "OptionContext": enum("Resolution", 1),
        "ListType": enum(list_type, 1 if list_type == "Static" else 3),
        "Ids": listing(*ids),
        "ZoneIds": listing(),
        "IdType": enum("None", 0),
        "StaticList": enum(static_list, 6),
        "ValidationType": enum("NonRepeatable", 1),
        "Weights": listing(),
        "Context": enum("Resolution", 1),
        "MinSel": min_sel,
        "MaxSel": max_sel,
        "MinWeight": -(2**31),
        "MaxWeight": 2**31 - 1,
        "SourceId": 9004,
        "ShouldCancel": False,
        "ReqPrompt": {"$c": "Wotc.Mtgo.Gre.External.Messaging.Prompt", "$h": 32, "promptId_": 0},
    }


COLOR_GETTERS = {"Type": enum("SelectN", 9), "IsCardColorSelection": True, "IsManaColorSelection": False}


# --- shared colour table ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"list_type": "SelectionListType_Static", "static_list": "StaticList_Colors"}, [1, 2, 3, 4, 5]),
        ({"list_type": "StaticSubset", "static_list": "CardColors", "ids": [3, 2]}, [2, 3]),
        ({"list_type": "Static", "static_list": "ManaColors"}, [1, 2, 3, 4, 5]),
        ({"context": "SelectionContext_ManaFromAbility", "ids": [4]}, [4]),
        ({"is_color": True}, [1, 2, 3, 4, 5]),
        ({"list_type": "Dynamic", "static_list": "None", "ids": [301, 302]}, None),
        ({"list_type": "StaticSubset", "static_list": "BasicLandTypes", "ids": [29, 43]}, None),
    ],
)
def test_color_selection_ids_follow_the_client_workflow(fields, expected):
    assert color_selection_ids(**fields) == expected


def test_color_names_and_letters_map_to_offered_ids_only():
    assert color_ids_for_names(["Blue"], [1, 2, 3, 4, 5]) == [2]
    assert color_ids_for_names(["black", "U"], [1, 2, 3, 4, 5]) == [3, 2]
    assert color_ids_for_names(["Blue"], [1, 3]) == []
    assert color_ids_for_names(["Swamp"], [1, 2, 3, 4, 5]) == []


# --- native Mac bridge ----------------------------------------------------------


def test_mac_bridge_exposes_the_colour_choice_with_its_ids():
    game = FakeGame(color_request(), COLOR_GETTERS)
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert response["request_type"] == "SelectN"
    assert response["select_n_ids"] == []
    assert response["select_n_list_type"] == "Static"
    assert response["select_n_static_list"] == "Colors"
    assert response["select_n_is_card_color"] is True
    assert [o["id"] for o in response["select_n_color_options"]] == [1, 2, 3, 4, 5]
    assert [o["name"] for o in response["select_n_color_options"]] == [
        "White",
        "Blue",
        "Black",
        "Red",
        "Green",
    ]
    assert response["decision_context"]["type"] == "choose_color"
    assert response["decision_context"]["source_id"] == 9004
    assert response["decision_context"]["options"][1] == "Blue"


def test_mac_bridge_detects_the_colour_list_from_fields_when_getters_are_unread():
    response = adapter_for(FakeGame(color_request())).handle({"action": "get_pending_actions"})
    assert [o["id"] for o in response["select_n_color_options"]] == [1, 2, 3, 4, 5]


def test_mac_bridge_static_subset_offers_only_the_listed_colours():
    game = FakeGame(color_request(list_type="StaticSubset", ids=(3, 2)), COLOR_GETTERS)
    response = adapter_for(game).handle({"action": "get_pending_actions"})
    assert [o["name"] for o in response["select_n_color_options"]] == ["Blue", "Black"]
    assert adapter_for(game).handle({"action": "submit_selection", "ids": [1]})["ok"] is False
    assert not game.submits()


def test_mac_bridge_submits_the_colour_id_as_the_client_does():
    game = FakeGame(color_request(), COLOR_GETTERS)
    response = adapter_for(game).handle({"action": "submit_selection", "ids": [2]})
    assert response == {"ok": True, "submitted_type": "SelectN"}
    [batch] = game.submits()
    assert batch[1] == {"op": "expect_pending", "target": {"h": 31}}
    assert batch[-1]["method"] == "SubmitSelection"
    assert submitted_uint_values(batch) == [2]


@pytest.mark.parametrize("ids", [[], [7], [2, 3], [2, 2]])
def test_mac_bridge_never_submits_an_invalid_colour_answer(ids):
    game = FakeGame(color_request(), COLOR_GETTERS)
    response = adapter_for(game).handle({"action": "submit_selection", "ids": ids})
    assert response["ok"] is False
    assert not game.submits()


# --- typed decision -------------------------------------------------------------


def mac_poll(**updates):
    poll = adapter_for(FakeGame(color_request(), COLOR_GETTERS)).handle({"action": "get_pending_actions"})
    poll["request_payload"] = {
        "requestType": "SelectN",
        "requestClass": "SelectNRequest",
        "prompt": {
            "promptId": 118,
            "parameters": [{"parameterName": "CardId", "type": "Number", "value": 240}],
        },
        "sourceId": 9004,
    }
    return {**poll, **updates}


def windows_poll():
    """The shipped plugin: shape flags but no static list and no ids."""
    return {
        "has_pending": True,
        "request_type": "SelectN",
        "request_class": "SelectNRequest",
        "select_n_ids": [],
        "select_n_id_type": "None",
        "select_n_list_type": "Static",
        "select_n_context": "Resolution",
        "select_n_min": 1,
        "select_n_max": 1,
        "select_n_is_card_color": True,
    }


@pytest.mark.parametrize("poll", [mac_poll(), windows_poll()])
def test_colour_select_n_builds_colour_options_not_card_ids(poll):
    decision = build_pending_decision(
        poll, resolve_instance=lambda iid: "Room of Refuge" if iid == 240 else ""
    )
    assert decision is not None and decision.request_type == "SelectN"
    assert [o.option_id for o in decision.options] == ["sel:1", "sel:2", "sel:3", "sel:4", "sel:5"]
    assert decision.find("sel:2").label == "Blue"
    assert decision.find("sel:2").meta == {"choice": "color", "color": "U", "color_id": 2}
    assert (decision.min_select, decision.max_select) == (1, 1)
    assert decision.selection_is_valid(["sel:3"])
    assert not decision.selection_is_valid(["sel:2", "sel:3"])


def test_colour_decision_names_the_card_asking():
    decision = build_pending_decision(
        mac_poll(), resolve_instance=lambda iid: {240: "Room of Refuge"}.get(iid, "")
    )
    assert decision.source_label == "Room of Refuge"


def test_live_hand_makes_blue_the_obvious_colour():
    decision = build_pending_decision(mac_poll())
    pick = pick_color(decision, live_state())
    assert pick is not None
    assert (pick.option_id, pick.name, pick.obvious) == ("sel:2", "Blue", True)
    assert "2.5 blue pip" in pick.reason and "no land of ours makes it" in pick.reason


def test_a_covered_colour_loses_to_an_uncovered_one():
    state = live_state()
    state["hand"] = [
        _card("Island", card_types=["Land"], oracle_text="({T}: Add {U}.)"),
        _card("Island", card_types=["Land"], oracle_text="({T}: Add {U}.)"),
        _card("Divining Duelist", "{2}{U}", card_types=["Creature"]),
        _card("Screeching Soulbreaker", "{2}{B}", card_types=["Creature"]),
    ]
    pick = pick_color(build_pending_decision(mac_poll()), state)
    assert (pick.name, pick.obvious) == ("Black", True)


def test_an_even_split_is_not_obvious_and_never_defaults_to_white():
    state = live_state()
    state["hand"] = [
        _card("A", "{1}{U}", card_types=["Creature"]),
        _card("B", "{1}{B}", card_types=["Creature"]),
    ]
    pick = pick_color(build_pending_decision(mac_poll()), state)
    assert pick.obvious is False
    assert pick.name in {"Blue", "Black"}


def test_planner_picks_the_obvious_colour_without_the_model(monkeypatch):
    planner = _planner_with("{}")

    def no_model(*_a, **_k):
        raise AssertionError("the model must not be asked for an obvious colour")

    monkeypatch.setattr(planner, "_llm_decision_options", no_model)
    assert planner.plan_decision_options(build_pending_decision(mac_poll()), live_state()) == ["sel:2"]
    assert planner._last_decision_reasoning.startswith("Blue: 2.5 blue pip")


def test_planner_asks_the_model_when_the_colour_is_not_obvious_and_falls_back_to_the_best_scorer(monkeypatch):
    state = live_state()
    state["hand"] = [
        _card("A", "{1}{U}", card_types=["Creature"]),
        _card("B", "{1}{B}", card_types=["Creature"]),
    ]
    decision = build_pending_decision(mac_poll())
    planner = _planner_with("{}")
    monkeypatch.setattr(planner, "_llm_decision_options", lambda *_a, **_k: ["sel:3"])
    assert planner.plan_decision_options(decision, state) == ["sel:3"]

    def broken(*_a, **_k):
        raise RuntimeError("model down")

    monkeypatch.setattr(planner, "_llm_decision_options", broken)
    assert planner.plan_decision_options(decision, state) != ["sel:1"]
    assert planner.plan_decision_options(decision, state)[0] in {"sel:2", "sel:3"}


def test_deterministic_pick_alone_would_have_said_white():
    """Why the colour fallback exists: the mechanical pick is positional."""
    assert ActionPlanner.deterministic_option_pick(build_pending_decision(mac_poll())) == ["sel:1"]


def test_autopilot_submits_the_colour_id_through_the_typed_path(monkeypatch):
    bridge = _TypedBridge(mac_poll(game_state_id=9, msg_id=27))
    planner = _planner_with("{}")
    engine = _engine(monkeypatch, bridge, planner)
    # The turn-1 "first plan" reform would call this backend from its own thread (the
    # test backend has no breaker, so the game-plan gate is open) and race the count below.
    engine._game_plan_mgr = None
    assert engine._try_typed_decision_path(live_state(), "decision_required") is True
    assert bridge.submitted == [("selection", [2])]
    assert planner._backend.calls == 0  # the colour was chosen without an LLM call


# --- log-first: the same choice without any bridge ------------------------------


def _log_state_with_stale_menu() -> GameState:
    state = GameState()
    state.set_local_seat_id(2, source=2)
    state.legal_actions = list(STALE_PRIORITY_MENU)
    state.legal_actions_raw = [dict(a) for a in STALE_PRIORITY_RAW]
    return state


def test_log_parser_names_the_colours_and_drops_the_stale_priority_menu():
    state = _log_state_with_stale_menu()
    _handle_decision_message(state, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    assert state.pending_decision == "Choose a Color"
    context = state.decision_context
    assert context["type"] == "choose_color"
    assert context["option_ids"] == [1, 2, 3, 4, 5]
    assert context["option_cards"] == ["White", "Blue", "Black", "Red", "Green"]
    assert context["id_type"] == "Color"
    assert context["static_list"] == "StaticList_Colors"
    assert (context["min"], context["max"]) == (1, 1)
    assert state.legal_actions == [] and state.legal_actions_raw == []


def test_rules_engine_offers_the_colours_instead_of_the_old_land_drops():
    state = _log_state_with_stale_menu()
    _handle_decision_message(state, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    snapshot = {"decision_context": state.decision_context, "legal_actions": list(STALE_PRIORITY_MENU)}
    assert RulesEngine.get_legal_actions(snapshot) == [
        "Choose color: White",
        "Choose color: Blue",
        "Choose color: Black",
        "Choose color: Red",
        "Choose color: Green",
    ]


def test_colour_menu_entry_is_a_selection_never_a_target():
    action = ActionPlanner._legal_action_to_action(_planner_with("{}"), "Choose color: Blue")
    assert action.action_type == ActionType.SELECT_N
    assert action.select_card_names == ["Blue"]


# --- bridge overlay: a non-priority request retires the priority menu ------------


@pytest.mark.parametrize(
    "poll",
    [
        mac_poll(),
        windows_poll(),
        {"has_pending": True, "request_type": "SelectTargets", "request_class": "SelectTargetsRequest"},
    ],
)
def test_bridge_request_other_than_actions_available_drops_stale_priority_actions(poll):
    snapshot = {
        "pending_decision": "Select Items",
        "decision_context": {
            "type": "select_n",
            "count": 1,
            "option_ids": [],
            "option_cards": None,
            "id_type": "",
        },
        "legal_actions": list(STALE_PRIORITY_MENU),
        "legal_actions_raw": [dict(a) for a in STALE_PRIORITY_RAW],
    }
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    assert snapshot["legal_actions"] == []
    assert snapshot["legal_actions_raw"] == []


def test_log_derived_selection_menu_survives_the_same_family_overlay():
    snapshot = {
        "decision_context": {"type": "discard", "option_cards": ["Forest"]},
        "legal_actions": ["Choose Forest"],
    }
    enrich_snapshot_from_pending_response(
        snapshot, {"has_pending": True, "request_type": "SelectN"}, bridge_connected=True
    )
    assert snapshot["legal_actions"] == ["Choose Forest"]


def test_actions_available_overlay_keeps_the_live_priority_menu():
    snapshot = {
        "legal_actions": list(STALE_PRIORITY_MENU),
        "legal_actions_raw": [dict(a) for a in STALE_PRIORITY_RAW],
    }
    enrich_snapshot_from_pending_response(
        snapshot,
        {
            "has_pending": True,
            "request_type": "ActionsAvailable",
            "actions": STALE_PRIORITY_RAW,
            "can_pass": True,
        },
        bridge_connected=True,
    )
    assert snapshot["legal_actions"] == STALE_PRIORITY_MENU


@pytest.mark.parametrize("poll", [mac_poll(), windows_poll()])
def test_overlay_labels_the_colour_choice_from_either_bridge(poll):
    state = _log_state_with_stale_menu()
    _handle_decision_message(state, "GREMessageType_SelectNReq", LOG_SELECT_N_REQ)
    snapshot = {"pending_decision": state.pending_decision, "decision_context": dict(state.decision_context)}
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    assert snapshot["decision_context"]["type"] == "choose_color"
    assert snapshot["decision_context"]["option_cards"] == ["White", "Blue", "Black", "Red", "Green"]
    assert snapshot["pending_decision"] == "Choose a Color"
