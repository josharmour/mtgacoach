"""Tests for Task 12: Freeze corpus lineage and split membership."""

import json
from pathlib import Path
import tempfile
import pytest

from tools.training import magezero_filters as FILTERS
from tools.training import run_wp3_pipeline as PIPELINE


def _make_row(overrides: dict | None = None) -> dict:
    base = {
        "game_id": "game_default:1:1",
        "turn": 1,
        "phase": "M1",
        "active_life": 20,
        "opp_life": 20,
        "hand": ["Island"],
        "battlefield_self": [],
        "battlefield_opp": [],
        "menu": ["Pass", "Play Island"],
        "chosen": "Play Island",
        "mcts_counts": {"Pass": 10, "Play Island": 90},
        "actor": "PlayerA",
        "outcome": "won",
        "decision_kind": "priority",
    }
    if overrides:
        base.update(overrides)
    return base


def test_adding_games_preserves_existing_split_assignments():
    """Acceptance 1: Appending new games does not change split membership of existing games."""
    initial_games = [_make_row({"game_id": f"game_{i:04d}:1:1"}) for i in range(100)]
    initial_splits = FILTERS.split_by_game(initial_games, seed=42)

    initial_assignment: dict[str, str] = {}
    for sname, rows in initial_splits.items():
        for r in rows:
            initial_assignment[r["game_id"]] = sname

    assert len(initial_assignment) == 100

    # Add 50 new games, some interleaved alphabetically
    new_games = [_make_row({"game_id": f"game_extra_{i:04d}:1:1"}) for i in range(50)]
    combined = initial_games + new_games

    combined_splits = FILTERS.split_by_game(combined, seed=42)
    combined_assignment: dict[str, str] = {}
    for sname, rows in combined_splits.items():
        for r in rows:
            combined_assignment[r["game_id"]] = sname

    assert len(combined_assignment) == 150

    # Check 100% stability of the original 100 games
    for gid, original_split in initial_assignment.items():
        assert combined_assignment[gid] == original_split, (
            f"Game {gid} moved from {original_split} to {combined_assignment[gid]} after adding games!"
        )


def test_all_decisions_and_kinds_of_one_game_stay_together():
    """Acceptance 2: Priority and combat decisions of the same game must land in the same split."""
    gid = "log_alpha.log:Thread-1:42"
    rows = [
        _make_row({"game_id": gid, "decision_kind": "priority", "turn": 1}),
        _make_row({"game_id": gid, "decision_kind": "priority", "turn": 2}),
        _make_row({"game_id": gid, "decision_kind": "attack_commit", "turn": 3}),
        _make_row({"game_id": gid, "decision_kind": "block_assign", "turn": 3}),
    ]
    # Add other filler games to form a multi-split corpus
    filler = [_make_row({"game_id": f"filler_{i:03d}:1:1"}) for i in range(60)]
    splits = FILTERS.split_by_game(rows + filler, seed=7)

    assigned_splits = set()
    for sname, srows in splits.items():
        if any(r["game_id"] == gid for r in srows):
            assigned_splits.add(sname)

    assert len(assigned_splits) == 1, f"Game {gid} was split across multiple splits: {assigned_splits}"


def test_renamed_path_does_not_leak_across_splits():
    """Acceptance 3: Renaming a source log file preserves canonical game ID and split assignment."""
    source_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    row_orig = _make_row({
        "game_id": "match_2026_09_01.log:Thread-1:001",
        "source_hash": source_sha,
    })
    row_renamed = _make_row({
        "game_id": "archive_renamed_copy.log:Thread-1:001",
        "source_hash": source_sha,
    })

    canon_orig = FILTERS.canonical_game_id(row_orig)
    canon_renamed = FILTERS.canonical_game_id(row_renamed)

    assert canon_orig == canon_renamed
    assert canon_orig == f"source:{source_sha[:16]}:Thread-1:001"

    # Split assignments must be identical
    split_orig = FILTERS.assign_game_split(canon_orig, seed=7)
    split_renamed = FILTERS.assign_game_split(canon_renamed, seed=7)
    assert split_orig == split_renamed


def test_missing_game_id_quarantined_and_counted():
    """Acceptance 4: Missing game IDs are quarantined and never given a session:turn pseudo-key."""
    valid_row = _make_row({"game_id": "valid_game:1:1"})
    missing_row_empty = _make_row({"game_id": "", "session": "sess1", "turn": 5})
    missing_row_none = _make_row({"game_id": None, "session": "sess2", "turn": 8})
    del missing_row_none["game_id"]

    assert FILTERS.canonical_game_id(missing_row_empty) == ""
    assert FILTERS.canonical_game_id(missing_row_none) == ""

    splits = FILTERS.split_by_game([valid_row, missing_row_empty, missing_row_none], seed=7)

    assert "quarantine" in splits
    assert len(splits["quarantine"]) == 2
    # Ensure neither missing row ended up in train/val/test
    for sname in ("train", "val", "test"):
        if sname in splits:
            for r in splits[sname]:
                assert r["game_id"] == "valid_game:1:1"


def test_manifest_lineage_and_provenance_recording():
    """Acceptance 5: Manifest records complete lineage, dirty source hashes, and teacher checkpoint."""
    with tempfile.TemporaryDirectory() as tmpdir:
        outdir = Path(tmpdir)
        splits = {
            "train": [_make_row({"game_id": "g1:1:1"})],
            "val": [_make_row({"game_id": "g2:1:1"})],
        }
        rendered = {
            "train": [{"user": "u", "system": "s", "response": "r", "meta": {}}],
            "val": [{"user": "u", "system": "s", "response": "r", "meta": {}}],
        }

        manifest = PIPELINE.stage_write(
            rendered=rendered,
            outdir=outdir,
            filter_counts={"drop": 0},
            splits=splits,
            raw_count=2,
            pass_rate=0.0,
            attackers_blockers_total=0,
            elapsed=1.0,
            teacher_checkpoint=None,  # unknown
            split_method="hash",
            split_migration=False,
        )

        assert "lineage" in manifest
        lineage = manifest["lineage"]
        assert lineage["schema_version"] == "wp3-v2"
        assert "dirty_source_hashes" in lineage
        assert "tools/training/run_wp3_pipeline.py" in lineage["dirty_source_hashes"]
        assert "tools/training/magezero_filters.py" in lineage["dirty_source_hashes"]

        # Unknown teacher provenance recorded explicitly
        assert lineage["teacher_checkpoint"]["identity"] == "unknown"
        assert lineage["teacher_checkpoint"]["provenance"] == "teacher_unknown"

        # Split config and game_splits map
        assert manifest["split_config"]["method"] == "hash"
        assert "game_splits" in manifest
        assert manifest["game_splits"]["g1:1:1"] == "train"
        assert manifest["game_splits"]["g2:1:1"] == "val"


def test_split_migration_and_split_from():
    """Acceptance 6: Existing manifests can be inherited with split_from / migration."""
    with tempfile.TemporaryDirectory() as tmpdir:
        prior_manifest_path = Path(tmpdir) / "prior_manifest.json"
        prior_manifest = {
            "game_splits": {
                "special_game:1:1": "test",
                "special_game:1:2": "train",
            }
        }
        prior_manifest_path.write_text(json.dumps(prior_manifest), encoding="utf-8")

        rows = [
            _make_row({"game_id": "special_game:1:1"}),
            _make_row({"game_id": "special_game:1:2"}),
            _make_row({"game_id": "new_game:1:1"}),
        ]

        splits = FILTERS.split_by_game(
            rows,
            seed=7,
            split_from=prior_manifest["game_splits"],
        )

        # special_game:1:1 must inherit 'test'
        test_gids = [r["game_id"] for r in splits.get("test", [])]
        assert "special_game:1:1" in test_gids

        # special_game:1:2 must inherit 'train'
        train_gids = [r["game_id"] for r in splits.get("train", [])]
        assert "special_game:1:2" in train_gids
