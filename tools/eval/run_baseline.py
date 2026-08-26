"""One-command advice-quality baseline against a litellm/OpenAI endpoint.

Runs the whole loop (run -> judge -> report) in one call, driving a
properly-keyed backend (the CLI openai-compatible spec hardcodes
api_key="any", which won't auth against a real keyed endpoint, so this
builds the BackendSpec directly).

Usage:
    python -m tools.eval.run_baseline \
        --endpoint http://10.0.0.10:8444/v1 \
        --model deepseek-v4-flash \
        --limit 60

Key resolution: --key, else MTGACOACH_LITELLM_KEY, else tools/eval/data/.eval_key.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "tools" / "eval" / "data"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.eval.judge import judge  # noqa: E402
from tools.eval.report import report  # noqa: E402
from tools.eval.run import BackendSpec, run  # noqa: E402


def _resolve_key(explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("MTGACOACH_LITELLM_KEY")
    if env:
        return env
    key_file = DATA / ".eval_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "no key: pass --key, set MTGACOACH_LITELLM_KEY, or write tools/eval/data/.eval_key"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--endpoint", default="http://10.0.0.10:8444/v1")
    ap.add_argument("--model", default="deepseek-v4-flash")
    ap.add_argument("--key", default=None)
    ap.add_argument("--prompts", type=Path, default=DATA / "prompts.jsonl")
    ap.add_argument("--responses", type=Path, default=DATA / "responses.jsonl")
    ap.add_argument("--scores", type=Path, default=DATA / "scores.jsonl")
    ap.add_argument("--csv", type=Path, default=DATA / "report.csv")
    ap.add_argument("--json", type=Path, default=DATA / "report.json")
    ap.add_argument("--judge-model", default=None, help="model for grading (default: same as --model)")
    ap.add_argument("--limit", type=int, default=None, help="only first N prompts (dev/quick runs)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--skip-run", action="store_true", help="judge+report only (reuse responses.jsonl)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
    )
    key = _resolve_key(args.key)

    label = f"{args.model}@{args.endpoint.split('://')[1].split(':')[0]}"
    candidate = BackendSpec(label=label, model=args.model, base_url=args.endpoint, api_key=key)

    if not args.skip_run:
        run(
            prompts_path=args.prompts,
            responses_path=args.responses,
            backends=[candidate],
            limit=args.limit,
            concurrency=args.concurrency,
        )

    judge_backend = BackendSpec(
        label=f"judge-{args.judge_model or args.model}",
        model=args.judge_model or args.model,
        base_url=args.endpoint,
        api_key=key,
    )
    judge(
        prompts_path=args.prompts,
        responses_path=args.responses,
        scores_path=args.scores,
        judge_backend=judge_backend,
        limit=args.limit,
    )
    report(
        responses_path=args.responses,
        scores_path=args.scores,
        csv_path=args.csv,
        json_path=args.json,
    )


if __name__ == "__main__":
    main()
