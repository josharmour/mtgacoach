# PROGRESS-wp3-baseline-arm.md

## Deliverables

### 1. Power Calculator — `tools/training/wp3/baseline_plan.py`

A self-contained power analysis using the two-proportion z-test (pooled SE):

```
MDE = (z_{α/2} + z_β) · √(p_baseline·(1-p_baseline) · (1/n_baseline + 1/n_net))
```

Key numbers from current observations (baseline=86/299=28.76%, net=398/1299=30.64%):

| Scenario | n_baseline | n_net | MDE |
|---|---|---|---|
| Current | 299 | 1,299 | **8.13 pp** |
| Net-only growth | 299 | 5,000 | 7.55 pp |
| Baseline growth | 1,000 | 1,299 | **5.34 pp** |
| Symmetric | 1,000 | 1,000 | 5.67 pp |
| Symmetric | 2,000 | 2,000 | 4.01 pp |

Inverse: for a target MDE of 5 pp (net fixed at 1299), need ~1,275 baseline games.

### 2. Config — `tools/training/wp3/run_baseline.yml`

Creates an offline / no-net baseline arm:

- **`start_from_version: null`** — no seed checkpoint (runner.py lines 173-174)
- **`version: 2`** — distinct from ver1 which has a 3.9 GB checkpoint. `has_checkpoint("UWTempo", 2)` returns False → `bootstrap=True` → `primary_offline=True`, so no servers are started (runner.py lines 430, 441-442, 457-458)
- 5 minimax opponents — no checkpoints needed
- Single generation, 1000 games_per_gen → ~1000 baseline games

### 3. Tests — `tests/test_baseline_plan.py`

7 tests: MDE at current sizes, net-only growth, symmetric, baseline growth, z-quantiles (scipy-dependent, skipped on this host), inverse search, monotonicity. 6 pass, 1 skipped on macOS.

### Runbook (do NOT execute)

```bash
# On blackwell (or wherever magezero is installed):
cd /home/joshu/repos/magezero
cp /path/to/mtgacoach/tools/training/wp3/run_baseline.yml configs/
python3 -m magezero.runner --run configs/run_baseline.yml
```

**⚠ Do NOT use `version: 1`** or the existing ver1 model will be overwritten.
Use `version: 2` as configured. The config lives inside the mtgaCoach repo at
`tools/training/wp3/run_baseline.yml` and must be copied/linked into magezero's
`configs/` before running.
