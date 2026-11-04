"""Bounded concurrent replay; no Arena connection. Private cases and output required.

Alternates fast-only, fast plus capped thinking, and two fast plus capped thinking.
Unlike the production race, this stress test lets every leg finish to measure the
fast leg's latency. Never sends more than three requests from this runner at once.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from pathlib import Path

import httpx
from replay_thinking import run_one


async def main(args):
    cases = json.loads(args.cases.read_text())["cases"]
    cases = [dict(c, budget_s=12, thinking_token_budget=256) for c in cases if c["id"] in args.ids.split(",")]
    schedule = [(c, i) for i in range(args.rounds) for c in cases]
    rng = random.Random(1104)
    rng.shuffle(schedule)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ["ARENAMCP_REPLAY_KEY"]},
        timeout=None,
        trust_env=False,
    ) as client:
        with os.fdopen(fd, "w") as out:
            for case, attempt in schedule:
                conditions = ["fast-only", "fast-plus-thinker", "two-fast-plus-thinker"]
                rng.shuffle(conditions)
                for condition in conditions:
                    specs = [(case, "none", "primary-fast")]
                    if condition != "fast-only":
                        specs.append((case, 1, "thinker"))
                    if condition == "two-fast-plus-thinker":
                        control = next(c for c in cases if c["id"] == "beneficial_self_target")
                        specs.append((control, "none", "other-client-fast"))
                    results = await asyncio.gather(
                        *(
                            run_one(client, args.url + "/chat/completions", c, effort, "current")
                            for c, effort, _ in specs
                        )
                    )
                    for result, (_, _, leg) in zip(results, specs, strict=True):
                        result.update(condition=condition, attempt=attempt, leg=leg, stress_run=True)
                        out.write(json.dumps(result) + "\n")
                        print(
                            case["id"],
                            attempt,
                            condition,
                            leg,
                            result["status"],
                            round(result["total_ms"]),
                            result["assessment"].get("option_ids"),
                            flush=True,
                        )
                    out.flush()
                    if any(r.get("http_status") in {401, 403, 429, 503} for r in results):
                        raise SystemExit("Stopped after authentication/admission/service failure")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--ids", default="slickshot_cast,slickshot_target,beneficial_self_target")
    parser.add_argument("--url", default="https://api.mtgacoach.com/v1")
    asyncio.run(main(parser.parse_args()))
