"""Compare two machines' saved Coach bundles against their own native room and game results."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import stat
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path

_LIMIT = 32 * 1024 * 1024


def require(ok):
    if not ok:
        raise ValueError("inconsistent or incomplete paired Arena evidence")


def read_bundle(path):
    # Open before fstat; a named pipe or swapped symlink must never block the CLI.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= _LIMIT)
        with gzip.GzipFile(fileobj=stream) as archive:
            payload = archive.read(_LIMIT + 1)
        require(len(payload) <= _LIMIT)
        return json.loads(payload)


@dataclass(frozen=True)
class Receipt:
    install: str
    match: str
    game: int
    seat: int
    teams: tuple
    winner: int | None
    room_completed: bool


def _winner(result):
    if result["result"] == "ResultType_Draw":
        require(result.get("winningTeamId") in (None, 0) and type(result.get("winningTeamId")) is not bool)
        return None
    require(result["result"] == "ResultType_WinLoss" and type(result.get("winningTeamId")) is int)
    return result["winningTeamId"]


def native_receipt(bundle):
    """A receipt binds a per-game winner to both a server room and a seat-specific GRE message."""
    match = bundle["arena_match_id"]
    game = bundle["game_number"]
    install = bundle["install_id"]
    require(type(match) is str and str(uuid.UUID(match)) == match and uuid.UUID(match).int)
    require(type(game) is int and 1 <= game <= 3)
    require(type(install) is str and install.strip() == install and 0 < len(install) <= 128)
    log = bundle["player_log"]
    require(type(log["truncated"]) is int and log["truncated"] == 0 and type(log["events"]) is list)
    teams = set()
    outcomes = set()
    room_winners = set()
    for event in log["events"]:
        kind = event["kind"]
        if kind not in ("MatchGameRoomStateChangedEvent", "GreToClientEvent"):
            continue
        payload = json.loads(event["json"])
        if kind == "MatchGameRoomStateChangedEvent":
            room = payload["matchGameRoomStateChangedEvent"]["gameRoomInfo"]
            config = room["gameRoomConfig"]
            if config["matchId"] != match:
                continue
            slots = config["reservedPlayers"]
            require(type(slots) is list and len(slots) == 2)
            require(all(type(slot.get(k)) is int for slot in slots for k in ("systemSeatId", "teamId")))
            members = tuple(sorted((slot["systemSeatId"], slot["teamId"]) for slot in slots))
            require(tuple(s for s, _ in members) == (1, 2) and len({t for _, t in members}) == 2)
            require(all(team > 0 for _, team in members))
            teams.add(members)
            if room.get("stateType") == "MatchGameRoomStateType_MatchCompleted":
                final = room.get("finalMatchResult") or {}
                require(final.get("matchId") == match)
                games = [r for r in final["resultList"] if r.get("scope") == "MatchScope_Game"]
                require(len(games) >= game)
                room_winners.add(_winner(games[game - 1]))
            continue
        for message in payload["greToClientEvent"]["greToClientMessages"]:
            info = (message.get("gameStateMessage") or {}).get("gameInfo") or {}
            if (
                info.get("matchID") != match
                or info.get("gameNumber") != game
                or info.get("stage") != "GameStage_GameOver"
            ):
                continue
            require(type(info["gameNumber"]) is int and not message.get("_summarized"))
            seats = message["systemSeatIds"]
            require(type(seats) is list and len(seats) == 1 and type(seats[0]) is int and seats[0] in (1, 2))
            rows = [r for r in info["results"] if r.get("scope") == "MatchScope_Game"]
            require(len(rows) == game)
            outcomes.add((seats[0], _winner(rows[game - 1])))
    require(len(teams) == 1 and len(outcomes) == 1)
    mapping = teams.pop()
    seat, winner = outcomes.pop()
    require((winner is None or winner in dict(mapping).values()) and room_winners <= {winner})
    expected = "draw" if winner is None else ("win" if dict(mapping)[seat] == winner else "loss")
    require(bundle["result"] == expected)
    final_state = bundle.get("final_state") or {}
    for key, value in (
        ("match_id", match),
        ("game_number", game),
        ("local_seat_id", seat),
        ("last_game_result", expected),
    ):
        if key in final_state:
            require(type(final_state[key]) is type(value) and final_state[key] == value)
    return Receipt(install, match, game, seat, mapping, winner, bool(room_winners))


def _side(paths):
    require(0 < len(paths) <= 100)
    rows = {}
    installs = set()
    for path in paths:
        row = native_receipt(read_bundle(path))
        key = row.match, row.game
        require(key not in rows)
        rows[key] = row
        installs.add(row.install)
    require(len(installs) == 1)
    return rows, installs.pop()


def audit(side_a, side_b):
    a, ia = _side(side_a)
    b, ib = _side(side_b)
    require(ia != ib and a.keys() == b.keys())
    wins = losses = draws = 0
    finished = set()
    for key, x in a.items():
        y = b[key]
        require(x.teams == y.teams and x.winner == y.winner and x.seat != y.seat)
        if x.room_completed and y.room_completed:
            finished.add(x.match)
        if x.winner is None:
            draws += 1
        elif dict(x.teams)[x.seat] == x.winner:
            wins += 1
        else:
            losses += 1
    return {
        "paired_games": len(a),
        "distinct_arena_matches": len({r.match for r in a.values()}),
        "matches_with_both_native_completion_events": len(finished),
        "a_wins": wins,
        "b_wins": losses,
        "draws": draws,
        "evidence": "Agreement between supplied local bundle headers, native room seats and GRE game results.",
        "open": "Direct invitation, unattended play, deck mirror, hardware swap and served model/code require separate evidence.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--side-a",
        nargs="+",
        type=Path,
        required=True,
        help="saved .json.gz bundles from the first installation",
    )
    parser.add_argument(
        "--side-b",
        nargs="+",
        type=Path,
        required=True,
        help="matching .json.gz bundles from the second installation",
    )
    args = parser.parse_args(argv)
    try:
        result = audit(args.side_a, args.side_b)
    except (
        OSError,
        EOFError,
        zlib.error,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        parser.exit(2, "Could not verify matching local Arena bundle evidence.\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
