# PROGRESS: wp3-combat-rebalance

## Deliverables (all complete)
1. ✅ Class histogram with percentages in `--report`
2. ✅ FAIL-CLOSED build guard (`--max-class-share`, default 0.50, `--allow-skewed`)
3. ✅ `--balance downsample` mode (seed=7, deterministic)
4. ✅ `tests/test_combat_balance.py` (10 tests, all passing)

## Problem reproduced
```
Block class histogram (raw):
  no_block:   56 (71.8%)   ← ABOVE 69% trivial policy score
  partial_block:  17 (21.8%)
  block_all:   5 (6.4%)

Attack class histogram (raw):
  proper_subset:   98 (94.2%)
  all_in:    6 (5.8%)
```

## Guard behavior
Both attack and block sides exceed `--max-class-share=0.50`. The guard aborts first on
attack (`proper_subset: 94.2%`). Pass `--allow-skewed` to force through, or
`--balance downsample` to rebalance.

## Rebalanced corpus
```
After --balance downsample --allow-skewed:
  Attack:     12 records (all_in: 6 @ 50%, proper_subset: 6 @ 50%)
  Block:      44 records (no_block: 22 @ 50%, partial_block: 17 @ 38.6%, block_all: 5 @ 11.4%)
  Total:      56 records (was 182)

NOTE: 56 records is very small for training — especially attacks at 12 records.
The smoke log only had 379 attackers + 199 blockers rows to begin with, so the
corpus was never large. A production build with more games would yield a more
usable rebalanced corpus.
```

## Tests
```
tests/test_combat_balance.py::test_guard_fires_on_skewed_block       PASSED
tests/test_combat_balance.py::test_guard_fires_on_skewed_attack      PASSED
tests/test_combat_balance.py::test_guard_passes_on_balanced          PASSED
tests/test_combat_balance.py::test_histogram_shares_sum_to_one       PASSED
tests/test_combat_balance.py::test_histogram_sorted_descending       PASSED
tests/test_combat_balance.py::test_downsample_deterministic          PASSED
tests/test_combat_balance.py::test_downsample_reduces_majority       PASSED
tests/test_combat_balance.py::test_downsample_clamps_below_one       PASSED
tests/test_combat_balance.py::test_empty_histogram_does_not_raise    PASSED
tests/test_combat_balance.py::test_empty_records_downsample          PASSED
```
