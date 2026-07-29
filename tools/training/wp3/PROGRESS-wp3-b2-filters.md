# WP-3 B-2 Filters — PROGRESS

## What was built

### `tools/training/magezero_filters.py`

Pure-function corpus filter pipeline (stdlib only, no arenamcp imports).

| Function | Purpose |
|---|---|
| `drop_single_option(rows)` | Remove rows with menu length < 2 |
| `outcome_filter(rows, mode)` | Keep only "won" rows, or all |
| `dedupe(rows)` | Order-preserving dedup by (menu, hand, battlefield+tapped, chosen) |
| `pass_rate_tripwire(rows, max_frac=0.40)` | `SystemExit(42)` if Pass fraction > threshold |
| `split_by_game(rows, seed=7, fracs=(0.90,0.05,0.05))` | Deterministic split by `game_id` (never row-level) |
| `write_manifest(splits, path, filter_counts={})` | JSON manifest with counts/sha256/git SHA |
| CLI entry point | `--in <jsonl> --outdir <path> --mode won_only` |

### `tests/test_magezero_filters.py`

43 tests across 7 test classes:
- **TestDropSingleOption** (6): multi-option keep, single drop, mixed, empty menu, missing menu, empty input
- **TestOutcomeFilter** (6): won_only keep, drop unknown, all preserves, returns copy, empty input, invalid mode
- **TestDedupe** (7): identical, diff chosen, diff menu, diff tapped, order-independent battlefield, first-occ preservation, hand order matters, empty input
- **TestPassRateTripwire** (5): below threshold, at threshold, above (SystemExit 42), all pass, empty input
- **TestSplitByGame** (7): game co-location, deterministic same seed, diff seed diff splits, fracs sum to 1, invalid fracs, no val/test, no leakage
- **TestWriteManifest** (5): required keys, split counts, sha256 hex, git SHA, writes JSONL files
- **TestCLI** (2): e2e pipeline, tripwire rejects high pass rate
- **TestInvariants** (4): required fields, field preservation, determinism, pipeline composition

## Test output

```
43 passed in 0.29s
```

## Decisions

1. **Duplicate key design**: `frozenset` of `(name, tapped)` tuples for battlefield — order-independent, which matches the MTGA client's non-deterministic battlefield ordering.
2. **Hand NOT order-independent**: hand is a `tuple`, preserving the MTGA client card order, because a reordered hand is a materially different game state.
3. **Split remainder**: test split gets the remainder after floor-based train/val allocation, avoiding rounding losses.
4. **Tripwire exit code 42**: matches project convention for retryable pipeline failures (used elsewhere in the repo).
5. **Manifest location**: written alongside split `.jsonl` files in `<outdir>/manifest.json`.

## Known gaps

- No test for CLI with `--mode all` (tested indirectly via `outcome_filter` unit tests)
- No test for very large corpuses (100K+ rows) — would be slow in CI
- The manifest `by_decision_kind` and `by_outcome` counts sort alphabetically; this is fine for JSON readability
