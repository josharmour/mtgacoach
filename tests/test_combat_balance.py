#!/usr/bin/env python3
"""Tests for class-histogram guard and downsample rebalance in build_magezero_combat.

Run:
    python3 -m pytest tests/test_combat_balance.py -v
"""

from __future__ import annotations

import copy
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
for _p in (str(SRC), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.training.build_magezero_combat import (
    _check_class_share,
    _class_histogram,
    _downsample,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def skewed_block_records() -> list[dict]:
    """78 block records: 56 no_block (71.8 %), 17 partial_block, 5 block_all."""
    recs: list[dict] = []
    for i in range(78):
        if i < 56:
            cls = "no_block"
        elif i < 73:
            cls = "partial_block"
        else:
            cls = "block_all"
        recs.append(
            {
                "id": f"mzc-skewed-{i:04d}",
                "kind": "combat_block",
                "meta": {"block_class": cls},
                "user": "",
                "response": "{}",
            }
        )
    return recs


@pytest.fixture
def balanced_block_records() -> list[dict]:
    """78 block records perfectly split: 26 each."""
    recs: list[dict] = []
    for i in range(78):
        if i < 26:
            cls = "no_block"
        elif i < 52:
            cls = "partial_block"
        else:
            cls = "block_all"
        recs.append(
            {
                "id": f"mzc-balanced-{i:04d}",
                "kind": "combat_block",
                "meta": {"block_class": cls},
                "user": "",
                "response": "{}",
            }
        )
    return recs


@pytest.fixture
def mixed_records(skewed_block_records) -> list[dict]:
    """Add 104 attack records (98 proper_subset + 6 all_in) to the block fixture."""
    recs = list(skewed_block_records)
    for i in range(104):
        cls = "proper_subset" if i < 98 else "all_in"
        recs.append(
            {
                "id": f"mzc-mix-atk-{i:04d}",
                "kind": "combat_attack",
                "meta": {"attack_class": cls},
                "user": "",
                "response": "{}",
            }
        )
    return recs


# ── Guard fires on skewed fixture ──────────────────────────────────────────


def test_guard_fires_on_skewed_block(skewed_block_records) -> None:
    """_check_class_share raises SystemExit for block class > 0.50."""
    hist = _class_histogram(
        skewed_block_records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )
    with pytest.raises(SystemExit) as exc:
        _check_class_share(hist, 0.50, "block")
    msg = str(exc.value)
    assert "no_block" in msg
    assert "71.8%" in msg or "71.79%" in msg or "71.8" in msg


def test_guard_fires_on_skewed_attack(mixed_records) -> None:
    """_check_class_share raises SystemExit for attack class > 0.50."""
    hist = _class_histogram(
        mixed_records,
        "attack_class",
        lambda r: r.get("kind") == "combat_attack",
    )
    with pytest.raises(SystemExit) as exc:
        _check_class_share(hist, 0.50, "attack")
    msg = str(exc.value)
    assert "proper_subset" in msg
    assert "FAIL-CLOSED" in msg


def test_guard_passes_on_balanced(balanced_block_records) -> None:
    """_check_class_share does NOT raise for balanced distributions."""
    hist = _class_histogram(
        balanced_block_records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )
    # Each class is 33.3% — well under 0.50, so no SystemExit
    _check_class_share(hist, 0.50, "block")
    # If we get here without exception, the test passes


# ── Histogram correctness ──────────────────────────────────────────────────


def test_histogram_shares_sum_to_one(skewed_block_records) -> None:
    """Shares in the histogram should sum to 1.0 (within rounding)."""
    hist = _class_histogram(
        skewed_block_records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )
    total_share = sum(share for _, _, share in hist)
    assert abs(total_share - 1.0) < 0.01, f"shares sum to {total_share}"


def test_histogram_sorted_descending(skewed_block_records) -> None:
    """Histogram items sorted by share descending."""
    hist = _class_histogram(
        skewed_block_records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )
    shares = [s for _, _, s in hist]
    assert shares == sorted(shares, reverse=True), f"histogram not sorted descending: {shares}"


# ── Downsample determinism and correctness ─────────────────────────────────


def test_downsample_deterministic(mixed_records) -> None:
    """Two downsample runs with the same seed produce the same kept record ids."""
    hist_a = _class_histogram(
        mixed_records,
        "attack_class",
        lambda r: r.get("kind") == "combat_attack",
    )
    hist_b = _class_histogram(
        mixed_records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )

    r1 = _downsample(copy.deepcopy(mixed_records), hist_a, hist_b, 0.50)
    r2 = _downsample(copy.deepcopy(mixed_records), hist_a, hist_b, 0.50)

    ids1 = sorted(r["id"] for r in r1)
    ids2 = sorted(r["id"] for r in r2)
    assert ids1 == ids2, "downsample is not deterministic"


def test_downsample_reduces_majority(mixed_records) -> None:
    """After downsampling, no class exceeds 0.50 share."""
    hist_a = _class_histogram(
        mixed_records,
        "attack_class",
        lambda r: r.get("kind") == "combat_attack",
    )
    hist_b = _class_histogram(
        mixed_records,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )

    balanced = _downsample(copy.deepcopy(mixed_records), hist_a, hist_b, 0.50)

    for kind_field, meta_field in [
        ("combat_attack", "attack_class"),
        ("combat_block", "block_class"),
    ]:
        subset = [r for r in balanced if r.get("kind") == kind_field]
        assert len(subset) > 0, f"no {kind_field} records after downsample"
        total = len(subset)
        counts = Counter(r["meta"][meta_field] for r in subset)
        for cls, n in counts.items():
            share = n / total
            assert share <= 0.50 + 0.01, f"{cls} in {kind_field} is {share:.1%} after downsample"


def test_downsample_clamps_below_one() -> None:
    """Downsample with a tiny minority class still works (no crash)."""
    recs: list[dict] = []
    for i in range(10):
        cls = "no_block" if i < 9 else "partial_block"
        recs.append(
            {
                "id": f"mzc-clamp-{i:04d}",
                "kind": "combat_block",
                "meta": {"block_class": cls},
                "user": "",
                "response": "{}",
            }
        )
    hist = _class_histogram(
        recs,
        "block_class",
        lambda r: r.get("kind") == "combat_block",
    )
    result = _downsample(recs, [], hist, 0.50)
    # partial_block has 1 record — must still be present
    partial_ids = [r["id"] for r in result if r["meta"].get("block_class") == "partial_block"]
    assert len(partial_ids) == 1, "minority class was lost"
    no_block_ids = [r["id"] for r in result if r["meta"].get("block_class") == "no_block"]
    # no_block should be downsampled from 9 toward 1 (can't go below 1)
    assert len(no_block_ids) >= 1
    assert len(no_block_ids) < 9, "majority class was not reduced"


# ── Empty / edge cases ─────────────────────────────────────────────────────


def test_empty_histogram_does_not_raise() -> None:
    """_check_class_share on an empty histogram is a no-op."""
    _check_class_share([], 0.50, "attack")  # should not raise


def test_empty_records_downsample() -> None:
    """Downsample on an empty list returns an empty list."""
    result = _downsample([], [], [], 0.50)
    assert result == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
