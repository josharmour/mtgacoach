#!/usr/bin/env python3
"""
baseline_plan.py — Power calculator for the MageZero no-net baseline arm.

Given (baseline_p, n_baseline, n_net, alpha, power) prints the minimum
detectable effect (MDE) in percentage points.  Inverts to find the number
of baseline games needed for a target MDE of 3, 4, and 5 pp.

Uses the standard two-proportion z-test (pooled SE) formula:

    MDE = (z_{α/2} + z_β) · √( p̄·(1-p̄) · (1/n₁ + 1/n₂) )

    where p̄ = (p₁·n₁ + p₂·n₂) / (n₁ + n₂) under the null, approximated
    by freezing p̄ at the baseline proportion (conservative for p ≈ 0.3).

Usage:
    python3 tools/training/wp3/baseline_plan.py

References
----------
- Fleiss, J. L., Levin, B., & Paik, M. C. (2003). Statistical Methods for
  Rates and Proportions (3rd ed.). Wiley. §4.2.
- https://en.wikipedia.org/wiki/Power_(statistics)#Two-proportion_z-test
"""

import math

# ── standard normal quantiles ─────────────────────────────────
Z_ALPHA_2 = 1.959964  # α = 0.05, two-sided
Z_BETA = 0.841621  # β = 0.20 (power = 0.80)


def mde(p_baseline: float, n_baseline: int, n_net: int) -> float:
    """Minimum detectable effect (percentage points) for a given baseline.

    Uses the **pooled** standard error evaluated at the baseline proportion
    (conservative — the true pooled proportion shifts toward the net arm's
    win rate, but freezing at p_baseline slightly overestimates SE when
    p_baseline ≈ 0.3, making this a conservative estimate of MDE).

    Parameters
    ----------
    p_baseline : float
        Observed win rate of the no-net arm (e.g. 0.2876).
    n_baseline : int
        Number of games in the baseline arm.
    n_net : int
        Number of games in the net-guided arm.

    Returns
    -------
    float
        Minimum detectable effect in percentage points.
    """
    p = p_baseline
    se = math.sqrt(p * (1 - p) * (1 / n_baseline + 1 / n_net))
    return (Z_ALPHA_2 + Z_BETA) * se * 100  # convert to pp


def baseline_games_needed(
    p_baseline: float, n_net: int, target_mde_pp: float, min_games: int = 10, max_games: int = 50_000
) -> int:
    """Binary-search for the smallest n_baseline meeting target MDE.

    Parameters
    ----------
    p_baseline : float
        Observed win rate of the no-net arm.
    n_net : int
        Number of net-guided games (fixed, does not grow).
    target_mde_pp : float
        Desired minimum detectable effect in percentage points.
    min_games, max_games : int
        Search bounds.

    Returns
    -------
    int
        Minimum n_baseline achieving the target MDE.
    """
    lo, hi = min_games, max_games
    while lo < hi:
        mid = (lo + hi) // 2
        if mde(p_baseline, mid, n_net) <= target_mde_pp:
            hi = mid
        else:
            lo = mid + 1
    # Ensure the found value actually meets the target
    if mde(p_baseline, lo, n_net) > target_mde_pp:
        return -1  # impossible within bounds
    return lo


def fmt_pp(val: float) -> str:
    """Format a proportion-difference as a readable pp string."""
    return f"{val:.2f} pp"


def current_state() -> dict:
    """Return current observed statistics."""
    return {
        "baseline_wins": 86,
        "baseline_games": 299,
        "net_wins": 398,
        "net_games": 1299,
    }


def main():
    cur = current_state()
    p_bl = cur["baseline_wins"] / cur["baseline_games"]
    p_net = cur["net_wins"] / cur["net_games"]
    n_bl = cur["baseline_games"]
    n_net = cur["net_games"]

    print("=" * 72)
    print("  MageZero Baseline Arm — Power Analysis")
    print("=" * 72)
    print()
    print("  Current observations")
    print(
        f"    No-net (baseline): {cur['baseline_wins']}/{cur['baseline_games']}"
        f"  = {p_bl:.4f}  ({p_bl * 100:.2f}%)"
    )
    print(f"    Net-guided:        {cur['net_wins']}/{cur['net_games']}  = {p_net:.4f}  ({p_net * 100:.2f}%)")
    print(f"    Observed gap:      {(p_net - p_bl) * 100:.2f} pp")
    print()

    # ── Worked example: current sample sizes ──────────────────
    print(f"  ── Current resolution (n_baseline={n_bl}, n_net={n_net})")
    current_mde = mde(p_bl, n_bl, n_net)
    print(f"     MDE = {fmt_pp(current_mde)}")
    print()

    # ── What growing the NET arm alone does ──────────────────
    print("  ── Growing the net arm alone (baseline frozen at 299)")
    for nn in [299, 1299, 5000, 10000]:
        m = mde(p_bl, 299, nn)
        print(f"     n_net={nn:>5} → MDE = {fmt_pp(m)}")
    print()

    # ── What growing the BASELINE arm does ───────────────────
    print("  ── Growing the baseline arm (net at current 1299)")
    for nb in [500, 1000, 1500, 2000]:
        m = mde(p_bl, nb, 1299)
        print(f"     n_baseline={nb:>4} → MDE = {fmt_pp(m)}")
    print()

    # ── Balanced approach ─────────────────────────────────────
    print("  ── Symmetric growth (n_baseline = n_net)")
    for n in [299, 1000, 2000, 5000]:
        m = mde(p_bl, n, n)
        print(f"     n={n:>4} each → MDE = {fmt_pp(m)}")
    print()

    # ── Inverse: games needed for target MDE ─────────────────
    print("  ── Baseline games needed for target MDE")
    print(f"     (net arm fixed at {n_net} games, p_baseline = {p_bl:.4f})")
    for target in [3, 4, 5]:
        needed = baseline_games_needed(p_bl, n_net, target)
        if needed > 0:
            print(
                f"     Target {target} pp  →  n_baseline ≥ {needed:,}  "
                f"(+{needed - n_bl:,} from current {n_bl})"
            )
        else:
            print(f"     Target {target} pp  →  impossible within 50,000 games")
    print()

    # ── Single-gen cost estimate ──────────────────────────────
    print("  ── Cost: single-generation config")
    print(
        "     games_per_gen: 1000  →  ~1,000 baseline games"
        f"  (MDE ≈ {mde(p_bl, 1000, n_net):.2f} pp, net arm at current 1299)"
    )
    print(
        "     games_per_gen: 1000  →  ~1,000 baseline games"
        f"  (MDE ≈ {mde(p_bl, 1000, 1000):.2f} pp, symmetric n_net=1000)"
    )
    print()

    # ── Key takeaway ──────────────────────────────────────────
    print("  Key takeaway:")
    print(f"    Current resolution (n_baseline={n_bl}) = {fmt_pp(current_mde)}")
    print(f"    ~1,000 baseline games  →  MDE ≈ {fmt_pp(mde(p_bl, 1000, n_net))}")
    print(f"    ~2,000 baseline games  →  MDE ≈ {fmt_pp(mde(p_bl, 2000, n_net))}")
    print("=" * 72)


if __name__ == "__main__":
    main()
