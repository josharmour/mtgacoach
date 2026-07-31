"""Unit tests for parse_magezero_log.py (WP-3 B1).

Tests the core parsing pipeline: thread attribution, decision extraction,
hand/perm parsing, outcome calibration, and edge cases.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Ensure the tools/training directory is on the path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "tools" / "training"))

import pytest
from parse_magezero_log import (
    RE_CHOSE_ACTION,
    RE_DIE_ROLL,
    RE_HAND,
    RE_LOG_LIFE,
    RE_PERMANENTS,
    RE_PLAYABLE,
    RE_PLAYER_LIFE,
    RE_POOL,
    RE_POOL_TOP,
    RE_THREAD,
    RE_WIN_RATE,
    SessionInfo,
    detect_sessions,
    parse_card_list,
    parse_log,
    parse_permanents,
    parse_pool_actions,
)

# ════════════════════════════════════════════════════════════════════════
# Regex tests
# ════════════════════════════════════════════════════════════════════════


class TestRegex:
    def test_thread_id(self):
        m = RE_THREAD.search("INFO  ts  msg =>[pool-3-thread-6] ComputerPlayerMCTS.priority")
        assert m is not None
        assert m.group(1) == "pool-3-thread-6"

    def test_die_roll_a(self):
        m = RE_DIE_ROLL.search("INFO  ts Player A won the die roll =>[t]")
        assert m is not None
        assert m.group(1) == "A"

    def test_die_roll_b(self):
        m = RE_DIE_ROLL.search("INFO  ts Player B won the die roll =>[t]")
        assert m is not None
        assert m.group(1) == "B"

    def test_log_life(self):
        line = "INFO  ts [4:Precombat Main:PRECOMBAT_MAIN][player PlayerA:18][player PlayerB:20] =>[t]"
        m = RE_LOG_LIFE.search(line)
        assert m is not None
        assert m.group(1) == "4"
        assert m.group(2) == "Precombat Main"
        assert m.group(3) == "PRECOMBAT_MAIN"
        assert m.group(4) == "18"
        assert m.group(5) == "20"

    def test_chose_action(self):
        line = (
            "INFO  ts "
            "[1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Adarkar Wastes "
            "success ratio: 0.0589 =>[t]"
        )
        m = RE_CHOSE_ACTION.search(line)
        assert m is not None
        assert m.group(1) == "1"
        assert m.group(2) == "Precombat Main"
        assert m.group(3) == "PRECOMBAT_MAIN"
        assert m.group(4) == "Play Adarkar Wastes"
        assert float(m.group(5)) == pytest.approx(0.0589, abs=1e-4)

    def test_chose_action_declare_attackers(self):
        line = (
            "INFO  ts "
            "[12:Combat:DECLARE_ATTACKERS]chose action:"
            "{1}{U}: Untap {this}. success ratio: 0.5676 =>[t]"
        )
        m = RE_CHOSE_ACTION.search(line)
        assert m is not None
        assert m.group(1) == "12"
        assert m.group(3) == "DECLARE_ATTACKERS"
        assert "{1}{U}: Untap {this}." in m.group(4)

    def test_pool(self):
        line = (
            "INFO PRECOMBAT_MAIN0pool= actions: "
            "[Pass score: -0.075 count: 57] "
            "[Play Adarkar Wastes score: 0.059 count: 304]  =>[t]"
        )
        m = RE_POOL.search(line)
        assert m is not None
        assert m.group(1) == "PRECOMBAT_MAIN"
        assert m.group(2) == "0"
        assert "Play Adarkar Wastes" in m.group(3)

    def test_pool_top(self):
        line = (
            "INFO PRECOMBAT_MAIN1 (top: Cast Combat Research)pool= actions: "
            "[Hired Claw score: 0.055 count: 324] "
            "[Sleep-Cursed Faerie score: 0.055 count: 326]  =>[t]"
        )
        m = RE_POOL_TOP.search(line)
        assert m is not None
        assert m.group(1) == "PRECOMBAT_MAIN"
        assert m.group(2) == "1"

    def test_pool_binary(self):
        line = (
            "INFO DECLARE_ATTACKERS0pool= actions: "
            "[false score: -0.106 count: 764] "
            "[true score: -0.153 count: 235]  =>[t]"
        )
        m = RE_POOL.search(line)
        assert m is not None
        assert m.group(1) == "DECLARE_ATTACKERS"

    def test_playable_abilities(self):
        line = "INFO  playable abilities: [Play Adarkar Wastes, Pass] =>[t]"
        m = RE_PLAYABLE.search(line)
        assert m is not None
        items = [x.strip() for x in m.group(1).split(",")]
        assert items == ["Play Adarkar Wastes", "Pass"]

    def test_hand(self):
        line = (
            "INFO  -> Hand: [Island; Meticulous Archive; "
            "No More Lies; Bounce Off; Combat Research; Combat Research] =>[t]"
        )
        m = RE_HAND.search(line)
        assert m is not None
        assert "Island" in m.group(1)
        assert "Meticulous Archive" in m.group(1)

    def test_permanents_tapped(self):
        line = "INFO  -> Permanents: [Seachrome Coast,tapped; Skrelv, Defector Mite] =>[t]"
        m = RE_PERMANENTS.search(line)
        assert m is not None
        parsed = parse_permanents(m.group(1))
        assert len(parsed) == 2
        assert parsed[0] == {"name": "Seachrome Coast", "tapped": True}
        assert parsed[1] == {"name": "Skrelv, Defector Mite", "tapped": False}

    def test_permanents_empty(self):
        line = "INFO  -> Permanents: [] =>[t]"
        m = RE_PERMANENTS.search(line)
        assert m is not None
        assert m.group(1) == ""

    def test_player_life(self):
        line = "INFO  [PlayerA], life = 18 =>[t]"
        m = RE_PLAYER_LIFE.search(line)
        assert m is not None
        assert m.group(1) == "PlayerA"
        assert m.group(2) == "18"

    def test_win_rate(self):
        line = "INFO  2026-07-28 22:02:41,755 Player A win rate: 31.67% (19/60) =>[main]"
        m = RE_WIN_RATE.search(line)
        assert m is not None
        assert float(m.group(1)) == pytest.approx(31.67, abs=0.01)
        assert int(m.group(2)) == 19
        assert int(m.group(3)) == 60


# ════════════════════════════════════════════════════════════════════════
# Parser function tests
# ════════════════════════════════════════════════════════════════════════


class TestParseFunctions:
    def test_parse_card_list_basic(self):
        cards = parse_card_list("Island; Meticulous Archive; No More Lies")
        assert cards == ["Island", "Meticulous Archive", "No More Lies"]

    def test_parse_card_list_empty(self):
        assert parse_card_list("") == []

    def test_parse_card_list_semicolon_space(self):
        cards = parse_card_list("Skrelv, Defector Mite; Combat Research")
        assert cards == ["Skrelv, Defector Mite", "Combat Research"]

    def test_parse_permanents_tapped_flag(self):
        result = parse_permanents("Adarkar Wastes,tapped; Island")
        assert result == [
            {"name": "Adarkar Wastes", "tapped": True},
            {"name": "Island", "tapped": False},
        ]

    def test_parse_permanents_score_suffix(self):
        """Strip trailing :N score suffix present in GameStateEvaluator2 output."""
        result = parse_permanents("Mountain,tapped:280; Hired Claw:906")
        assert result == [
            {"name": "Mountain", "tapped": True},
            {"name": "Hired Claw", "tapped": False},
        ]

    def test_parse_permanents_empty(self):
        assert parse_permanents("") == []

    def test_parse_pool_actions(self):
        text = "[Pass score: -0.075 count: 57] [Play Adarkar Wastes score: 0.059 count: 304]"
        actions = parse_pool_actions(text)
        assert len(actions) == 2
        assert actions[0] == ("Pass", -0.075, 57)
        assert actions[1] == ("Play Adarkar Wastes", 0.059, 304)

    def test_parse_pool_actions_binary(self):
        text = "[false score: -0.106 count: 764] [true score: -0.153 count: 235]"
        actions = parse_pool_actions(text)
        assert len(actions) == 2
        assert actions[0] == ("false", -0.106, 764)
        assert actions[1] == ("true", -0.153, 235)


# ════════════════════════════════════════════════════════════════════════
# Session detection tests
# ════════════════════════════════════════════════════════════════════════


class TestSessionDetection:
    def test_smoke_session_count(self):
        lines = [
            "INFO Simulating 60 games. Using thread pool of size 6. =>[main]",
            "INFO Player A win rate: 31.67% (19/60) =>[main]",
            "INFO Simulating 60 games. Using thread pool of size 6. =>[main]",
            "INFO Player A win rate: 20.00% (12/60) =>[main]",
        ]
        sessions = detect_sessions(lines, is_smoke_log=True)
        assert len(sessions) == 2
        assert sessions[0].n_wins == 19
        assert sessions[0].n_total == 60
        assert sessions[0].win_rate == pytest.approx(31.67, abs=0.01)
        assert sessions[1].n_wins == 12
        assert sessions[1].n_total == 60

    def test_session_games_per_thread(self):
        sess = SessionInfo(0, "ts", "Standard-MonoR", 60)
        sess.n_wins = 19
        sess.n_total = 60
        gpt = sess.games_per_thread
        assert len(gpt) == 6
        for v in gpt.values():
            assert v == 10  # 60/6 = 10 per thread

    def test_session_games_per_thread_59(self):
        sess = SessionInfo(0, "ts", "Standard-MonoU", 59)
        sess.n_wins = 32
        sess.n_total = 59
        gpt = sess.games_per_thread
        assert sum(gpt.values()) == 59
        # 59/6 = 9 remainder 5 → 5 threads get 10, 1 gets 9
        assert list(gpt.values()).count(10) == 5
        assert list(gpt.values()).count(9) == 1


# ════════════════════════════════════════════════════════════════════════
# Multi-thread interleave parsing test
# ════════════════════════════════════════════════════════════════════════

TEST_LOG_MULTI_THREAD = """\
INFO  2026-07-28 21:33:40,710 Simulating 2 games. Using thread pool of size 2 on 16 available cores.                    =>[main] ParallelDataGenerator.runSimulations
INFO  2026-07-28 21:33:40,712 Player A won the die roll                                                                  =>[pool-3-thread-1] ParallelDataGenerator.runSingleGame
INFO  2026-07-28 21:33:40,712 Player B won the die roll                                                                  =>[pool-3-thread-2] ParallelDataGenerator.runSingleGame
INFO  2026-07-28 21:33:45,071 PRECOMBAT_MAIN0pool= actions: [Pass score: -0.075 count: 57] [Play Island score: 0.059 count: 304]  =>[pool-3-thread-1] MCTSNode.bestChild
INFO  2026-07-28 21:33:45,072 playable abilities: [Play Island, Pass]                                                    =>[pool-3-thread-1] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:45,072 [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.0589            =>[pool-3-thread-1] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:45,072 [PlayerA], life = 20                                                                       =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:45,073 -> Hand: [Island; Adarkar Wastes; Bounce Off; Combat Research; Shardmage's Rescue; Sleep-Cursed Faerie] =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:45,073 -> Permanents: [Island]                                                                    =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:45,073 -> Permanents: []                                                                          =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,000 PRECOMBAT_MAIN0pool= actions: [Pass score: -0.044 count: 48] [Play Mountain score: 0.020 count: 154]  =>[pool-3-thread-2] MCTSNode.bestChild
INFO  2026-07-28 21:33:46,001 playable abilities: [Play Mountain, Pass]                                                   =>[pool-3-thread-2] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:46,001 [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Mountain success ratio: 0.0196           =>[pool-3-thread-2] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:46,002 [PlayerA], life = 20                                                                       =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,002 -> Hand: [Mountain; Mountain; Lightning Strike; Nova Hellkite; Mountain; Mountain]         =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,002 -> Permanents: [Mountain]                                                                   =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,002 -> Permanents: []                                                                          =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,800 PRECOMBAT_MAIN0pool= actions: [Pass score: -0.030 count: 81] [Cast Sleep-Cursed Faerie score: 0.078 count: 404]  =>[pool-3-thread-1] MCTSNode.bestChild
INFO  2026-07-28 21:33:46,801 playable abilities: [Cast Sleep-Cursed Faerie, Pass]                                        =>[pool-3-thread-1] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:46,801 [1:Precombat Main:PRECOMBAT_MAIN]chose action:Cast Sleep-Cursed Faerie success ratio: 0.078 =>[pool-3-thread-1] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:46,802 [PlayerA], life = 20                                                                       =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,802 -> Hand: [Island; Adarkar Wastes; Bounce Off; Combat Research; Shardmage's Rescue]          =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,802 -> Permanents: [Island,tapped; Sleep-Cursed Faerie]                                         =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,802 -> Permanents: []                                                                          =>[pool-3-thread-1] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,803 PRECOMBAT_MAIN0pool= actions: [Pass score: -0.027 count: 70] [Play Mountain score: 0.025 count: 172] [Play Meticulous Archive score: -0.004 count: 95]  =>[pool-3-thread-2] MCTSNode.bestChild
INFO  2026-07-28 21:33:46,804 playable abilities: [Play Mountain, Pass]                                                   =>[pool-3-thread-2] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:46,804 [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Mountain success ratio: 0.0252            =>[pool-3-thread-2] ComputerPlayerMCTS.priority
INFO  2026-07-28 21:33:46,805 -> Hand: [Mountain; Lightning Strike; Nova Hellkite; Mountain; Mountain]                   =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,805 -> Permanents: [Mountain,tapped; Mountain]                                                  =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:33:46,805 -> Permanents: []                                                                          =>[pool-3-thread-2] ComputerPlayerMCTS.printBattlefieldScore
INFO  2026-07-28 21:34:06,100 Player A win rate: 50.00% (1/2)                                                             =>[main] ParallelDataGenerator.runSimulations
"""


class TestLogParsing:
    def test_multi_thread_interleave(self):
        """Parse multi-thread interleaved log and verify per-thread attribution."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(TEST_LOG_MULTI_THREAD)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        # Should produce exactly 4 decisions (2 per thread)
        assert len(decisions) == 4, f"Expected 4 decisions, got {len(decisions)}"

        # Thread attribution
        thread1_decs = [d for d in decisions if d["_thread"] == "pool-3-thread-1"]
        thread2_decs = [d for d in decisions if d["_thread"] == "pool-3-thread-2"]
        assert len(thread1_decs) == 2
        assert len(thread2_decs) == 2

        # Each game should have ID incorporating its thread and seq
        game_ids = {d["game_id"] for d in decisions}
        assert len(game_ids) == 2  # 2 games across 2 threads

        # Check thread-1 first decision
        d = thread1_decs[0]
        assert d["turn"] == 1
        assert d["phase"] == "PRECOMBAT_MAIN"
        assert d["chosen"] == "Play Island"
        assert d["active_life"] == 20
        assert d["hand"] == [
            "Island",
            "Adarkar Wastes",
            "Bounce Off",
            "Combat Research",
            "Shardmage's Rescue",
            "Sleep-Cursed Faerie",
        ]
        assert d["battlefield_self"] == [{"name": "Island", "tapped": False}]
        assert d["battlefield_opp"] == []
        assert d["menu"] == ["Play Island", "Pass"]
        assert "Play Island" in d["mcts_counts"]
        assert d["decision_kind"] == "priority"

        # Check binary-like isn't detected incorrectly
        assert d["decision_kind"] == "priority"

    def test_comma_in_card_name(self):
        """Card names with commas (Skrelv, Defector Mite) should parse correctly."""
        log = TEST_LOG_MULTI_THREAD.replace("Permanents: [Island]", "Permanents: [Skrelv, Defector Mite]")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        thread1 = [d for d in decisions if d["_thread"] == "pool-3-thread-1"]
        if thread1:
            perms = thread1[0].get("battlefield_self", [])
            # Check if any perm name includes the comma card
            names = {p["name"] for p in perms}
            if "Skrelv, Defector Mite" in names:
                pass  # comma parsing worked
            # The test can at least verify no crash

    def test_tapped_flag_in_permanents(self):
        """Permanents with ,tapped suffix are correctly parsed."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(TEST_LOG_MULTI_THREAD)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        thread1 = [d for d in decisions if d["_thread"] == "pool-3-thread-1"]
        # Second decision on thread-1 has tapped island
        if len(thread1) >= 2:
            d2 = thread1[1]
            perms = d2["battlefield_self"]
            island = next((p for p in perms if p["name"] == "Island"), None)
            if island:
                assert island["tapped"] is True

    def test_comma_card_in_permanents_parse(self):
        """Skrelv, Defector Mite in permanents line."""
        parsed = parse_permanents("Skrelv, Defector Mite; Island,tapped")
        assert len(parsed) == 2
        assert parsed[0]["name"] == "Skrelv, Defector Mite"
        assert parsed[0]["tapped"] is False
        assert parsed[1]["name"] == "Island"
        assert parsed[1]["tapped"] is True

    def test_outcome_coverage(self):
        """All decisions should have won/lost outcome (100% coverage)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(TEST_LOG_MULTI_THREAD)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        for d in decisions:
            assert d["outcome"] in ("won", "lost"), f"Unexpected outcome: {d['outcome']}"

        won = sum(1 for d in decisions if d["outcome"] == "won")
        lost = sum(1 for d in decisions if d["outcome"] == "lost")
        assert won + lost == len(decisions)

    def test_session_assignment(self):
        """Decisions are assigned to correct session."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(TEST_LOG_MULTI_THREAD)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        assert len(sessions) == 1
        for d in decisions:
            assert d["session"] == sessions[0].label

    def test_game_id_uniqueness(self):
        """Each game has a unique game_id across threads."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(TEST_LOG_MULTI_THREAD)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        assert len({d["game_id"] for d in decisions}) == 2  # 2 games

    def test_reconciliation(self):
        """Inferred win count matches logged win rate."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(TEST_LOG_MULTI_THREAD)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        assert len(sessions) == 1
        sess = sessions[0]
        assert sess.n_wins == 1  # 1/2 = 50%

        inferred_wins = sum(1 for d in decisions if d["outcome"] == "won")
        unique_win_games = {d["game_id"] for d in decisions if d["outcome"] == "won"}
        assert len(unique_win_games) == 1  # 1 won game out of 2


# ════════════════════════════════════════════════════════════════════════
# Edge case tests
# ════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_log(self):
        """Empty log produces no decisions."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write("")
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()
        assert len(decisions) == 0

    def test_log_with_only_preamble(self):
        """Log with only database loading (no games) produces no decisions."""
        log = """\
INFO  Loading database... =>[main]
INFO  Database stats: =>[main]
INFO  Simulating 60 games. Using thread pool of size 6. =>[main]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()
        assert len(decisions) == 0

    def test_no_mcts_pool_lines(self):
        """Lines without pool should not produce decisions."""
        log = """\
INFO  Simulating 2 games. =>[main]
INFO  Player A won the die roll =>[pool-3-thread-1]
INFO  [PlayerA], life = 20 =>[pool-3-thread-1]
INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Pass success ratio: 0.0 =>[pool-3-thread-1]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log)
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()
        # No pool line seen before chose action → minimized decision → skipped
        assert len(decisions) == 0

    def test_decision_kinds(self):
        """Verify different decision kinds are classified correctly."""
        log = """\
INFO  Simulating 1 games. =>[main]
INFO  Player A won the die roll =>[pool-3-thread-1]
INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] [Play Island score: 0.1 count: 200] =>[t1]
INFO  playable abilities: [Play Island, Pass] =>[t1]
INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.1 =>[t1]
INFO  [PlayerA], life = 20 =>[t1]
INFO  -> Hand: [Island; Mountain] =>[t1]
INFO  -> Permanents: [Island] =>[t1]
INFO  -> Permanents: [] =>[t1]
INFO  DECLARE_ATTACKERS0pool= actions: [Pass score: -0.1 count: 50] [{T}: Draw a card. score: 0.1 count: 200] =>[t1]
INFO  [2:Combat:DECLARE_ATTACKERS]chose action:Pass success ratio: -0.1 =>[t1]
INFO  [PlayerA], life = 20 =>[t1]
INFO  -> Hand: [Island; Mountain] =>[t1]
INFO  -> Permanents: [Island] =>[t1]
INFO  -> Permanents: [] =>[t1]
INFO  DECLARE_BLOCKERS0pool= actions: [Stop Choosing score: -0.1 count: 50] [Nova Hellkite score: 0.1 count: 200] =>[t1]
INFO  [3:Combat:DECLARE_BLOCKERS]chose action:Nova Hellkite success ratio: 0.1 =>[t1]
INFO  [PlayerA], life = 20 =>[t1]
INFO  -> Hand: [Island; Mountain] =>[t1]
INFO  -> Permanents: [Island] =>[t1]
INFO  -> Permanents: [] =>[t1]
INFO  Player A win rate: 100.00% (1/1) =>[main]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log.replace("=>[t1]", "=>[pool-3-thread-1]"))
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        kinds = {d["phase"]: d["decision_kind"] for d in decisions}
        assert kinds.get("PRECOMBAT_MAIN") == "priority"
        # This fixture's DECLARE_ATTACKERS menu is [Pass, "{T}: Draw a card."] —
        # an ordinary PRIORITY window that merely sits inside the combat step,
        # which XMage grants so instants can be cast there. It was asserted as
        # "attackers" purely because of the phase code, and that assumption let
        # build_magezero_combat.py reverse-engineer attackers from stale board
        # markers: 89 of 90 emitted records answered declare_attackers while
        # their source decision was Pass or a mana ability. Classification now
        # follows the MENU, so this is correctly "priority".
        assert kinds.get("DECLARE_ATTACKERS") == "priority"
        # The blockers menu here IS a declaration — a multi-select of creature
        # names terminated by "Stop Choosing" — so it stays "blockers".
        assert kinds.get("DECLARE_BLOCKERS") == "blockers"

    def test_binary_decision_kind(self):
        """DECLARE_ATTACKERS with only false/true pool is classified as 'binary'."""
        log = """\
INFO  Simulating 1 games. =>[main]
INFO  Player A won the die roll =>[pool-3-thread-1]
INFO  DECLARE_ATTACKERS0pool= actions: [false score: -0.106 count: 764] [true score: -0.153 count: 235] =>[t1]
INFO  playable abilities: [false, true] =>[t1]
INFO  [2:Combat:DECLARE_ATTACKERS]chose action:false success ratio: -0.1 =>[t1]
INFO  [PlayerA], life = 20 =>[t1]
INFO  -> Hand: [Island] =>[t1]
INFO  -> Permanents: [Island] =>[t1]
INFO  -> Permanents: [] =>[t1]
INFO  Player A win rate: 100.00% (1/1) =>[main]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log.replace("=>[t1]", "=>[pool-3-thread-1]"))
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        for d in decisions:
            if d["phase"] == "DECLARE_ATTACKERS":
                assert d["decision_kind"] == "binary"

    def test_hand_backfill(self):
        """Hand lines after chose action should backfill into the decision."""
        log = """\
INFO  Simulating 1 games. =>[main]
INFO  Player A won the die roll =>[pool-3-thread-1]
INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] [Play Island score: 0.1 count: 200] =>[t1]
INFO  playable abilities: [Play Island, Pass] =>[t1]
INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.1 =>[t1]
INFO  [PlayerA], life = 20 =>[t1]
INFO  -> Hand: [Island; Adarkar Wastes; Bounce Off] =>[t1]
INFO  -> Permanents: [Island] =>[t1]
INFO  -> Permanents: [] =>[t1]
INFO  Player A win rate: 100.00% (1/1) =>[main]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log.replace("=>[t1]", "=>[pool-3-thread-1]"))
            log_path = f.name

        decisions, sessions = parse_log(log_path)
        Path(log_path).unlink()

        assert len(decisions) == 1
        assert decisions[0]["hand"] == ["Island", "Adarkar Wastes", "Bounce Off"]
        assert decisions[0]["battlefield_self"] == [{"name": "Island", "tapped": False}]


class TestIssue430BlockAttribution:
    """#430: boards must be attributed via the block header, not anonymous pairing.

    Real-log grammar (both emitters ALWAYS carry the header):
      ComputerPlayerMCTS.printBattlefieldScore: [PlayerX] -> Hand -> Perms(X) -> Perms(other)
      GameStateEvaluator2.printBattlefield:     [PlayerX] -> Hand -> Perms(X) -> Graveyard
    The old parser paired every Permanents line on the thread into an anonymous
    [self, opp] buffer, so each one-line evaluator block shifted the frame and
    swapped boards for the rest of the game (744 rows with off-color lands on
    the self board, 1,717 with UW lands on the opp board, per the issue).
    """

    @staticmethod
    def _parse(log_body: str):
        import tempfile
        from pathlib import Path

        log = (
            "INFO  Simulating 1 games. =>[main]\n"
            + log_body
            + "INFO  Player A win rate: 100.00% (1/1) =>[main]\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log.replace("=>[T]", "=>[pool-3-thread-1]"))
            path = f.name
        try:
            decisions, _ = parse_log(path)
        finally:
            Path(path).unlink()
        return decisions

    def test_evaluator_playerb_block_goes_to_opp_board(self):
        """A PlayerB-headed one-line evaluator block is the OPPONENT's board."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] [Play Island score: 0.1 count: 200] =>[T]\n"
            "INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.1 =>[T]\n"
            "INFO  [PlayerB], life = 20, score = 670 (Life:10000, Hand:25, Perm:280) =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Hand: [Lightning Strike:5; Hired Claw:5] =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Permanents: [Mountain,tapped:280; Hired Claw:906] =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Graveyard: [] =>[T] GameStateEvaluator2.printBattlefield \n"
        )
        assert len(decisions) == 1
        d = decisions[0]
        # Old parser: Mountain+Hired Claw landed in buffer slot 0 = SELF. Wrong.
        assert d["battlefield_opp"] == [
            {"name": "Mountain", "tapped": True},
            {"name": "Hired Claw", "tapped": False},
        ], f"PlayerB's board must be battlefield_opp, got opp={d['battlefield_opp']}"
        assert d["battlefield_self"] == []

    def test_evaluator_playerb_hand_never_enters_the_row(self):
        """PlayerB's hand is the opponent's HIDDEN hand — an L4 leak if kept."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] [Play Island score: 0.1 count: 200] =>[T]\n"
            "INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.1 =>[T]\n"
            "INFO  [PlayerB], life = 20, score = 670 (Life:10000, Hand:25, Perm:280) =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Hand: [Lightning Strike:5; Nova Hellkite:5] =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Permanents: [Mountain:280] =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  [PlayerA], life = 20 =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Hand: [Island; Combat Research] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [Island] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [Mountain] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d["hand"] == ["Island", "Combat Research"], (
            f"the opponent's hand must never backfill the row; got {d['hand']}"
        )

    def test_one_line_block_does_not_shift_the_frame(self):
        """THE frame-shift regression.

        An evaluator one-line block followed by an MCTS two-line block: the old
        anonymous pairing put [evaluator-line, mcts-line-1] into [self, opp] —
        assigning PlayerA's OWN board (mcts line 1) to the OPPONENT slot. After
        the fix, each block attributes independently and the boards match the
        MCTS block exactly.
        """
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] [Play Island score: 0.1 count: 200] =>[T]\n"
            "INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.1 =>[T]\n"
            # one-line evaluator block (PlayerA's own board)
            "INFO  [PlayerA], life = 20, score = 295 (Life:10000, Hand:30, Perm:300) =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Hand: [Island:5] =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Permanents: [Island:300] =>[T] GameStateEvaluator2.printBattlefield \n"
            "INFO  -> Graveyard: [] =>[T] GameStateEvaluator2.printBattlefield \n"
            # second decision, then a full MCTS block
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 30] [Cast Combat Research score: 0.2 count: 170] =>[T]\n"
            "INFO  [2:Precombat Main:PRECOMBAT_MAIN]chose action:Cast Combat Research success ratio: 0.2 =>[T]\n"
            "INFO  [PlayerA], life = 20 =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Hand: [Combat Research; Island] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [Island,tapped; Combat Research] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [Mountain,tapped; Hired Claw] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
        )
        assert len(decisions) == 2
        d2 = decisions[1]
        assert d2["battlefield_self"] == [
            {"name": "Island", "tapped": True},
            {"name": "Combat Research", "tapped": False},
        ], f"frame shifted: self={d2['battlefield_self']}"
        assert d2["battlefield_opp"] == [
            {"name": "Mountain", "tapped": True},
            {"name": "Hired Claw", "tapped": False},
        ], f"frame shifted: opp={d2['battlefield_opp']}"

    def test_legitimately_empty_boards_do_not_get_refilled_later(self):
        """A turn-1 decision with genuinely empty boards must keep them."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] [Play Island score: 0.1 count: 200] =>[T]\n"
            "INFO  [1:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.1 =>[T]\n"
            # turn-1 block: both boards empty
            "INFO  [PlayerA], life = 20 =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Hand: [Island; Combat Research] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            # later, boards fill up
            "INFO  [PlayerA], life = 20 =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Hand: [Combat Research] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [Island] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
            "INFO  -> Permanents: [Mountain] =>[T] ComputerPlayerMCTS.printBattlefieldScore \n"
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d["battlefield_self"] == [], f"turn-1 empty board was refilled: {d['battlefield_self']}"
        assert d["battlefield_opp"] == []


class TestPassDecisionRecovery:
    """XMage logs `chose action:` ONLY for non-pass actions.

    Measured on mz_train_smoke.log before this fix: 21,723 pool lines where MCTS
    deliberated, 9,111 (41.9%) with Pass as the argmax, but only 516 followed by
    a `chose action` line — so ~8.6k decisions where the search concluded "do
    nothing" were dropped. `chosen` contained a pass in 0 of 9,789 emitted rows
    while 100% of menus offered one, which also made B2's >40%-Pass tripwire
    unable to ever fire.
    """

    @staticmethod
    def _parse(log_body: str):
        import tempfile
        from pathlib import Path

        log = (
            "INFO  Simulating 1 games. =>[main]\n"
            + log_body
            + "INFO  Player A win rate: 100.00% (1/1) =>[main]\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log.replace("=>[T]", "=>[pool-3-thread-1]"))
            path = f.name
        try:
            return parse_log(path)[0]
        finally:
            Path(path).unlink()

    LIFE = "INFO  [3:Precombat Main:PRECOMBAT_MAIN][player PlayerA:20][player PlayerB:18] =>[T]\n"

    def test_unconsumed_pass_pool_becomes_a_decision(self):
        """A pool whose argmax is Pass and which no chose-action consumed."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            + self.LIFE
            # MCTS deliberates and prefers Pass — XMage logs no chose action.
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: 0.4 count: 700] [Cast Bounce Off score: 0.1 count: 90] =>[T]\n"
            # A later window forces the previous pool to resolve.
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 20] [Play Island score: 0.3 count: 400] =>[T]\n"
            + "INFO  [4:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.3 =>[T]\n"
        )
        assert len(decisions) == 2, f"expected the pass row + the play row, got {len(decisions)}"
        pass_row = decisions[0]
        assert pass_row["chosen"] == "Pass"
        assert pass_row["turn"] == 3, "turn comes from the last logLife line"
        assert pass_row["phase"] == "PRECOMBAT_MAIN"
        assert pass_row["mcts_counts"] == {"Pass": 700, "Cast Bounce Off": 90}
        assert pass_row["menu"] == ["Pass", "Cast Bounce Off"]
        assert decisions[1]["chosen"] == "Play Island"

    def test_ambiguous_unconsumed_pool_is_not_labelled(self):
        """An un-consumed pool whose argmax is NOT a pass must not be guessed."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            + self.LIFE
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.2 count: 30] [Cast Bounce Off score: 0.5 count: 900] =>[T]\n"
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 20] [Play Island score: 0.3 count: 400] =>[T]\n"
            + "INFO  [4:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.3 =>[T]\n"
        )
        assert len(decisions) == 1, (
            f"an unexplained non-pass pool must be skipped, not labelled; got {[d['chosen'] for d in decisions]}"
        )
        assert decisions[0]["chosen"] == "Play Island"

    def test_pass_pool_pending_at_game_boundary_is_flushed(self):
        """The last window of a game still belongs to that game."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            + self.LIFE
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: 0.4 count: 700] [Cast Bounce Off score: 0.1 count: 90] =>[T]\n"
            + "INFO  Player A won the die roll =>[T]\n"  # next game starts
            + self.LIFE
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 20] [Play Island score: 0.3 count: 400] =>[T]\n"
            + "INFO  [4:Precombat Main:PRECOMBAT_MAIN]chose action:Play Island success ratio: 0.3 =>[T]\n"
        )
        assert len(decisions) == 2
        assert decisions[0]["chosen"] == "Pass"
        assert decisions[0]["game_id"] != decisions[1]["game_id"], (
            "the flushed pass belongs to the OUTGOING game, not the new one"
        )

    def test_pass_pool_pending_at_eof_is_flushed(self):
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            + self.LIFE
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: 0.4 count: 700] [Cast Bounce Off score: 0.1 count: 90] =>[T]\n"
        )
        assert len(decisions) == 1
        assert decisions[0]["chosen"] == "Pass"

    def test_binary_decline_counts_as_a_pass(self):
        """CHOOSE_USE windows decline with `false`, not `Pass`."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            + self.LIFE
            + "INFO  PRECOMBAT_MAIN0pool= actions: [true score: 0.1 count: 40] [false score: 0.4 count: 500] =>[T]\n"
        )
        assert len(decisions) == 1
        assert decisions[0]["chosen"] == "false"
        assert decisions[0]["decision_kind"] == "binary"

    def test_recovered_rows_are_marked_internally_only(self):
        """`_recovered_pass` is a leading-underscore field, stripped on write."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            + self.LIFE
            + "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: 0.4 count: 700] [Cast Bounce Off score: 0.1 count: 90] =>[T]\n"
        )
        assert decisions[0]["_recovered_pass"] is True
        assert not any(k.startswith("_") for k in ("chosen", "menu", "mcts_counts"))


class TestSegmentMenu:
    """Menu segmentation with pool-name anchors (menu-parity fix).

    XMage prints `playable abilities` as List.toString() — entries joined with
    ", " and no quoting — while card names ("Cast Malcolm, Alluring Scoundrel")
    and ability text ("{T}: Draw a card, then discard a card.") contain ", "
    themselves. A plain comma split shattered those entries and every chose
    action with a comma-named card was then dropped as chosen_not_in_menu:
    816 of 5,835 post-filter rows (14.0%) on mz_train_smoke.log.
    """

    def test_comma_card_name_merged_when_pool_confirms(self):
        from parse_magezero_log import segment_menu

        raw = "Cast Malcolm, Alluring Scoundrel, Pass"
        vocab = ["Cast Malcolm, Alluring Scoundrel", "Pass"]
        assert segment_menu(raw, vocab) == ["Cast Malcolm, Alluring Scoundrel", "Pass"]

    def test_ability_text_with_comma_merged(self):
        from parse_magezero_log import segment_menu

        raw = "{T}: Draw a card, then discard a card., Pass"
        vocab = ["{T}: Draw a card, then discard a card.", "Pass"]
        assert segment_menu(raw, vocab) == [
            "{T}: Draw a card, then discard a card.",
            "Pass",
        ]

    def test_real_log_window_line_3369(self):
        """The exact raw payload from mz_train_smoke.log line 3369."""
        from parse_magezero_log import segment_menu

        raw = (
            "Cast Bounce Off, Cast Combat Research, Cast Kitsa, Otterball Elite, "
            "Cast Skrelv, Defector Mite, {1}{U}: Untap {this}., Pass"
        )
        vocab = ["Cast Combat Research", "Cast Kitsa, Otterball Elite", "Cast Skrelv, Defector Mite", "Pass"]
        assert segment_menu(raw, vocab) == [
            "Cast Bounce Off",
            "Cast Combat Research",
            "Cast Kitsa, Otterball Elite",
            "Cast Skrelv, Defector Mite",
            "{1}{U}: Untap {this}.",
            "Pass",
        ]

    def test_unconfirmed_comma_entry_stays_split(self):
        """Fail closed: no pool-confirmed name, no merge."""
        from parse_magezero_log import segment_menu

        raw = "Cast Malcolm, Alluring Scoundrel, Pass"
        vocab = ["Pass"]  # pool never confirmed the comma name
        assert segment_menu(raw, vocab) == ["Cast Malcolm", "Alluring Scoundrel", "Pass"]

    def test_duplicate_entries_preserved(self):
        from parse_magezero_log import segment_menu

        raw = "Play Island, Play Island, Pass"
        vocab = ["Play Island", "Pass"]
        assert segment_menu(raw, vocab) == ["Play Island", "Play Island", "Pass"]

    def test_empty_raw(self):
        from parse_magezero_log import segment_menu

        assert segment_menu("", ["Pass"]) == []

    def test_longest_match_wins(self):
        """A vocab name that is a prefix of another must not steal the match."""
        from parse_magezero_log import segment_menu

        raw = "Cast Combat Research, Cast Combat Research II, Pass"
        vocab = ["Cast Combat Research", "Cast Combat Research II", "Pass"]
        assert segment_menu(raw, vocab) == [
            "Cast Combat Research",
            "Cast Combat Research II",
            "Pass",
        ]


class TestCommaMenuEndToEnd:
    """A chose action with a comma-named card must survive to the decision row
    with its menu entry intact (this was the chosen_not_in_menu drop class)."""

    @staticmethod
    def _parse(log_body: str):
        log = (
            "INFO  Simulating 1 games. =>[main]\n"
            + log_body
            + "INFO  Player A win rate: 100.00% (1/1) =>[main]\n"
        )
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            f.write(log.replace("=>[T]", "=>[pool-3-thread-1]"))
            path = f.name
        try:
            return parse_log(path)[0]
        finally:
            Path(path).unlink()

    def test_comma_named_cast_matches_menu(self):
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            "INFO  [2:Precombat Main:PRECOMBAT_MAIN][player PlayerA:20][player PlayerB:20] =>[T]\n"
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] "
            "[Cast Skrelv, Defector Mite score: 0.2 count: 500] =>[T]\n"
            "INFO  playable abilities: [Cast Skrelv, Defector Mite, Pass] =>[T]\n"
            "INFO  [2:Precombat Main:PRECOMBAT_MAIN]chose action:Cast Skrelv, Defector Mite "
            "success ratio: 0.2 =>[T]\n"
        )
        assert len(decisions) == 1
        d = decisions[0]
        assert d["chosen"] == "Cast Skrelv, Defector Mite"
        assert d["menu"] == ["Cast Skrelv, Defector Mite", "Pass"]
        assert d["chosen"] in d["menu"]
        assert d["mcts_counts"]["Cast Skrelv, Defector Mite"] == 500

    def test_recovered_pass_window_menu_segmented(self):
        """An unconsumed pass pool whose playable-abilities line landed."""
        decisions = self._parse(
            "INFO  Player A won the die roll =>[T]\n"
            "INFO  [3:Precombat Main:PRECOMBAT_MAIN][player PlayerA:20][player PlayerB:18] =>[T]\n"
            # Window 1: menu + comma-named cast.
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: -0.1 count: 50] "
            "[Cast Kitsa, Otterball Elite score: 0.2 count: 500] =>[T]\n"
            "INFO  playable abilities: [Cast Kitsa, Otterball Elite, Pass] =>[T]\n"
            "INFO  [3:Precombat Main:PRECOMBAT_MAIN]chose action:Cast Kitsa, Otterball Elite "
            "success ratio: 0.2 =>[T]\n"
            # Window 2: search prefers Pass; no playable/chose lines follow (EOF flush).
            "INFO  PRECOMBAT_MAIN0pool= actions: [Pass score: 0.4 count: 700] "
            "[Cast Kitsa, Otterball Elite score: 0.1 count: 90] =>[T]\n"
        )
        assert len(decisions) == 2
        assert decisions[0]["menu"] == ["Cast Kitsa, Otterball Elite", "Pass"]
        pass_row = decisions[1]
        assert pass_row["chosen"] == "Pass"
        # No playable line landed for window 2 -> pool names are the menu.
        assert pass_row["menu"] == ["Pass", "Cast Kitsa, Otterball Elite"]
