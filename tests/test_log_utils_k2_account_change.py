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
(server._handle_match_created -> log_utils.bind_match_account) and used when
the record is finally written, so a delayed/pre-reset/replayed record for an
earlier match keeps the account that actually played it. The integration
tests below drive the real login -> room -> record flow with no recorder
mocks.
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


def _room_event(match_id: str, local_user: str, opponent_name: str) -> dict:
    """Real room-event shape (cf. tests/test_snapshot_opponent_facts.py)."""
    return {
        "matchGameRoomStateChangedEvent": {
            "gameRoomInfo": {
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
                "stateType": "MatchGameRoomStateType_Playing",
            }
        }
    }


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
