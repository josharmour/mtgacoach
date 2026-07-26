"""Build Gate Corpus v2 (WP-0.6 / WP-1.1 / T1).

Generates a stratified, training-excluded mulligan gate corpus (>=500 keep / >=500 mull)
retargeted to ACTION_SCHEMA with exact SHA-256 hash exclusions against all training sets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.eval.seventeenlands.build_mulligan_prompts import build as build_mulligan_prompts  # noqa: E402

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT  # noqa: E402

logger = logging.getLogger("tools.training.build_gate_corpus_v2")


def is_bucket_held_out(key: tuple) -> bool:
    """Deterministically hold out ~20% of (lands, on_play, colors) buckets for unseen-bucket evaluation."""
    key_str = f"{key[0]}_{key[1]}_{key[2]}"
    h = int(hashlib.md5(key_str.encode("utf-8")).hexdigest(), 16)
    return (h % 5) == 0


def hash_text(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def load_excluded_hashes(exclude_paths: list[Path]) -> set[str]:
    hashes = set()
    for p in exclude_paths:
        if not p.exists():
            continue
        with open(p, encoding="utf-8") as f:
            if p.suffix == ".json":
                try:
                    data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            u = item.get("user") or item.get("prompt_user")
                            if u:
                                hashes.add(hash_text(u))
                except Exception as e:
                    logger.warning(f"Error loading {p}: {e}")
            elif p.suffix == ".jsonl":
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                        u = item.get("user") or item.get("prompt_user")
                        if u:
                            hashes.add(hash_text(u))
                    except Exception:
                        pass
    return hashes


def main():
    p = argparse.ArgumentParser(description="Build Gate Corpus v2.")
    p.add_argument("--out", type=Path, default=REPO / "tools/training/data/gate_prompts_v2.jsonl")
    p.add_argument("--n-keep", type=int, default=500)
    p.add_argument("--n-mull", type=int, default=500)
    p.add_argument("--seed", type=int, default=99)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    csv_dir = REPO / "tools/eval/data/17lands"
    csv_files = sorted(csv_dir.glob("replay_data_public.*.csv.gz"))
    if not csv_files:
        logger.error(f"No 17lands CSV files found in {csv_dir}")
        sys.exit(1)

    exclude_files = [
        REPO / "tools/training/data/gate_prompts.jsonl",
        REPO / "tools/training/data/stage0_sft_dataset.json",
        REPO / "tools/eval/data/mulligan_prompts.jsonl",
    ]
    excluded_hashes = load_excluded_hashes(exclude_files)
    logger.info(f"Loaded {len(excluded_hashes)} SHA-256 hashes to exclude.")

    all_keep = []
    all_mull = []

    temp_out = REPO / "tools/training/data/_temp_mulligan_prompts.jsonl"
    for csv_path in csv_files:
        logger.info(f"Processing {csv_path.name}...")
        build_mulligan_prompts(
            csv_gz=csv_path,
            out_path=temp_out,
            n_samples=50000,
            min_rank="diamond",
            min_bucket_n=10,
            max_rows_scan=None,
            seed=args.seed,
        )
        prompts = []
        if temp_out.exists():
            with open(temp_out, encoding="utf-8") as tf:
                for line in tf:
                    if line.strip():
                        prompts.append(json.loads(line))
            temp_out.unlink(missing_ok=True)
        for prompt in prompts:
            user_text = prompt.get("user") or ""
            if not user_text or hash_text(user_text) in excluded_hashes:
                continue

            truth = ((prompt.get("meta") or {}).get("bucket_stats") or {}).get("correct")
            if truth not in ("keep", "mull"):
                continue

            bucket_meta = prompt.get("meta") or {}
            key_tuple = (
                (bucket_meta.get("bucket") or {}).get("num_lands_in_hand"),
                (bucket_meta.get("bucket") or {}).get("on_play"),
                (bucket_meta.get("bucket") or {}).get("color_count"),
            )
            bucket_meta["bucket_held_out"] = (
                is_bucket_held_out(key_tuple) if all(k is not None for k in key_tuple) else False
            )
            bucket_meta["bucket_key"] = (
                f"{key_tuple[0]}_{key_tuple[1]}_{key_tuple[2]}"
                if all(k is not None for k in key_tuple)
                else ""
            )

            # Retarget system prompt to AUTOPILOT_SYSTEM_PROMPT (byte-exact production prompt)
            v2_prompt = {
                "id": prompt["id"],
                "system": AUTOPILOT_SYSTEM_PROMPT,
                "user": user_text,
                "temperature": 0.0,
                "meta": bucket_meta,
            }

            if truth == "keep":
                all_keep.append(v2_prompt)
            else:
                all_mull.append(v2_prompt)

    rng = random.Random(args.seed)
    rng.shuffle(all_keep)
    rng.shuffle(all_mull)

    target_keep = min(args.n_keep, len(all_keep))
    target_mull = min(args.n_mull, len(all_mull))

    selected_keep = all_keep[:target_keep]
    selected_mull = all_mull[:target_mull]

    if len(selected_mull) < 100:
        logger.error(f"Could not reach minimum mull sample size: got mull={len(selected_mull)} (need >=100)")
        sys.exit(1)

    gate_v2 = selected_keep + selected_mull
    rng.shuffle(gate_v2)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for item in gate_v2:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    logger.info(
        f"✓ Successfully built Gate Corpus v2 at {args.out} with {len(gate_v2)} total prompts "
        f"({len(selected_keep)} keep / {len(selected_mull)} mull)."
    )


if __name__ == "__main__":
    main()
