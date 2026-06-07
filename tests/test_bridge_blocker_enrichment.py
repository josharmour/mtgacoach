"""Tests for bridge DeclareBlockers enrichment (gre_bridge._apply_bridge_blockers).

On a bridge-authoritative DeclareBlockers decision the plugin sends a `blockers`
array. Before this enrichment the snapshot carried no attacker flags and no
legal_blocker_ids, so combat_solver / _format_block_combat produced nothing and
the coach could only give vague prose ("take the small hit"). These tests pin
the merge that lights up the existing combat machinery.
"""

from __future__ import annotations

from typing import Any

from arenamcp.gre_bridge import enrich_snapshot_from_pending_response
from arenamcp import combat_solver


def _blockers_poll() -> dict[str, Any]:
    # One legal blocker (Llanowar Elves, inst 10) that may block the lone
    # attacker (Ilysian Caryatid, inst 50).
    return {
        "ok": True,
        "has_pending": True,
        "request_type": "DeclareBlockers",
        "request_class": "DeclareBlockersRequest",
        "actions": [],
        "can_pass": False,
        "blockers": [
            {
                "blockerInstanceId": 10,
                "mustBlock": False,
                "minAttackers": 0,
                "maxAttackers": 1,
                "attackerInstanceIds": [50],
            }
        ],
    }


def _snapshot_with_board() -> dict[str, Any]:
    return {
        "battlefield": [
            {
                "instance_id": 10,
                "name": "Llanowar Elves",
                "type_line": "Creature — Elf Druid",
                "power": 1,
                "toughness": 1,
                "owner_seat_id": 1,
                "is_tapped": False,
            },
            {
                "instance_id": 50,
                "name": "Grizzly Bears",
                "type_line": "Creature — Bear",
                "power": 2,
                "toughness": 2,
                "owner_seat_id": 2,
                "is_tapped": True,
            },
        ],
    }


def test_bridge_blockers_populate_decision_context():
    snapshot = _snapshot_with_board()
    enrich_snapshot_from_pending_response(snapshot, _blockers_poll(), bridge_connected=True)

    ctx = snapshot["decision_context"]
    assert ctx["legal_blocker_ids"] == [10]
    assert ctx["attacker_ids"] == [50]
    assert ctx["raw_blockers"][0]["blockerInstanceId"] == 10
    assert ctx["raw_blockers"][0]["attackerInstanceIds"] == [50]


def test_bridge_blockers_flag_attackers_on_battlefield():
    snapshot = _snapshot_with_board()
    enrich_snapshot_from_pending_response(snapshot, _blockers_poll(), bridge_connected=True)

    by_id = {c["instance_id"]: c for c in snapshot["battlefield"]}
    assert by_id[50].get("is_attacking") is True   # the attacker is flagged
    assert "is_attacking" not in by_id[10] or by_id[10]["is_attacking"] is not True  # our blocker isn't


def test_enriched_shape_is_readable_by_combat_solver():
    """The raw_blockers we write must be exactly what combat_solver consumes."""
    snapshot = _snapshot_with_board()
    enrich_snapshot_from_pending_response(snapshot, _blockers_poll(), bridge_connected=True)
    ctx = snapshot["decision_context"]

    # Attacker objects recovered from the raw_blockers attackerInstanceIds.
    attackers = combat_solver.collect_attackers_from_raw_blockers(snapshot, ctx["raw_blockers"])
    assert [a["instance_id"] for a in attackers] == [50]

    # blocker -> allowed-attacker map.
    amap = combat_solver.blocker_allowed_attackers_map(ctx["raw_blockers"])
    assert amap == {10: {50}}

    # legal_blocker_ids resolves to our untapped creature.
    blockers = combat_solver.collect_blockers_from_decision(snapshot, ctx)
    assert [b["instance_id"] for b in blockers] == [10]


def test_no_blockers_payload_is_a_noop():
    snapshot = {"battlefield": [{"instance_id": 50, "owner_seat_id": 2}]}
    poll = {
        "ok": True,
        "has_pending": True,
        "request_type": "ActionsAvailable",
        "request_class": "ActionsAvailableRequest",
        "actions": [],
        "can_pass": True,
    }
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    ctx = snapshot.get("decision_context") or {}
    assert "raw_blockers" not in ctx
    assert "is_attacking" not in snapshot["battlefield"][0]


def test_empty_blockers_list_is_a_noop():
    snapshot = _snapshot_with_board()
    poll = _blockers_poll()
    poll["blockers"] = []
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    ctx = snapshot.get("decision_context") or {}
    assert "raw_blockers" not in ctx


def test_enrichment_lights_up_coach_combat_formatter():
    """End-to-end: flagging is_attacking (what the enrichment does) makes the
    coach's combat formatter emit concrete attacker + optimal-block lines.

    This is the data that was missing from the vague-advice prompt. The planner
    reuses CoachEngine._format_game_context, so this flows into advisory advice.
    """
    from arenamcp.coach import CoachEngine

    snapshot = _snapshot_with_board()
    enrich_snapshot_from_pending_response(snapshot, _blockers_poll(), bridge_connected=True)
    by_id = {c["instance_id"]: c for c in snapshot["battlefield"]}

    coach = CoachEngine.__new__(CoachEngine)
    your_cards = [dict(by_id[10], oracle_text="")]
    opp_cards = [dict(by_id[50], oracle_text="")]
    lines = coach._format_block_combat(
        your_cards, opp_cards, {"life_total": 20, "seat_id": 1}, 4, "Phase_Combat", set()
    )
    blob = "\n".join(lines)
    assert "Attackers:" in blob and "Grizzly Bears" in blob
    assert "Computed optimal blocks:" in blob


def test_malformed_blocker_entries_are_skipped_not_fatal():
    snapshot = _snapshot_with_board()
    poll = _blockers_poll()
    poll["blockers"] = [
        {"blockerInstanceId": "not-an-int", "attackerInstanceIds": ["x", 50]},
        "garbage",
        {"blockerInstanceId": 10, "attackerInstanceIds": [50]},
    ]
    enrich_snapshot_from_pending_response(snapshot, poll, bridge_connected=True)
    ctx = snapshot["decision_context"]
    assert ctx["legal_blocker_ids"] == [10]      # bad id skipped, good kept
    assert ctx["attacker_ids"] == [50]           # bad attacker id skipped
