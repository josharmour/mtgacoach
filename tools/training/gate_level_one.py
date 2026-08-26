"""Level One strategic gate — score a model per MTG skill, not as one blended number.

The position set is mined by ``mine_level_one_skills.py`` against the skill
taxonomy of Reid Duke's *Level One* (Wizards of the Coast's own strategy
course). Each position isolates one in-game decision skill: mana sequencing,
tempo, attacking and blocking, threats and answers, and so on.

WHY THIS EXISTS
---------------
The previous "55 tripwires" were 7 distinct situations duplicated 10-15x, so
every score built on them (including dsv4's headline 96.4%) had the precision
of n=7. This gate uses distinct real positions, each with >=3 genuine options
after Pass / FloatMana / Activate_Mana are removed.

GOLD ANSWERS ARE HUMAN-CONFIRMED, NOT TEACHER-DERIVED
-----------------------------------------------------
Scoring a dsv4-distilled student against dsv4's own picks measures imitation,
not skill — the student would score highest by copying its teacher's mistakes.
So the gold file is produced by human grading (the review UI), where the owner
either confirms the agreed pick or, on disagreements, rules for the model, for
themselves, for "either", or for "neither".

Positions the owner marked "either" are scored as correct for any of the two
named picks. Positions marked "skip"/"unsure" are excluded from scoring, never
silently counted as wrong.

REPORTING
---------
Per-skill accuracy with the sample size beside it, because several skills have
single-digit support and a bare percentage would overstate them. Skills the
corpus cannot support at all (board sweepers; symmetric effects) are absent by
construction — see the miner's verdict table.

Usage:
    python -m tools.training.gate_level_one \
        --backend online:dsv4 --gold tools/training/data/level_one_gold.json
    python -m tools.training.gate_level_one \
        --backend lora:tools/training/checkpoints/dsv4_v4_sft --report out.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATEWAY = os.environ.get("MTGACOACH_GATEWAY", "http://localhost:8444/v1/chat/completions")

SKILL_ORDER = [
    "MANA_SEQUENCING", "TEMPO_CURVE", "TEMPO_VS_CARDS", "ATTACK_BLOCK",
    "DAMAGE_RACING", "THREATS_ANSWERS", "PERMISSION_TIMING", "CREATURE_LANDS",
    "INVESTMENT", "AHEAD_BEHIND", "ROLE_ASSIGNMENT", "FLEXIBILITY", "INEVITABILITY",
]
SKILL_LABEL = {
    "MANA_SEQUENCING": "Mana sequencing",
    "TEMPO_CURVE": "Tempo / curve",
    "TEMPO_VS_CARDS": "Tempo vs card advantage",
    "ATTACK_BLOCK": "Attacking and blocking",
    "DAMAGE_RACING": "Damage racing",
    "THREATS_ANSWERS": "Threats and answers",
    "PERMISSION_TIMING": "Permission timing",
    "CREATURE_LANDS": "Creature lands",
    "INVESTMENT": "Investment / overextension",
    "AHEAD_BEHIND": "Playing ahead / behind",
    "ROLE_ASSIGNMENT": "Role assignment",
    "FLEXIBILITY": "Flexibility",
    "INEVITABILITY": "Inevitability",
}


def load_key() -> str:
    k = os.environ.get("LITELLM_MASTER_KEY")
    if k:
        return k
    env = Path("/home/joshu/docker-stack/litellm/.env")
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("no LITELLM_MASTER_KEY (env or litellm .env)")


ASK = (
    "\n\nAnswer with STRICT JSON only: "
    '{"pick": "<exactly one of the action keys listed below>", "why": "<one sentence>"}\n'
    "Action keys: "
)


def ask_model(session, model: str, key: str, system: str, user: str,
              keys: list[str], thinking: bool) -> tuple[str | None, str, str]:
    """Return (pick, rationale, raw). pick is None when unparseable/illegal."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user + ASK + ", ".join(keys)},
        ],
        "temperature": 0,
        "max_tokens": 1500 if thinking else 300,
    }
    if not thinking:
        body["extra_body"] = {"chat_template_kwargs": {"thinking": False}}
    for attempt in range(3):
        try:
            r = session.post(GATEWAY, json=body,
                             headers={"Authorization": f"Bearer {key}"}, timeout=180)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            raw = msg.get("content") or ""
            m = re.search(r'"pick"\s*:\s*"([^"]+)"', raw)
            if m and m.group(1) in keys:
                w = re.search(r'"why"\s*:\s*"([^"]*)"', raw)
                return m.group(1), (w.group(1) if w else ""), raw
            # production autopilot schema fallback: {"actions":[{"pick": <index>}]}
            m2 = re.search(r'"pick"\s*:\s*(\d+)', raw)
            if m2:
                idx = int(m2.group(1)) - 1
                if 0 <= idx < len(keys):
                    return keys[idx], "", raw
            if attempt == 2:
                return None, "", raw
        except Exception as e:  # noqa: BLE001 — a dead call must not kill the gate
            if attempt == 2:
                return None, f"error: {type(e).__name__}", ""
            time.sleep(2 * (attempt + 1))
    return None, "", ""


def score(gold_entry: dict, pick: str | None) -> str:
    """correct / wrong / excluded — never silently penalise an ungraded position."""
    v = (gold_entry or {}).get("verdict")
    if v in (None, "", "skip", "unsure", "UNGRADED"):
        return "excluded"
    accept = set(gold_entry.get("accept") or [])
    if not accept:
        return "excluded"
    if pick is None:
        return "wrong"
    return "correct" if pick in accept else "wrong"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--positions", type=Path,
                   default=REPO / "tools/training/data/level_one_positions.json")
    p.add_argument("--gold", type=Path,
                   default=REPO / "tools/training/data/level_one_gold.json")
    p.add_argument("--backend", default="online:dsv4",
                   help="online:<model-name> (through the LiteLLM gateway)")
    p.add_argument("--thinking", action="store_true", help="let the model reason first")
    p.add_argument("--report", type=Path, default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    import requests  # noqa: PLC0415 — optional dep, only needed to actually run

    positions = json.loads(args.positions.read_text())
    if args.limit:
        positions = positions[: args.limit]
    gold = {}
    if args.gold.exists():
        gold = {g["id"]: g for g in json.loads(args.gold.read_text())}
    else:
        print(f"[warn] no gold file at {args.gold} — every position will be 'excluded'.\n"
              f"       Grade positions in the review UI and export them first.", file=sys.stderr)

    if not args.backend.startswith("online:"):
        raise SystemExit("only online:<model> is wired today; point --backend at the gateway")
    model = args.backend.split(":", 1)[1]
    key = load_key()
    session = requests.Session()

    per = defaultdict(lambda: {"correct": 0, "wrong": 0, "excluded": 0})
    rows = []
    for i, pos in enumerate(positions, 1):
        keys = pos["menu_keys"]
        pick, why, _raw = ask_model(session, model, key, pos["system"], pos["user"],
                                    keys, args.thinking)
        res = score(gold.get(pos["id"]), pick)
        per[pos["cat"]][res] += 1
        rows.append({"id": pos["id"], "skill": pos["cat"], "pick": pick,
                     "why": why, "result": res,
                     "accept": (gold.get(pos["id"]) or {}).get("accept")})
        if i % 10 == 0:
            print(f"  {i}/{len(positions)}", flush=True)

    print(f"\nLevel One gate — {model}"
          f"{' (thinking on)' if args.thinking else ''}\n")
    print(f"{'skill':<26} {'score':>10}   n")
    tot_c = tot_n = 0
    for s in SKILL_ORDER:
        d = per.get(s)
        if not d:
            continue
        n = d["correct"] + d["wrong"]
        tot_c += d["correct"]; tot_n += n
        acc = f"{100*d['correct']/n:.0f}%" if n else "—"
        note = f"   ({d['excluded']} ungraded)" if d["excluded"] else ""
        print(f"{SKILL_LABEL[s]:<26} {acc:>10}   {n}{note}")
    print(f"{'-'*44}\n{'OVERALL':<26} "
          f"{(f'{100*tot_c/tot_n:.0f}%' if tot_n else '—'):>10}   {tot_n}")
    if tot_n == 0:
        print("\nNothing scored: the gold file is empty or every position is ungraded.")

    if args.report:
        args.report.write_text(json.dumps(
            {"model": model, "thinking": args.thinking,
             "per_skill": {k: dict(v) for k, v in per.items()}, "rows": rows}, indent=1))
        print(f"\nwrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
