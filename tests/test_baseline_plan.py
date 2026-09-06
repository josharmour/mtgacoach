#!/usr/bin/env python3
"""Tests for baseline_plan.py — power maths against hand-computed values."""

import sys

import pytest

sys.path.insert(0, "tools/training/wp3")
from baseline_plan import Z_ALPHA_2, Z_BETA, baseline_games_needed, mde

# ── known constants ────────────────────────────────────────────
P_BL = 86 / 299  # 0.287625...


def assert_approx(actual, expected, tol_pp=0.02, label=""):
    """Assert |actual - expected| <= tol_pp (in percentage points)."""
    ok = abs(actual - expected) <= tol_pp
    status = "✓" if ok else "✗"
    print(
        f"  {status} {label}: got {actual:.4f} pp, expected ~{expected:.2f} pp"
        + ("" if ok else f"  *** FAIL (delta={abs(actual - expected):.4f})")
    )
    if not ok:
        raise AssertionError(f"{label}: {actual:.4f} != {expected:.2f} pp")


def assert_int_approx(actual, expected, tol=50, label=""):
    """Assert |actual - expected| <= tol integer."""
    ok = abs(actual - expected) <= tol
    status = "✓" if ok else "✗"
    print(
        f"  {status} {label}: got {actual}, expected ~{expected}"
        + ("" if ok else f"  *** FAIL (delta={abs(actual - expected)})")
    )
    if not ok:
        raise AssertionError(f"{label}: {actual} != {expected}")


def test_mde_current():
    """MDE at current sample sizes (n_baseline=299, n_net=1299)."""
    print("\n1) Current MDE (299 baseline / 1299 net)")
    val = mde(P_BL, 299, 1299)
    assert_approx(val, 8.13, label="MDE(299, 1299)")


def test_mde_net_only_growth():
    """Growing the net arm alone (baseline frozen at 299)."""
    print("\n2) Net-only growth (n_baseline=299)")
    # n_net=5000 → ~7.55 pp
    val = mde(P_BL, 299, 5000)
    assert_approx(val, 7.55, label="MDE(299, 5000)")


def test_mde_balanced():
    """Symmetric sample sizes."""
    print("\n3) Symmetric sample sizes")
    val = mde(P_BL, 500, 500)
    assert_approx(val, 8.02, label="MDE(500, 500)")

    val = mde(P_BL, 1000, 1000)
    assert_approx(val, 5.67, label="MDE(1000, 1000)")

    val = mde(P_BL, 2000, 2000)
    assert_approx(val, 4.01, label="MDE(2000, 2000)")


def test_mde_baseline_growth():
    """Growing baseline arm (net fixed at 1299)."""
    print("\n4) Baseline growth (n_net=1299)")
    val = mde(P_BL, 1000, 1299)
    assert_approx(val, 5.34, label="MDE(1000, 1299)")

    val = mde(P_BL, 2000, 1299)
    assert_approx(val, 4.52, label="MDE(2000, 1299)")


def test_z_values():
    """Verify the z-quantile constants are correct."""
    pytest.importorskip("scipy.stats")
    from scipy.stats import norm

    # z_{0.025} = 1.959964
    expected_alpha = norm.ppf(1 - 0.05 / 2)
    assert abs(Z_ALPHA_2 - expected_alpha) < 0.0001, f"Z_ALPHA_2 off: {Z_ALPHA_2} vs {expected_alpha}"
    # z_{0.20} = 0.841621
    expected_beta = norm.ppf(0.80)
    assert abs(Z_BETA - expected_beta) < 0.0001, f"Z_BETA off: {Z_BETA} vs {expected_beta}"
    print(f"\n5) Z-quantiles: Z_ALPHA_2={Z_ALPHA_2:.6f} Z_BETA={Z_BETA:.6f}")


def test_inverse():
    """Inverse: baseline games needed for target MDE."""
    print("\n6) Inverse: n_baseline for target MDE")
    # At 5 pp with n_net=1299, should be ~1275 games
    n = baseline_games_needed(P_BL, 1299, 5.0)
    assert_int_approx(n, 1275, tol=50, label="n_baseline for 5 pp (net=1299)")
    # Verify that n actually achieves <= 5pp
    assert mde(P_BL, n, 1299) <= 5.0, f"MDE({n}, 1299) = {mde(P_BL, n, 1299):.4f} > 5.0 pp"

    # At 4 pp with n_net=1299, should be ~4444 games
    n = baseline_games_needed(P_BL, 1299, 4.0)
    assert_int_approx(n, 4444, tol=50, label="n_baseline for 4 pp (net=1299)")
    assert mde(P_BL, n, 1299) <= 4.0, f"MDE({n}, 1299) = {mde(P_BL, n, 1299):.4f} > 4.0 pp"


def test_formula_bounds():
    """Sanity: MDE decreases as n increases; more unbalanced → larger MDE."""
    print("\n7) Monotonicity / bounds sanity")
    # More data → smaller MDE
    assert mde(P_BL, 100, 100) > mde(P_BL, 1000, 1000)
    # Asymmetric is less powerful than symmetric at same total N
    total = 2000
    asymmetric = mde(P_BL, 500, 1500)
    symmetric = mde(P_BL, 1000, 1000)
    assert asymmetric > symmetric, f"Asymmetric ({asymmetric:.4f}) should be > symmetric ({symmetric:.4f})"
    print(f"  ✓ asymmetric (500+1500)={asymmetric:.4f} > symmetric (1000+1000)={symmetric:.4f}")


if __name__ == "__main__":
    print("baseline_plan.py — hand-validated tests")
    print("=" * 56)

    test_mde_current()
    test_mde_net_only_growth()
    test_mde_balanced()
    test_mde_baseline_growth()
    test_z_values()
    test_inverse()
    test_formula_bounds()

    print("\n" + "=" * 56)
    print("All tests passed.")
