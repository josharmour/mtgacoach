"""Ordering-contract tests for neural response ingestion (checkpoint-1 fix).

Ordering policy is all-or-none on the request_index echo:
- fully indexed rows must echo their position exactly (order verified);
- fully unindexed rows are LEGACY positional mode (no verified-ordering claim);
- a PARTIAL echo (some rows echo, some omit) is rejected outright.
Mirrored in ``magezero_client._validate_result`` and evaluator
``_validate_batch_rows``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arenamcp.magezero_client import _validate_result
from arenamcp.mcts_evaluator import MCTSEvaluator
from arenamcp.magezero_client import MageZeroClient


def _row(i: int | None = None, value: float = 0.2) -> dict[str, Any]:
    row: dict[str, Any] = {
        "value": value,
        "policy_player": [0.0] * 128,
        "policy_opponent": [0.0] * 128,
    }
    if i is not None:
        row["request_index"] = i
    return row


# ------------- client-level: _validate_result -------------


def test_partial_echo_rejected():
    rows = [_row(0), _row()]  # row 1 omits the index
    result, reason = _validate_result(rows, 2, 128)
    assert result == [] and reason is not None
    assert "partial" in reason


def test_partial_echo_rejected_any_position():
    rows = [_row(), _row(1)]  # now row 0 is the one missing
    result, reason = _validate_result(rows, 2, 128)
    assert result == [] and reason is not None
    assert "partial" in reason


def test_fully_indexed_out_of_order_rejected():
    rows = [_row(1), _row(0)]
    result, reason = _validate_result(rows, 2, 128)
    assert result == [] and reason is not None
    assert "order" in reason


def test_fully_indexed_in_order_accepted():
    rows = [_row(0), _row(1)]
    result, reason = _validate_result(rows, 2, 128)
    assert reason is None and len(result) == 2
    assert [r["request_index"] for r in result] == [0, 1]


def test_fully_unindexed_is_legacy_positional():
    """No echo at all: positional sync, explicitly labelled legacy mode."""
    rows = [_row(), _row(), _row()]
    result, reason = _validate_result(rows, 3, 128)
    assert reason is None and len(result) == 3
    assert all("request_index" in r for r in result)


def test_larger_partial_echo_rejected_mid_batch():
    rows = [_row()] + [_row() for _ in range(7)] + [_row(8)]
    result, reason = _validate_result(rows, 9, 128)
    assert result == [] and reason is not None and "partial" in reason


# ------------- evaluator-level: _validate_batch_rows -------------


def test_evaluator_partial_echo_rejected():
    rows = [_row(0), _row()]  # row 1 lacks the echo
    assert MCTSEvaluator._validate_batch_rows(rows, 2) is None


def test_evaluator_fully_unindexed_accepted():
    """Legacy positional mode passes intake (validation, not ordering, applies)."""
    rows = [_row(), _row(), _row()]
    out = MCTSEvaluator._validate_batch_rows(rows, 3)
    assert out is not None and len(out) == 3


def test_evaluator_fully_indexed_wrong_order_rejected():
    assert MCTSEvaluator._validate_batch_rows([_row(1), _row(0)], 2) is None


def test_evaluator_fully_indexed_in_order_accepted():
    out = MCTSEvaluator._validate_batch_rows([_row(0), _row(1)], 2)
    assert out is not None and len(out) == 2


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch):
    """Hermetic: network mocked; no discovery contact possible."""
    monkeypatch.setattr(
        MageZeroClient, "check_health", classmethod(lambda cls, *a, **kw: False)
    )
    yield
