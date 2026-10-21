"""Tests for Bug #106: countered cast outcome tracking via ZoneTransfer category."""

from __future__ import annotations

from arenamcp.decision_memory import CANCELLED, COUNTERED, DecisionMemory
from arenamcp.decisions import DecisionOption, PendingDecision
from arenamcp.gamestate import GameState
from arenamcp.plan_ledger import TurnEntry


def _make_decision(request_type: str, options: list[tuple[str, str]], source: str = "") -> PendingDecision:
    return PendingDecision(
        request_id=(1, 1),
        request_type=request_type,
        options=tuple(DecisionOption(oid, label) for oid, label in options),
        source_label=source,
    )


def test_zone_transfer_countered_marks_cast_and_ledger_reports_not_executed():
    memory = DecisionMemory()
    state = {
        "match_id": "m-106",
        "local_seat_id": 1,
        "turn": {"turn_number": 5, "phase": "Phase_Main1", "step": ""},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}],
        "battlefield": [],
        "stack": [],
        "recent_events": [],
    }
    cast_dec = _make_decision("ActionsAvailable", [("cast:1", "Cast Cosmogrand Zenith"), ("pass", "Pass")])
    memory.record(state, cast_dec, ["cast:1"], "deploy Zenith on curve")

    # Arena counters Cosmogrand Zenith: ZoneTransfer has category="Countered"
    state["recent_events"] = [
        {
            "type": "zone_transfer",
            "card": "Cosmogrand Zenith",
            "instance_id": 291,
            "orig_instance_id": 287,
            "from_zone": 27,
            "to_zone": 33,
            "category": "Countered",
            "owner_seat_id": 1,
            "controller_seat_id": 1,
            "turn": 5,
        }
    ]
    lines = memory.lines_for_turn(5, state=state)
    assert len(lines) == 1
    assert f"[{COUNTERED}]" in lines[0]

    entry = TurnEntry(
        turn=5,
        ours=True,
        planned="Cast Cosmogrand Zenith",
        planned_casts=["Cosmogrand Zenith"],
        did=lines,
        start={"our_life": 20, "opp_life": 20},
        end={"our_life": 20, "opp_life": 20},
    )
    made, missed = entry.executed()
    assert made == []
    assert missed == ["Cosmogrand Zenith"]
    assert "NOT executed (missing Cosmogrand Zenith)" in entry.line()


def test_zone_transfer_resolve_after_paid_spell_pierce_not_marked_countered():
    """Paying Spell Pierce results in category='Resolve', never 'Countered'."""
    memory = DecisionMemory()
    state = {
        "match_id": "m-106-resolve",
        "local_seat_id": 1,
        "turn": {"turn_number": 4, "phase": "Phase_Main1", "step": ""},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}],
        "battlefield": [],
        "stack": [],
        "action_history": [
            {"turn": 4, "seat": 1, "action": "Cast", "card": "Sheltered by Ghosts"},
            {"turn": 4, "seat": 2, "action": "Cast", "card": "Spell Pierce"},
        ],
        "recent_events": [
            {
                "type": "zone_transfer",
                "card": "Spell Pierce",
                "instance_id": 310,
                "from_zone": 27,
                "to_zone": 37,
                "category": "Resolve",
                "owner_seat_id": 2,
                "controller_seat_id": 2,
                "turn": 4,
            },
            {
                "type": "zone_transfer",
                "card": "Sheltered by Ghosts",
                "instance_id": 305,
                "from_zone": 27,
                "to_zone": 28,
                "category": "Resolve",
                "owner_seat_id": 1,
                "controller_seat_id": 1,
                "turn": 4,
            },
        ],
    }
    cast_dec = _make_decision("ActionsAvailable", [("cast:1", "Cast Sheltered by Ghosts"), ("pass", "Pass")])
    memory.record(state, cast_dec, ["cast:1"], "exile blocker and gain ward/lifelink")

    lines = memory.lines_for_turn(4, state=state)
    assert len(lines) == 1
    assert COUNTERED not in lines[0]

    entry = TurnEntry(
        turn=4,
        ours=True,
        planned="Cast Sheltered by Ghosts",
        planned_casts=["Sheltered by Ghosts"],
        did=lines,
        start={"our_life": 20, "opp_life": 20},
        end={"our_life": 20, "opp_life": 20},
    )
    made, missed = entry.executed()
    assert made == ["Sheltered by Ghosts"]
    assert missed == []


def test_mark_cancelled_does_not_reach_past_later_priority_decision():
    """Declining a sub-decision later in the turn must not cancel an earlier resolved cast."""
    memory = DecisionMemory()
    state = {
        "match_id": "m-106-cancel-scope",
        "local_seat_id": 1,
        "turn": {"turn_number": 6, "phase": "Phase_Main1", "step": ""},
        "players": [{"seat_id": 1, "is_local": True, "life_total": 20}],
        "battlefield": [],
        "stack": [],
    }
    cast_dec = _make_decision("ActionsAvailable", [("cast:1", "Cast Shock"), ("pass", "Pass")])
    memory.record(state, cast_dec, ["cast:1"], "burn opponent")
    tgt_dec = _make_decision("SelectTargets", [("tgt:2", "Opponent")], source="Shock")
    memory.record(state, tgt_dec, ["tgt:2"], "go face")

    # Later priority window after Shock resolved
    pass_dec = _make_decision("ActionsAvailable", [("pass", "Pass")])
    memory.record(state, pass_dec, ["pass"], "pass to combat")

    # A later declined sub-decision naming Shock must not walk back past the pass
    later_sub = _make_decision("SelectTargets", [("tgt:2", "Opponent")], source="Shock")
    memory.record(state, later_sub, ["__decline__"], "cancelled")

    records = memory.this_turn(state)
    assert records[0].outcome == ""
    assert records[1].outcome == ""
    assert records[3].chosen_labels == [CANCELLED]


def test_gamestate_zone_transfer_resolves_object_id_changed_on_counter():
    gs = GameState()
    gs.set_local_seat_id(1)
    gs._card_name_cache[98386] = "Cosmogrand Zenith"
    gs.update_from_message(
        {
            "turnInfo": {"turnNumber": 5, "phase": "Phase_Main1", "activePlayer": 1, "priorityPlayer": 1},
            "gameObjects": [
                {
                    "instanceId": 287,
                    "grpId": 98386,
                    "type": "GameObjectType_Card",
                    "zoneId": 27,
                    "ownerSeatId": 1,
                    "controllerSeatId": 1,
                }
            ],
            "annotations": [
                {
                    "id": 900,
                    "affectorId": 289,
                    "affectedIds": [287],
                    "type": ["AnnotationType_ObjectIdChanged"],
                    "details": [
                        {"key": "orig_id", "type": "AnnotationOptionType_Int32", "valueInt32": [287]},
                        {"key": "new_id", "type": "AnnotationOptionType_Int32", "valueInt32": [291]},
                    ],
                },
                {
                    "id": 901,
                    "affectorId": 289,
                    "affectedIds": [291],
                    "type": ["AnnotationType_ZoneTransfer"],
                    "details": [
                        {"key": "zone_src", "type": "AnnotationOptionType_Int32", "valueInt32": [27]},
                        {"key": "zone_dest", "type": "AnnotationOptionType_Int32", "valueInt32": [33]},
                        {
                            "key": "category",
                            "type": "AnnotationOptionType_String",
                            "valueString": ["Countered"],
                        },
                    ],
                },
            ],
        }
    )
    zt_events = [e for e in gs.recent_events if e.get("type") == "zone_transfer"]
    assert len(zt_events) == 1
    assert zt_events[0]["card"] == "Cosmogrand Zenith"
    assert zt_events[0]["category"] == "Countered"
    assert zt_events[0]["orig_instance_id"] == 287
    assert zt_events[0]["owner_seat_id"] == 1
