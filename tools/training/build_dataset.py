"""Dataset builder for MTG coach models.

Parses self-play trajectories, identifies winning decisions for SFT,
and pairs differing model decisions for DPO preference tuning. Also supports
bootstrapping SFT data from 17lands mulligan and turn-action prompt corpora.

Usage:
    python -m tools.training.build_dataset \\
        --trajectories tools/eval/data/self_play_trajectories.jsonl \\
        --seventeenlands-mulligan tools/eval/data/mulligan_prompts.jsonl \\
        --seventeenlands-turn-action tools/eval/data/turn_action_prompts.jsonl \\
        --out-sft tools/training/data/sft_dataset.json \\
        --out-dpo tools/training/data/dpo_dataset.json
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

logger = logging.getLogger("tools.training.build_dataset")


def main():
    p = argparse.ArgumentParser(description="Build SFT and DPO datasets from self-play and 17lands.")
    p.add_argument("--trajectories", type=Path, help="Input self-play trajectories JSONL")
    p.add_argument("--seventeenlands-mulligan", type=Path, help="Input 17lands mulligan prompts JSONL")
    p.add_argument("--seventeenlands-turn-action", type=Path, help="Input 17lands turn action prompts JSONL")
    p.add_argument("--out-sft", type=Path, default=Path("tools/training/data/sft_dataset.json"))
    p.add_argument("--out-dpo", type=Path, default=Path("tools/training/data/dpo_dataset.json"))
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    sft_data = []
    dpo_data = []

    # 1. Process self-play trajectories if provided
    if args.trajectories and args.trajectories.exists():
        logger.info(f"Processing self-play trajectories: {args.trajectories}")
        with open(args.trajectories, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                try:
                    rec = json.loads(line.strip())
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping malformed self-play line {idx + 1}: {e}")
                    continue

                winner = rec.get("winner")
                if not winner or winner not in ("local", "opp"):
                    continue

                seat = rec.get("seat")
                prompt_system = rec.get("prompt_system") or ""
                prompt_user = rec.get("prompt_user") or ""
                planned = rec.get("planned_action")
                alt = rec.get("alt_planned_action")

                if not planned:
                    continue

                is_active_winner = (seat == "local" and winner == "local") or (seat == "opp" and winner == "opp")

                if is_active_winner:
                    chosen = planned
                    rejected = alt
                else:
                    chosen = alt
                    rejected = planned

                if chosen:
                    sft_data.append({
                        "system": prompt_system,
                        "user": prompt_user,
                        "response": chosen,
                    })

                if chosen and rejected and chosen != rejected:
                    dpo_data.append({
                        "system": prompt_system,
                        "user": prompt_user,
                        "chosen": chosen,
                        "rejected": rejected,
                    })

    # 2. Process 17lands mulligans if provided (bootstrap SFT)
    if args.seventeenlands_mulligan and args.seventeenlands_mulligan.exists():
        logger.info(f"Processing 17lands mulligan prompts: {args.seventeenlands_mulligan}")
        count = 0
        with open(args.seventeenlands_mulligan, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                try:
                    rec = json.loads(line.strip())
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping malformed 17l-mull line {idx + 1}: {e}")
                    continue

                meta = rec.get("meta") or {}
                stats = meta.get("bucket_stats") or {}
                correct = stats.get("correct")  # "keep" or "mull"

                if not correct:
                    # Fallback to actually played
                    correct = meta.get("actually_played")

                if correct:
                    response = "KEEP" if correct == "keep" else "MULLIGAN"
                    sft_data.append({
                        "system": rec.get("system") or "",
                        "user": rec.get("user") or "",
                        "response": response,
                    })
                    count += 1
        logger.info(f"Added {count} 17lands mulligan SFT examples")

    # 3. Process 17lands turn actions if provided (bootstrap SFT)
    if args.seventeenlands_turn_action and args.seventeenlands_turn_action.exists():
        logger.info(f"Processing 17lands turn-action prompts: {args.seventeenlands_turn_action}")
        count = 0
        with open(args.seventeenlands_turn_action, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                try:
                    rec = json.loads(line.strip())
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping malformed 17l-action line {idx + 1}: {e}")
                    continue

                meta = rec.get("meta") or {}
                did = meta.get("actually_did") or []

                if did:
                    response = ", ".join(did)
                    sft_data.append({
                        "system": rec.get("system") or "",
                        "user": rec.get("user") or "",
                        "response": response,
                    })
                    count += 1
        logger.info(f"Added {count} 17lands turn-action SFT examples")

    # Save SFT dataset
    if sft_data:
        args.out_sft.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out_sft, "w", encoding="utf-8") as f:
            json.dump(sft_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(sft_data)} total SFT examples to {args.out_sft}")
    else:
        logger.warning("No SFT examples generated.")

    # Save DPO dataset
    if dpo_data:
        args.out_dpo.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out_dpo, "w", encoding="utf-8") as f:
            json.dump(dpo_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved {len(dpo_data)} total DPO examples to {args.out_dpo}")
    else:
        logger.warning("No DPO examples generated.")


if __name__ == "__main__":
    main()
