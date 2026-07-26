"""Prompt length measuring tool for MTGA coach training datasets.

Audits token length distribution (P50, P95, P99, Max) over prompt corpora
to ensure max_length is correctly configured.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from transformers import AutoTokenizer

logger = logging.getLogger("tools.training.measure_prompt_lengths")


def main():
    p = argparse.ArgumentParser(description="Audit token lengths of prompt datasets.")
    p.add_argument("--model_id", default="google/gemma-4-E2B-it", help="Tokenizer model ID or path")
    p.add_argument("--dataset", required=True, type=Path, help="JSON or JSONL dataset file")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    logger.info(f"Loading tokenizer: {args.model_id}")
    tokenizer = AutoTokenizer.from_pretrained(args.model_id)

    lengths = []
    logger.info(f"Reading dataset: {args.dataset}")
    with open(args.dataset, encoding="utf-8") as f:
        content = f.read().strip()
        if content.startswith("["):
            data = json.loads(content)
        else:
            data = [json.loads(line) for line in content.splitlines() if line.strip()]

    for item in data:
        sys_p = item.get("system") or item.get("prompt_system") or ""
        usr_p = item.get("user") or item.get("prompt_user") or ""
        resp = item.get("response") or item.get("chosen") or ""
        text = f"{sys_p}\n{usr_p}\n{resp}"
        tokens = tokenizer.encode(text)
        lengths.append(len(tokens))

    if not lengths:
        logger.warning("No records found in dataset.")
        return

    lengths.sort()
    n = len(lengths)
    p50 = lengths[int(n * 0.50)]
    p95 = lengths[int(n * 0.95)]
    p99 = lengths[int(n * 0.99)]
    max_len = lengths[-1]

    logger.info(f"Audited {n} prompts:")
    logger.info(f"  P50: {p50} tokens")
    logger.info(f"  P95: {p95} tokens")
    logger.info(f"  P99: {p99} tokens")
    logger.info(f"  Max: {max_len} tokens")
    logger.info(f"Recommended --max_length setting: >= {max(2048, p95 + 512)}")


if __name__ == "__main__":
    main()
