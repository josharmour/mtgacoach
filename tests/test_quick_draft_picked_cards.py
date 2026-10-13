"""Quick Draft picks come from Arena's own PickedCards list.

Player.log 2026-10-12 (QuickDraft_FRA_20261008): picks are logged as
``==> BotDraftDraftPick {"request": "{...\\"PickInfo\\":{\\"CardIds\\":[\\"106276\\"]...}}"}``
(no underscore, CardIds as strings), which no handler matched, so
``picked_cards`` stayed empty for the whole draft ("only 0 creatures so far" on
every pick) and a coach restarted at P3p1 drafted pack 3 with an empty pool. Each
BotDraft response carries ``PickedCards``: everything picked so far.
"""

from __future__ import annotations

import json

from arenamcp.draftstate import DraftState, create_draft_handler

EVENT = "QuickDraft_FRA_20261008"
PACK_ONE_PICKS = ["106358", "106291", "106364", "106328", "106283", "106304", "106276"]


def _response(pack: int, pick: int, picked: list[str], draft_pack: list[str]) -> dict:
    payload = {
        "Result": "Success",
        "EventName": EVENT,
        "DraftStatus": "PickNext",
        "PackNumber": pack,
        "PickNumber": pick,
        "NumCardsToPick": 1,
        "DraftPack": draft_pack,
        "PackStyles": [],
        "PickedCards": picked,
        "PickedStyles": [],
    }
    return {"CurrentModule": "BotDraft", "Payload": json.dumps(payload)}


PICK_REQUEST = {
    "id": "b06e930b-5969-4819-b1b8-0a9d8b100371",
    "request": json.dumps(
        {
            "EventName": EVENT,
            "PickInfo": {"EventName": EVENT, "CardIds": ["106276"], "PackNumber": 1, "PickNumber": 0},
        }
    ),
}


def test_picked_cards_follow_arenas_list():
    state = DraftState()
    handle = create_draft_handler(state)
    handle("EventName", _response(0, 0, [], ["106229", "106269"]))
    assert state.picked_cards == []
    handle("EventName", PICK_REQUEST)
    handle("EventName", _response(0, 7, PACK_ONE_PICKS, ["106325", "106231"]))
    assert state.picked_cards == [int(c) for c in PACK_ONE_PICKS]
    assert (state.pack_number, state.pick_number) == (1, 8)


def test_a_restarted_coach_recovers_the_pool_from_the_latest_response():
    fresh = DraftState()
    create_draft_handler(fresh)("EventName", _response(2, 0, PACK_ONE_PICKS * 4, ["106400"]))
    assert len(fresh.picked_cards) == 28
    assert fresh.is_active and fresh.event_name == EVENT
