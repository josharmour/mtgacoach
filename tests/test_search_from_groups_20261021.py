"""SearchFromGroups: Fblthp, Knows the Way's pre-grouped library search (2026-10-21).

bug_20261021_210715 / bug_20261021_211718 (Sealed FRA, Mac IL2CPP bridge): Fblthp's
ETB "search your library for up to X basic lands" arrives as one
SearchFromGroupsReq per land — a single group holding the matching library ids
(maxSelect 1), maxFind 1, AllowFailToFind_Any. Nothing typed the request, so the
legacy planner saw ['Select from options', 'Done'], answered "Done", the bridge
tried submit_pass ("Pending is SearchFromGroupsRequest, not
ActionsAvailableRequest") and the autopilot stood down MANUAL REQUIRED twice on
an ETB that only wanted a Forest.

These tests pin:
  - the typed builder exposes the grouped library ids as ``sfg:`` options with
    the request's minFind/maxFind as the totals (maxFind 1 across two groups);
  - the names come from the library cards the GRE revealed (``library_visible``);
  - the safe default finds a land our hand needs rather than failing to find;
  - the answer leaves through ``submit_search_from_groups``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import arenamcp.autopilot as ap
from arenamcp.autopilot_bridge import _BridgeSubmitMixin
from arenamcp.decisions import (
    build_pending_decision,
    search_from_groups_safe_default,
    select_from_groups_note,
    select_from_groups_safe_default,
    submit_option,
)
from arenamcp.gamestate import GameState

_NAMES = {274: "Forest", 278: "Island", 279: "Forest", 285: "Island", 288: "Island", 291: "Island"}


def _fblthp_pending(groups=None, max_find=1):
    """The Mac adapter poll (generic request_payload) for Fblthp's search."""
    return {
        "ok": True,
        "has_pending": True,
        "request_type": "SearchFromGroups",
        "request_class": "SearchFromGroupsRequest",
        "can_cancel": False,
        "request_payload": {
            "requestType": "SearchFromGroups",
            "requestClass": "SearchFromGroupsRequest",
            "prompt": {
                "promptId": 15804,
                "parameters": [
                    {"parameterName": "CardId", "type": "Number", "value": 352, "valueCase": "NumberValue"},
                    {"parameterName": "CardId", "type": "Number", "value": 2, "valueCase": "NumberValue"},
                ],
            },
            "sourceId": 357,
            "zonesToSearch": [32],
            "maxFind": max_find,
            "groups": groups
            or [
                {
                    "ids": [278, 288, 285, 291, 274, 279],
                    "groupId": 5008,
                    "zoneType": "None",
                    "subZoneType": "None",
                    "minSelect": 0,
                    "maxSelect": 1,
                    "isFacedown": False,
                }
            ],
        },
    }


def _state():
    """T15 of the 21:07 game: two Islands out, GG cards in hand, Forests in the offer."""
    return {
        "local_seat_id": 1,
        "hand": [
            {"instance_id": 401, "name": "Greenhouse Propagator", "mana_cost": "{3}{G}{G}"},
            {"instance_id": 402, "name": "Loot, the Nexus", "mana_cost": "{1}{U}"},
        ],
        "battlefield": [
            {
                "instance_id": 201,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "controller_seat_id": 1,
            },
            {
                "instance_id": 203,
                "name": "Island",
                "type_line": "Basic Land — Island",
                "controller_seat_id": 1,
            },
        ],
        "library_visible": [
            {"instance_id": iid, "name": name, "type_line": f"Basic Land — {name}"}
            for iid, name in _NAMES.items()
        ],
    }


def test_fblthp_search_is_typed_from_the_grouped_library_ids():
    decision = build_pending_decision(_fblthp_pending(), resolve_instance=lambda iid: _NAMES.get(iid, ""))
    assert decision is not None and decision.request_type == "SearchFromGroups"
    assert decision.request_id is not None
    assert [o.option_id for o in decision.options] == [
        f"sfg:0:{iid}" for iid in (278, 288, 285, 291, 274, 279)
    ]
    assert {o.label for o in decision.options} == {"Forest", "Island"}
    assert all(o.meta["identity_known"] for o in decision.options)
    assert (decision.min_select, decision.max_select) == (0, 1)
    assert decision.selection_is_valid(["sfg:0:274"])
    assert decision.selection_is_valid([])  # fail to find is legal...
    assert not decision.selection_is_valid(["sfg:0:274", "sfg:0:278"])  # ...two finds are not
    note = select_from_groups_note(decision)
    assert note.startswith("LIBRARY SEARCH — find 0-1 card(s)") and "fails to find" in note


def test_two_groups_share_the_requests_max_find():
    """21:17 game: two groups (Forests, Islands), maxSelect 1 each, maxFind 1 overall."""
    pending = _fblthp_pending(
        groups=[
            {"ids": [247, 230, 243, 231], "groupId": 5005, "minSelect": 0, "maxSelect": 1},
            {"ids": [248, 232, 251, 240], "groupId": 5006, "minSelect": 0, "maxSelect": 1},
        ]
    )
    decision = build_pending_decision(pending, resolve_instance=lambda _iid: "")
    assert decision is not None and decision.request_type == "SearchFromGroups"
    assert decision.max_select == 1
    assert decision.find("sfg:1:248").label == "Card 248"
    assert decision.selection_is_valid(["sfg:1:248"])
    assert not decision.selection_is_valid(["sfg:0:247", "sfg:1:248"])
    chosen = search_from_groups_safe_default(decision, _state())
    assert chosen is not None and len(chosen) == 1 and decision.selection_is_valid(chosen)


def test_safe_default_finds_the_land_the_hand_needs_and_submits_through_the_search_command():
    decision = build_pending_decision(_fblthp_pending(), resolve_instance=lambda iid: _NAMES.get(iid, ""))
    chosen = select_from_groups_safe_default(decision, _state())
    assert chosen == ["sfg:0:274"]  # the first Forest: GG wanted, no green source

    bridge = MagicMock()
    bridge.submit_search_from_groups.return_value = True
    assert submit_option(bridge, decision, chosen)
    bridge.submit_search_from_groups.assert_called_once_with(groups=[{"ids": [274], "groupId": 5008}])
    bridge.submit_select_from_groups.assert_not_called()

    bridge.reset_mock()
    assert submit_option(bridge, decision, [])
    bridge.submit_search_from_groups.assert_called_once_with(groups=[])


def test_the_search_is_a_typed_family_and_library_names_resolve():
    assert "SearchFromGroups" in ap.AutopilotEngine._TYPED_DECISION_FAMILIES
    resolver = _BridgeSubmitMixin._card_name_by_instance_id
    assert resolver(None, _state(), 274) == "Forest"
    assert resolver(None, _state(), 999) == ""


def test_snapshot_exposes_the_library_cards_the_gre_revealed_to_us():
    """The search candidates arrive as private library objects with us as viewer."""
    state = GameState()
    state.local_seat_id = 1
    state.update_from_message(
        {
            "zones": [
                {"zoneId": 32, "type": "ZoneType_Library", "ownerSeatId": 1, "objectInstanceIds": [274, 275]}
            ],
            "gameObjects": [
                {
                    "instanceId": 274,
                    "grpId": 65393,
                    "type": "GameObjectType_Card",
                    "zoneId": 32,
                    "visibility": "Visibility_Private",
                    "ownerSeatId": 1,
                    "controllerSeatId": 1,
                    "cardTypes": ["CardType_Land"],
                    "subtypes": ["SubType_Forest"],
                    "viewers": [1],
                }
            ],
        }
    )
    snapshot = state.get_published_snapshot()
    visible = snapshot["zones"]["library_visible"]
    assert [card["instance_id"] for card in visible] == [274]
    assert visible[0]["grp_id"] == 65393
