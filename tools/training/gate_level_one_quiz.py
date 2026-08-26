"""Level One *quiz* gate — a verifiable strategy exam, no judge required.

Companion to ``gate_level_one.py``. That gate scores free-form picks on mined
positions and needs human-graded gold. This one asks multiple-choice questions
extracted from Reid Duke's *Level One* (Wizards of the Coast's official
strategy course), where every correct answer is backed by a VERBATIM quotation
from the source article. That means:

  * no expert grader is needed — the key is fixed and citable;
  * no LLM-as-judge is in the loop, so the score cannot drift with the judge;
  * a disputed item is settled by reading its ``citation`` against its ``url``.

POSITION BIAS
-------------
A model that always answers "C" should score 25%, not 40%. Options are shuffled
per item with a fixed seed (``--seed``, default 1337) and the model's letter is
mapped back to the true option, so the letter layout is stable across runs but
uncorrelated with the answer key.

LENGTH BIAS
-----------
Hand-written multiple choice leaks through option length — the correct answer
tends to be the one with the full explanation attached. The runner prints what
an "always pick the longest option" heuristic would score on this exact quiz,
so a suspiciously good result can be checked against the dumbest possible
cheat. If that baseline ever climbs far above 25%, the item set needs
rebalancing, not the model.

UNPARSEABLE OUTPUT
------------------
Counted as wrong (a coach that cannot emit the schema is useless in-product)
but reported separately, so "the model is bad at Magic" is never confused with
"the model is bad at JSON".

Usage:
    python -m tools.training.gate_level_one_quiz --backend online:dsv4
    python -m tools.training.gate_level_one_quiz --backend online:dsv4 \
        --thinking --report tools/training/data/level_one_quiz_dsv4.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATEWAY = os.environ.get("MTGACOACH_GATEWAY", "http://localhost:8444/v1/chat/completions")
LETTERS = ["A", "B", "C", "D"]

SKILL_LABEL = {
    "MANA": "Mana",
    "CARD_ADVANTAGE": "Card advantage",
    "TEMPO": "Tempo",
    "TEMPO_VS_CARDS": "Tempo vs card advantage",
    "ATTACK_BLOCK": "Attacking and blocking",
    "DAMAGE_RACING": "Damage racing",
    "THREATS_ANSWERS": "Threats and answers",
    "PERMISSION": "Permission spells",
    "CREATURE_LANDS": "Creature lands",
    "SYMMETRIC": "Symmetric effects",
    "SWEEPERS": "Board sweepers",
    "SEQUENCING": "Sequencing",
    "INEVITABILITY": "Inevitability",
    "INVESTMENT": "Investment",
    "ROLE_ASSIGNMENT": "Role assignment",
    "AHEAD_BEHIND": "Playing ahead / behind",
    "MULLIGANS": "Mulligans",
    "FLEXIBILITY": "Flexibility",
    "WHEN_TO_CAST": "When to cast your spells",
    "SAFE_VS_SCARED": "Playing safe / scared",
}

SYSTEM = (
    "You are an expert Magic: The Gathering strategist being examined on "
    "fundamental in-game strategy. Choose the single best answer. Judge each "
    "question on Magic strategy merit, not on how the options are worded."
)

ASK = (
    "\n\nRespond with STRICT JSON only, no prose and no code fences:\n"
    '{"answer": "A|B|C|D", "why": "<one sentence>"}'
)


def load_key() -> str:
    """Gateway key from the environment, else the LiteLLM .env. Never hardcoded."""
    k = os.environ.get("LITELLM_MASTER_KEY")
    if k:
        return k
    env = Path(os.environ.get("LITELLM_ENV", "/home/joshu/docker-stack/litellm/.env"))
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("no LITELLM_MASTER_KEY (env or litellm .env)")


def shuffled(item: dict, seed: int) -> tuple[list[str], int]:
    """Return (options in presentation order, index of the correct one).

    Seeded per item id so a rerun presents the identical layout — the score is
    reproducible — while the correct letter still varies across items.
    """
    order = list(range(len(item["options"])))
    random.Random(f"{seed}:{item['id']}").shuffle(order)
    opts = [item["options"][i] for i in order]
    return opts, order.index(item["answer"])


def render(item: dict, opts: list[str]) -> str:
    lines = [item["question"], ""]
    lines += [f"{LETTERS[i]}. {o}" for i, o in enumerate(opts)]
    return "\n".join(lines) + ASK


def parse(raw: str) -> str | None:
    """Pull the chosen letter out of the model's reply, or None."""
    if not raw:
        return None
    m = re.search(r'"answer"\s*:\s*"?\s*([ABCD])', raw, re.I)
    if m:
        return m.group(1).upper()
    # bare-letter fallback: a lone "B" or "Answer: B" on its own line
    m = re.search(r"(?:^|\n)\s*(?:answer\s*[:=]\s*)?\(?([ABCD])\)?\s*[.):]?\s*(?:$|\n)",
                  raw.strip(), re.I)
    return m.group(1).upper() if m else None


def ask(session, model: str, key: str, prompt: str, thinking: bool) -> tuple[str | None, str, str]:
    """Return (letter, why, raw). One retry on unparseable output, per spec."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 2000 if thinking else 300,
    }
    if not thinking:
        body["extra_body"] = {"chat_template_kwargs": {"thinking": False}}
    raw = ""
    for attempt in range(2):
        try:
            r = session.post(GATEWAY, json=body,
                             headers={"Authorization": f"Bearer {key}"}, timeout=180)
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"].get("content") or ""
            letter = parse(raw)
            if letter:
                w = re.search(r'"why"\s*:\s*"([^"]*)"', raw)
                return letter, (w.group(1) if w else ""), raw
        except Exception as e:  # noqa: BLE001 — one dead call must not kill the gate
            raw = f"error: {type(e).__name__}: {e}"
        if attempt == 0:
            time.sleep(1.5)
    return None, "", raw


def table(title: str, buckets: dict, label=lambda k: k) -> None:
    print(f"\n{title}")
    print(f"  {'':<26} {'score':>7}  {'n':>4}   correct")
    for k in sorted(buckets, key=lambda x: (-buckets[x]["n"], x)):
        d = buckets[k]
        pct = f"{100 * d['correct'] / d['n']:.0f}%" if d["n"] else "—"
        print(f"  {label(k):<26} {pct:>7}  {d['n']:>4}   {d['correct']}/{d['n']}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--quiz", type=Path,
                   default=REPO / "tools/training/data/level_one_quiz.json")
    p.add_argument("--backend", default="online:dsv4",
                   help="online:<model-name> (through the LiteLLM gateway)")
    p.add_argument("--thinking", action="store_true",
                   help="let the model reason before answering")
    p.add_argument("--seed", type=int, default=1337,
                   help="option-shuffle seed; same seed = same layout")
    p.add_argument("--report", type=Path, default=None,
                   help="dump per-item results (model answer, key, citation) as JSON")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--concurrency", type=int, default=4)
    args = p.parse_args()

    import requests  # noqa: PLC0415 — optional dep, only needed to actually run

    items = json.loads(args.quiz.read_text())
    if args.limit:
        items = items[: args.limit]
    if not args.backend.startswith("online:"):
        raise SystemExit("only online:<model> is wired today; point --backend at the gateway")
    model = args.backend.split(":", 1)[1]
    key = load_key()
    session = requests.Session()

    done = [0]

    def run(item: dict) -> dict:
        opts, correct_idx = shuffled(item, args.seed)
        letter, why, raw = ask(session, model, key, render(item, opts), args.thinking)
        picked = LETTERS.index(letter) if letter else None
        row = {
            "id": item["id"], "lesson": item["lesson"], "skill": item["skill"],
            "type": item["type"], "question": item["question"],
            "options_presented": opts,
            "correct_letter": LETTERS[correct_idx],
            "correct_option": opts[correct_idx],
            "model_letter": letter,
            "model_option": opts[picked] if picked is not None else None,
            "unparseable": letter is None,
            "correct": picked == correct_idx,
            "why": why, "citation": item["citation"], "url": item["url"],
        }
        if letter is None:
            row["raw"] = raw[:1000]
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"  {done[0]}/{len(items)}", flush=True)
        return row

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as ex:
        rows = list(ex.map(run, items))

    n = len(rows)
    correct = sum(r["correct"] for r in rows)
    unparse = sum(r["unparseable"] for r in rows)
    by_skill: dict = defaultdict(lambda: {"n": 0, "correct": 0})
    by_type: dict = defaultdict(lambda: {"n": 0, "correct": 0})
    by_lesson: dict = defaultdict(lambda: {"n": 0, "correct": 0})
    for r in rows:
        for b, k in ((by_skill, r["skill"]), (by_type, r["type"]), (by_lesson, r["lesson"])):
            b[k]["n"] += 1
            b[k]["correct"] += r["correct"]

    # Dumbest-possible-cheat control: does option length alone answer the quiz?
    longest = sum(1 for it in items
                  if max(range(len(it["options"])), key=lambda k: len(it["options"][k]))
                  == it["answer"])

    print(f"\nLevel One quiz gate — {model}"
          f"{' (thinking on)' if args.thinking else ''}, seed {args.seed}")
    print(f"{'=' * 52}")
    print(f"OVERALL  {100 * correct / n:.1f}%   {correct}/{n} correct"
          f"   ({unparse} unparseable, counted wrong)"
          f"   [chance = 25%]   {time.time() - t0:.0f}s")
    print(f"         baseline: 'always pick the longest option' scores "
          f"{100 * longest / n:.0f}% ({longest}/{n})")
    table("By skill", by_skill, lambda k: SKILL_LABEL.get(k, k))
    table("By type", by_type)
    table("By lesson", by_lesson)

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            "model": model, "thinking": args.thinking, "seed": args.seed,
            "n": n, "correct": correct, "accuracy": correct / n if n else None,
            "unparseable": unparse,
            "by_skill": {k: dict(v) for k, v in by_skill.items()},
            "by_type": {k: dict(v) for k, v in by_type.items()},
            "by_lesson": {k: dict(v) for k, v in by_lesson.items()},
            "rows": rows,
        }, indent=1))
        print(f"\nwrote {args.report}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
