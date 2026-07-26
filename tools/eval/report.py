"""Aggregate eval scores → printed summary + optional CSV.

Reads responses.jsonl + scores.jsonl, groups by backend, prints a table of
mean scores, latency, and length. Optionally writes a per-prompt CSV.

Usage:
    python -m tools.eval.report \\
        --responses tools/eval/data/responses.jsonl \\
        --scores tools/eval/data/scores.jsonl \\
        --csv tools/eval/data/report.csv

If --csv is omitted, just prints the summary table.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.eval.run import _read_jsonl  # noqa: E402

SCORE_DIMS = ("correctness", "reasoning", "conciseness", "legality")


def _mean(xs: list[float]) -> float:
    return statistics.fmean(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    return statistics.median(xs) if xs else 0.0


def report(
    responses_path: Path, scores_path: Path, csv_path: Path | None, json_path: Path | None = None
) -> None:
    responses = list(_read_jsonl(responses_path))
    scores = list(_read_jsonl(scores_path))

    # Index scores by (prompt_id, backend)
    score_idx = {(str(s["prompt_id"]), str(s["backend"])): s for s in scores}

    # Per-backend aggregations
    by_backend: dict[str, dict] = {}
    for r in responses:
        be = str(r.get("backend"))
        slot = by_backend.setdefault(
            be,
            {
                "n": 0,
                "errors": 0,
                "latency_ms": [],
                "response_chars": [],
                **{k: [] for k in SCORE_DIMS},
            },
        )
        slot["n"] += 1
        if r.get("error"):
            slot["errors"] += 1
        else:
            slot["latency_ms"].append(float(r.get("latency_ms") or 0.0))
            slot["response_chars"].append(int(r.get("response_chars") or 0))
        s = score_idx.get((str(r.get("prompt_id")), be))
        if s:
            for k in SCORE_DIMS:
                v = s.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    slot[k].append(float(v))

    if not by_backend:
        print("(no responses)")
        return

    # Print table
    cols = [
        ("backend", 30),
        ("n", 4),
        ("err", 4),
        ("latency_med", 12),
        ("chars_med", 10),
        *[(k[:6], 7) for k in SCORE_DIMS],
        ("overall", 8),
    ]
    header = " ".join(f"{name:<{width}}" for name, width in cols)
    print(header)
    print("-" * len(header))

    rows = []
    for be in sorted(by_backend):
        slot = by_backend[be]
        latency_med = _median(slot["latency_ms"])
        chars_med = _median(slot["response_chars"])
        score_means = {k: _mean(slot[k]) for k in SCORE_DIMS}
        # Overall = simple mean of the four dimensions, weighted equally.
        overall_vals = [score_means[k] for k in SCORE_DIMS if score_means[k] > 0]
        overall = _mean(overall_vals)
        row = [
            be[:30],
            slot["n"],
            slot["errors"],
            f"{latency_med:.0f}ms",
            f"{int(chars_med)}",
            *[f"{score_means[k]:.2f}" for k in SCORE_DIMS],
            f"{overall:.2f}",
        ]
        rows.append((be, row, slot, score_means, overall, latency_med, chars_med))
        line = " ".join(f"{str(val):<{width}}" for val, (_, width) in zip(row, cols, strict=True))
        print(line)

    print()
    print(f"Total prompts evaluated: {len({(r['prompt_id'],) for r in responses})}")
    print(f"Total responses recorded: {len(responses)}")
    print(f"Total scored: {len(scores)}")

    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "backend",
                    "n",
                    "errors",
                    "latency_ms_median",
                    "response_chars_median",
                    *[f"{k}_mean" for k in SCORE_DIMS],
                    "overall_mean",
                ]
            )
            for be, row, slot, score_means, overall, latency_med, chars_med in rows:
                w.writerow(
                    [
                        be,
                        slot["n"],
                        slot["errors"],
                        f"{latency_med:.1f}",
                        int(chars_med),
                        *[f"{score_means[k]:.3f}" for k in SCORE_DIMS],
                        f"{overall:.3f}",
                    ]
                )
        print(f"\nWrote {csv_path}")

    if json_path:
        import json as _json
        import time as _time

        backends_payload = []
        for be, row, slot, score_means, overall, latency_med, chars_med in rows:
            # Score histograms: per-dimension count of 1..5
            hist = {k: {str(i): 0 for i in range(1, 6)} for k in SCORE_DIMS}
            for v in slot[SCORE_DIMS[0]]:
                pass  # iterate below
            # We need to recount from raw scores stored on the slot, but they're
            # already in slot[k] as floats from the rubric. Re-bucket.
            for k in SCORE_DIMS:
                for v in slot[k]:
                    bucket = max(1, min(5, int(round(v))))
                    hist[k][str(bucket)] += 1
            backends_payload.append(
                {
                    "backend": be,
                    "n": slot["n"],
                    "errors": slot["errors"],
                    "latency_ms_median": round(latency_med, 1),
                    "response_chars_median": int(chars_med),
                    **{f"{k}_mean": round(score_means[k], 3) for k in SCORE_DIMS},
                    "overall_mean": round(overall, 3),
                    "score_histograms": hist,
                }
            )
        payload = {
            "target": "general",
            "ts": _time.time(),
            "n_prompts_unique": len({(r["prompt_id"],) for r in responses}),
            "n_responses": len(responses),
            "n_scored": len(scores),
            "backends": backends_payload,
        }
        with open(json_path, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2)
        print(f"\nWrote {json_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--responses", required=True, type=Path)
    parser.add_argument("--scores", required=True, type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument(
        "--json", type=Path, help="Optional structured JSON summary (for the admin dashboard)"
    )
    args = parser.parse_args()
    report(args.responses, args.scores, args.csv, args.json)


if __name__ == "__main__":
    main()
