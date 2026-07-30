"""Tests for tools/training/magezero_filters.py.

Every function is tested with synthetic fixtures — no external data required.
Covers normal operation, edge cases, tripwire firing, and split determinism.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training import magezero_filters as mf

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_row(overrides: dict | None = None) -> dict:
    """Build a minimal valid decision row with sensible defaults."""
    base = {
        "game_id": "log_a:1:3",
        "turn": 4,
        "phase": "PRECOMBAT_MAIN",
        "active_life": 18,
        "opp_life": 18,
        "hand": ["Forest"],
        "battlefield_self": [{"name": "Elf", "tapped": False}],
        "battlefield_opp": [{"name": "Mountain", "tapped": True}],
        "menu": ["Play Forest", "Attack with Elf", "Pass"],
        "chosen": "Play Forest",
        "mcts_counts": {"Pass": 30, "Play Forest": 609, "Attack with Elf": 200},
        "actor": "PlayerA",
        "outcome": "won",
        "session": "session21",
        "decision_kind": "priority",
    }
    if overrides:
        base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# drop_single_option
# ---------------------------------------------------------------------------


class TestDropSingleOption:
    def test_keeps_multi_option(self):
        rows = [_make_row(), _make_row({"menu": ["A", "B"]})]
        kept, dropped = mf.drop_single_option(rows)
        assert dropped == 0
        assert len(kept) == 2

    def test_drops_single_option(self):
        rows = [_make_row({"menu": ["Pass"]}), _make_row({"menu": ["Keep"]})]
        kept, dropped = mf.drop_single_option(rows)
        assert dropped == 2
        assert len(kept) == 0

    def test_mixed(self):
        rows = [
            _make_row({"menu": ["Pass"]}),
            _make_row({"menu": ["A", "B"]}),
            _make_row({"menu": ["Only"]}),
            _make_row({"menu": ["X", "Y", "Z"]}),
        ]
        kept, dropped = mf.drop_single_option(rows)
        assert dropped == 2
        assert len(kept) == 2

    def test_empty_menu_counts_as_single_option(self):
        rows = [_make_row({"menu": []})]
        kept, dropped = mf.drop_single_option(rows)
        assert dropped == 1
        assert len(kept) == 0

    def test_missing_menu_defaults_empty(self):
        rows = [{"game_id": "x"}, {"game_id": "y", "menu": ["A", "B"]}]
        kept, dropped = mf.drop_single_option(rows)
        assert dropped == 1
        assert len(kept) == 1

    def test_empty_input(self):
        kept, dropped = mf.drop_single_option([])
        assert dropped == 0
        assert kept == []


# ---------------------------------------------------------------------------
# outcome_filter
# ---------------------------------------------------------------------------


class TestOutcomeFilter:
    def test_won_only_keeps_won(self):
        rows = [
            _make_row({"outcome": "won"}),
            _make_row({"outcome": "lost"}),
            _make_row({"outcome": "won"}),
        ]
        kept, dropped = mf.outcome_filter(rows, "won_only")
        assert dropped == 1
        assert len(kept) == 2
        assert all(r["outcome"] == "won" for r in kept)

    def test_won_only_drops_unknown(self):
        rows = [
            _make_row({"outcome": "won"}),
            _make_row({"outcome": "unknown"}),
        ]
        kept, dropped = mf.outcome_filter(rows, "won_only")
        assert dropped == 1
        assert len(kept) == 1

    def test_all_preserves_everything(self):
        rows = [
            _make_row({"outcome": "won"}),
            _make_row({"outcome": "lost"}),
            _make_row({"outcome": "unknown"}),
        ]
        kept, dropped = mf.outcome_filter(rows, "all")
        assert dropped == 0
        assert len(kept) == 3

    def test_all_returns_copy(self):
        rows = [_make_row()]
        kept, dropped = mf.outcome_filter(rows, "all")
        assert dropped == 0
        assert kept is not rows  # should be a shallow copy

    def test_empty_input(self):
        kept, dropped = mf.outcome_filter([], "won_only")
        assert dropped == 0
        assert kept == []

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Unknown outcome filter mode"):
            mf.outcome_filter([_make_row()], "bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_identical_rows_deduplicated(self):
        r = _make_row()
        rows = [r, r, r]
        kept, dropped = mf.dedupe(rows)
        assert dropped == 2
        assert len(kept) == 1

    def test_different_chosen_is_different(self):
        r1 = _make_row({"chosen": "Play Forest"})
        r2 = _make_row({"chosen": "Pass"})
        kept, dropped = mf.dedupe([r1, r2])
        assert dropped == 0
        assert len(kept) == 2

    def test_different_menu_is_different(self):
        r1 = _make_row({"menu": ["A", "B"]})
        r2 = _make_row({"menu": ["A", "B", "C"]})
        kept, dropped = mf.dedupe([r1, r2])
        assert dropped == 0
        assert len(kept) == 2

    def test_different_battlefield_tapped_status_is_different(self):
        r1 = _make_row({"battlefield_self": [{"name": "Elf", "tapped": False}]})
        r2 = _make_row({"battlefield_self": [{"name": "Elf", "tapped": True}]})
        kept, dropped = mf.dedupe([r1, r2])
        assert dropped == 0
        assert len(kept) == 2

    def test_battlefield_order_independent(self):
        r1 = _make_row(
            {
                "battlefield_self": [
                    {"name": "Elf", "tapped": False},
                    {"name": "Goblin", "tapped": True},
                ],
            }
        )
        r2 = _make_row(
            {
                "battlefield_self": [
                    {"name": "Goblin", "tapped": True},
                    {"name": "Elf", "tapped": False},
                ],
            }
        )
        kept, dropped = mf.dedupe([r1, r2])
        assert dropped == 1
        assert len(kept) == 1

    def test_preserves_first_occurrence(self):
        rows = [
            _make_row({"chosen": "Pass", "mcts_counts": {"Pass": 100}}),
            _make_row({"chosen": "Pass", "mcts_counts": {"Pass": 200}}),
        ]
        kept, dropped = mf.dedupe(rows)
        assert dropped == 1
        assert kept[0]["mcts_counts"]["Pass"] == 100

    def test_empty_input(self):
        kept, dropped = mf.dedupe([])
        assert dropped == 0
        assert kept == []

    def test_hand_order_matters(self):
        """Hand is a tuple, so order IS significant (as in the MTGA client)."""
        r1 = _make_row({"hand": ["Forest", "Mountain"]})
        r2 = _make_row({"hand": ["Mountain", "Forest"]})
        kept, dropped = mf.dedupe([r1, r2])
        assert dropped == 0
        assert len(kept) == 2


# ---------------------------------------------------------------------------
# pass_rate_tripwire
# ---------------------------------------------------------------------------


class TestPassRateTripwire:
    def test_below_threshold_passes(self):
        rows = [
            _make_row({"chosen": "Play Forest"}),
            _make_row({"chosen": "Pass"}),
            _make_row({"chosen": "Attack"}),
        ]
        # 1/3 ≈ 33 % < 40 %
        mf.pass_rate_tripwire(rows, max_frac=0.40)  # should not raise

    def test_at_threshold_passes(self):
        rows = [
            _make_row({"chosen": "Pass"}),
            _make_row({"chosen": "Pass"}),
            _make_row({"chosen": "Play Forest"}),
            _make_row({"chosen": "Play Forest"}),
            _make_row({"chosen": "Attack"}),
        ]
        mf.pass_rate_tripwire(rows, max_frac=0.40)

    def test_above_threshold_raises_systemexit(self):
        rows = [
            _make_row({"chosen": "Pass"}),
            _make_row({"chosen": "Pass"}),
            _make_row({"chosen": "Pass"}),
            _make_row({"chosen": "Play Forest"}),
            _make_row({"chosen": "Attack"}),
        ]
        # 3/5 = 60 % > 40 %
        with pytest.raises(SystemExit) as exc:
            mf.pass_rate_tripwire(rows, max_frac=0.40)
        assert exc.value.code == 42

    def test_all_pass_exits(self):
        rows = [_make_row({"chosen": "Pass"}), _make_row({"chosen": "Pass"})]
        with pytest.raises(SystemExit) as exc:
            mf.pass_rate_tripwire(rows, max_frac=0.40)
        assert exc.value.code == 42

    def test_empty_input_does_not_raise(self):
        mf.pass_rate_tripwire([], max_frac=0.40)


# ---------------------------------------------------------------------------
# split_by_game
# ---------------------------------------------------------------------------


class TestSplitByGame:
    def test_two_games_get_separate_splits(self):
        rows = [
            _make_row({"game_id": "game_a:1:1", "turn": 1}),
            _make_row({"game_id": "game_a:1:2", "turn": 2}),
            _make_row({"game_id": "game_b:1:1", "turn": 1}),
            _make_row({"game_id": "game_c:1:1", "turn": 1}),
            _make_row({"game_id": "game_c:1:2", "turn": 2}),
        ]
        splits = mf.split_by_game(rows, seed=7)
        assert sum(len(v) for v in splits.values()) == 5
        # All rows of each game should be in the same split
        for group in splits.values():
            gids = {r["game_id"] for r in group}
            for gid in gids:
                # Every other row with the same base game prefix is also here
                prefix = gid.rsplit(":", 1)[0]
                siblings = {r["game_id"] for r in group if r["game_id"].startswith(prefix)}
                assert siblings == {r["game_id"] for r in rows if r["game_id"].startswith(prefix)} & gids

    def test_deterministic_same_seed(self):
        rows = [_make_row({"game_id": f"g{i}:1:1"}) for i in range(100)]
        a = mf.split_by_game(rows, seed=7)
        b = mf.split_by_game(rows, seed=7)
        for name in ("train", "val", "test"):
            assert len(a.get(name, [])) == len(b.get(name, []))
            # Contents should be identical in order
            assert [r["game_id"] for r in a.get(name, [])] == [r["game_id"] for r in b.get(name, [])]

    def test_different_seed_different_splits(self):
        rows = [_make_row({"game_id": f"g{i}:1:1"}) for i in range(100)]
        a = mf.split_by_game(rows, seed=7)
        b = mf.split_by_game(rows, seed=42)
        # Same total count
        assert sum(len(v) for v in a.values()) == sum(len(v) for v in b.values())
        # But likely different assignments — at least one split size differs
        # (the chance two seeds give the exact same sizes for 100 games is negligible)
        a_counts = tuple(len(a.get(n, [])) for n in ("train", "val", "test"))
        b_counts = tuple(len(b.get(n, [])) for n in ("train", "val", "test"))

        # These COULD coincidentally match (rare), so we just check the overall
        # game->split mapping differs for at least one game
        def _game_to_split(splits):
            mapping = {}
            for name, group in splits.items():
                for r in group:
                    mapping[r["game_id"]] = name
            return mapping

        assert _game_to_split(a) != _game_to_split(b)

    def test_default_fracs_sum_to_one(self):
        rows = [_make_row({"game_id": f"g{i}:1:1"}) for i in range(100)]
        splits = mf.split_by_game(rows)
        total = sum(len(v) for v in splits.values())
        assert total == 100

    def test_invalid_fracs_raises(self):
        rows = [_make_row()]
        with pytest.raises(ValueError, match="fracs must sum to 1.0"):
            mf.split_by_game(rows, fracs=(0.5, 0.3, 0.3))

    def test_without_val_or_test(self):
        rows = [_make_row({"game_id": "g:1:1"})]
        splits = mf.split_by_game(rows, fracs=(1.0, 0.0, 0.0))
        assert "train" in splits
        assert "val" not in splits or len(splits["val"]) == 0
        assert "test" not in splits or len(splits["test"]) == 0
        assert len(splits["train"]) == 1

    def test_no_leakage(self):
        """No game_id appears in more than one split."""
        rows = [_make_row({"game_id": f"g{i}:1:{j}"}) for i in range(10) for j in range(3)]
        splits = mf.split_by_game(rows, seed=7)
        seen_games: dict[str, str] = {}
        for name, group in splits.items():
            for r in group:
                gid = mf._game_id(r)
                if gid in seen_games:
                    assert seen_games[gid] == name, f"Game {gid} leaked from {seen_games[gid]} to {name}"
                seen_games[gid] = name


# ---------------------------------------------------------------------------
# write_manifest
# ---------------------------------------------------------------------------


class TestWriteManifest:
    def test_manifest_has_required_keys(self):
        rows = [_make_row() for _ in range(5)]
        splits = {"train": rows[:3], "val": rows[3:4], "test": rows[4:]}

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            mf.write_manifest(splits, manifest_path, filter_counts={"drop_single_option": 2})

            with open(manifest_path) as f:
                manifest = json.load(f)

        assert "version" in manifest
        assert "git_sha" in manifest
        assert "total_rows" in manifest
        assert manifest["total_rows"] == 5
        assert "splits" in manifest
        assert set(manifest["splits"].keys()) == {"train", "val", "test"}
        assert manifest["filter_counts"] == {"drop_single_option": 2}

    def test_manifest_split_counts(self):
        win_rows = [_make_row({"outcome": "won"}) for _ in range(3)]
        lost_rows = [_make_row({"outcome": "lost"}) for _ in range(2)]
        splits = {"train": win_rows + lost_rows}

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            mf.write_manifest(splits, manifest_path)

            with open(manifest_path) as f:
                manifest = json.load(f)

        train_info = manifest["splits"]["train"]
        assert train_info["rows"] == 5
        assert train_info["by_outcome"]["won"] == 3
        assert train_info["by_outcome"]["lost"] == 2

    def test_manifest_sha256_is_hex(self):
        splits = {"train": [_make_row()]}

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            mf.write_manifest(splits, manifest_path)

            with open(manifest_path) as f:
                manifest = json.load(f)

        sha = manifest["splits"]["train"]["sha256"]
        assert len(sha) == 64
        int(sha, 16)  # should not raise

    def test_manifest_git_sha(self):
        splits = {"train": [_make_row()]}
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            mf.write_manifest(splits, manifest_path)
            with open(manifest_path) as f:
                manifest = json.load(f)
        # Should either be a valid git SHA or "unknown" (CI)
        sha = manifest["git_sha"]
        assert sha == "unknown" or (len(sha) == 40 and all(c in "0123456789abcdef" for c in sha))

    def test_writes_jsonl_files(self):
        splits = {"train": [_make_row({"game_id": "g:1:1"})] * 3}

        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            mf.write_manifest(splits, manifest_path)

            # Check JSONL was written
            train_path = Path(tmp) / "train.jsonl"
            assert train_path.exists()
            lines = train_path.read_text().strip().split("\n")
            assert len(lines) == 3


# ---------------------------------------------------------------------------
# Full CLI integration
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_pipeline_e2e(self):
        """End-to-end: write a JSONL fixture, run the CLI, check outputs."""
        rows = []
        for i in range(50):
            outcome = "won" if i < 40 else "lost"
            chosen = "Play Forest"  # never Pass so tripwire doesn't fire
            rows.append(
                _make_row(
                    {
                        "game_id": f"game{i:03d}:1:1",
                        "outcome": outcome,
                        "chosen": chosen,
                        "menu": ["Play Forest", "Attack", "Pass"],
                        "hand": [f"Card_{i}"],  # unique hand to avoid dedupe collapse
                    }
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "input.jsonl"
            with open(input_path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")

            outdir = Path(tmp) / "wp3_out"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "tools/training/magezero_filters.py"),
                    "--in",
                    str(input_path),
                    "--outdir",
                    str(outdir),
                    "--mode",
                    "won_only",
                ],
                capture_output=True,
                text=True,
            )

            assert result.returncode == 0, f"CLI failed:\nstdout:{result.stdout}\nstderr:{result.stderr}"

            # Check outputs
            assert (outdir / "manifest.json").exists()
            assert (outdir / "train.jsonl").exists()

            # Verify manifest is valid JSON
            with open(outdir / "manifest.json") as f:
                manifest = json.load(f)
            assert manifest["total_rows"] > 0
            assert "filter_counts" in manifest
            assert "drop_single_option" in manifest["filter_counts"]

    def test_cli_tripwire_rejects_high_pass_rate(self):
        """Rows where most decisions are Pass should trip the CLI."""
        rows = []
        for i in range(20):
            rows.append(
                _make_row(
                    {
                        "game_id": f"game{i:03d}:1:1",
                        "chosen": "Play Forest" if i < 7 else "Pass",
                        "menu": ["Play Forest", "Attack", "Pass"],
                        "outcome": "won",
                    }
                )
            )

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "high_pass.jsonl"
            with open(input_path, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")

            outdir = Path(tmp) / "wp3_out"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "tools/training/magezero_filters.py"),
                    "--in",
                    str(input_path),
                    "--outdir",
                    str(outdir),
                    "--mode",
                    "won_only",
                ],
                capture_output=True,
                text=True,
            )

            assert result.returncode == 42
            assert "TRIPWIRE TRIPPED" in result.stderr


# ---------------------------------------------------------------------------
# Schema / invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    """Contract tests for the shared decisions JSONL schema."""

    REQUIRED_FIELDS = [
        "game_id",
        "turn",
        "phase",
        "active_life",
        "opp_life",
        "hand",
        "battlefield_self",
        "battlefield_opp",
        "menu",
        "chosen",
        "mcts_counts",
        "actor",
        "outcome",
        "session",
        "decision_kind",
    ]

    def test_make_row_has_all_required_fields(self):
        r = _make_row()
        for field in self.REQUIRED_FIELDS:
            assert field in r, f"Required field {field!r} missing from _make_row()"

    def test_drop_single_option_preserves_fields(self):
        r = _make_row({"menu": ["A", "B"]})
        kept, _ = mf.drop_single_option([r])
        assert kept[0] == r

    def test_deterministic_split_across_runs(self):
        """Same seed must produce identical split assignments across two independent calls."""
        rows = [_make_row({"game_id": f"g{i:04d}:1:1"}) for i in range(50)]
        a = mf.split_by_game(rows, seed=42)
        b = mf.split_by_game(rows, seed=42)
        for name in ("train", "val", "test"):
            a_ids = [r["game_id"] for r in a.get(name, [])]
            b_ids = [r["game_id"] for r in b.get(name, [])]
            assert a_ids == b_ids, f"Split {name} differs between runs"

    def test_filter_composition_pipeline(self):
        """Pipeline: drop_single_option -> outcome_filter -> dedupe -> split_by_game
        produces a valid result with no errors."""
        rows = []
        for i in range(30):
            outcome = "won" if i < 24 else "lost"
            menu = ["Only"] if i == 29 else [f"A{i}", f"B{i}", f"C{i}"]
            rows.append(
                _make_row(
                    {
                        "game_id": f"game_{i % 10:03d}:1:1",
                        "outcome": outcome,
                        "menu": menu,
                        "chosen": f"A{i}",
                        "hand": [f"Card_{i}"],  # unique hand avoids dedupe collapse
                    }
                )
            )

        # Run full pipeline
        rows, n1 = mf.drop_single_option(rows)
        rows, n2 = mf.outcome_filter(rows, "won_only")
        rows, n3 = mf.dedupe(rows)
        mf.pass_rate_tripwire(rows, max_frac=0.40)
        splits = mf.split_by_game(rows, seed=7)

        total = sum(len(v) for v in splits.values())
        assert total == len(rows)
        assert n1 >= 0
        assert n2 >= 0
        assert n3 >= 0
        assert set(splits.keys()).issubset({"train", "val", "test"})
