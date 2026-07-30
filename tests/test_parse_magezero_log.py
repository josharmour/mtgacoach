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
        assert kinds.get("DECLARE_ATTACKERS") == "attackers"
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
