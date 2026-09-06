"""Regression tests for train.py's DDP device-placement resolution.

The distinction these pin down is the whole point of the flag:

* ``device_map="auto"`` *shards one model* across the visible GPUs (model
  parallelism). Layers run sequentially, so a second card adds capacity, not
  throughput.
* DDP puts a *full replica per rank* and feeds each a different micro-batch, so
  both cards compute at once.

A 31B in bf16 fits on one 96 GB card, so the sharded path silently wastes the
second GPU. Getting this backwards is invisible until you compare wall-clock on
a 10-hour run, hence the tests.

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

from tools.training.train import resolve_ddp_context


def test_default_is_unchanged_single_process_auto_sharding():
    """Without --ddp the pre-existing behaviour must be bit-for-bit preserved."""
    ctx = resolve_ddp_context(False, {})
    assert ctx["device_map"] == "auto"
    assert ctx["ddp"] is False
    assert ctx["world_size"] == 1


def test_default_ignores_stray_torchrun_env():
    """A LOCAL_RANK left over in the environment must not silently flip an
    ordinary single-process run into a half-configured DDP one."""
    ctx = resolve_ddp_context(False, {"LOCAL_RANK": "1", "WORLD_SIZE": "2"})
    assert ctx["device_map"] == "auto"
    assert ctx["ddp"] is False


@pytest.mark.parametrize("rank", [0, 1, 3])
def test_ddp_pins_full_replica_to_own_rank(rank):
    """Each rank must pin the *entire* model to its own card. Anything that
    still spreads across devices would have every rank sharding over every GPU
    under torchrun — the deadlock/OOM case."""
    ctx = resolve_ddp_context(True, {"LOCAL_RANK": str(rank), "WORLD_SIZE": "4"})
    assert ctx["device_map"] == {"": rank}, "DDP must pin one whole replica per rank"
    assert ctx["device_map"] != "auto"
    assert ctx["ddp"] is True
    assert ctx["local_rank"] == rank
    assert ctx["world_size"] == 4


def test_ddp_without_torchrun_fails_loudly():
    """--ddp outside torchrun must abort, not quietly run on one GPU: a job that
    looks distributed but isn't wastes the whole run before anyone notices."""
    with pytest.raises(SystemExit) as exc:
        resolve_ddp_context(True, {})
    assert "LOCAL_RANK" in str(exc.value)


def test_ddp_world_size_defaults_when_absent():
    ctx = resolve_ddp_context(True, {"LOCAL_RANK": "0"})
    assert ctx["world_size"] == 1
    assert ctx["device_map"] == {"": 0}
