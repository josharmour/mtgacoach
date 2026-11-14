"""Transport-free, aggregate-only repeated benchmark trial summary.

Consumes hashed identities and bounded outcomes, not prompts or selections.
Missing/error trials stay in the denominator. No model runner or CLI is supplied.
"""

from __future__ import annotations

import math
import re
import statistics

OUTCOMES = frozenset({"acceptable", "blunder", "unlabelled_choice", "withheld", "error"})


def _hash(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def summarize_repeats(*, case_ids, repeats, trials, corpus_sha256, labels_sha256, configuration_sha256):
    """Summarize a fixed labelled-case cohort, including unobserved trials.

    A caller must bind hashes to actual retained inputs/configuration. This
    function checks shape, not provenance truth or whether real inference ran.
    Spread is descriptive across repeats, not a confidence interval or win rate.
    """
    if any(not _hash(x) for x in (corpus_sha256, labels_sha256, configuration_sha256)):
        raise ValueError("explicit provenance hashes required")
    if (
        type(case_ids) is not list
        or not 1 <= len(case_ids) <= 10000
        or any(not _hash(x) for x in case_ids)
        or len(set(case_ids)) != len(case_ids)
    ):
        raise ValueError("unique hashed case cohort required")
    if type(repeats) is not int or not 2 <= repeats <= 100:
        raise ValueError("repeats must be 2..100")
    if type(trials) is not list or len(trials) > len(case_ids) * repeats:
        raise ValueError("invalid trials")
    cohort = set(case_ids)
    buckets = [
        {
            "repeat": i,
            "expected": len(case_ids),
            "observed": 0,
            "seconds": 0.0,
            **{k: 0 for k in sorted(OUTCOMES)},
        }
        for i in range(repeats)
    ]
    seen = set()
    for row in trials:
        if type(row) is not dict or set(row) != {"case_id", "repeat", "outcome", "seconds"}:
            raise ValueError("invalid trial shape")
        identity, index, outcome, seconds = (row[k] for k in ("case_id", "repeat", "outcome", "seconds"))
        if (
            not _hash(identity)
            or identity not in cohort
            or type(index) is not int
            or not 0 <= index < repeats
            or type(outcome) is not str
            or outcome not in OUTCOMES
        ):
            raise ValueError("invalid trial identity/outcome")
        if type(seconds) not in (int, float) or seconds < 0 or seconds > 86400 or not math.isfinite(seconds):
            raise ValueError("invalid trial timing")
        key = (identity, index)
        if key in seen:
            raise ValueError("duplicate trial")
        seen.add(key)
        bucket = buckets[index]
        bucket["observed"] += 1
        bucket[outcome] += 1
        bucket["seconds"] += seconds
        if not math.isfinite(bucket["seconds"]):
            raise ValueError("timing overflow")
    for bucket in buckets:
        bucket["missing"] = bucket["expected"] - bucket["observed"]
        bucket["acceptable_rate"] = bucket["acceptable"] / bucket["expected"]
        bucket["blunder_rate"] = bucket["blunder"] / bucket["expected"]
    spread = {}
    for name in ("acceptable_rate", "blunder_rate"):
        values = [b[name] for b in buckets]
        spread[name] = {
            "mean": statistics.fmean(values),
            "min": min(values),
            "max": max(values),
            "population_sd": statistics.pstdev(values),
        }
    total_seconds = sum(b["seconds"] for b in buckets)
    if not math.isfinite(total_seconds):
        raise ValueError("timing overflow")
    return {
        "corpus_sha256": corpus_sha256,
        "labels_sha256": labels_sha256,
        "configuration_sha256": configuration_sha256,
        "cases": len(case_ids),
        "repeats": repeats,
        "expected_trials": len(case_ids) * repeats,
        "observed_trials": len(seen),
        "seconds": total_seconds,
        "per_repeat": buckets,
        "spread": spread,
        "scope": "descriptive trial outcomes; not inference provenance, win rate or acceptance",
    }
