import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.gate_stage0 import paired_bootstrap_ci


def test_paired_bootstrap_ci_identical():
    # If candidate and baseline are identical, delta CI should be centered at 0
    pairs = [{"cand_correct": True, "base_correct": True, "truth": "keep"} for _ in range(50)] + [
        {"cand_correct": False, "base_correct": False, "truth": "mull"} for _ in range(50)
    ]
    ci = paired_bootstrap_ci(pairs, n_bootstraps=1000, seed=42)
    assert ci["raw_accuracy_delta_95ci"] == [0.0, 0.0]
    assert ci["balanced_accuracy_delta_95ci"] == [0.0, 0.0]


def test_paired_bootstrap_ci_improvement():
    # Candidate strictly better than baseline
    pairs = [{"cand_correct": True, "base_correct": False, "truth": "keep"} for _ in range(50)] + [
        {"cand_correct": True, "base_correct": True, "truth": "mull"} for _ in range(50)
    ]
    ci = paired_bootstrap_ci(pairs, n_bootstraps=1000, seed=42)
    assert ci["raw_accuracy_delta_95ci"][0] > 0.35
    assert ci["balanced_accuracy_delta_95ci"][0] > 0.20
