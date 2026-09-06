"""Regression tests for per-replay local-seat detection in replay eval.

Background
----------
``tools/eval/replay/state.py`` used to declare
``walk_states(messages, local_seat_id: int = 2)`` and every call site in
``replay/run.py`` relied on that default. But the recording seat is a
property of the individual replay, carried by ``GREMessageType_ConnectResp``:
across the 104-file ``mtgacoach_*.rply`` corpus it is **seat 1 for 55 files
and seat 2 for 49**. The hardcoded 2 therefore scored the OPPONENT's hand
and board on 53% of the corpus, silently.

These tests pin the three properties that prevent a repeat:
  1. the seat is read from ConnectResp,
  2. an undeterminable seat raises instead of defaulting,
  3. hand/battlefield/life read the detected seat, not seat 2.

The real-corpus test runs only when a ``.rply`` directory is reachable
(``MTGACOACH_RPLY_DIR``); otherwise synthetic fixtures carry the load.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from tools.eval.replay.reader import ReplayMessage, parse_replay_path
from tools.eval.replay.state import (
    LocalSeatUndetermined,
    detect_local_seat,
    opponent_seat,
    resolve_local_seat,
    snapshot_at_decision,
    walk_states,
)

# ---------------------------------------------------------------------------
# Synthetic fixtures — shaped after real lines from mtgacoach_Replay*.rply
# ---------------------------------------------------------------------------


def _msg(direction: str, payload: dict, ts: int = 0) -> ReplayMessage:
    return ReplayMessage(direction=direction, timestamp_us=ts, payload=payload)


def _connect_resp(seat: int) -> ReplayMessage:
    """A ConnectResp addressed to exactly one seat (the recording client)."""
    return _msg(
        "IN",
        {
            "type": "GREMessageType_ConnectResp",
            "systemSeatIds": [seat],
            "msgId": 1,
            "connectResp": {"status": "ConnectionStatus_Success"},
        },
        ts=15,
    )


def _actions_available(seat: int, msg_id: int) -> ReplayMessage:
    return _msg(
        "IN",
        {
            "type": "GREMessageType_ActionsAvailableReq",
            "systemSeatIds": [seat],
            "msgId": msg_id,
            "actionsAvailableReq": {"actions": [{"actionType": "ActionType_Pass"}]},
        },
        ts=100 + msg_id,
    )


def _game_state(*, hands: dict[int, list[int]], life: dict[int, int]) -> ReplayMessage:
    """A Full game state giving each seat a hand zone with the listed grpIds.

    Instance ids are ``seat * 100 + i`` so the seats are trivially
    distinguishable in assertions.
    """
    zones, objs, players = [], [], []
    for seat, grp_ids in hands.items():
        zone_id = 30 + seat
        zones.append({"zoneId": zone_id, "type": "ZoneType_Hand", "ownerSeatId": seat})
        zones.append({"zoneId": 40 + seat, "type": "ZoneType_Battlefield", "ownerSeatId": seat})
        for i, grp in enumerate(grp_ids):
            objs.append(
                {
                    "instanceId": seat * 100 + i,
                    "grpId": grp,
                    "zoneId": zone_id,
                    "ownerSeatId": seat,
                    "controllerSeatId": seat,
                }
            )
        players.append({"systemSeatNumber": seat, "lifeTotal": life[seat]})
    return _msg(
        "IN",
        {
            "type": "GREMessageType_GameStateMessage",
            "msgId": 2,
            "gameStateMessage": {
                "type": "GameStateType_Full",
                "turnInfo": {"turnNumber": 1, "phase": "Phase_Main1", "activePlayer": 1, "priorityPlayer": 1},
                "players": players,
                "zones": zones,
                "gameObjects": objs,
            },
        },
        ts=50,
    )


def _replay(seat: int) -> list[ReplayMessage]:
    """A minimal but structurally faithful replay recorded from ``seat``."""
    return [
        _connect_resp(seat),
        _game_state(hands={1: [1001, 1002], 2: [2001, 2002, 2003]}, life={1: 20, 2: 20}),
        _actions_available(seat, msg_id=10),
    ]


# ---------------------------------------------------------------------------
# detect_local_seat
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seat", [1, 2])
def test_detect_local_seat_reads_connect_resp(seat):
    assert detect_local_seat(_replay(seat)) == seat


def test_detect_local_seat_falls_back_to_modal_request_seat():
    """No ConnectResp: the seat that requests are addressed to wins."""
    messages = [
        _game_state(hands={1: [1001], 2: [2001]}, life={1: 20, 2: 20}),
        _actions_available(1, msg_id=10),
        _actions_available(1, msg_id=11),
        # A two-seat request is ambiguous and must not sway the vote.
        _msg("IN", {"type": "GREMessageType_ActionsAvailableReq", "systemSeatIds": [1, 2], "msgId": 12}),
    ]
    assert detect_local_seat(messages) == 1


def test_detect_local_seat_returns_none_when_undeterminable():
    messages = [_game_state(hands={1: [1001], 2: [2001]}, life={1: 20, 2: 20})]
    assert detect_local_seat(messages) is None


def test_multi_seat_connect_resp_does_not_pick_a_seat():
    """A ConnectResp naming both seats identifies nobody; fall through."""
    messages = [
        _msg("IN", {"type": "GREMessageType_ConnectResp", "systemSeatIds": [1, 2], "msgId": 1}),
        _game_state(hands={1: [1001], 2: [2001]}, life={1: 20, 2: 20}),
    ]
    assert detect_local_seat(messages) is None


# ---------------------------------------------------------------------------
# resolve_local_seat — the "fail loudly" contract
# ---------------------------------------------------------------------------


def test_resolve_local_seat_raises_instead_of_defaulting_to_two():
    """The whole point of the bug fix: no silent fallback to seat 2."""
    messages = [_game_state(hands={1: [1001], 2: [2001]}, life={1: 20, 2: 20})]
    with pytest.raises(LocalSeatUndetermined):
        resolve_local_seat(messages)


def test_resolve_local_seat_honours_explicit_override():
    # Replay records as seat 1, but an explicit caller wins.
    assert resolve_local_seat(_replay(1), 2) == 2
    assert resolve_local_seat(_replay(1)) == 1


def test_opponent_seat():
    assert opponent_seat(1) == 2
    assert opponent_seat(2) == 1


# ---------------------------------------------------------------------------
# The seat actually reaches the snapshot (this is what got corrupted)
# ---------------------------------------------------------------------------


def test_snapshot_at_decision_detects_seat_one_replay():
    """A seat-1 replay must expose seat 1's 2-card hand, not seat 2's 3-card hand."""
    messages = _replay(1)
    request = messages[-1]
    snap = snapshot_at_decision(messages, request)
    assert snap.local_seat_id == 1
    assert snap.opponent_seat_id == 2
    assert sorted(c["grpId"] for c in snap.hand(snap.local_seat_id)) == [1001, 1002]
    assert snap.hand_size(snap.local_seat_id) == 2
    # The pre-fix behaviour, for contrast: forcing seat 2 reads the opponent.
    wrong = snapshot_at_decision(messages, request, local_seat_id=2)
    assert wrong.hand_size(2) == 3
    assert sorted(c["grpId"] for c in wrong.hand(2)) == [2001, 2002, 2003]


def test_snapshot_at_decision_detects_seat_two_replay():
    messages = _replay(2)
    snap = snapshot_at_decision(messages, messages[-1])
    assert snap.local_seat_id == 2
    assert snap.hand_size(2) == 3


def test_walk_states_uses_detected_seat_not_hardcoded_two():
    messages = _replay(1)
    seats = {snap.local_seat_id for _, snap in walk_states(messages)}
    assert seats == {1}


def test_walk_states_raises_when_seat_undeterminable():
    messages = [_game_state(hands={1: [1001], 2: [2001]}, life={1: 20, 2: 20})]
    with pytest.raises(LocalSeatUndetermined):
        list(walk_states(messages))


def test_life_totals_follow_the_detected_seat():
    """Asymmetric life makes a swapped seat unmistakable."""
    messages = [
        _connect_resp(1),
        _game_state(hands={1: [1001], 2: [2001]}, life={1: 7, 2: 19}),
        _actions_available(1, msg_id=10),
    ]
    snap = snapshot_at_decision(messages, messages[-1])
    assert snap.local_seat_id == 1
    assert snap.life(snap.local_seat_id) == 7
    assert snap.life(snap.opponent_seat_id) == 19


# ---------------------------------------------------------------------------
# Anti-drift: menu_groundtruth carries a second copy of this algorithm
# ---------------------------------------------------------------------------


def test_menu_groundtruth_detection_agrees_with_state_detection():
    """``menu_groundtruth.detect_local_seat`` must not diverge from ours.

    The two implementations should ultimately be one function (that module
    already imports from ``state``); until they are merged, pin them to
    identical behaviour so a fix to one is never silently missing from the
    other.
    """
    mg = pytest.importorskip("tools.eval.replay.menu_groundtruth")
    cases = [
        _replay(1),
        _replay(2),
        [_game_state(hands={1: [1]}, life={1: 20, 2: 20})],  # undeterminable
        [_actions_available(2, 5), _actions_available(2, 6), _actions_available(1, 7)],  # modal fallback
        [],  # empty
    ]
    for messages in cases:
        assert mg.detect_local_seat(messages) == detect_local_seat(messages)


# ---------------------------------------------------------------------------
# Real corpus (optional): 104 mtgacoach_*.rply files
# ---------------------------------------------------------------------------

_LINE_RE = re.compile(r"^(IN|OUT)-(\d+):(.*)$", re.DOTALL)


def _rply_dir() -> Path | None:
    env = os.environ.get("MTGACOACH_RPLY_DIR")
    candidates = [Path(env)] if env else []
    candidates.append(
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
        "/steamapps/common/MTGA/MTGA_Data/StreamingAssets/Tests"
    )
    for c in candidates:
        if c.is_dir() and any(c.glob("*.rply")):
            return c
    return None


def _connect_seat_streaming(path: Path) -> int | None:
    """Read only as far as the ConnectResp — corpus files run to ~5 MB each."""
    with open(path, encoding="utf-8") as f:
        f.readline()  # #Version2
        f.readline()  # metadata blob
        for line in f:
            if "ConnectResp" not in line:
                continue
            m = _LINE_RE.match(line.rstrip("\r\n"))
            if not m:
                continue
            try:
                payload = json.loads(m.group(3))
            except json.JSONDecodeError:
                continue
            if payload.get("type") != "GREMessageType_ConnectResp":
                continue
            ids = payload.get("systemSeatIds") or []
            return int(ids[0]) if len(ids) == 1 else None
    return None


def test_real_corpus_is_seat_mixed():
    """Guards the premise: the corpus is NOT all seat 2.

    Measured 2026-07-25 over the 104-file corpus: 55 seat 1 / 49 seat 2.
    The assertion is deliberately loose (both seats present, every file
    resolvable) so it survives a corpus refresh.
    """
    d = _rply_dir()
    if d is None:
        pytest.skip("no .rply corpus reachable (set MTGACOACH_RPLY_DIR)")
    seats = {}
    for p in sorted(d.glob("*.rply")):
        seats[p.name] = _connect_seat_streaming(p)
    assert seats, "corpus directory contained no readable replays"
    unresolved = [n for n, s in seats.items() if s is None]
    assert not unresolved, f"{len(unresolved)} replay(s) without a single-seat ConnectResp"
    counts = {1: sum(1 for s in seats.values() if s == 1), 2: sum(1 for s in seats.values() if s == 2)}
    assert counts[1] > 0 and counts[2] > 0, (
        f"corpus is seat-uniform ({counts}); a hardcoded seat would be harmless, "
        "which contradicts the bug this module guards"
    )


def test_real_replay_full_parse_matches_streaming_detection():
    """End-to-end on one real file: parse_replay_path -> detect_local_seat."""
    d = _rply_dir()
    if d is None:
        pytest.skip("no .rply corpus reachable (set MTGACOACH_RPLY_DIR)")
    # Prefer a seat-1 file: those are the ones the old default got wrong.
    chosen = None
    for p in sorted(d.glob("*.rply")):
        if _connect_seat_streaming(p) == 1:
            chosen = p
            break
    if chosen is None:
        pytest.skip("no seat-1 replay in the reachable corpus")
    _meta, messages = parse_replay_path(chosen)
    assert detect_local_seat(messages) == 1
    assert resolve_local_seat(messages) == 1
