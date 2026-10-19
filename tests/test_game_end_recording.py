"""Each game end is recorded once, for the game it belongs to (field report 2026-10-19).

Autopilot played every game that morning. The coach log showed:

* real results discarded as "stale on startup" right after the game ended
  (07:22:51, 07:29:46, ...): the match-boundary reset (X -> None) ran first,
  and the next iteration read last_match_id None as a fresh start;
* game ends never taken (07:46:16, 07:56:56, ...): the loop only took the
  event with advice history, and autopilot records none;
* the previous game's result applied mid-game (07:52:11, 08:11:50,
  09:06:01): a concede offer recorded advice ~1 s later, releasing the
  leftover "win" as the current game's result ("win vs Doki" for a loss to
  Taniquetil), stopping its packet and uploading its bundle mid-game.

These tests feed Player.log-shaped GRE batches and MatchGameRoomStateChanged
events through the real GameState handler and server room handler, and run
the real coaching loop (the bare-coach harness of
test_standalone_coaching_loop) with real history, packets and bundles.
"""

from __future__ import annotations

import json
import logging

import pytest
from tests.test_standalone_coaching_loop import make_loop_coach, make_state, run_loop

import arenamcp.server as server
from arenamcp import match_bundle, match_history, match_packets
from arenamcp.gamestate import GameState, create_game_state_handler

# Seat and team 2 are ours (armour.tv in most of the 2026-10-19 games).
OUR_TEAM = 2
THEIR_TEAM = 1


def result_row(winning_team: int, scope: str = "MatchScope_Game") -> dict:
    return {
        "scope": scope,
        "result": "ResultType_WinLoss",
        "winningTeamId": winning_team,
        "reason": "ResultReason_Game",
    }


def room_event(match_id: str, opponent: str, state_type: str, results: list | None = None) -> dict:
    info: dict = {
        "gameRoomConfig": {
            "matchId": match_id,
            "reservedPlayers": [
                {"userId": "opp", "playerName": opponent, "systemSeatId": 1, "teamId": THEIR_TEAM},
                {"userId": "me", "playerName": "armour.tv", "systemSeatId": 2, "teamId": OUR_TEAM},
            ],
        },
        "stateType": state_type,
    }
    if results is not None:
        info["finalMatchResult"] = {
            "matchId": match_id,
            "matchCompletedReason": "MatchCompletedReasonType_Success",
            "resultList": results,
        }
    return {"matchGameRoomStateChangedEvent": {"gameRoomInfo": info}}


class Arena:
    """Player.log events into a real GameState: GRE batches and room-state events."""

    def __init__(self, gs: GameState) -> None:
        self.gs = gs
        self.handler = create_game_state_handler(gs)
        self.state_id = 0

    def _gre(self, *messages: dict) -> None:
        self.handler({"greToClientEvent": {"greToClientMessages": list(messages)}})

    def _state(
        self,
        match_id: str,
        game_number: int,
        turn: int,
        *,
        stage: str = "GameStage_Play",
        results: list | tuple = (),
        our_life: int = 20,
        their_life: int = 20,
    ) -> dict:
        self.state_id += 1
        return {
            "type": "GREMessageType_GameStateMessage",
            "systemSeatIds": [2],
            "gameStateMessage": {
                "type": "GameStateType_Full",
                "gameStateId": self.state_id,
                "turnInfo": {
                    "turnNumber": turn,
                    "phase": "Phase_Main1",
                    "activePlayer": 1,
                    "priorityPlayer": 1,
                },
                "players": [
                    {
                        "systemSeatNumber": 1,
                        "teamId": THEIR_TEAM,
                        "lifeTotal": their_life,
                        "status": "PlayerStatus_InGame",
                    },
                    {
                        "systemSeatNumber": 2,
                        "teamId": OUR_TEAM,
                        "lifeTotal": our_life,
                        "status": "PlayerStatus_InGame",
                    },
                ],
                "gameInfo": {
                    "matchID": match_id,
                    "gameNumber": game_number,
                    "stage": stage,
                    "results": list(results),
                },
            },
        }

    def start_match(self, match_id: str, opponent: str) -> None:
        server._handle_match_created(room_event(match_id, opponent, "MatchGameRoomStateType_Playing"))

    def play(self, match_id: str, game_number: int, turn: int, *, earlier: tuple = ()) -> None:
        """A game in progress; ``earlier`` are the finished games' rows Arena keeps listing."""
        self._gre(self._state(match_id, game_number, turn, results=earlier))

    def end_game(
        self,
        match_id: str,
        game_number: int,
        winning_team: int | None,
        *,
        earlier: tuple = (),
        match_over: bool = True,
    ) -> None:
        """GameOver state then IntermissionReq, one batch (Player.log 07:52:28 for c36db3a3).

        ``winning_team=None``: Arena's GameOver carried no result (both alive).
        """
        rows = list(earlier)
        intermission: dict = {}
        if winning_team is not None:
            rows.append(result_row(winning_team))
            if match_over:
                rows.append(result_row(winning_team, "MatchScope_Match"))
            scope = "MatchScope_Match" if match_over else "MatchScope_Game"
            intermission = {"result": result_row(winning_team, scope)}
        turn = self.gs.turn_info.turn_number
        over = self._state(
            match_id,
            game_number,
            turn,
            stage="GameStage_GameOver",
            results=rows,
            our_life=0 if winning_team == THEIR_TEAM else 20,
            their_life=0 if winning_team == OUR_TEAM else 20,
        )
        self._gre(
            over,
            {"type": "GREMessageType_IntermissionReq", "systemSeatIds": [2], "intermissionReq": intermission},
        )

    def complete_match(self, match_id: str, opponent: str, rows: list) -> None:
        """MatchCompleted with finalMatchResult (it re-binds the finished match id)."""
        server._handle_match_created(
            room_event(match_id, opponent, "MatchGameRoomStateType_MatchCompleted", results=rows)
        )

    def mulligan_reset(self) -> None:
        """A MulliganReq at turn > 1 resets the "ghost state" (the 2026-10-06 13:53:40 Bo3 game 2 start)."""
        self._gre({"type": "GREMessageType_MulliganReq", "systemSeatIds": [2], "mulliganReq": {}})


@pytest.fixture
def arena(monkeypatch):
    gs = GameState()
    monkeypatch.setattr(server, "game_state", gs)
    monkeypatch.setattr(server, "_completed_match_for_navigation", {})
    monkeypatch.setattr(server, "_match_event_ids", {})
    monkeypatch.setattr(server, "_match_event_from_log", lambda match_id: "")
    monkeypatch.setattr("arenamcp.log_utils.get_local_player_id", lambda: "me")
    match_packets.stop_match_packet()
    yield Arena(gs)
    match_packets.stop_match_packet()


@pytest.fixture
def bundles(monkeypatch) -> list[tuple[str, str]]:
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        match_bundle, "schedule_upload", lambda ctx, **kw: scheduled.append((ctx.bundle_id, kw["result"]))
    )
    return scheduled


def loop_coach(arena: Arena):
    """The loop harness reading the real GameState, with the real post-match recording."""
    coach = make_loop_coach(triggers=[])
    gs = arena.gs

    def view() -> dict:
        state = make_state(match_id=gs.match_id, turn=gs.turn_info.turn_number)
        state["game_number"] = gs.game_number
        state["local_seat_id"] = gs.local_seat_id
        return state

    coach._mcp.get_game_state = view  # type: ignore[method-assign]
    del coach._stage_post_match_analysis  # the harness stub: use the real one
    coach._staged_analyses = {}
    coach._auto_post_match_analysis = False
    coach._start_auto_advice_score = lambda *a, **k: None  # type: ignore[method-assign]
    coach._last_game_end_check_error = ""
    return coach


def script(coach, steps: dict) -> None:
    """Run ``steps[n]()`` in the log poll at the top of loop iteration n."""
    polls = {"n": 0}

    def hook() -> None:
        polls["n"] += 1
        step = steps.get(polls["n"])
        if step is not None:
            step()

    coach._mcp.poll_hooks.append(hook)


def history() -> dict[str, tuple[str, str]]:
    return {r.match_id: (r.result, r.opponent_name) for r in match_history.MatchHistory().get_recent(50)}


def packets(game_id: str) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(match_packets.PACKETS_DIR.glob(f"packet_*_{game_id}.json"))
    ]


def test_an_autopilot_game_end_is_recorded_and_never_lands_on_the_next_game(
    monkeypatch, arena, bundles, caplog
):
    """07:46:16 -> 07:52:11: no advice under autopilot, then a concede offer in the next game."""
    a, b = "a-11c61fa5-36c5-4e88", "b-c36db3a3-6452-4cd0"
    coach = loop_coach(arena)
    seen: dict[str, object] = {}

    def concede_offer() -> None:
        # _concede_announce records the offer as advice (standalone_diagnostics._record_advice).
        arena.play(b, 1, 15)
        coach._advice_history.append({"advice": "Concede: 7 damage gets through", "trigger": "concede"})

    def while_b_is_live() -> None:
        seen["history"] = history()
        seen["bundles"] = list(bundles)
        seen["concede_game_over"] = coach._concede_game_over()
        seen["oops_game_over"] = coach._oops_game_over()
        seen["recorded"] = dict(coach._recorded_games())

    script(
        coach,
        {
            1: lambda: (arena.start_match(a, "Doki"), arena.play(a, 1, 3)),
            2: lambda: arena.play(a, 1, 11),
            3: lambda: (
                arena.end_game(a, 1, OUR_TEAM),
                arena.complete_match(
                    a, "Doki", [result_row(OUR_TEAM), result_row(OUR_TEAM, "MatchScope_Match")]
                ),
            ),
            5: lambda: (arena.start_match(b, "Taniquetil"), arena.play(b, 1, 5)),
            6: concede_offer,
            7: while_b_is_live,
            8: lambda: (
                arena.end_game(b, 1, THEIR_TEAM),
                arena.complete_match(
                    b, "Taniquetil", [result_row(THEIR_TEAM), result_row(THEIR_TEAM, "MatchScope_Match")]
                ),
            ),
            9: lambda: seen.update(game_over_after_b=coach._concede_game_over()),
        },
    )
    with caplog.at_level(logging.INFO):
        run_loop(monkeypatch, coach, iterations=9)

    # Game A was recorded at its end, without advice; nothing was applied to B mid-game.
    assert seen["history"] == {a: ("win", "Doki")}
    assert seen["bundles"] == [(a, "win")]
    assert seen["concede_game_over"] is False and seen["oops_game_over"] is False
    assert seen["recorded"] == {a: "win"}
    # B's own end, with B's result and opponent.
    assert history() == {a: ("win", "Doki"), b: ("loss", "Taniquetil")}
    assert bundles == [(a, "win"), (b, "loss")]
    assert [p["result"] for p in packets(a)] == ["win"]
    assert [p["result"] for p in packets(b)] == ["loss"]
    assert seen["game_over_after_b"] is True
    # Only B had advice (the concede offer): only B's analysis is staged.
    assert list(coach._staged_analyses) == [b]
    assert coach._staged_analyses[b]["result"] == "loss"
    ended = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Game ended (event signal)")]
    assert ended == [
        f"Game ended (event signal): win (game {a})",
        f"Game ended (event signal): loss (game {b})",
    ]
    assert "Discarded stale" not in caplog.text


def test_a_game_end_that_lands_with_the_match_boundary_reset_is_recorded(monkeypatch, arena, bundles, caplog):
    """07:22:50: Game-end prepared -> Match boundary (X -> None) -> "Discarded stale ... on startup"."""
    a = "a-c23d80c4-bd20-4db7-822c"
    coach = loop_coach(arena)
    seen: dict[str, object] = {}
    script(
        coach,
        {
            1: lambda: (arena.start_match(a, "Bord3l"), arena.play(a, 1, 3)),
            2: lambda: arena.play(a, 1, 10),
            # IntermissionReq's reset: the loop reads no match and the set event in one iteration.
            3: lambda: arena.end_game(a, 1, OUR_TEAM),
            4: lambda: (
                seen.update(match_number=coach._match_number),
                arena.complete_match(
                    a, "Bord3l", [result_row(OUR_TEAM), result_row(OUR_TEAM, "MatchScope_Match")]
                ),
            ),
        },
    )
    with caplog.at_level(logging.INFO):
        run_loop(monkeypatch, coach, iterations=6)

    assert seen["match_number"] == 1  # the X -> None boundary did run in iteration 3
    assert history() == {a: ("win", "Bord3l")}
    assert bundles == [(a, "win")]
    assert [p["result"] for p in packets(a)] == ["win"]
    assert "Discarded stale" not in caplog.text
    # MatchCompleted re-binds the finished id; it neither re-signals nor re-arms recording.
    assert not arena.gs.game_ended_event.is_set()
    assert coach._match_bundle_ctx is None and match_packets.get_current_packet() is None


def test_bo3_games_are_recorded_as_their_own_games(monkeypatch, arena, bundles):
    """Game 2 starts listing game 1's row; a mulligan ghost reset mid-start; MatchCompleted after game 3."""
    m = "m-bo3-3da54de9-9820-4ae2"
    g1, g2 = result_row(THEIR_TEAM), result_row(OUR_TEAM)
    coach = loop_coach(arena)
    seen: dict[str, object] = {}

    def game_two_starts() -> None:
        arena.play(m, 2, 2, earlier=(g1,))
        seen["result_at_game_2_start"] = arena.gs.last_game_result
        seen["over_at_game_2_start"] = arena.gs.game_over_signalled()

    def a_decision_then_the_ghost_reset() -> None:
        match_packets.get_current_packet().add_executed_action("play_land", "Island", turn=2)
        arena.mulligan_reset()

    script(
        coach,
        {
            1: lambda: (arena.start_match(m, "Indoraptor2474"), arena.play(m, 1, 3)),
            2: lambda: arena.play(m, 1, 13),
            3: lambda: arena.end_game(m, 1, THEIR_TEAM, match_over=False),
            5: game_two_starts,
            6: a_decision_then_the_ghost_reset,
            7: lambda: arena.play(m, 2, 2, earlier=(g1,)),
            8: lambda: arena.play(m, 2, 9, earlier=(g1,)),
            9: lambda: arena.end_game(m, 2, OUR_TEAM, earlier=(g1,), match_over=False),
            10: lambda: arena.play(m, 3, 1, earlier=(g1, g2)),
            11: lambda: arena.play(m, 3, 7, earlier=(g1, g2)),
            12: lambda: (
                arena.end_game(m, 3, OUR_TEAM, earlier=(g1, g2)),
                arena.complete_match(
                    m,
                    "Indoraptor2474",
                    [g1, g2, result_row(OUR_TEAM), result_row(OUR_TEAM, "MatchScope_Match")],
                ),
            ),
        },
    )
    run_loop(monkeypatch, coach, iterations=13)

    # Game 2's start lists game 1's loss; it is not game 2's result, and game 1 is no longer "over".
    assert seen["result_at_game_2_start"] is None
    assert seen["over_at_game_2_start"] is False
    assert {game: result for game, (result, _) in history().items()} == {
        m: "loss",
        f"{m}-g2": "win",
        f"{m}-g3": "win",
    }
    assert bundles == [(m, "loss"), (f"{m}-g2", "win"), (f"{m}-g3", "win")]
    assert [p["result"] for p in packets(m)] == ["loss"]
    # The ghost reset (game 2 -> no match -> game 2) kept game 2's packet open.
    game_two = packets(f"{m}-g2")
    assert [p["result"] for p in game_two] == ["win"] and len(game_two[0]["decisions"]) == 1
    assert [p["result"] for p in packets(f"{m}-g3")] == ["win"]


def test_final_match_result_corrects_a_game_recorded_without_a_result(monkeypatch, arena, bundles, caplog):
    """A GameOver without results (nobody at 0) records "unknown"; MatchCompleted's loss corrects it."""
    a = "a-unknown-41753907-9013"
    coach = loop_coach(arena)
    script(
        coach,
        {
            1: lambda: (arena.start_match(a, "Llimark"), arena.play(a, 1, 3)),
            2: lambda: arena.play(a, 1, 14),
            3: lambda: arena.end_game(a, 1, None),
            4: lambda: arena.complete_match(
                a, "Llimark", [result_row(THEIR_TEAM), result_row(THEIR_TEAM, "MatchScope_Match")]
            ),
        },
    )
    with caplog.at_level(logging.INFO):
        run_loop(monkeypatch, coach, iterations=6)

    assert history() == {a: ("loss", "Llimark")}  # one record, corrected in place
    assert f"Corrected game result for {a}: unknown -> loss" in caplog.text
    assert bundles == [(a, "unknown")]  # uploaded at the game end, once


def test_a_game_end_from_the_startup_catch_up_is_discarded(monkeypatch, arena, bundles, caplog):
    """The log catch-up replays a finished match before the loop starts; only the live game is recorded."""
    p, q = "p-finished-before-start", "q-live-at-start"
    arena.start_match(p, "Nadarr")
    arena.play(p, 1, 9)
    arena.end_game(p, 1, OUR_TEAM)
    arena.complete_match(p, "Nadarr", [result_row(OUR_TEAM), result_row(OUR_TEAM, "MatchScope_Match")])
    arena.start_match(q, "Kinley")
    arena.play(q, 1, 4)
    assert arena.gs.game_ended_event.is_set()  # P's end was never taken

    coach = loop_coach(arena)
    script(
        coach,
        {
            2: lambda: arena.play(q, 1, 6),
            3: lambda: arena.end_game(q, 1, THEIR_TEAM),
        },
    )
    with caplog.at_level(logging.INFO):
        run_loop(monkeypatch, coach, iterations=4)

    assert f"Discarded stale game-end event (result=win, match={p})" in caplog.text
    assert history() == {q: ("loss", "Kinley")}
    assert bundles == [(q, "loss")]


def test_an_untaken_game_end_stays_with_its_game():
    """08:00:04 "Game-end prepared: result=win" with no detection: a stale result was reused."""
    gs = GameState()
    arena = Arena(gs)
    arena.play("a", 1, 8)
    arena.end_game("a", 1, OUR_TEAM)
    assert gs.game_over_signalled() and gs.last_game_result == "win"

    # The next game starts before anyone took A's end.
    arena.play("b", 1, 1)
    assert not gs.game_over_signalled()
    assert gs.last_game_result is None
    # B ends without a result: it is not A's leftover "win".
    arena.end_game("b", 1, None)
    record = gs.take_game_end()
    assert (record["match_id"], record["game_number"], record["result"]) == ("b", 1, None)
    assert gs.take_game_end() is None  # taken exactly once
    assert gs.game_over_signalled()  # B is over until the next game starts
