"""Evaluate matched OFF/ON replay measurements without running a game or model.

python -m arenamcp.commentary.qualification baseline.json commentary.json
Times are seconds. This evaluator does not manufacture hardware measurements.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


def p95(values):
    values = sorted(float(v) for v in values)
    if not values or any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("Nonempty finite nonnegative measurements required")
    return values[math.ceil(0.95 * len(values)) - 1]


def evaluate(baseline, enabled):
    """Fail closed on missing/mismatched replay or contention measurements."""
    for key in ("replay", "strategy", "load", "platform"):
        if key not in baseline or baseline[key] != enabled.get(key):
            raise ValueError("Runs must share replay, strategy, load and platform")
    off = baseline["decisions"]
    on = enabled["decisions"]
    if len(off) < 20 or len(off) != len(on) or [r["id"] for r in off] != [r["id"] for r in on]:
        raise ValueError("At least 20 decisions with matching ordered IDs required")
    before, after = p95([r["seconds"] for r in off]), p95([r["seconds"] for r in on])
    checks = {
        "decision_p95_relative": after <= before * 1.10,
        "decision_p95_absolute": after <= before + 0.2,
        "no_extra_timeouts": sum(bool(r["timeout"]) for r in on) <= sum(bool(r["timeout"]) for r in off),
        "no_extra_fallbacks": sum(bool(r["fallback"]) for r in on) <= sum(bool(r["fallback"]) for r in off),
        "essential_start": p95(enabled["essential_start_seconds"]) <= 1
        and max(enabled["essential_start_seconds"]) <= 1,
        "playback_preemption": p95(enabled["preemption_seconds"]) <= 0.25
        and max(enabled["preemption_seconds"]) <= 0.25,
        "no_stale_speech": enabled["stale_game_utterances"] == 0,
        "no_overlap": enabled["overlapping_utterances"] == 0,
        "upstream_cancellation_verified": enabled["upstream_cancellation_verified"] is True,
        "gateway_admission_verified": enabled["gateway_admission_verified"] is True,
    }
    return {
        "passed": all(checks.values()),
        "baseline_p95_seconds": before,
        "enabled_p95_seconds": after,
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("commentary", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.baseline.read_text()), json.loads(args.commentary.read_text()))
    except (ValueError, TypeError, KeyError) as error:
        parser.exit(2, f"Incomplete or invalid qualification data: {error}\n")
    print(json.dumps(result, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
