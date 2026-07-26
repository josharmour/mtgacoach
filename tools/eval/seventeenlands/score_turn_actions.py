"""Score coach responses against the turn-action ground truth from 17lands.

Reads prompts (with ``meta.actually_did``) + responses, parses each response
into an action category set, and reports per-backend metrics:

  - Exact-match rate     — coach's set equals the actually-played set
  - Mean Jaccard         — |intersection| / |union| across decisions
  - Per-category P/R/F1  — precision, recall, F1 for each tag

Usage:
    python -m tools.eval.seventeenlands.score_turn_actions \\
        --prompts tools/eval/data/turn_action_prompts.jsonl \\
        --responses tools/eval/data/turn_action_responses.jsonl \\
        --json tools/eval/data/turn_action_summary.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


CATEGORIES = ("PLAY_LAND", "CAST_CREATURE", "CAST_SPELL", "ATTACK", "PASS")

# ACTIVATE is parsed (so the regex can still recognize it in responses) but
# stripped from both predicted and actual sets before scoring. Reasoning: the
# 17lands ground truth ``user_abilities`` count includes mana-tap activations
# the coach can't see in our prompts — there's no way for any model to
# correctly predict ACTIVATE under the current prompt format. We drop it from
# scoring rather than penalize models for an unmeasurable category.
_TAG_RE = re.compile(
    r"\b(PLAY_LAND|CAST_CREATURE|CAST_SPELL|CAST|ATTACK|ACTIVATE|PASS)\b",
    re.IGNORECASE,
)
_DROP_TAGS = {"ACTIVATE"}


def parse_action_set(response: str) -> set[str] | None:
    """Extract action tags from the coach response.

    Looks at the FIRST LINE primarily. Falls back to scanning the whole
    response for tag keywords. Returns None if nothing recognized.
    """
    if not response:
        return None
    first = response.strip().splitlines()[0] if response.strip() else ""
    targets: set[str] = set()
    for line in (first, response):
        for m in _TAG_RE.finditer(line):
            tag = m.group(1).upper()
            if tag == "CAST":
                # Bare "CAST" is ambiguous; ignore unless we also saw CAST_*
                continue
            targets.add(tag)
        if targets:
            break
    return targets or None


def _read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def score(
    prompts_path: Path,
    responses_path: Path,
    json_out: Path | None,
    set_code: str | None = None,
) -> None:
    prompts = {p["id"]: p for p in _read_jsonl(prompts_path)}
    responses = list(_read_jsonl(responses_path))
    if not prompts:
        print(f"no prompts in {prompts_path}", file=sys.stderr)
        sys.exit(1)
    if not responses:
        print(f"no responses in {responses_path}", file=sys.stderr)
        sys.exit(1)

    stats: dict[str, dict] = defaultdict(
        lambda: {
            "n": 0,
            "errors": 0,
            "parsed": 0,
            "exact_match": 0,
            "jaccard_sum": 0.0,
            "unparsed_examples": [],
            # Per-category confusion: tp/fp/fn per category
            "per_cat": {c: {"tp": 0, "fp": 0, "fn": 0} for c in CATEGORIES},
        }
    )

    for r in responses:
        be = r.get("backend") or "?"
        pid = r.get("prompt_id")
        prompt = prompts.get(pid)
        if not prompt:
            continue
        s = stats[be]
        s["n"] += 1
        if r.get("error"):
            s["errors"] += 1
            continue

        meta = prompt.get("meta") or {}
        actual = set(meta.get("actually_did") or [])
        # Drop unmeasurable tags (see _DROP_TAGS comment above).
        actual -= _DROP_TAGS
        if not actual:
            continue

        predicted = parse_action_set(r.get("response") or "")
        if predicted is None:
            if len(s["unparsed_examples"]) < 3:
                s["unparsed_examples"].append((pid, (r.get("response") or "")[:160]))
            continue
        predicted -= _DROP_TAGS
        s["parsed"] += 1

        if predicted == actual:
            s["exact_match"] += 1
        s["jaccard_sum"] += _jaccard(predicted, actual)

        # Per-category confusion
        for c in CATEGORIES:
            in_pred = c in predicted
            in_act = c in actual
            if in_pred and in_act:
                s["per_cat"][c]["tp"] += 1
            elif in_pred and not in_act:
                s["per_cat"][c]["fp"] += 1
            elif (not in_pred) and in_act:
                s["per_cat"][c]["fn"] += 1

    # Print headline table
    cols = [
        ("backend", 30),
        ("n", 4),
        ("parse%", 7),
        ("exact_match%", 14),
        ("mean_jaccard", 14),
    ]
    header = " ".join(f"{name:<{w}}" for name, w in cols)
    print()
    print(header)
    print("-" * len(header))
    for be in sorted(stats):
        s = stats[be]
        n = s["n"] or 1
        parse_pct = s["parsed"] / n * 100
        exact = s["exact_match"] / s["parsed"] * 100 if s["parsed"] else 0.0
        mean_j = s["jaccard_sum"] / s["parsed"] if s["parsed"] else 0.0
        print(
            " ".join(
                f"{str(v):<{w}}"
                for v, (_, w) in zip(
                    [be[:30], s["n"], f"{parse_pct:.0f}%", f"{exact:.1f}%", f"{mean_j:.3f}"],
                    cols,
                    strict=True,
                )
            )
        )

    print()
    print("Per-category P/R/F1 (per backend):")
    for be in sorted(stats):
        print(f"  {be}")
        for c in CATEGORIES:
            pc = stats[be]["per_cat"][c]
            tp, fp, fn = pc["tp"], pc["fp"], pc["fn"]
            prec = tp / (tp + fp) if (tp + fp) else 0.0
            rec = tp / (tp + fn) if (tp + fn) else 0.0
            f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
            print(f"    {c:14}  tp={tp:3d}  fp={fp:3d}  fn={fn:3d}  P={prec:.2f}  R={rec:.2f}  F1={f1:.2f}")

    print()
    print("Unparsed examples (first 3 per backend):")
    for be in sorted(stats):
        for pid, snippet in stats[be]["unparsed_examples"]:
            print(f"  {be:30} {pid}: {snippet!r}")

    if json_out:
        backends_payload = []
        for be in sorted(stats):
            s = stats[be]
            n = s["n"] or 0
            parse_rate = s["parsed"] / n if n else None
            exact_rate = s["exact_match"] / s["parsed"] if s["parsed"] else None
            mean_j = s["jaccard_sum"] / s["parsed"] if s["parsed"] else None
            cats: dict[str, dict] = {}
            for c in CATEGORIES:
                pc = s["per_cat"][c]
                tp, fp, fn = pc["tp"], pc["fp"], pc["fn"]
                prec = tp / (tp + fp) if (tp + fp) else None
                rec = tp / (tp + fn) if (tp + fn) else None
                f1 = (
                    2 * prec * rec / (prec + rec)
                    if (prec is not None and rec is not None and (prec + rec))
                    else None
                )
                cats[c] = {
                    "tp": tp,
                    "fp": fp,
                    "fn": fn,
                    "precision": round(prec, 4) if prec is not None else None,
                    "recall": round(rec, 4) if rec is not None else None,
                    "f1": round(f1, 4) if f1 is not None else None,
                }
            backends_payload.append(
                {
                    "backend": be,
                    "n": s["n"],
                    "errors": s["errors"],
                    "parsed": s["parsed"],
                    "exact_match": s["exact_match"],
                    "exact_match_rate": round(exact_rate, 4) if exact_rate is not None else None,
                    "mean_jaccard": round(mean_j, 4) if mean_j is not None else None,
                    "parse_rate": round(parse_rate, 4) if parse_rate is not None else None,
                    "per_category": cats,
                }
            )
        payload = {
            "target": "17lands_turn_action",
            "ts": time.time(),
            "n_prompts": len(prompts),
            "n_responses": len(responses),
            "categories": list(CATEGORIES),
            "backends": backends_payload,
        }
        if set_code:
            payload["set_code"] = set_code
        with open(json_out, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote {json_out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--prompts", required=True, type=Path)
    parser.add_argument("--responses", required=True, type=Path)
    parser.add_argument("--json", type=Path)
    parser.add_argument(
        "--set", dest="set_code", default=None, help="Optional set code to embed in the summary payload"
    )
    args = parser.parse_args()
    score(args.prompts, args.responses, args.json, set_code=args.set_code)


if __name__ == "__main__":
    main()
