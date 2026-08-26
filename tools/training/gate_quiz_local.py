"""Run the Level One quiz against a LOCAL model (base + LoRA adapter).

The gateway runner (``gate_level_one_quiz.py``) scores models that are already
served. This one loads a base model and an optional PEFT adapter directly, so a
freshly trained LoRA can be gated without standing up a server — useful when the
serving GPUs are occupied by something else.

Prompt construction, option shuffling and answer parsing are imported from the
gateway runner rather than reimplemented, so scores from the two paths are
directly comparable (same seed => same option layout).

Device: defaults to CPU on purpose. The R9700 is usually mid-self-play for
MageZero, and a 12B in bf16 does not fit beside it; risking an OOM in a live
training run to save wall-clock is a bad trade. Pass --device cuda when a card
is genuinely free.

Usage:
    python -m tools.training.gate_quiz_local \
        --base google/gemma-4-12B-it \
        --adapter tools/training/checkpoints/dsv4_v4_sft \
        --report tools/training/data/quiz_v4.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.training.gate_level_one_quiz import (  # noqa: E402
    LETTERS, SYSTEM, parse, render, shuffled,
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quiz", type=Path,
                   default=REPO / "tools/training/data/level_one_quiz.json")
    p.add_argument("--base", default="google/gemma-4-12B-it")
    p.add_argument("--adapter", type=Path, default=None,
                   help="PEFT adapter dir; omit to score the base model")
    p.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--max-new", type=int, default=64)
    p.add_argument("--report", type=Path, default=None)
    args = p.parse_args()

    import torch  # noqa: PLC0415
    from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: PLC0415

    items = json.loads(args.quiz.read_text())
    if args.limit:
        items = items[: args.limit]

    print(f"loading {args.base} on {args.device} ...", flush=True)
    tok = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForCausalLM.from_pretrained(
        args.base, dtype=torch.bfloat16,
        device_map=None, low_cpu_mem_usage=True,
    )
    if args.adapter:
        from peft import PeftModel  # noqa: PLC0415
        model = PeftModel.from_pretrained(model, str(args.adapter))
        print(f"adapter: {args.adapter}", flush=True)
    model = model.to(args.device).eval()

    per_skill = defaultdict(lambda: [0, 0])
    per_type = defaultdict(lambda: [0, 0])
    correct = unparse = 0
    rows = []
    t0 = time.time()
    for i, item in enumerate(items, 1):
        opts, gold = shuffled(item, args.seed)
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": render(item, opts)}]
        text = tok.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        enc = tok(text, return_tensors="pt").to(args.device)
        with torch.no_grad():
            out = model.generate(**enc, max_new_tokens=args.max_new, do_sample=False,
                                 pad_token_id=tok.eos_token_id)
        raw = tok.decode(out[0][enc["input_ids"].shape[1]:], skip_special_tokens=True)
        letter = parse(raw)
        if letter is None:
            unparse += 1
        ok = letter is not None and LETTERS.index(letter) == gold
        correct += ok
        per_skill[item["skill"]][0] += ok
        per_skill[item["skill"]][1] += 1
        per_type[item["type"]][0] += ok
        per_type[item["type"]][1] += 1
        rows.append({"id": item["id"], "skill": item["skill"], "type": item["type"],
                     "model": letter, "gold": LETTERS[gold], "ok": bool(ok),
                     "raw": raw[:200]})
        if i % 20 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(items)}  running {100*correct/i:.0f}%  "
                  f"{el/i:.1f}s/item  eta {(len(items)-i)*el/i/60:.0f}m", flush=True)

    name = f"{args.base}" + (f" + {args.adapter.name}" if args.adapter else " (base)")
    n = len(items)
    print(f"\n{name}  seed {args.seed}")
    print(f"OVERALL {100*correct/n:.1f}%  ({correct}/{n})"
          f"{f', {unparse} unparseable' if unparse else ''}\n")
    print(f"{'skill':<22} {'score':>8}   n")
    for s in sorted(per_skill, key=lambda k: -per_skill[k][1]):
        c, t = per_skill[s]
        print(f"{s:<22} {100*c/t:7.0f}%   {t}")
    print()
    for ty in sorted(per_type):
        c, t = per_type[ty]
        print(f"{ty:<22} {100*c/t:7.0f}%   {t}")

    if args.report:
        args.report.write_text(json.dumps(
            {"model": name, "seed": args.seed, "overall": correct / n,
             "correct": correct, "n": n, "unparseable": unparse,
             "per_skill": {k: v for k, v in per_skill.items()},
             "per_type": {k: v for k, v in per_type.items()}, "rows": rows}, indent=1))
        print(f"\nwrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
