"""+1/+1 and -1/-1 counters from Player.log get their names (ledger #99, match 18b1601a, 2026-10-19).

Player.log sends counters only as CounterAdded/CounterRemoved annotations with
the GRE CounterType number (``counter_type`` 1 = P1P1, 2 = M1M1; re-output
GreProtobuf CounterType.cs). Only Stun (172) was decoded, so Sazh's Chocobo's
four +1/+1 counters were "unknown": ``concede._counter_delta`` never saw them
and, on our turn, took the 4/5 for its printed 0/1 pumped "until end of turn".
The object's power/toughness already include its counters; the names must not
be added to them again.
"""

from __future__ import annotations

from arenamcp.concede import _printed_now
from arenamcp.gamestate import GameState, create_game_state_handler
from arenamcp.gamestate_transforms import _counter_type_name

BATTLEFIELD = 28
US, THEM = 2, 1
CHOCOBO = 346  # Sazh's Chocobo, printed 0/1


def _deliver(state: GameState, message: dict) -> None:
    create_game_state_handler(state)(
        {
            "greToClientEvent": {
                "greToClientMessages": [
                    {
                        "type": "GREMessageType_GameStateMessage",
                        "systemSeatIds": [US],
                        "gameStateMessage": message,
                    }
                ]
            }
        }
    )


def _chocobo(power: int, toughness: int) -> dict:
    return {
        "instanceId": CHOCOBO,
        "grpId": 96067,
        "type": "GameObjectType_Card",
        "zoneId": BATTLEFIELD,
        "visibility": "Visibility_Public",
        "ownerSeatId": THEM,
        "controllerSeatId": THEM,
        "cardTypes": ["CardType_Creature"],
        "power": {"value": power},
        "toughness": {"value": toughness},
    }


def _counter(ann_id: int, counter_type: int, amount: int, *, added: bool = True) -> dict:
    return {
        "id": ann_id,
        "affectorId": 355,
        "affectedIds": [CHOCOBO],
        "type": ["AnnotationType_CounterAdded" if added else "AnnotationType_CounterRemoved"],
        "details": [
            {"key": "counter_type", "type": "KeyValuePairValueType_int32", "valueInt32": [counter_type]},
            {"key": "transaction_amount", "type": "KeyValuePairValueType_int32", "valueInt32": [amount]},
        ],
    }


def _board() -> GameState:
    state = GameState()
    _deliver(
        state,
        {
            "type": "GameStateType_Full",
            "gameStateId": 1,
            "turnInfo": {"phase": "Phase_Main1", "turnNumber": 2, "activePlayer": THEM},
            "zones": [
                {
                    "zoneId": BATTLEFIELD,
                    "type": "ZoneType_Battlefield",
                    "visibility": "Visibility_Public",
                    "objectInstanceIds": [CHOCOBO],
                }
            ],
            "players": [
                {"systemSeatNumber": THEM, "lifeTotal": 20},
                {"systemSeatNumber": US, "lifeTotal": 20},
            ],
            "gameObjects": [_chocobo(0, 1)],
        },
    )
    return state


def test_counter_types_decode_by_the_gre_enum():
    assert _counter_type_name({"counter_type": 1}) == "P1P1"
    assert _counter_type_name({"counter_type": 2}) == "M1M1"
    assert _counter_type_name({"counter_type": 172}) == "Stun"
    assert _counter_type_name({"counter_type": 5}) == "Time"
    assert _counter_type_name({"counter_type": 108}) == "Lore"
    # Loyalty stays out: the object's loyalty field sets it, a delta would double it.
    assert _counter_type_name({"counter_type": 7}) == "unknown"
    assert _counter_type_name({"counterType": "CounterType_P1P1", "counter_type": 1}) == "CounterType_P1P1"


def test_landfall_counters_are_p1p1_and_not_added_to_the_power_again():
    state = _board()
    # Four landfall triggers over the game (gsid 97-211 in Player.log): the GRE sends the new
    # P/T on the object and a CounterAdded annotation for each counter.
    for index in range(4):
        _deliver(
            state,
            {
                "type": "GameStateType_Diff",
                "gameStateId": 10 + index,
                "gameObjects": [_chocobo(index + 1, index + 2)],
                "annotations": [_counter(900 + index, 1, 1)],
            },
        )
    chocobo = state.game_objects[CHOCOBO]
    assert chocobo.counters == {"P1P1": 4}
    assert (chocobo.power, chocobo.toughness) == (4, 5)
    # A -1/-1 counter and a removal.
    _deliver(
        state,
        {
            "type": "GameStateType_Diff",
            "gameStateId": 20,
            "gameObjects": [_chocobo(3, 4)],
            "annotations": [_counter(910, 2, 1)],
        },
    )
    assert state.game_objects[CHOCOBO].counters == {"P1P1": 4, "M1M1": 1}
    _deliver(
        state,
        {"type": "GameStateType_Diff", "gameStateId": 21, "annotations": [_counter(911, 2, 1, added=False)]},
    )
    assert state.game_objects[CHOCOBO].counters == {"P1P1": 4}


def test_concede_reads_the_counters_against_printed_power():
    # The 4/5 Chocobo with its four counters is no "until end of turn" pump of a 0/1.
    card = {"power": 4, "toughness": 5, "printed_power": 0, "printed_toughness": 1, "counters": {"P1P1": 4}}
    assert _printed_now(card) == (4, 5)
    shrunk = {
        "power": 3,
        "toughness": 4,
        "printed_power": 0,
        "printed_toughness": 1,
        "counters": {"P1P1": 4, "M1M1": 1},
    }
    assert _printed_now(shrunk) == (3, 4)
