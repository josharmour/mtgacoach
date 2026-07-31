#!/usr/bin/env python3
"""Eyeball helper: sample rendered combat records + show raw log context.

Not part of the pipeline; used for the manual validation step of the
combat-adapter PR (render N records and check them against the raw log).
"""
import argparse
import json
import random


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    recs = []
    for split in ("train", "val", "test"):
        with open(f"{args.outdir}/{split}.jsonl", encoding="utf-8") as f:
            for line in f:
                r = json.loads(line)
                if r.get("kind") in ("attack_commit", "block_assign"):
                    r["_split"] = split
                    recs.append(r)
    atk = [r for r in recs if r["kind"] == "attack_commit"]
    blk = [r for r in recs if r["kind"] == "block_assign"]
    random.seed(args.seed)
    sample = random.sample(atk, args.n) + random.sample(blk, args.n)

    wanted = {}
    for i, r in enumerate(sample):
        p = r["meta"]["provenance"]
        for key in ("line_opener", "line_pool", "line_resolution", "line_context"):
            wanted.setdefault(p[key], []).append((i, key))

    raw = {}
    with open(args.log, encoding="utf-8", errors="replace") as f:
        for ln, line in enumerate(f, 1):
            if ln in wanted:
                raw[ln] = line.rstrip()[:240]

    for i, r in enumerate(sample):
        m = r["meta"]
        print("=" * 110)
        print(
            f"RECORD {i}: {r['kind']} split={r['_split']} "
            f"creature={m['creature']!r} pick={m['answer_pick']}/{m['menu_size']} "
            f"game={m['game_id']} turn={m['turn']} outcome={m['outcome']}"
        )
        p = m["provenance"]
        for key in ("line_opener", "line_pool", "line_resolution", "line_context"):
            print(f"  RAW {key} L{p[key]}: {raw.get(p[key], '<not read>')}")
        print("- USER PROMPT " + "-" * 60)
        print(r["user"])
        print("- RESPONSE -", r["response"])


if __name__ == "__main__":
    main()
