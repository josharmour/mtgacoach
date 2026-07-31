#!/usr/bin/env python3
"""Tests for oracle-text coverage: parser noise strips, oracle attachment in
the MZ bridge, the [NO TARGETS] sanitizer, the audit module, and the
pipeline's fail-closed --min-oracle-coverage floor."""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO), str(REPO / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from tools.training import build_magezero_bridge as BRIDGE  # noqa: E402
from tools.training import oracle_coverage as ORACLE  # noqa: E402
from tools.training import parse_magezero_log as PARSER  # noqa: E402
from tools.training import run_wp3_pipeline as PIPE  # noqa: E402

# ---------------------------------------------------------------------------
# Parser log-noise strips
# ---------------------------------------------------------------------------


class TestParserSuffixStrips:
    def test_hand_score_suffix_stripped(self):
        """GameStateEvaluator2 hand entries carry ':<score>' — log noise."""
        got = PARSER.parse_card_list("Adarkar Wastes:5; Negate:5; Skrelv, Defector Mite:5")
        assert got == ["Adarkar Wastes", "Negate", "Skrelv, Defector Mite"]

    def test_hand_without_suffix_unchanged(self):
        got = PARSER.parse_card_list("Island; Kitsa, Otterball Elite")
        assert got == ["Island", "Kitsa, Otterball Elite"]

    def test_permanents_attacking_extracted(self):
        got = PARSER.parse_permanents(
            "Kitsa, Otterball Elite,tapped,attacking; Island,tapped; Skrelv, Defector Mite"
        )
        assert got[0]["name"] == "Kitsa, Otterball Elite"
        assert got[0]["tapped"] is True
        assert got[0]["attacking"] is True
        assert got[1] == {"name": "Island", "tapped": True}
        assert got[2] == {"name": "Skrelv, Defector Mite", "tapped": False}

    def test_permanents_blocking_extracted(self):
        got = PARSER.parse_permanents("Malcolm, Alluring Scoundrel,blocking")
        assert got[0]["name"] == "Malcolm, Alluring Scoundrel"
        assert got[0]["blocking"] is True
        assert not got[0]["tapped"]


# ---------------------------------------------------------------------------
# Bridge oracle attachment
# ---------------------------------------------------------------------------


def _row(**over):
    row = {
        "game_id": "t:g:1",
        "turn": 4,
        "phase": "PRECOMBAT_MAIN",
        "outcome": "won",
        "actor": "PlayerA",
        "session": "s",
        "decision_kind": "priority",
        "hand": ["Lightning Strike", "Island"],
        "battlefield_self": [{"name": "Floodfarm Verge", "tapped": False}],
        "battlefield_opp": [{"name": "Burnout Bashtronaut", "tapped": False}],
        "menu": ["Cast Lightning Strike", "Pass"],
        "chosen": "Cast Lightning Strike",
        "mcts_counts": {},
    }
    row.update(over)
    return row


class TestBridgeOracleAttachment:
    def test_hand_oracle_in_prompt(self):
        rec, reason = BRIDGE.build_record(_row())
        assert reason == ""
        assert "Lightning Strike deals 3 damage to any target." in rec["user"]

    def test_board_oracle_in_prompt(self):
        rec, _ = BRIDGE.build_record(_row())
        # Non-basic land oracle text renders (#461 behavior)
        assert "Activate only if you control a Plains or an Island" in rec["user"]

    def test_basic_land_oracle_suppressed(self):
        rec, _ = BRIDGE.build_record(_row())
        # Basic Island: reminder-only oracle must NOT render as text
        assert "Add {U}.)" not in rec["user"].replace(
            "Activate only if you control a Plains or an Island.", ""
        )

    def test_no_targets_tag_stripped_and_counted(self):
        BRIDGE.ORACLE_STATS.clear()
        rec, _ = BRIDGE.build_record(_row())
        # Burnout Bashtronaut is untyped (MZ carries no type lines), so the
        # formatter cannot see opponent creatures and would emit a false
        # [NO TARGETS] on the removal spell. The sanitizer must remove it.
        assert "[NO TARGETS]" not in rec["user"]
        assert BRIDGE.ORACLE_STATS["no_targets_stripped"] >= 1

    def test_removal_tag_kept(self):
        # [RM:...] derives from the card's own oracle text — a true card fact.
        rec, _ = BRIDGE.build_record(_row())
        assert "[RM:<=3T]" in rec["user"]

    def test_opp_never_marked_attacking(self):
        """An is_attacking-marked opp card makes the formatter run block
        analysis on fabricated 0/0 stats — must never happen from MZ data."""
        row = _row(
            battlefield_opp=[{"name": "Burnout Bashtronaut", "tapped": True, "attacking": True}],
            phase="COMBAT_DECLARE_BLOCKERS",
        )
        gs = BRIDGE.build_game_state(row)
        opp = [c for c in gs["battlefield"] if c["owner_seat_id"] == BRIDGE.OPP_SEAT]
        assert all(not c.get("is_attacking") for c in opp)

    def test_local_attacking_marked(self):
        row = _row(battlefield_self=[{"name": "Skrelv, Defector Mite", "tapped": True, "attacking": True}])
        gs = BRIDGE.build_game_state(row)
        me = [c for c in gs["battlefield"] if c["owner_seat_id"] == BRIDGE.LOCAL_SEAT]
        assert me[0]["is_attacking"] is True

    def test_unresolved_name_renders_bare_and_counted(self):
        BRIDGE.ORACLE_STATS.clear()
        row = _row(hand=["Totally Invented Cardname", "Island"])
        rec, reason = BRIDGE.build_record(row)
        assert reason == ""
        assert "Totally Invented Cardname" in rec["user"]
        assert BRIDGE.ORACLE_STATS["oracle_unresolved"] >= 1


# ---------------------------------------------------------------------------
# Audit module
# ---------------------------------------------------------------------------


def _lookup(name):
    table = {
        "shockbolt": "Shockbolt deals 3 damage to any target.",
        "vanilla bear": "",
        "wind drake": "Flying",
        "verge land": "{T}: Add {W}.\n{T}: Add {U}. Activate only if you control a Plains.",
    }
    return table.get(name.lower())


class TestAuditClassification:
    def test_covered(self):
        prompt = ORACLE.normalize(
            "HAND:\n  Shockbolt {R} [S,OK]\n    Shockbolt deals 3 damage to any target."
        )
        assert ORACLE.classify_mention("Shockbolt", prompt, _lookup) == "covered"

    def test_uncovered(self):
        prompt = ORACLE.normalize("HAND:\n  Shockbolt {R} [S,OK]")
        assert ORACLE.classify_mention("Shockbolt", prompt, _lookup) == "uncovered"

    def test_unresolved(self):
        assert ORACLE.classify_mention("Unknown Card", "x", _lookup) == "unresolved"

    def test_no_text(self):
        assert ORACLE.classify_mention("Vanilla Bear", "x", _lookup) == "no_text"

    def test_keyword_only(self):
        assert ORACLE.classify_mention("Wind Drake", "x", _lookup) == "keyword_only"

    def test_symbol_dialects_match(self):
        """MTGA {oT} dialect vs Scryfall {T} must not break matching."""
        prompt = ORACLE.normalize(
            "  Verge Land  [S,LAND]\n    {oT}: Add {oW}.\n{oT}: Add {oU}. Activate only if you control a Plains."
        )
        assert ORACLE.classify_mention("Verge Land", prompt, _lookup) == "covered"


class TestAuditPromptParsing:
    PROMPT = (
        "TRIGGER: t\n\n=== GAME ===\n"
        "Legal: (pick by number)\n"
        "  1. Cast Shockbolt\n"
        "  2. Play Verge Land\n"
        "  3. Pass\n"
        "T3 YOUR | Main1 | Pri:You\n"
        "Life: You=20 Opp=20\n"
        "\nYOUR BOARD:\n"
        "  Vanilla Bear\n"
        "OPP BOARD:\n"
        "  Wind Drake [FLY]\n"
        "Atk: None (T/SS)\n"
        "\nHAND:\n"
        "  Shockbolt {R} [S,OK]\n"
        "    Shockbolt deals 3 damage to any target.\n"
        "  Verge Land  [S,LAND]\n"
        "\nRespond with ONLY a JSON action plan matching the schema.\n"
    )

    def test_contexts_and_classes(self):
        stats = ORACLE.new_stats()
        ORACLE.audit_prompt(self.PROMPT, _lookup, stats)
        assert stats["hand"]["covered"] == 1  # Shockbolt
        assert stats["hand"]["uncovered"] == 1  # Verge Land (no text attached)
        assert stats["your_board"]["no_text"] == 1  # Vanilla Bear
        assert stats["opp_board"]["keyword_only"] == 1  # Wind Drake
        # menu: Shockbolt covered (text present in prompt), Verge Land uncovered
        assert stats["menu"]["covered"] == 1
        assert stats["menu"]["uncovered"] == 1

    def test_summary_coverage(self):
        stats = ORACLE.new_stats()
        ORACLE.audit_prompt(self.PROMPT, _lookup, stats)
        summary = ORACLE.summarize(stats)
        # covered=2 (hand+menu Shockbolt), uncovered=2 -> 0.5
        assert summary["overall_coverage"] == 0.5

    def test_block_menu_two_names(self):
        names = ORACLE._menu_entry_names(
            "Block Ojer Axonil, Deepest Might with Malcolm, Alluring Scoundrel", {}
        )
        assert names == ["Ojer Axonil, Deepest Might", "Malcolm, Alluring Scoundrel"]

    def test_ability_text_menu_entry_is_not_a_gap(self):
        from collections import Counter

        unparsed = Counter()
        assert ORACLE._menu_entry_names("{T}: Draw a card, then discard a card.", unparsed) == []
        assert not unparsed


# ---------------------------------------------------------------------------
# Pipeline floor (fail closed)
# ---------------------------------------------------------------------------


def _fake_rendered(user: str):
    return {"train": [{"user": user, "system": "s", "response": "r", "meta": {}}]}


class TestPipelineFloor:
    BARE = (
        "Legal: (pick by number)\n  1. Cast Lightning Strike\n  2. Pass\nHAND:\n  Lightning Strike  [S,OK]\n"
    )
    COVERED = (
        "Legal: (pick by number)\n  1. Cast Lightning Strike\n  2. Pass\n"
        "HAND:\n  Lightning Strike  [S,OK]\n"
        "    Lightning Strike deals 3 damage to any target.\n"
    )

    def test_floor_off_by_default(self):
        summary = PIPE.stage_oracle_coverage(_fake_rendered(self.BARE), None)
        assert summary["overall_coverage"] == 0.0

    def test_floor_trips_below(self):
        with pytest.raises(SystemExit) as exc:
            PIPE.stage_oracle_coverage(_fake_rendered(self.BARE), 0.9)
        assert exc.value.code == 43

    def test_floor_passes_above(self):
        summary = PIPE.stage_oracle_coverage(_fake_rendered(self.COVERED), 0.9)
        assert summary["overall_coverage"] == 1.0

    def test_floor_trips_on_no_mentions(self):
        """No measurable mentions with a floor set is a failure, not a pass."""
        with pytest.raises(SystemExit) as exc:
            PIPE.stage_oracle_coverage(_fake_rendered("Legal: (pick by number)\n  1. Pass\n"), 0.5)
        assert exc.value.code == 43
