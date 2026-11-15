import gzip
import json
import os
import uuid

import pytest
from tools import head_to_head_bundles as h2h

MATCH = str(uuid.UUID(int=7))


def bundle(install, seat, *, game=1, win=2, match=MATCH, completed=True):
    rows = [{"scope": "MatchScope_Game", "result": "ResultType_WinLoss", "winningTeamId": 1}] * (game - 1)
    rows += [
        {
            "scope": "MatchScope_Game",
            "result": "ResultType_Draw" if win is None else "ResultType_WinLoss",
            "winningTeamId": win,
        }
    ]
    room = {
        "gameRoomConfig": {
            "matchId": match,
            "reservedPlayers": [{"systemSeatId": i, "teamId": i} for i in (1, 2)],
        },
        "stateType": "MatchGameRoomStateType_MatchCompleted"
        if completed
        else "MatchGameRoomStateType_Playing",
    }
    if completed:
        room["finalMatchResult"] = {"matchId": match, "resultList": rows}
    native = {
        "gameInfo": {"gameNumber": game, "matchID": match, "stage": "GameStage_GameOver", "results": rows}
    }
    events = [
        {
            "kind": "MatchGameRoomStateChangedEvent",
            "json": json.dumps({"matchGameRoomStateChangedEvent": {"gameRoomInfo": room}}),
        },
        {
            "kind": "GreToClientEvent",
            "json": json.dumps(
                {
                    "greToClientEvent": {
                        "greToClientMessages": [{"systemSeatIds": [seat], "gameStateMessage": native}]
                    }
                }
            ),
        },
    ]
    return {
        "arena_match_id": match,
        "game_number": game,
        "install_id": install,
        "result": "draw" if win is None else ("win" if win == seat else "loss"),
        "player_log": {"truncated": 0, "events": events},
    }


def save(tmp_path, obj, file):
    path = tmp_path / file
    path.write_bytes(gzip.compress(json.dumps(obj).encode()))
    return path


def test_played_games_are_paired_independently_of_file_order_and_one_match_can_have_multiple_games(tmp_path):
    sides = [
        [
            save(tmp_path, bundle(inst, seat, game=i, win=winner, completed=i == 2), f"{inst}-{i}")
            for i, winner in [(1, 1), (2, None)]
        ]
        for inst, seat in [("sample-a", 1), ("sample-b", 2)]
    ]
    result = h2h.audit(sides[0], sides[1][::-1])
    assert result["paired_games"] == 2 and result["distinct_arena_matches"] == 1
    assert result["matches_with_both_native_completion_events"] == result["a_wins"] == result["draws"] == 1
    assert result["b_wins"] == 0 and "Direct invitation" in result["open"]
    assert MATCH not in json.dumps(result) and "sample-a" not in json.dumps(result)


@pytest.mark.parametrize(
    "fault",
    [
        "opposite_result",
        "wrong_game",
        "same_seat",
        "same_install",
        "truncated",
        "duplicate",
        "wrong_room",
        "wrong_gre",
        "conflicting_native",
        "conflicting_completion",
        "unknown_winner",
        "mismatched_snapshot",
        "missing_native",
    ],
)
def test_incomplete_or_crossed_evidence_never_counts(tmp_path, fault):
    first = save(tmp_path, bundle("sample-a", 1), "a")
    b = bundle("sample-b", 2)
    events = b["player_log"]["events"]
    if fault == "same_install":
        b["install_id"] = "sample-a"
    elif fault == "truncated":
        b["player_log"]["truncated"] = 1
    elif fault == "opposite_result":
        b["result"] = "loss"
    elif fault == "wrong_game":
        b["game_number"] = 2
    elif fault == "missing_native":
        events.pop()
    elif fault == "wrong_room":
        events[0]["json"] = events[0]["json"].replace(MATCH, str(uuid.UUID(int=9)))
    elif fault == "wrong_gre":
        events[1]["json"] = events[1]["json"].replace(MATCH, str(uuid.UUID(int=9)))
    elif fault == "mismatched_snapshot":
        b["final_state"] = {"local_seat_id": 1}
    elif fault == "same_seat":
        b = bundle("sample-b", 1)
    elif fault == "unknown_winner":
        b = bundle("sample-b", 2, win=3)
    elif fault == "conflicting_native":
        events.append(bundle("sample-b", 2, win=1)["player_log"]["events"][1])
    elif fault == "conflicting_completion":
        events[0] = bundle("sample-b", 2, win=1)["player_log"]["events"][0]
    second = save(tmp_path, b, "b")
    with pytest.raises(ValueError):
        h2h.audit([first], [second, second] if fault == "duplicate" else [second])


def test_offline_reader_rejects_symlinks_nonregular_oversized_inputs_without_disclosing_files(
    tmp_path, capsys, monkeypatch
):
    path = save(tmp_path, bundle("sample-a", 1), "private-file")
    link = tmp_path / "link"
    link.symlink_to(path)
    fifo = tmp_path / "fifo"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)
    monkeypatch.setattr(h2h, "_LIMIT", 100)
    for entry in [link, fifo, path]:
        with pytest.raises(SystemExit) as error:
            h2h.main(["--side-a", str(entry), "--side-b", str(path)])
        assert error.value.code == 2
        output = capsys.readouterr()
        assert output.out == "" and "private-file" not in output.err
