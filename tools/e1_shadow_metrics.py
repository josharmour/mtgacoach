#!/usr/bin/env python3
"""Offline E1 evidence inventory. Never treats replay potential as observed savings."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict

from upgrade_metrics import is_match_bundle, iter_bundle_paths, load_bundle, summarize_bundle


def summarize(bundles):
    matches, shadow_matches = set(), set()
    types = defaultdict(Counter)
    singletons = Counter()
    calls = 0
    fingerprints = {}
    duplicates = 0
    for bundle in bundles:
        if not is_match_bundle(bundle):
            continue
        match = bundle.get("match_id")
        if not match:
            continue
        if type(match) is not str:
            raise ValueError("invalid match identity in E1 evidence")
        # Repeated copies are not independent observations. A different copy
        # of the same match may be partial or contradictory: never guess which
        # is authoritative or deduplicate individual BO3 events without IDs.
        fingerprint = hashlib.sha256(
            json.dumps(bundle, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).digest()
        previous = fingerprints.get(match)
        if previous is not None:
            if previous != fingerprint:
                raise ValueError("conflicting copies of a match in E1 evidence")
            duplicates += 1
            continue
        fingerprints[match] = fingerprint
        matches.add(match)
        metrics = summarize_bundle(bundle)
        calls += sum(bucket["calls"] for bucket in metrics["call_classes"].values())
        for row in (bundle.get("packet") or {}).get("decisions") or []:
            pending, trace = row.get("pending_decision") or {}, row.get("trace") or {}
            if row.get("chosen_options") and len(pending.get("options") or []) == 1 and "llm_usage" in trace:
                singletons[pending.get("request_type") or "unknown"] += 1
            event = trace.get("planner_certain") or {}
            if event.get("mode") != "shadow" or not event.get("proposed") or not event.get("model"):
                continue
            shadow_matches.add(match)
            bucket = types[event.get("decision_type") or pending.get("request_type") or "unknown"]
            bucket["observations"] += 1
            bucket["agreements"] += event["proposed"] == event["model"]
    return {
        "matches": len(matches),
        "duplicate_bundles_ignored": duplicates,
        "recorded_calls": calls,
        "recorded_calls_per_match": calls / len(matches) if matches else None,
        "raw_singleton_model_answers_by_type": dict(singletons),
        "shadow_matches": len(shadow_matches),
        "minimum_match_gate_met": len(shadow_matches) >= 20,
        "agreement_by_type": {
            key: {**value, "agreement_rate": value["agreements"] / value["observations"]}
            for key, value in types.items()
        },
        "observed_before_after_savings": None,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    args = parser.parse_args()
    try:
        result = summarize(load_bundle(p) for p in iter_bundle_paths(args.directory))
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(2, "invalid or conflicting E1 evidence\n")
    print(json.dumps(result, indent=2))
