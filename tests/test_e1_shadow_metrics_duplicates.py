"""Synthetic metrics-only consistency; no real match gate evidence."""

import copy
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def metrics(monkeypatch):
    tools = Path(__file__).parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    spec = importlib.util.spec_from_file_location("e1_dedup_metrics", tools / "e1_shadow_metrics.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bundle(match, choice):
    return {
        "match_id": match,
        "packet": {
            "decisions": [
                {
                    "trace": {
                        "planner_certain": {
                            "mode": "shadow",
                            "proposed": ["a"],
                            "model": [choice],
                            "decision_type": "ActionsAvailable",
                        }
                    }
                }
            ]
        },
    }


def test_exact_duplicate_does_not_reweight_any_statistic(metrics):
    a, b = bundle("a", "a"), bundle("b", "b")
    baseline = metrics.summarize([a, b])
    observed = metrics.summarize([a, copy.deepcopy(a), b])
    assert observed.pop("duplicate_bundles_ignored") == 1
    baseline.pop("duplicate_bundles_ignored")
    assert observed == baseline
    assert observed["agreement_by_type"]["ActionsAvailable"]["agreement_rate"] == 0.5


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_same_match_refuses_in_either_order(metrics, reverse):
    inputs = [bundle("a", "a"), bundle("a", "b")]
    with pytest.raises(ValueError, match="conflicting copies"):
        metrics.summarize(reversed(inputs) if reverse else inputs)


def test_two_distinct_events_within_one_match_are_not_deduplicated(metrics):
    a = bundle("a", "a")
    a["packet"]["decisions"] *= 2
    result = metrics.summarize([a])
    assert result["shadow_matches"] == 1
    assert result["agreement_by_type"]["ActionsAvailable"]["observations"] == 2


def test_twenty_copies_are_one_real_match(metrics):
    result = metrics.summarize(copy.deepcopy(bundle("a", "a")) for _ in range(20))
    assert result["shadow_matches"] == 1
    assert result["duplicate_bundles_ignored"] == 19
    assert not result["minimum_match_gate_met"]


def test_key_order_is_not_a_distinct_bundle(metrics):
    a = bundle("a", "a")
    b = dict(reversed(list(a.items())))
    assert metrics.summarize([a, b])["duplicate_bundles_ignored"] == 1
