"""Strict aggregate-only offline benchmark report/table/diff utilities.

Inputs contain hashes and bounded metrics, never prompts, states or raw choices.
This tool does not build cases, run models or assert acceptance completion.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import re
from pathlib import Path

STATUSES = frozenset({"answered", "withheld", "invalid", "error"})
SCORES = frozenset({"acceptable", "blunder", "unlabelled_choice", "not_scored"})
HEADER = {
    "version",
    "corpus_sha256",
    "configuration_sha256",
    "facts_sha256",
    "labels_sha256",
    "model_layer",
    "results",
}
ROW = {"case_id", "status", "selection_sha256", "seconds", "generation_attempts", "score"}


class InvalidReport(ValueError):
    pass


def digest(value):
    return type(value) is str and re.fullmatch("[0-9a-f]{64}", value) is not None


def validate(report):
    if type(report) is not dict or set(report) != HEADER:
        raise InvalidReport("invalid report shape")
    if type(report["version"]) is not int or report["version"] != 1:
        raise InvalidReport("unsupported report version")
    if any(
        not digest(report[key])
        for key in ("corpus_sha256", "configuration_sha256", "facts_sha256", "labels_sha256")
    ):
        raise InvalidReport("invalid provenance digest")
    if report["model_layer"] not in ("not_run", "run"):
        raise InvalidReport("invalid model layer status")
    if type(report["results"]) is not list:
        raise InvalidReport("invalid results")
    seen = set()
    total_seconds = 0.0
    for row in report["results"]:
        if type(row) is not dict or set(row) != ROW:
            raise InvalidReport("invalid result shape")
        if not digest(row["case_id"]) or row["case_id"] in seen:
            raise InvalidReport("invalid or duplicate case digest")
        seen.add(row["case_id"])
        if type(row["status"]) is not str or row["status"] not in STATUSES:
            raise InvalidReport("invalid result status")
        if not digest(row["selection_sha256"]):
            raise InvalidReport("invalid selection digest")
        if (
            type(row["seconds"]) not in (int, float)
            or not 0 <= row["seconds"] <= 1e308
            or not math.isfinite(row["seconds"])
        ):
            raise InvalidReport("invalid duration")
        total_seconds += row["seconds"]
        if not math.isfinite(total_seconds):
            raise InvalidReport("duration aggregate overflow")
        if type(row["generation_attempts"]) is not int or not 0 <= row["generation_attempts"] <= 2**63 - 1:
            raise InvalidReport("invalid generation count")
        if type(row["score"]) is not str or row["score"] not in SCORES:
            raise InvalidReport("invalid score")
        if row["status"] != "answered" and row["score"] != "not_scored":
            raise InvalidReport("only answered results may be scored")
        if report["model_layer"] == "not_run" and row["status"] == "answered" and row["generation_attempts"]:
            raise InvalidReport("offline generation fallback cannot count as answered")
    return report


def summary(report):
    validate(report)
    rows = report["results"]
    return {
        "cases": len(rows),
        "statuses": dict(sorted(collections.Counter(r["status"] for r in rows).items())),
        "scores": dict(sorted(collections.Counter(r["score"] for r in rows).items())),
        "seconds": sum(r["seconds"] for r in rows),
        "generation_attempts": sum(r["generation_attempts"] for r in rows),
        "model_layer": report["model_layer"],
        "acceptance": "not_assessed",
    }


def compare(before, after):
    validate(before)
    validate(after)
    keys = ("corpus_sha256", "configuration_sha256", "facts_sha256", "labels_sha256", "model_layer")
    old = {r["case_id"]: r for r in before["results"]}
    new = {r["case_id"]: r for r in after["results"]}
    mismatch = [key for key in keys if before[key] != after[key]]
    if old.keys() != new.keys():
        mismatch.append("case_set")
    result = {
        "comparable": not mismatch,
        "mismatches": mismatch,
        "before": summary(before),
        "after": summary(after),
    }
    if mismatch:
        return result
    result["changed_status"] = sum(old[k]["status"] != new[k]["status"] for k in old)
    result["changed_selection"] = sum(old[k]["selection_sha256"] != new[k]["selection_sha256"] for k in old)
    result["changed_score"] = sum(old[k]["score"] != new[k]["score"] for k in old)
    return result


def table(value):
    """Render only values produced by this module, not arbitrary input fields."""
    lines = []
    for prefix, report in (
        [("before", value["before"]), ("after", value["after"])] if "comparable" in value else [("", value)]
    ):
        lines.append(
            f"{prefix or 'report'}: {report['cases']} cases; {report['seconds']:.3f} seconds; model {report['model_layer']}"
        )
        lines.extend(f"  {key}: {count}" for key, count in report["statuses"].items())
        lines.extend(f"  score {key}: {count}" for key, count in report["scores"].items())
    if "comparable" in value:
        lines.append("comparable: " + str(value["comparable"]).lower())
        lines.extend(
            f"{key}: {value[key]}"
            for key in ("changed_status", "changed_selection", "changed_score")
            if key in value
        )
        if value["mismatches"]:
            lines.append("mismatches: " + ", ".join(value["mismatches"]))
    lines.append("acceptance: not assessed")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--report", type=Path)
    mode.add_argument("--diff", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    parser.add_argument("--format", choices=("json", "table"), default="table")
    args = parser.parse_args(argv)
    try:
        if args.diff:
            value = compare(*(json.loads(path.read_text()) for path in args.diff))
        else:
            value = summary(json.loads(args.report.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(2, "invalid benchmark report\n")
    print(json.dumps(value, sort_keys=True) if args.format == "json" else table(value))


if __name__ == "__main__":
    main()
