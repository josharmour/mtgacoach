"""Build a real prompt corpus from captured rendered-prompt data.

Reads forge_rendered_prompts.jsonl (real decision prompts rendered from live
Forge game states) and writes the canonical tools/eval/data/prompts.jsonl that
run.py/judge.py consume. Idempotent: reframes records into the
{id, system, user, max_tokens, temperature, meta} schema.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, default=Path("tools/eval/data/forge_rendered_prompts.jsonl"))
    ap.add_argument("--out", type=Path, default=Path("tools/eval/data/prompts.jsonl"))
    args = ap.parse_args()

    n = 0
    with args.src.open(encoding="utf-8") as f, args.out.open("w", encoding="utf-8") as o:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rec = {
                "id": d.get("id"),
                "system": d.get("system") or "",
                "user": d.get("user") or "",
                "max_tokens": d.get("max_tokens") or 400,
                "temperature": d.get("temperature") if d.get("temperature") is not None else 0.0,
                "meta": {"corpus": "forge-rendered-v2", "provenance": "rendered decision prompts from live Forge game states"},
            }
            if d.get("meta"):
                for k in ("protocol", "turn", "trigger", "source", "menu_size", "gaps", "fabrications"):
                    if k in d["meta"]:
                        rec["meta"][k] = d["meta"][k]
            if rec["user"]:
                o.write(json.dumps(rec) + "\n")
                n += 1
    print(f"wrote {n} prompts to {args.out}")


if __name__ == "__main__":
    main()
