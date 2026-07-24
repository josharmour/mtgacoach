"""Unit tests for fail-closed gate decisions in run_pipeline.py."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.run_pipeline import PipelineRunner


def test_gate_decision_fails_closed_when_quality_none(tmp_path):
    runner = PipelineRunner(
        champion_backend="google/gemma-4-E2B-it",
        challenger_backend="google/gemma-4-E2B-it",
        iterations=1,
        matches_per_iter=10,
        gate_matches=6,
        sets="ONE",
    )
    
    # Even if win rate is 100%, if quality is None, the gate MUST return False (BLOCKED)
    verdict = runner._gate_decision(win_rate=1.0, quality=None)
    assert verdict is False
