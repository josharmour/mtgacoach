#!/usr/bin/env python3
"""Search-budget saturation sweep for MageZero (between-runs experiment).

Motivation (2026-07-30): Godlewski & Sawicki (arXiv:2109.12112) measured
winrate-vs-playout-budget for MCTS card-game agents and found a hard knee
(~40 playouts) past which compute buys nothing. MageZero runs
search_budget=300 / timeout_ms=4000 as unvalidated guesses, and the live logs
show some positions are timeout-bound (191 evals in 4.00s), some budget-bound
(290 evals in 2.04s). If the knee for the current net is below 300, every
generation is paying wall-clock for strength that isn't there — at 550
games/gen that is hours per generation.

Design:
  * Mirror match (UWTempo vs UWTempo, same net): player_a at the swept budget,
    player_b pinned at the reference budget (300). Win rate ~50% => the swept
    budget is strength-equivalent to the reference.
  * timeout_ms is raised for BOTH players so search_budget is the binding
    variable (the whole point is to measure the budget axis, not the timeout).
  * Arms run sequentially via `mz batch` — one JVM at a time, so thread count
    and inference-server contention match the production configuration.

Interpretation gate (pre-registered):
  * Take the smallest budget whose winrate vs the 300 control sits within the
    95% CI of 50%. If that budget is <300, adopt it for the NEXT run's config —
    never edit a run already in flight (games_per_gen restart lesson, 07-30).
  * If 300 loses to a higher probe arm (500) by more than the CI, the budget is
    UNDER-provisioned and raising it is on the table for the next run instead.

Safety: refuses to start while an `mz train` process is alive — the sweep
shares the R9700 inference server and would distort both experiments
(addendum-16 lesson: co-tenancy that looks free usually is not).

Usage (from the mtgacoach repo root, venv with pyyaml):
  python tools/training/wp3/mz_budget_sweep.py \
      --magezero /home/joshu/repos/magezero \
      --games 300 --arms 75 150 300 500
"""

from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

WIN_RATE_RE = re.compile(r"Player A win rate: ([0-9.]+)% \((\d+)/(\d+)\)")
REFERENCE_BUDGET = 300
SWEEP_TIMEOUT_MS = 20000  # high enough that search_budget binds, not the clock


def mz_train_running() -> bool:
    out = subprocess.run(
        ["pgrep", "-af", "mz train"], capture_output=True, text=True
    ).stdout
    return any("mz train" in line for line in out.splitlines())


def build_arm_config(base: dict, budget: int, games: int, out_dir: Path) -> Path:
    cfg = copy.deepcopy(base)
    cfg["player_a"]["mcts"]["search_budget"] = budget
    cfg["player_a"]["mcts"]["timeout_ms"] = SWEEP_TIMEOUT_MS
    cfg["player_b"]["mcts"]["search_budget"] = REFERENCE_BUDGET
    cfg["player_b"]["mcts"]["timeout_ms"] = SWEEP_TIMEOUT_MS
    cfg["training"]["games"] = games
    path = out_dir / f"sweep_budget_{budget}.yml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def wilson_ci(wins: int, n: int) -> tuple[float, float]:
    # 95% Wilson interval — same family as the paper's binomial CI (their eq. 2).
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (centre - half, centre + half)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--magezero", type=Path, default=Path.home() / "repos/magezero")
    ap.add_argument("--base-config", type=Path, default=None,
                    help="default: <magezero>/configs/game.yml")
    ap.add_argument("--games", type=int, default=300, help="games per arm")
    ap.add_argument("--arms", type=int, nargs="+", default=[75, 150, 300, 500])
    ap.add_argument("--force", action="store_true",
                    help="run even if mz train is alive (DON'T)")
    args = ap.parse_args()

    if mz_train_running() and not args.force:
        sys.exit(
            "REFUSING: `mz train` is running. The sweep shares the inference "
            "server and GPU; results from a contended run are worthless and "
            "it would slow the curriculum. Run between curriculum runs."
        )

    base_cfg_path = args.base_config or args.magezero / "configs/game.yml"
    base = yaml.safe_load(base_cfg_path.read_text())
    out_dir = args.magezero / "configs" / "sweep"
    out_dir.mkdir(exist_ok=True)
    results_path = args.magezero / f"budget_sweep_{time.strftime('%Y%m%d_%H%M%S')}.tsv"

    results: list[tuple[int, float, int, int]] = []
    for budget in args.arms:
        cfg_path = build_arm_config(base, budget, args.games, out_dir)
        print(f"=== arm search_budget={budget} ({args.games} games) -> {cfg_path}")
        proc = subprocess.run(
            ["mz", "batch", "--config", str(cfg_path)],
            cwd=args.magezero, capture_output=True, text=True,
        )
        matches = WIN_RATE_RE.findall(proc.stdout + proc.stderr)
        if not matches:
            # batch logs may go to the xmage log instead of stdout
            log = args.magezero / "xmage" / "magezero.log"
            if log.exists():
                matches = WIN_RATE_RE.findall(log.read_text(errors="replace"))
        if not matches:
            print(f"  !! no win-rate line found for arm {budget}; JVM output tail:")
            print("\n".join((proc.stdout + proc.stderr).splitlines()[-10:]))
            continue
        pct, wins, n = matches[-1]
        wins, n = int(wins), int(n)
        lo, hi = wilson_ci(wins, n)
        results.append((budget, float(pct), wins, n))
        print(f"  arm {budget}: {pct}% ({wins}/{n})  95% CI [{lo:.1%}, {hi:.1%}]"
              f"  {'~EQUIVALENT to 300' if lo <= 0.5 <= hi else 'DIFFERENT from 300'}")

    with results_path.open("w") as f:
        f.write("search_budget\twin_rate_pct\twins\tgames\n")
        for budget, pct, wins, n in results:
            f.write(f"{budget}\t{pct}\t{wins}\t{n}\n")
    print(f"\nresults -> {results_path}")
    print("Adopt the smallest arm whose CI covers 50% — in the NEXT run's "
          "config, never mid-run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
