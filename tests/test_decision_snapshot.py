"""B1: every recorded decision carries a compact, replayable state snapshot.

Real logged case (named per chunk rule): bug_20261006_135027 — TradDraft,
match 3da54de9-9820-4ae2-b49e-8f81c913c446, seat 2 after two mulligans, the
opponent's T7 Main1 where both Undulating Witnesses can basic-landcycle for
{2} (the landcycling case). The board fixture is
``tests.strategic_states.BUG_135027``, replayed from that day's Player.log.
"""

from __future__ import annotations

import copy
import json

import pytest
from tests.strategic_states import BUG_135027, actions_decision, cast

from arenamcp.action_planner import _legal_payable, _opponent_tapped_out
from arenamcp.board_assessment import assess
from arenamcp.decision_snapshot import (
    DECISION_SNAPSHOT_VERSION,
    build_decision_snapshot,
    game_state_from_snapshot,
)
from arenamcp.match_packets import MatchPacket

MENU = [
    ("idx:0", "Cycle Undulating Witness", cast(240, 106272), True),
    ("idx:1", "Cycle Undulating Witness", cast(304, 106272), True),
    ("idx:2", "Play Island", None, True),
    ("pass", "Pass", None, True),
]


@pytest.fixture()
def state() -> dict:
    return copy.deepcopy(BUG_135027)


def _record(state: dict) -> tuple[MatchPacket, dict]:
    packet = MatchPacket("3da54de9-9820-4ae2-b49e-8f81c913c446")
    decision = actions_decision(MENU)
    packet.add_decision(
        decision,
        ["idx:0"],
        explanation="landcycle the only pressure available",
        game_state=state,
    )
    return packet, packet.decisions[0]


def test_every_recorded_decision_carries_a_versioned_snapshot(state: dict) -> None:
    _, entry = _record(state)
    snapshot = entry["state_snapshot"]
    assert snapshot["version"] == DECISION_SNAPSHOT_VERSION
    assert entry["turn"]["turn_number"] == 7  # the pre-existing turn excerpt


def test_snapshot_restores_the_exact_planner_state(state: dict) -> None:
    """The snapshot alone rebuilds the game_state the planner was given."""
    _, entry = _record(state)
    restored = game_state_from_snapshot(entry["state_snapshot"])
    assert restored == state


def test_snapshot_replays_the_option_layer(state: dict) -> None:
    """Option facts computed on the restored state match the original's."""
    _, entry = _record(state)
    restored = game_state_from_snapshot(entry["state_snapshot"])
    legal = [o["option_id"] for o in entry["pending_decision"]["options"]]
    assert _legal_payable(state, legal) == _legal_payable(restored, legal)
    assert _opponent_tapped_out(state) == _opponent_tapped_out(restored)


def test_snapshot_replays_the_line_search(state: dict) -> None:
    """The assessment over the restored state equals the original's."""
    _, entry = _record(state)
    restored = game_state_from_snapshot(entry["state_snapshot"])
    from arenamcp import board_assessment as ba

    ba._CACHE.clear()
    first = assess(copy.deepcopy(state))
    second = assess(copy.deepcopy(restored))
    assert first == second


def test_snapshot_keeps_hidden_information_hidden(state: dict) -> None:
    """Opposing hidden cards stay counts; no names or raw events leak in."""
    _, entry = _record(state)
    dumped = json.dumps(entry["state_snapshot"])
    snap_state = entry["state_snapshot"]["state"]
    local = state["local_seat_id"]
    assert all(card["controller_seat_id"] == local for card in snap_state["hand"])
    assert snap_state["zones"]["opponent_hand_count"] == 6
    for key in ("player_names", "recent_events", "raw_events", "commentary", "action_history"):
        assert key not in snap_state


def test_snapshot_stays_compact(state: dict) -> None:
    _, entry = _record(state)
    assert len(json.dumps(entry["state_snapshot"])) < 40_000


def test_recording_survives_a_snapshot_failure(state: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    """A broken snapshot must never break decision recording."""
    import arenamcp.decision_snapshot as ds

    def _boom(game_state):
        raise RuntimeError("boom")

    monkeypatch.setattr(ds, "build_decision_snapshot", _boom)
    packet = MatchPacket("3da54de9-9820-4ae2-b49e-8f81c913c446")
    packet.add_decision(actions_decision(MENU), ["idx:0"], game_state=state)
    entry = packet.decisions[0]
    assert "state_snapshot" not in entry
    assert entry["chosen_options"] == ["idx:0"]


def test_combat_declarations_carry_the_snapshot_too(state: dict) -> None:
    packet = MatchPacket("3da54de9-9820-4ae2-b49e-8f81c913c446")
    packet.add_combat_decision(
        "DeclareBlockers",
        chosen=["block 265 -> 287"],
        options=["block 265 -> 287", "pass"],
        game_state=state,
    )
    entry = packet.decisions[0]
    assert entry["state_snapshot"]["version"] == DECISION_SNAPSHOT_VERSION
    assert game_state_from_snapshot(entry["state_snapshot"]) == state


def test_build_handles_missing_state() -> None:
    assert build_decision_snapshot(None) is None
    assert build_decision_snapshot({}) is None
    raw = {"turn_info": {"turn_number": 3, "phase": "Phase_Main1"}}
    snapshot = build_decision_snapshot(raw)
    assert snapshot["state"]["turn"]["turn_number"] == 3  # turn_info normalized


def test_zone_aliases_are_rebuilt_not_duplicated() -> None:
    gs = {
        "turn": {"turn_number": 5},
        "players": [{"seat_id": 1, "life_total": 20}],
        "battlefield": [{"instance_id": 1, "name": "Island"}],
        "hand": [],
        "zones": {"library_count": 40, "opponent_hand_count": 7},
        "local_seat_id": 1,
        "opponent_seat_id": 2,
    }
    snapshot = build_decision_snapshot(gs)
    # The card list is stored once; zones keeps only the counts it had.
    assert "battlefield" not in snapshot["state"]["zones"]
    restored = game_state_from_snapshot(snapshot)
    assert restored == gs  # no aliases invented for a counts-only zones


def test_server_shaped_zones_are_realiased() -> None:
    """The server's zones alias the card lists; the restore reproduces that."""
    island = {"instance_id": 1, "name": "Island"}
    gs = {
        "turn": {"turn_number": 5},
        "players": [{"seat_id": 1, "life_total": 20}],
        "battlefield": [island],
        "hand": [],
        "zones": {
            "library_count": 40,
            "opponent_hand_count": 7,
            "battlefield": [island],
            "my_hand": [],
            "hand": [],
        },
        "local_seat_id": 1,
        "opponent_seat_id": 2,
    }
    snapshot = build_decision_snapshot(gs)
    assert "battlefield" not in snapshot["state"]["zones"]
    restored = game_state_from_snapshot(snapshot)
    assert restored == gs
    assert restored["zones"]["battlefield"] is restored["battlefield"]
