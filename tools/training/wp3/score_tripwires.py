#!/usr/bin/env python3
"""Score tripwire-fixture responses from a responses JSONL (WP-3 / B5 gate slice).

The 55 tripwire fixtures (#443, ``tools/training/build_tripwires.py``) ship a
programmatic evaluator (``run_tripwire_eval``) that takes a live ``model_fn``.
The gate pipeline, however, produces *response files* via ``tools.eval.run``.
This script bridges the two WITHOUT re-implementing any scoring logic: it
builds a lookup ``model_fn`` over a responses JSONL and feeds it to the
existing ``run_tripwire_eval``.

Fail-closed semantics:
  * a fixture with no matching response row, or a row with ``error`` set,
    scores as FAILED and is separately counted under ``missing_responses`` /
    ``error_responses`` — never silently skipped;
  * duplicate ``prompt_id`` rows for the same backend are an error (exit 2);
  * exit 0 does NOT mean the model passed — it means scoring completed.
    Read ``pass_rate`` and the per-category histogram in the output JSON.

Usage:
    python tools/training/wp3/score_tripwires.py \
        --fixtures tools/training/data/tripwire_fixtures_55.jsonl \
        --responses tools/training/data/gate_b5_tripwires.jsonl \
        --backend-label openai-compat:b5 \
        --out tools/training/data/gate_b5_tripwires_candidate_score.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from tools.training.build_tripwires import run_tripwire_eval  # noqa: E402


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"FAIL-CLOSED: malformed JSONL {path}:{i}: {e}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixtures", type=Path, required=True)
    ap.add_argument("--responses", type=Path, required=True)
    ap.add_argument("--backend-label", required=True)
    ap.add_argument("--out", type=Path, default=None, help="write summary JSON here")
    ap.add_argument(
        "--expect-fixtures",
        type=int,
        default=55,
        help="fail closed unless the fixture file has exactly this many records",
    )
    args = ap.parse_args(argv)

    if not args.fixtures.exists():
        print(f"FAIL-CLOSED: fixtures file missing: {args.fixtures}", file=sys.stderr)
        return 2
    if not args.responses.exists():
        print(f"FAIL-CLOSED: responses file missing: {args.responses}", file=sys.stderr)
        return 2

    fixtures = _read_jsonl(args.fixtures)
    if len(fixtures) != args.expect_fixtures:
        print(
            f"FAIL-CLOSED: fixture count {len(fixtures)} != expected "
            f"{args.expect_fixtures} in {args.fixtures} "
            f"(regenerate with tools/training/build_tripwires.py)",
            file=sys.stderr,
        )
        return 2

    by_id: dict[str, dict] = {}
    duplicates = 0
    for row in _read_jsonl(args.responses):
        if row.get("backend") != args.backend_label:
            continue
        pid = str(row.get("prompt_id"))
        if pid in by_id:
            duplicates += 1
            continue
        by_id[pid] = row

    if duplicates:
        print(
            f"FAIL-CLOSED: {duplicates} duplicate prompt_id rows for backend "
            f"{args.backend_label!r} in {args.responses}",
            file=sys.stderr,
        )
        return 2

    missing: list[str] = []
    errored: list[str] = []

    # run_tripwire_eval iterates fixtures in order and calls
    # model_fn(system, user) with no id, so walk the same list in lockstep and
    # assert each call matches the fixture we expect (fail closed on skew).
    fixture_iter = iter(fixtures)

    def model_fn(system: str, user: str) -> str:
        fixture = next(fixture_iter)
        if fixture["system"] != system or fixture["user"] != user:
            raise AssertionError(
                f"fixture/model_fn ordering skew at {fixture['id']} — "
                "run_tripwire_eval iteration order changed; refusing to score"
            )
        row = by_id.get(str(fixture["id"]))
        if row is None:
            missing.append(fixture["id"])
            return ""  # fails schema validation -> counted as failed
        if row.get("error"):
            errored.append(fixture["id"])
            return ""
        return row.get("response") or ""

    summary = run_tripwire_eval(fixtures, model_fn, verbose=True)
    summary["backend_label"] = args.backend_label
    summary["responses_file"] = str(args.responses)
    summary["fixtures_file"] = str(args.fixtures)
    summary["missing_responses"] = missing
    summary["error_responses"] = errored

    if missing or errored:
        print(
            f"WARNING: {len(missing)} missing + {len(errored)} errored responses "
            "were scored as FAILED (fail-closed accounting, not silently dropped)",
            file=sys.stderr,
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
