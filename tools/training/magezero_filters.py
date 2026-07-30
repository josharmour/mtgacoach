#!/usr/bin/env python3
"""Corpus-level filters, tripwires, splits, and manifest for MageZero → gemma distillation.

Every function here is pure (stdlib only). Rows are dicts as parsed from the
decisions JSONL schema shared by all WP-3 agents.

Functions are designed to be composed in a pipeline:

    rows = load_jsonl(path)
    rows, n1 = drop_single_option(rows)
    rows, n2 = outcome_filter(rows, mode)
    rows, n3 = dedupe(rows)
    pass_rate_tripwire(rows)        # raises SystemExit if pass rate > 40 %
    splits = split_by_game(rows)
    manifest = write_manifest(splits, outdir, filter_counts={...})
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def drop_single_option(rows: list[dict]) -> tuple[list[dict], int]:
    """Remove rows whose *menu* has fewer than 2 items.

    A single-option decision contributes no signal — the model has nothing
    to choose, so the row teaches nothing about decision quality.
    Returns ``(filtered, count_dropped)``.
    """
    kept: list[dict] = []
    dropped = 0
    for r in rows:
        menu = r.get("menu", [])
        if isinstance(menu, list) and len(menu) < 2:
            dropped += 1
        else:
            kept.append(r)
    return kept, dropped


def outcome_filter(rows: list[dict], mode: str) -> tuple[list[dict], int]:
    """Filter rows by game outcome.

    * ``mode="won_only"`` — keep only rows where ``outcome == "won"``.
    * ``mode="all"`` — keep everything.
    Returns ``(filtered, count_dropped)``.
    """
    if mode == "all":
        return rows[:], 0
    if mode != "won_only":
        raise ValueError(f"Unknown outcome filter mode: {mode!r}")
    kept = [r for r in rows if r.get("outcome") == "won"]
    return kept, len(rows) - len(kept)


def _dedupe_key(row: dict) -> tuple:
    """Build an order-independent deduplication key for one decision row."""
    menu = tuple(row.get("menu", []))
    hand = tuple(row.get("hand", []))
    bf = row.get("battlefield_self", [])
    # frozenset of (name, tapped) tuples so order & dupes don't matter
    bf_key = frozenset((p.get("name", ""), bool(p.get("tapped", False))) for p in bf)
    chosen = row.get("chosen", "")
    return (menu, hand, bf_key, chosen)


def dedupe(rows: list[dict]) -> tuple[list[dict], int]:
    """Deduplicate rows by decision context key.

    Key = ``(tuple(menu), tuple(hand), frozenset(battlefield name+tapped), chosen)``.

    Uses order-preserving dedup: the **first** occurrence of each key is kept,
    which matters when rows have the same context but different MCTS counts
    (the earliest is typically the most useful for training).
    Returns ``(filtered, count_dropped)``.
    """
    seen: set[tuple] = set()
    kept: list[dict] = []
    dropped = 0
    for r in rows:
        k = _dedupe_key(r)
        if k in seen:
            dropped += 1
        else:
            seen.add(k)
            kept.append(r)
    return kept, dropped


# ---------------------------------------------------------------------------
# Tripwire
# ---------------------------------------------------------------------------


def pass_rate_tripwire(rows: list[dict], max_frac: float = 0.40) -> None:
    """Raise ``SystemExit`` if the fraction of rows where ``chosen == "Pass"``
    exceeds *max_frac* **after** all other filters have been applied.

    This guard exists because a prior corpus with a **56 % Pass rate** trained
    a pass-reflex into the model, wasting a week of training.  The tripwire
    ensures we detect and reject such corpora before they reach training.

    On violation, prints a detailed stats dump to stderr and exits with
    code 42 (the project convention for retryable pipeline failures).
    """
    total = len(rows)
    if total == 0:
        return  # nothing to check
    n_pass = sum(1 for r in rows if r.get("chosen") == "Pass")
    frac = n_pass / total

    if frac > max_frac:
        n_nonpass = total - n_pass
        print(
            f"PASS RATE TRIPWIRE TRIPPED\n"
            f"  Total rows (post-filter): {total}\n"
            f"  Pass choices:             {n_pass}\n"
            f"  Non-Pass choices:         {n_nonpass}\n"
            f"  Pass fraction:            {frac:.4f}  ({frac*100:.1f}%)\n"
            f"  Max allowed fraction:     {max_frac}  ({max_frac*100:.0f}%)\n"
            f"\n"
            f"A prior corpus with a 56 % Pass rate trained a pass-reflex\n"
            f"into the model, wasting a week of training.  This corpus\n"
            f"has a {frac*100:.1f} % Pass rate and is being rejected.\n",
            file=sys.stderr,
        )
        raise SystemExit(42)


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------


def _game_id(row: dict) -> str:
    """Extract the game identifier from a row."""
    gid = row.get("game_id", "")
    if not gid:
        # fallback: use session + turn as a pseudo-key
        session = row.get("session", "")
        turn = row.get("turn", 0)
        gid = f"{session}:turn_{turn}"
    return gid


def split_by_game(
    rows: list[dict],
    seed: int = 7,
    fracs: tuple[float, float, float] = (0.90, 0.05, 0.05),
) -> dict[str, list[dict]]:
    """Split rows by *game_id* (never row-level) to prevent train/val leakage.

    All rows sharing the same ``game_id`` go to the same split.  The
    assignment is deterministic given *seed* and stable across runs.

    Parameters
    ----------
    rows:
        Decision records.
    seed:
        PRNG seed for deterministic shuffling of game IDs.
    fracs:
        (train, val, test) fractions.  Must sum to 1.0.

    Returns
    -------
    ``{"train": [...], "val": [...], "test": [...]}``.
    Any split whose cumulative count rounds to zero is dropped from the result.
    """
    if len(fracs) != 3 or abs(sum(fracs) - 1.0) > 1e-9:
        raise ValueError(f"fracs must sum to 1.0, got {fracs}")
    train_frac, val_frac, test_frac = fracs

    # Group rows by game_id
    game_groups: dict[str, list[dict]] = {}
    for r in rows:
        gid = _game_id(r)
        game_groups.setdefault(gid, []).append(r)

    game_ids = sorted(game_groups.keys())

    # Deterministic shuffle
    rng = __import__("random").Random(seed)
    rng.shuffle(game_ids)

    n_games = len(game_ids)
    n_train = max(0, int(n_games * train_frac))
    n_val = max(0, int(n_games * val_frac))
    # test gets the remainder to avoid off-by-one due to rounding
    remainder = n_games - n_train - n_val
    n_test = max(0, remainder)

    result: dict[str, list[dict]] = {}
    splits = [("train", n_train), ("val", n_val), ("test", n_test)]
    pos = 0
    for name, count in splits:
        if count == 0:
            continue
        batch: list[dict] = []
        for gid in game_ids[pos : pos + count]:
            batch.extend(game_groups[gid])
        result[name] = batch
        pos += count

    return result


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _git_sha() -> str:
    """Return the current git HEAD SHA, or ``"unknown"`` if unavailable."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192 * 1024)  # 8 MiB
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _counts_by_field(
    rows: list[dict], field: str
) -> dict[str, int]:
    """Count occurrences of each distinct value of *field* in *rows*."""
    counts: dict[str, int] = {}
    for r in rows:
        val = r.get(field, "unknown")
        if not isinstance(val, str):
            val = str(val)
        counts[val] = counts.get(val, 0) + 1
    return dict(sorted(counts.items()))


def write_manifest(
    splits: dict[str, list[dict]],
    path: str | Path,
    filter_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Write a JSON manifest describing the corpus and return the manifest dict.

    The manifest includes:
    * Per-split row counts.
    * Per-split counts by ``decision_kind`` and by ``outcome``.
    * SHA-256 digest of each emitted JSONL file.
    * Git SHA of the repository HEAD at manifest time.
    * Per-filter drop counts (from the upstream pipeline).

    Parameters
    ----------
    splits:
        ``{"train": [...], "val": [...], "test": [...]}`` — each value is a
        list of decision rows already serialized as JSONL files in the same
        directory as *path*.
    path:
        Path to write the manifest JSON to.  Also used as the base directory
        for looking up the split .jsonl files.
    filter_counts:
        Optional dict of filter names to dropped-count values, e.g.
        ``{"drop_single_option": 12, "outcome_filter": 45, "dedupe": 8}``.
    """
    path = Path(path)
    parent = path.parent

    manifest: dict[str, Any] = {
        "version": 1,
        "git_sha": _git_sha(),
        "total_rows": sum(len(v) for v in splits.values()),
        "splits": {},
        "filter_counts": dict(filter_counts or {}),
    }

    for name, rows in sorted(splits.items()):
        # Write JSONL for this split
        split_path = parent / f"{name}.jsonl"
        with open(split_path, "w") as f:
            for r in rows:
                f.write(json.dumps(r, sort_keys=True, ensure_ascii=False))
                f.write("\n")

        sha = _sha256_file(split_path)

        manifest["splits"][name] = {
            "rows": len(rows),
            "file": split_path.name,
            "sha256": sha,
            "by_decision_kind": _counts_by_field(rows, "decision_kind"),
            "by_outcome": _counts_by_field(rows, "outcome"),
        }

    with open(path, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    return manifest


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def load_jsonl(path: str | Path) -> list[dict]:
    """Load a decisions JSONL file into a list of dicts."""
    rows: list[dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filter, tripwire, split, and manifest MageZero decision records.",
    )
    parser.add_argument(
        "--in",
        dest="input_path",
        type=str,
        required=True,
        help="Path to input decisions JSONL.",
    )
    parser.add_argument(
        "--outdir",
        type=str,
        default="tools/training/data/wp3/",
        help="Output directory for split JSONL files and manifest.",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="won_only",
        choices=("won_only", "all"),
        help="Outcome filter mode (default: won_only).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="PRNG seed for deterministic splitting (default: 7).",
    )
    parser.add_argument(
        "--max-pass-frac",
        type=float,
        default=0.40,
        help="Maximum allowed Pass fraction (default: 0.40).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Load
    rows = load_jsonl(args.input_path)
    print(f"Loaded {len(rows)} rows from {args.input_path}")

    # Pipeline
    filter_counts: dict[str, int] = {}

    rows, n = drop_single_option(rows)
    filter_counts["drop_single_option"] = n
    print(f"  drop_single_option: {n} dropped ({len(rows)} kept)")

    rows, n = outcome_filter(rows, args.mode)
    filter_counts["outcome_filter"] = n
    print(f"  outcome_filter ({args.mode}): {n} dropped ({len(rows)} kept)")

    rows, n = dedupe(rows)
    filter_counts["dedupe"] = n
    print(f"  dedupe: {n} dropped ({len(rows)} kept)")

    # Tripwire
    pass_rate_tripwire(rows, max_frac=args.max_pass_frac)

    # Split
    splits = split_by_game(rows, seed=args.seed)
    for name, group in splits.items():
        print(f"  split '{name}': {len(group)} rows ({len(set(_game_id(r) for r in group))} games)")

    # Manifest + write splits
    manifest_path = outdir / "manifest.json"
    manifest = write_manifest(splits, manifest_path, filter_counts=filter_counts)
    print(f"  Manifest written to {manifest_path}")

    print(f"\nDone.  {sum(len(v) for v in splits.values())} rows across {len(splits)} splits.")


if __name__ == "__main__":
    main()
