"""Regression tests for train.py's eval/save/early-stopping schedule.

The plan specifies "5% eval split + early stopping". Early stopping is only
meaningful with `load_best_model_at_end=True`, and HF validates that pairing
strictly — the pre-existing defaults (`save_strategy="steps"` with
`eval_strategy="epoch"`) would have raised at trainer construction. These tests
pin the resolution so the combination cannot silently regress.

Deliberately import-only: `tools.training.train` must stay importable without
torch/transformers/trl so this runs on any dev machine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.training.train import (
    DEFAULT_EARLY_STOPPING_PATIENCE,
    resolve_eval_save_strategy,
)


def _hf_would_accept(cfg: dict) -> bool:
    """Mirror of transformers' TrainingArguments.__post_init__ validation."""
    if not cfg["load_best_model_at_end"]:
        return True
    if cfg["eval_strategy"] != cfg["save_strategy"]:
        return False
    if cfg["eval_strategy"] == "steps":
        if not cfg["eval_steps"]:
            return False
        if cfg["save_steps"] % cfg["eval_steps"] != 0:
            return False
    return True


def test_module_imports_without_ml_stack():
    """Guards the deferred heavy imports; if torch leaks back to module scope
    this test fails on any machine without a GPU training environment."""
    import tools.training.train as train_mod

    assert "torch" not in dir(train_mod)
    assert callable(train_mod.resolve_eval_save_strategy)


def test_early_stopping_enabled_with_eval_split():
    cfg = resolve_eval_save_strategy(
        save_steps=25, has_eval=True, early_stopping_patience=3, save_total_limit=3
    )
    assert cfg["early_stopping"] is True
    assert cfg["load_best_model_at_end"] is True
    assert cfg["metric_for_best_model"] == "eval_loss"
    assert cfg["greater_is_better"] is False
    assert _hf_would_accept(cfg)


def test_step_cadence_conflict_is_resolved_onto_save_steps():
    """save_strategy=steps + eval_strategy=epoch is what HF rejects.

    The resolution keeps the operator's checkpoint cadence and moves eval onto
    it, so the best checkpoint is always one that was actually written.
    """
    cfg = resolve_eval_save_strategy(
        save_steps=40, has_eval=True, early_stopping_patience=2, save_total_limit=3
    )
    assert cfg["save_strategy"] == "steps"
    assert cfg["eval_strategy"] == "steps"
    assert cfg["save_steps"] == 40
    assert cfg["eval_steps"] == 40
    assert cfg["save_steps"] % cfg["eval_steps"] == 0
    assert _hf_would_accept(cfg)


def test_per_epoch_checkpointing_evaluates_per_epoch():
    cfg = resolve_eval_save_strategy(
        save_steps=0, has_eval=True, early_stopping_patience=3, save_total_limit=3
    )
    assert cfg["save_strategy"] == "epoch"
    assert cfg["eval_strategy"] == "epoch"
    assert cfg["eval_steps"] is None
    assert _hf_would_accept(cfg)


def test_save_total_limit_leaves_room_for_the_best_checkpoint():
    cfg = resolve_eval_save_strategy(
        save_steps=25, has_eval=True, early_stopping_patience=3, save_total_limit=1
    )
    assert cfg["save_total_limit"] >= 2
    # Larger operator-chosen limits are respected.
    cfg = resolve_eval_save_strategy(
        save_steps=25, has_eval=True, early_stopping_patience=3, save_total_limit=7
    )
    assert cfg["save_total_limit"] == 7


def test_no_eval_split_disables_early_stopping_and_keeps_save_behaviour():
    cfg = resolve_eval_save_strategy(
        save_steps=25, has_eval=False, early_stopping_patience=3, save_total_limit=3
    )
    assert cfg["early_stopping"] is False
    assert cfg["load_best_model_at_end"] is False
    assert cfg["eval_strategy"] == "no"
    assert cfg["save_strategy"] == "steps"
    assert cfg["save_steps"] == 25
    assert cfg["save_total_limit"] == 3
    assert _hf_would_accept(cfg)


def test_patience_zero_opts_out_without_changing_save_behaviour():
    cfg = resolve_eval_save_strategy(
        save_steps=25, has_eval=True, early_stopping_patience=0, save_total_limit=3
    )
    assert cfg["early_stopping"] is False
    assert cfg["load_best_model_at_end"] is False
    assert cfg["eval_strategy"] == "epoch"  # legacy behaviour preserved
    assert cfg["save_strategy"] == "steps"
    assert _hf_would_accept(cfg)


@pytest.mark.parametrize("metric,greater", [("eval_loss", False), ("eval_accuracy", True)])
def test_greater_is_better_follows_the_metric(metric, greater):
    cfg = resolve_eval_save_strategy(
        save_steps=25,
        has_eval=True,
        early_stopping_patience=1,
        save_total_limit=3,
        metric_for_best_model=metric,
    )
    assert cfg["greater_is_better"] is greater


def test_default_patience_is_positive():
    assert DEFAULT_EARLY_STOPPING_PATIENCE > 0
