"""Unit tests for match-weighted win rate in run_pipeline._score_gating_trajectories.

The test fixtures are designed so that record-weighted (per-trajectory-row) and
match-weighted win rates differ, confirming the metric has been fixed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.run_pipeline import _score_gating_trajectories


def _write_trajectories(path: Path, rows: list[dict]) -> Path:
    """Write synthetic trajectory records as JSONL."""
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


# ---------------------------------------------------------------------------
# Fixture A: record-weighted ≠ match-weighted
# ---------------------------------------------------------------------------
# One long lost match (40 rows, winner="opp") + three short won matches
# (2 rows each, winner="local").
#
# Record-weighted:   6 wins / 46 rows  ≈ 13 %
# Match-weighted:    3 wins / 4  matches = 75 %
# ---------------------------------------------------------------------------

FIXTURE_A_LONG_LOST: list[dict] = [
    {"match_id": "match_lost", "seat": "local", "winner": "opp"} for _ in range(40)
]

FIXTURE_A_SHORT_WON: list[dict] = []
for i in range(3):
    FIXTURE_A_SHORT_WON.extend(
        [{"match_id": f"match_won_{i}", "seat": "local", "winner": "local"} for _ in range(2)]
    )

FIXTURE_A = FIXTURE_A_LONG_LOST + FIXTURE_A_SHORT_WON


def test_record_weighted_mismatch_match_weighted(tmp_path):
    """Match-weighted win rate differs from record-weighted (fixture A)."""
    path = _write_trajectories(tmp_path / "traj_a.jsonl", FIXTURE_A)
    scores = _score_gating_trajectories(path)

    # Match-weighted outcome
    assert scores["total_matches"] == 4, f"Expected 4 resolved matches, got {scores['total_matches']}"
    assert scores["challenger_wins"] == 3, f"Expected 3 challenger wins, got {scores['challenger_wins']}"
    assert scores["unresolved"] == 0, f"Expected 0 unresolved, got {scores['unresolved']}"
    assert scores["malformed"] == 0, f"Expected 0 malformed, got {scores['malformed']}"

    win_rate = scores["win_rate"]
    assert win_rate == 0.75, f"Expected match-weighted win rate 0.75, got {win_rate}"

    # Sanity: confirm it is NOT the record-weighted number (~0.13).
    rec_weighted = sum(1 for r in FIXTURE_A if r["winner"] == "local") / len(FIXTURE_A)
    assert abs(win_rate - rec_weighted) > 0.5, (
        f"Win rate {win_rate} suspiciously close to record-weighted "
        f"{rec_weighted:.4f} — dedup may not be working"
    )


# ---------------------------------------------------------------------------
# Fixture B: missing / unrecognised winners → unresolved counter
# ---------------------------------------------------------------------------
# 2 resolved matches + 2 matches with None winner + 1 with "draw"
# (an unrecognised value).
# ---------------------------------------------------------------------------

FIXTURE_B: list[dict] = [
    {"match_id": "won", "seat": "local", "winner": "local"},
    {"match_id": "won", "seat": "local", "winner": "local"},
    {"match_id": "lost", "seat": "opp", "winner": "opp"},
    {"match_id": "no_winner", "seat": "local", "winner": None},
    {"match_id": "no_winner", "seat": "opp", "winner": None},
    {"match_id": "draw_label", "seat": "local", "winner": "draw"},  # unrecognised
    {"match_id": "also_none", "seat": "local", "winner": None},
]


def test_unresolved_counter_with_missing_winners(tmp_path):
    """Matches with missing/unrecognised winners increment unresolved."""
    path = _write_trajectories(tmp_path / "traj_b.jsonl", FIXTURE_B)
    scores = _score_gating_trajectories(path)

    # Resolved: "won" (local wins) + "lost" (opp wins) = 2
    assert scores["total_matches"] == 2, f"Expected 2 resolved matches, got {scores['total_matches']}"
    assert scores["challenger_wins"] == 1, f"Expected 1 challenger win, got {scores['challenger_wins']}"
    assert scores["win_rate"] == 0.5, f"Expected win rate 0.5, got {scores['win_rate']}"

    # Unresolved: "no_winner" (None), "draw_label" (unrecognised), "also_none" = 3
    assert scores["unresolved"] == 3, f"Expected 3 unresolved matches, got {scores['unresolved']}"
    assert scores["malformed"] == 0


# ---------------------------------------------------------------------------
# Fixture C: contradictory winners within one match_id → unresolved
# ---------------------------------------------------------------------------
# Two matches: one clean win, one clean loss.  One match: "bad_data" has
# both "local" and "opp" winners across its rows (a data bug).
# ---------------------------------------------------------------------------

FIXTURE_C: list[dict] = [
    {"match_id": "good_win", "seat": "local", "winner": "local"},
    {"match_id": "good_win", "seat": "local", "winner": "local"},
    {"match_id": "good_loss", "seat": "opp", "winner": "opp"},
    {"match_id": "contradictory", "seat": "local", "winner": "local"},
    {"match_id": "contradictory", "seat": "opp", "winner": "opp"},  # same match, diff winner!
]


def test_contradictory_winners_counted_as_unresolved(tmp_path):
    """A match whose rows disagree on winner is a data bug → unresolved."""
    path = _write_trajectories(tmp_path / "traj_c.jsonl", FIXTURE_C)
    scores = _score_gating_trajectories(path)

    # Resolved: "good_win" + "good_loss" = 2
    assert scores["total_matches"] == 2, f"Expected 2 resolved, got {scores['total_matches']}"
    assert scores["challenger_wins"] == 1
    assert scores["win_rate"] == 0.5

    # Unresolved: "contradictory" = 1
    assert scores["unresolved"] == 1, f"Expected 1 unresolved (contradictory), got {scores['unresolved']}"
    assert scores["malformed"] == 0


# ---------------------------------------------------------------------------
# Fixture D: empty file vs nonexistent file
# ---------------------------------------------------------------------------


def test_empty_trajectories_file(tmp_path):
    """An empty file yields zero matches and zero unresolved."""
    path = _write_trajectories(tmp_path / "empty.jsonl", [])
    scores = _score_gating_trajectories(path)
    assert scores["total_matches"] == 0
    assert scores["challenger_wins"] == 0
    assert scores["unresolved"] == 0
    assert scores["win_rate"] == 0.0
    assert scores["malformed"] == 0


def test_nonexistent_trajectories_file(tmp_path):
    """A non-existent file yields zero matches (without crashing)."""
    path = tmp_path / "does_not_exist.jsonl"
    scores = _score_gating_trajectories(path)
    assert scores["total_matches"] == 0
    assert scores["unresolved"] == 0
    assert scores["win_rate"] == 0.0
    assert scores["malformed"] == 0


# ---------------------------------------------------------------------------
# Fixture E: malformed JSON lines
# ---------------------------------------------------------------------------

FIXTURE_E_CONTENT = (
    b'{"match_id": "m1", "winner": "local"}\nnot-json\n{"match_id": "m2", "winner": "opp"}\ncorrupt\n'
)


def test_malformed_lines_counted(tmp_path):
    """Malformed JSON lines are counted but do not affect win rate."""
    path = tmp_path / "malformed.jsonl"
    with open(path, "wb") as f:
        f.write(FIXTURE_E_CONTENT)
    scores = _score_gating_trajectories(path)
    assert scores["total_matches"] == 2, f"Expected 2 resolved, got {scores['total_matches']}"
    assert scores["challenger_wins"] == 1
    assert scores["malformed"] == 2, f"Expected 2 malformed, got {scores['malformed']}"
