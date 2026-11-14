import copy
import json

import pytest
from tools.benchmark_repeats import summarize_repeats


def args():
    return dict(
        case_ids=["a" * 64, "b" * 64],
        repeats=2,
        trials=[
            dict(case_id="a" * 64, repeat=0, outcome="acceptable", seconds=1),
            dict(case_id="b" * 64, repeat=0, outcome="error", seconds=2),
            dict(case_id="a" * 64, repeat=1, outcome="blunder", seconds=3),
        ],
        corpus_sha256="c" * 64,
        labels_sha256="d" * 64,
        configuration_sha256="e" * 64,
    )


def test_rates_keep_errors_missing_and_spread():
    data = args()
    original = copy.deepcopy(data)
    out = summarize_repeats(**data)
    assert data == original
    assert out["expected_trials"] == 4 and out["observed_trials"] == 3
    assert out["per_repeat"][0]["acceptable_rate"] == 0.5
    assert out["per_repeat"][1]["missing"] == 1
    assert out["spread"]["acceptable_rate"] == {"mean": 0.25, "min": 0.0, "max": 0.5, "population_sd": 0.25}
    assert out["seconds"] == 6
    assert "case_id" not in json.dumps(out)


@pytest.mark.parametrize(
    "change",
    [
        "extra",
        "duplicate",
        "unknown",
        "bool_repeat",
        "bool_seconds",
        "nan",
        "infinity",
        "negative",
        "outcome",
        "overflow",
    ],
)
def test_invalid_trials_fail_closed(change):
    data = args()
    row = data["trials"][0]
    if change == "extra":
        row["prompt"] = "private"
    elif change == "duplicate":
        data["trials"][1] = copy.deepcopy(row)
    elif change == "unknown":
        row["case_id"] = "f" * 64
    elif change == "bool_repeat":
        row["repeat"] = True
    elif change == "bool_seconds":
        row["seconds"] = True
    elif change == "nan":
        row["seconds"] = float("nan")
    elif change == "infinity":
        row["seconds"] = float("inf")
    elif change == "negative":
        row["seconds"] = -1
    elif change == "outcome":
        row["outcome"] = "private text"
    elif change == "overflow":
        row["seconds"] = 1e308
        data["trials"][1]["seconds"] = 1e308
    with pytest.raises(ValueError):
        summarize_repeats(**data)


@pytest.mark.parametrize(
    "key,value",
    [
        ("case_ids", []),
        ("case_ids", ["a" * 64, "a" * 64]),
        ("repeats", True),
        ("repeats", 1),
        ("labels_sha256", "missing"),
    ],
)
def test_invalid_cohort(key, value):
    data = args()
    data[key] = value
    with pytest.raises(ValueError):
        summarize_repeats(**data)


def test_empty_observations_are_not_success():
    data = args()
    data["trials"] = []
    out = summarize_repeats(**data)
    assert out["observed_trials"] == 0
    assert all(b["missing"] == 2 and b["acceptable_rate"] == 0 for b in out["per_repeat"])
