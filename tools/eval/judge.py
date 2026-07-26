"""LLM-as-judge scoring for the eval responses.

Reads prompts.jsonl + responses.jsonl, sends each (prompt, response) pair to
a strong online judge model, records 1-5 scores on a fixed rubric, and
appends the scores to a separate JSONL file.

Rubric (each scored 1-5; 1 = bad, 5 = great):
    correctness   — Does the advice match what an expert MTG coach would
                    actually do given the legal actions and game state?
    reasoning     — Is the reasoning sound and tied to specific game-state
                    facts, not generic platitudes?
    conciseness   — Is the advice short enough for real-time voice, with
                    no filler?
    legality      — Does the advice reference an action that's actually
                    legal, given the prompt's listed legal_actions?

Usage:
    python -m tools.eval.judge \\
        --prompts tools/eval/data/prompts.jsonl \\
        --responses tools/eval/data/responses.jsonl \\
        --scores tools/eval/data/scores.jsonl \\
        --judge-backend online:deepseek-v4-flash \\
        --license-key $MTGACOACH_LICENSE_KEY

Idempotent: existing (prompt_id, backend) score rows are preserved.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from tools.eval.run import BackendSpec, _append_jsonl, _read_jsonl, _stable_prompt_id  # noqa: E402

logger = logging.getLogger("eval.judge")


JUDGE_SYSTEM = """You are an expert Magic: The Gathering Arena coach evaluator.

You will be shown the system+user prompt that was sent to a coaching LLM,
and the response that LLM produced. Score the response on the following
rubric, each 1-5 (1 = bad, 5 = great):

  - correctness:  Does the advice match what an expert MTG coach would
                  actually do given the legal actions and game state shown
                  in the prompt? Penalize plays that ignore obvious tempo,
                  card disadvantage, or lethal lines.
  - reasoning:    Is the reasoning grounded in *specific* game-state facts
                  from the prompt (cards in hand, opponent's threats,
                  available mana, turn number) rather than generic
                  platitudes ("develop your board")? Penalize hallucinated
                  cards or rules.
  - conciseness:  Is the advice short enough to be spoken in real time
                  during a turn? Penalize wall-of-text responses.
  - legality:     If the prompt lists legal actions, does the advice
                  reference one that's actually in that list? If no legal
                  actions are listed, score 5.

Output STRICT JSON with these fields (and nothing else):
{
  "correctness": int,
  "reasoning": int,
  "conciseness": int,
  "legality": int,
  "notes": "one sentence explaining the lowest score"
}
"""


def _build_user_message(prompt: dict, response: dict) -> str:
    parts = [
        "=== ORIGINAL SYSTEM PROMPT ===",
        prompt.get("system") or "(none)",
        "",
        "=== ORIGINAL USER MESSAGE ===",
        prompt.get("user") or "(none)",
        "",
        "=== RESPONSE FROM CANDIDATE LLM ===",
        response.get("response") or "(empty)",
        "",
        "Now score the response per the rubric. Output JSON only.",
    ]
    return "\n".join(parts)


def _parse_judge_json(text: str) -> dict:
    """Extract a JSON object from the judge's response, tolerating fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        # Strip ```json ... ``` fence
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    # Find the first { ... last } in case there's prose around it.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return json.loads(text)


def _existing_keys(scores_path: Path) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for r in _read_jsonl(scores_path):
        pid = r.get("prompt_id")
        be = r.get("backend")
        if pid and be:
            keys.add((str(pid), str(be)))
    return keys


def judge(
    prompts_path: Path,
    responses_path: Path,
    scores_path: Path,
    judge_backend: BackendSpec,
    limit: Optional[int] = None,
) -> None:
    prompts = {_stable_prompt_id(p, i): p for i, p in enumerate(_read_jsonl(prompts_path))}
    responses = list(_read_jsonl(responses_path))
    if not responses:
        logger.error(f"no responses in {responses_path}")
        sys.exit(1)

    done = _existing_keys(scores_path)
    judge_client = judge_backend.build()

    todo = [r for r in responses if (str(r.get("prompt_id")), str(r.get("backend"))) not in done]
    if limit:
        todo = todo[:limit]
    logger.info(f"to-judge={len(todo)} of {len(responses)} responses; judge={judge_backend.label}")

    for r in todo:
        pid = str(r.get("prompt_id"))
        be = str(r.get("backend"))
        prompt = prompts.get(pid)
        if not prompt:
            logger.warning(f"no prompt for response {pid}; skipping")
            continue
        if r.get("error"):
            logger.info(f"{pid} {be}: response had error, recording zero scores")
            score_record = {
                "prompt_id": pid,
                "backend": be,
                "correctness": 0,
                "reasoning": 0,
                "conciseness": 0,
                "legality": 0,
                "notes": f"backend errored: {r.get('error')}",
                "judge_backend": judge_backend.label,
                "ts": time.time(),
            }
            _append_jsonl(scores_path, score_record)
            continue

        user_msg = _build_user_message(prompt, r)
        t0 = time.perf_counter()
        try:
            judge_text = judge_client.complete(
                JUDGE_SYSTEM,
                user_msg,
                max_tokens=400,
                temperature=0.0,
                request_timeout_s=120.0,
            )
            scores = _parse_judge_json(judge_text)
        except Exception as e:
            logger.warning(f"judge {pid} {be} failed: {e}")
            logger.debug(traceback.format_exc())
            continue
        latency = (time.perf_counter() - t0) * 1000

        for k in ("correctness", "reasoning", "conciseness", "legality"):
            v = scores.get(k)
            try:
                scores[k] = max(1, min(5, int(v)))
            except (TypeError, ValueError):
                scores[k] = 0

        record = {
            "prompt_id": pid,
            "backend": be,
            **{k: scores.get(k) for k in ("correctness", "reasoning", "conciseness", "legality")},
            "notes": str(scores.get("notes", ""))[:300],
            "judge_backend": judge_backend.label,
            "judge_latency_ms": round(latency, 1),
            "ts": time.time(),
        }
        _append_jsonl(scores_path, record)
        logger.info(
            f"{pid} {be}: c={record['correctness']} r={record['reasoning']} "
            f"q={record['conciseness']} L={record['legality']}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--prompts", required=True, type=Path)
    parser.add_argument("--responses", required=True, type=Path)
    parser.add_argument("--scores", required=True, type=Path)
    parser.add_argument(
        "--judge-backend",
        default="online:deepseek-v4-flash",
        help="BackendSpec for the grader (default: online:deepseek-v4-flash)",
    )
    parser.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    judge_backend = BackendSpec.parse(args.judge_backend, license_key=args.license_key)
    judge(args.prompts, args.responses, args.scores, judge_backend, limit=args.limit)


if __name__ == "__main__":
    main()
