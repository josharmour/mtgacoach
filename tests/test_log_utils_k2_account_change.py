"""K2 — recognise the local player after an account change.

Real logged case (evidence copied from the Mac, READ ONLY):
  /home/joshu/fable-nov-evidence/arenamcp/standalone.log
    The coach logged "Found Local Player ID via Regex: T3MARZNUZRD4RDKHEA6GNFXM3M"
    continuously through 2026-11-10, because get_local_player_id() returned the
    FIRST authenticateResponse in Player.log — belonging to an account that had
    already been switched away from on 2026-11-09 ~23:15.
  /home/joshu/fable-nov-evidence/arenamcp/bug_reports/bug_20261109_231712.json
    (mtga_log excerpt) — after the switch, Player.log addresses match room
    events to the account actually playing:
      "[11/9/2026 11:10:33 PM] ... Match to FMRW2YNB5NFRPBE7I5I7CQ2RYI:
       MatchGameRoomStateChangedEvent"
    yet the coach kept attributing matches to T3MARZNUZRD4RDKHEA6GNFXM3M and
    recorded opponents as the account that was actually playing them.

These tests feed a Player.log with two logins (the old account first, the new
account second) and assert that matches after the second login are attributed
to the second account, with the opponent named correctly.

Account identity is bound to the match when its room event arrives
(server._handle_match_created -> log_utils.bind_match_account_from_room) and
used when the record is finally written, so a delayed/pre-reset/replayed
record for an earlier match keeps the account that actually played it. The
integration tests below drive the real login -> room -> record flow with no
recorder mocks.

Round 2 (Dotty review): the binding must survive late completed-room events
and replays of old room events. Evidence strength is the room's own
local-seat declaration > an established binding > the current login when the
room corroborates it; an unrelated current login is never bound.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from arenamcp import log_utils, server
from arenamcp.gamestate import GameState
from arenamcp.log_utils import get_local_player_id
from arenamcp.match_history import record_from_game_end
from arenamcp.server import select_opponent_name

# Real account ids from the evidence above.
OLD_ACCOUNT_ID = "T3MARZNUZRD4RDKHEA6GNFXM3M"
NEW_ACCOUNT_ID = "FMRW2YNB5NFRPBE7I5I7CQ2RYI"

LOGIN_BLOCK_TEMPLATE = """\
[{ts}] [UnityCrossThreadLogger][Accounts - Login] Logged in successfully
[{ts}] [UnityCrossThreadLogger]{ts}: {{"authenticateResponse":{{"clientId":"{client_id}","screenName":"{screen_name}"}}}}
"""


def _write_player_log(tmp_path, blocks: str) -> str:
    log_path = tmp_path / "Player.log"
    log_path.write_text(blocks, encoding="utf-8")
    return str(log_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Never let a developer's MTGA_PLAYER_ID or saved settings pin the answer.
    monkeypatch.delenv("MTGA_PLAYER_ID", raising=False)


@pytest.fixture(autouse=True)
def _clear_match_bindings():
    # Bindings are module-level state; isolate every test from the others.
    log_utils._MATCH_ACCOUNT_BINDINGS.clear()
    yield
    log_utils._MATCH_ACCOUNT_BINDINGS.clear()


def _use_player_log(monkeypatch, tmp_path, blocks: str) -> None:
    log_path = _write_player_log(tmp_path, blocks)
    monkeypatch.setattr("arenamcp.log_utils.PLAYER_LOG_PATH", log_path)


def test_two_logins_resolve_to_most_recent_account(monkeypatch, tmp_path):
    """Two logins in Player.log -> the SECOND login's account is local."""
    blocks = (
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/8/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        )
        + "[11/9/2026 9:14:02 PM] [UnityCrossThreadLogger]Match to "
        + OLD_ACCOUNT_ID
        + ": MatchGameRoomStateChangedEvent\n"
        + LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 11:15:07 PM]",
            client_id=NEW_ACCOUNT_ID,
            screen_name="NewAccount",
        )
        + "[11/9/2026 11:16:08 PM] [UnityCrossThreadLogger]Match to "
        + NEW_ACCOUNT_ID
        + ": MatchGameRoomStateChangedEvent\n"
    )
    _use_player_log(monkeypatch, tmp_path, blocks)

    assert get_local_player_id() == NEW_ACCOUNT_ID


def test_single_login_still_resolves(monkeypatch, tmp_path):
    """Regression: a log with one login still resolves that account."""
    blocks = LOGIN_BLOCK_TEMPLATE.format(
        ts="[11/8/2026 8:02:11 PM]",
        client_id=OLD_ACCOUNT_ID,
        screen_name="OldAccount",
    )
    _use_player_log(monkeypatch, tmp_path, blocks)

    assert get_local_player_id() == OLD_ACCOUNT_ID


def test_no_login_marker_falls_back_to_last_auth_response(monkeypatch, tmp_path):
    """Older logs without the login marker resolve to the LAST auth response."""
    blocks = (
        f'[11/8/2026 8:02:12 PM] [UnityCrossThreadLogger]{{"authenticateResponse":{{"clientId":"{OLD_ACCOUNT_ID}"}}}}\n'
        f'[11/9/2026 11:15:08 PM] [UnityCrossThreadLogger]{{"authenticateResponse":{{"clientId":"{NEW_ACCOUNT_ID}"}}}}\n'
    )
    _use_player_log(monkeypatch, tmp_path, blocks)

    assert get_local_player_id() == NEW_ACCOUNT_ID


def test_opponent_named_correctly_after_account_change():
    """Room participants after the switch -> opponent is the OTHER player.

    Real room shape from bug_20261109_231712.json / bug_20261110_070459.json:
    matches after the switch were addressed to FMRW2YNB5NFRPBE7I5I7CQ2RYI,
    while the coach still believed T3MARZNUZRD4RDKHEA6GNFXM3M was local and
    recorded the local player's own name as the opponent.
    """
    participants = [
        {
            "systemSeatId": 1,
            "userId": NEW_ACCOUNT_ID,
            "playerName": "NewAccount",
        },
        {
            "systemSeatId": 2,
            "userId": "OPPONENTUSERID000000000000",
            "playerName": "ActualOpponent",
        },
    ]

    # Correct local id (second login) -> opponent named correctly.
    assert select_opponent_name(participants, None, NEW_ACCOUNT_ID) == "ActualOpponent"

    # Stale local id from before the switch matches nobody in this room;
    # it must NOT name the local player's own account as the opponent.
    assert select_opponent_name(participants, None, OLD_ACCOUNT_ID) is None

    # Seat-based exclusion still wins when the event carries a seat.
    assert select_opponent_name(participants, 1, OLD_ACCOUNT_ID) == "ActualOpponent"


def test_match_record_stamps_logged_in_account(monkeypatch, tmp_path):
    """Each match record carries the account logged in when it ended."""
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: NEW_ACCOUNT_ID)
    snapshot = {
        "opponent_name": "ActualOpponent",
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 0, "name": "ActualOpponent"},
        ],
    }

    rec = record_from_game_end(
        match_id="match-after-switch",
        result="win",
        game_state_snapshot=snapshot,
    )

    assert rec.local_account_id == NEW_ACCOUNT_ID


# --- binding: identity travels with the match, not with the log tail ------------


def _room_event(
    match_id: str,
    local_user: str,
    opponent_name: str,
    *,
    declared_local_seat: int | None = None,
    state_type: str = "MatchGameRoomStateType_Playing",
) -> dict:
    """Real room-event shape (cf. tests/test_snapshot_opponent_facts.py).

    declared_local_seat mirrors events that pin the local seat at the top
    level of matchGameRoomStateChangedEvent (Dotty round-2 repro); real
    MatchGameRoomStateChangedEvent payloads frequently omit it
    (tests/fixtures/player_log_idle_after_match_20261019.log).
    """
    info: dict = {
        "gameRoomConfig": {
            "matchId": match_id,
            "reservedPlayers": [
                {
                    "systemSeatId": 1,
                    "userId": local_user,
                    "playerName": "LocalPlayer",
                },
                {
                    "systemSeatId": 2,
                    "userId": "OPPONENTUSERID000000000000",
                    "playerName": opponent_name,
                },
            ],
        },
        "stateType": state_type,
    }
    if state_type == "MatchGameRoomStateType_MatchCompleted":
        # Real completed-room shape (same fixture, requestId 47).
        info["finalMatchResult"] = {
            "matchId": match_id,
            "matchCompletedReason": "MatchCompletedReasonType_Success",
        }
    event: dict = {"matchGameRoomStateChangedEvent": {"gameRoomInfo": info}}
    if declared_local_seat is not None:
        event["matchGameRoomStateChangedEvent"]["systemSeatId"] = declared_local_seat
    return event


def _start_server_flow(monkeypatch, log_path: str) -> GameState:
    """Wire the real handler to a fresh GameState (only plumbing mocked)."""
    state = GameState()
    monkeypatch.setattr(server, "game_state", state)
    monkeypatch.setattr(server, "_deactivate_draft_state", Mock())
    monkeypatch.setattr(server, "mark_match_ended", Mock())
    monkeypatch.setattr("arenamcp.log_utils.PLAYER_LOG_PATH", log_path)
    return state


def test_login_room_record_flow_binds_account_to_the_match(monkeypatch, tmp_path):
    """Full login -> room -> record flow with NO recorder mocks.

    Reviewer's handler-level case: match A starts under account A, the user
    switches to account B and starts match B, and only afterwards is A's
    preserved snapshot recorded — A must still be stamped with A, even though
    the most recent login in Player.log is now B.
    """
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    state = _start_server_flow(monkeypatch, str(log_path))

    # Match A starts while the OLD account is logged in.
    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    assert state.opponent_name == "ActualOpponent"

    # Account switch: the second login lands in Player.log, then match B starts.
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            LOGIN_BLOCK_TEMPLATE.format(
                ts="[11/9/2026 11:15:07 PM]",
                client_id=NEW_ACCOUNT_ID,
                screen_name="NewAccount",
            )
        )
    server._handle_match_created(_room_event("match-B", NEW_ACCOUNT_ID, "SecondOpponent"))
    assert state.opponent_name == "SecondOpponent"

    # Delayed/pre-reset record: A's preserved snapshot is recorded after the
    # switch and after B's room event reset the game state.
    state.reset()
    rec_a = record_from_game_end("match-A", "win", {"opponent_name": "ActualOpponent"})
    rec_b = record_from_game_end("match-B", "loss", {"opponent_name": "SecondOpponent"})

    assert rec_a.local_account_id == OLD_ACCOUNT_ID
    assert rec_a.opponent_name == "ActualOpponent"
    assert rec_b.local_account_id == NEW_ACCOUNT_ID
    assert rec_b.opponent_name == "SecondOpponent"


def test_replayed_record_keeps_its_own_account_across_further_switches(monkeypatch, tmp_path):
    """Recording a preserved snapshot again never re-stamps it; unbound
    matches still fall back to the most recent login."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    state = _start_server_flow(monkeypatch, str(log_path))
    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))

    # Two further switches after match A ended.
    with log_path.open("a", encoding="utf-8") as f:
        f.write(
            LOGIN_BLOCK_TEMPLATE.format(
                ts="[11/9/2026 11:15:07 PM]",
                client_id=NEW_ACCOUNT_ID,
                screen_name="NewAccount",
            )
        )
        f.write(
            LOGIN_BLOCK_TEMPLATE.format(
                ts="[11/10/2026 9:00:00 AM]",
                client_id="THIRDACCOUNTID0000000000000",
                screen_name="ThirdAccount",
            )
        )

    # Replay of A's preserved snapshot: still the account that played it.
    assert record_from_game_end("match-A", "win", {}).local_account_id == OLD_ACCOUNT_ID
    assert record_from_game_end("match-A", "win", {}).local_account_id == OLD_ACCOUNT_ID

    # A match whose room event was never seen has no binding: it falls back
    # to the freshest login (third account).
    fallback = record_from_game_end("match-never-seen", "loss", {})
    assert fallback.local_account_id == "THIRDACCOUNTID0000000000000"


# --- round 2: late completed-room events and replays must not rebind -----------


def _switch_login(log_path, ts: str, client_id: str, screen_name: str) -> None:
    with log_path.open("a", encoding="utf-8") as f:
        f.write(LOGIN_BLOCK_TEMPLATE.format(ts=ts, client_id=client_id, screen_name=screen_name))


def test_late_completed_room_event_does_not_rebind_to_current_login(monkeypatch, tmp_path):
    """Dotty round-2 case: A starts under A; the user switches to B and starts
    B; then a late COMPLETED-room event for A arrives carrying A's own
    explicit local seat while B is logged in. The event's own evidence agrees
    with the established binding, so A stays bound to A and A's record stamps
    A — never the unrelated current login.

    Real completed-room shape: tests/fixtures/player_log_idle_after_match_20261019.log
    (requestId 47, MatchGameRoomStateType_MatchCompleted).
    """
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    # Match A starts while the OLD account is logged in; the event pins the
    # local seat explicitly.
    server._handle_match_created(
        _room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent", declared_local_seat=1)
    )
    assert log_utils.account_bound_to_match("match-A") == OLD_ACCOUNT_ID

    # Switch to B; match B starts.
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")
    server._handle_match_created(_room_event("match-B", NEW_ACCOUNT_ID, "SecondOpponent"))

    # Late completed-room event for A: explicitly carries local seat 1 =
    # participant OLD, while the freshest login is NEW.
    server._handle_match_created(
        _room_event(
            "match-A",
            OLD_ACCOUNT_ID,
            "ActualOpponent",
            declared_local_seat=1,
            state_type="MatchGameRoomStateType_MatchCompleted",
        )
    )

    assert log_utils.account_bound_to_match("match-A") == OLD_ACCOUNT_ID
    rec_a = record_from_game_end(
        "match-A",
        "win",
        {"opponent_name": "ActualOpponent", "local_account_id": OLD_ACCOUNT_ID},
    )
    assert rec_a.local_account_id == OLD_ACCOUNT_ID

    # Even with the binding gone (restart/eviction), a snapshot that
    # explicitly declares its account outranks the freshest login.
    log_utils._MATCH_ACCOUNT_BINDINGS.clear()
    rec_again = record_from_game_end("match-A", "win", {"local_account_id": OLD_ACCOUNT_ID})
    assert rec_again.local_account_id == OLD_ACCOUNT_ID


def test_late_completed_room_without_identity_preserves_binding(monkeypatch, tmp_path):
    """A completed-room replay carrying no seat and no user evidence at all
    must not overwrite an established binding with whoever logged in later."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    assert log_utils.account_bound_to_match("match-A") == OLD_ACCOUNT_ID

    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")
    server._handle_match_created(_room_event("match-B", NEW_ACCOUNT_ID, "SecondOpponent"))

    # Bare completed-room event for A: no reservedPlayers, no declared seat —
    # nothing in it identifies a player, so the binding must simply survive.
    server._handle_match_created(
        {
            "matchGameRoomStateChangedEvent": {
                "gameRoomInfo": {
                    "gameRoomConfig": {"matchId": "match-A"},
                    "stateType": "MatchGameRoomStateType_MatchCompleted",
                }
            }
        }
    )

    assert log_utils.account_bound_to_match("match-A") == OLD_ACCOUNT_ID
    rec_a = record_from_game_end("match-A", "win", {"opponent_name": "ActualOpponent"})
    assert rec_a.local_account_id == OLD_ACCOUNT_ID


def test_replayed_room_binds_the_seat_declared_account_not_current_login(monkeypatch, tmp_path):
    """Dotty round-2 case: a fresh replay of a historical room event whose
    explicit local seat names an account that is NOT logged in binds the
    seat's account — never the unrelated current login."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/10/2026 9:00:00 AM]",
            client_id=NEW_ACCOUNT_ID,
            screen_name="NewAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(
        _room_event("historical-A", OLD_ACCOUNT_ID, "ActualOpponent", declared_local_seat=1)
    )

    assert log_utils.account_bound_to_match("historical-A") == OLD_ACCOUNT_ID
    rec = record_from_game_end("historical-A", "win", {"opponent_name": "ActualOpponent"})
    assert rec.local_account_id == OLD_ACCOUNT_ID


def test_unrelated_current_login_is_never_bound_without_room_evidence(monkeypatch, tmp_path):
    """A room whose player list does not include the current login carries no
    evidence for it: nothing is bound rather than binding an unrelated
    account (the record then falls back to the freshest login, as before)."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/10/2026 9:00:00 AM]",
            client_id=NEW_ACCOUNT_ID,
            screen_name="NewAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    # No declared seat; the room's players are OLD + opponent; NEW is logged
    # in but played no part in this room.
    server._handle_match_created(_room_event("old-match", OLD_ACCOUNT_ID, "ActualOpponent"))

    assert log_utils.account_bound_to_match("old-match") is None


# --- round 3: Bo3 game records (<match>-g2) must resolve the match's binding ----


def _bo3_game_id(match_id: str, game_number: int) -> str:
    """The exact id standalone_postmatch records game n under."""
    from arenamcp.match_bundle import game_bundle_id

    return game_bundle_id(match_id, game_number)


def test_bo3_game2_and_game3_records_keep_the_match_account_after_switch(monkeypatch, tmp_path):
    """Audit case: A's Bo3 continues after the user switches to B.

    Match A starts under account A; the user switches to account B and starts
    match B; only then are A's game 2 and game 3 recorded — under their
    game_bundle_id ids ("<match>-g2"/"<match>-g3", exactly as
    standalone_postmatch._take_game_end_signal passes them) with snapshots
    that name the bare match. Both must stamp A, not the current login B.
    """
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")
    server._handle_match_created(_room_event("match-B", NEW_ACCOUNT_ID, "SecondOpponent"))

    snapshot = {"match_id": "match-A", "opponent_name": "ActualOpponent"}
    rec_g2 = record_from_game_end(_bo3_game_id("match-A", 2), "win", dict(snapshot))
    rec_g3 = record_from_game_end(_bo3_game_id("match-A", 3), "loss", dict(snapshot))

    assert rec_g2.local_account_id == OLD_ACCOUNT_ID
    assert rec_g3.local_account_id == OLD_ACCOUNT_ID
    # The game-specific history id is preserved on the record.
    assert rec_g2.match_id == _bo3_game_id("match-A", 2)
    assert rec_g3.match_id == _bo3_game_id("match-A", 3)


def test_bare_game1_record_with_matching_snapshot_resolves_the_binding(monkeypatch, tmp_path):
    """Game 1 keeps the bare match id; a snapshot naming that same match
    resolves the binding directly (exact correspondence)."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")

    rec = record_from_game_end("match-A", "win", {"match_id": "match-A", "opponent_name": "ActualOpponent"})
    assert rec.local_account_id == OLD_ACCOUNT_ID


def test_snapshot_declared_account_outranks_login_when_no_binding_exists(monkeypatch, tmp_path):
    """Explicit captured account precedence survives for game records: with
    no binding at all (restart/eviction), a -g2 snapshot that explicitly
    declares its local_account_id outranks the freshest login."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/10/2026 9:00:00 AM]",
            client_id=NEW_ACCOUNT_ID,
            screen_name="NewAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    rec = record_from_game_end(
        _bo3_game_id("match-A", 2),
        "win",
        {"match_id": "match-A", "local_account_id": OLD_ACCOUNT_ID},
    )
    assert rec.local_account_id == OLD_ACCOUNT_ID


def test_mismatched_snapshot_match_id_does_not_adopt_unrelated_binding(monkeypatch, tmp_path):
    """Fail closed: a -g2 record whose snapshot names a DIFFERENT match must
    not adopt that match's binding — it falls back to the snapshot's own
    declared account, or to the freshest login when there is none."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    # match-A is bound to OLD; match-Z is bound to NEW (its own room event).
    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")
    server._handle_match_created(_room_event("match-Z", NEW_ACCOUNT_ID, "ZOpponent"))
    # A third account logs in afterwards; it played no part in either match.
    _switch_login(log_path, "[11/10/2026 9:00:00 AM]", "THIRDACCOUNTID0000000000000", "Third")

    # Snapshot names match-Z (bound to NEW) but the record is A's game 2:
    # Z's binding is unrelated and must not be adopted.
    rec_declared = record_from_game_end(
        _bo3_game_id("match-A", 2),
        "win",
        {"match_id": "match-Z", "local_account_id": OLD_ACCOUNT_ID},
    )
    assert rec_declared.local_account_id == OLD_ACCOUNT_ID

    rec_login = record_from_game_end(_bo3_game_id("match-A", 2), "win", {"match_id": "match-Z"})
    assert rec_login.local_account_id == "THIRDACCOUNTID0000000000000"


def test_arbitrary_suffix_is_not_stripped_to_find_a_binding(monkeypatch, tmp_path):
    """Only the exact id or the legitimate -g<n> game form resolves through
    the snapshot's match identity; an arbitrary suffix ("-final", "-g2x")
    must not be stripped to reach the binding."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")

    for odd_record_id in ("match-A-final", "match-A-g2x", "match-A-g"):
        rec = record_from_game_end(
            odd_record_id, "win", {"match_id": "match-A", "opponent_name": "ActualOpponent"}
        )
        assert rec.local_account_id == NEW_ACCOUNT_ID, odd_record_id


def test_multi_digit_game_numbers_resolve_the_binding(monkeypatch, tmp_path):
    """Numeric boundary: game_bundle_id's suffix is a generic integer, so a
    Bo3/BoN series that reaches game 10+ records under "<match>-g10" etc.
    Those multi-digit game forms must resolve the base binding exactly like
    -g2/-g3 do (delayed record after the switch to B)."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")
    server._handle_match_created(_room_event("match-B", NEW_ACCOUNT_ID, "SecondOpponent"))

    snapshot = {"match_id": "match-A", "opponent_name": "ActualOpponent"}
    for game_number in (9, 10, 11, 19, 123):
        record_id = _bo3_game_id("match-A", game_number)
        rec = record_from_game_end(record_id, "win", dict(snapshot))
        assert rec.local_account_id == OLD_ACCOUNT_ID, record_id
        # The game-specific history id is preserved on the record.
        assert rec.match_id == record_id


def test_non_canonical_game_suffixes_fail_closed(monkeypatch, tmp_path):
    """Malformed near-misses of the -g<n> form must not be adopted as game
    records: a trailing newline ($-vs-\\Z trap), a leading zero, a sign, a
    space, and game 1 (which game_bundle_id keeps bare) all fall through to
    the current login instead of the base binding."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")

    for bad_record_id in (
        "match-A-g2\n",  # trailing newline: full-string validation rejects it
        "match-A-g02",  # leading zero: not the producer's canonical form
        "match-A-g+2",  # sign: never produced
        "match-A-g2 ",  # trailing space
        "match-A-g1",  # game 1 keeps the bare match id
        "match-A-g2\nmatch-A-g3",  # embedded newline
    ):
        rec = record_from_game_end(
            bad_record_id, "win", {"match_id": "match-A", "opponent_name": "ActualOpponent"}
        )
        assert rec.local_account_id == NEW_ACCOUNT_ID, repr(bad_record_id)


def test_unicode_decimal_digits_are_not_canonical_game_suffixes(monkeypatch, tmp_path):
    """Python's \\d matches Unicode decimal digits, but game_bundle_id builds
    its suffix with str(int), which only ever emits ASCII digits. A record id
    whose suffix contains a Unicode digit (U+0662 ARABIC-INDIC DIGIT TWO,
    U+FF10 FULLWIDTH DIGIT ZERO) is therefore not a canonical -g<n> form and
    must fail closed to the current login instead of adopting the base
    binding."""
    log_path = tmp_path / "Player.log"
    log_path.write_text(
        LOGIN_BLOCK_TEMPLATE.format(
            ts="[11/9/2026 8:02:11 PM]",
            client_id=OLD_ACCOUNT_ID,
            screen_name="OldAccount",
        ),
        encoding="utf-8",
    )
    _start_server_flow(monkeypatch, str(log_path))

    server._handle_match_created(_room_event("match-A", OLD_ACCOUNT_ID, "ActualOpponent"))
    _switch_login(log_path, "[11/9/2026 11:15:07 PM]", NEW_ACCOUNT_ID, "NewAccount")

    for unicode_record_id in (
        "match-A-g2" + chr(0x0662),  # Arabic-Indic digit two after the ASCII 2
        "match-A-g1" + chr(0xFF10),  # fullwidth digit zero after the ASCII 1
        "match-A-g" + chr(0x0662),  # lone Unicode digit as the whole suffix
    ):
        rec = record_from_game_end(
            unicode_record_id, "win", {"match_id": "match-A", "opponent_name": "ActualOpponent"}
        )
        assert rec.local_account_id == NEW_ACCOUNT_ID, repr(unicode_record_id)
