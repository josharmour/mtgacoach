"""17lands Public Match Replay Ingestion Tool (WP-1.1 / Stage-0 v2).

Converts 17lands-derived decision records into Stage 0 v2 SFT training examples
whose system prompt is the byte-exact AUTOPILOT_SYSTEM_PROMPT and whose responses
serialize to contract-complete ACTION_SCHEMA JSON (WP-0.1).

Two input shapes are supported:

1. ``--prompts-jsonl`` (preferred): output of
   ``tools.eval.seventeenlands.build_mulligan_prompts`` — one prompt per line
   with a ``meta.bucket_stats`` block. The SFT target is the bucket's
   higher-win-rate option (empirical ground truth from diamond+ replays),
   serialized as a ``mulligan_keep`` / ``mulligan_mull`` action whose
   reasoning is grounded strictly in hand facts.

2. ``--turn-action-jsonl``: output of
   ``tools.eval.seventeenlands.build_turn_action_prompts`` — played turn actions
   mapped to ACTION_SCHEMA targets.

Eval-contamination guard: every ``--exclude-prompts`` file (the frozen eval
corpora and gate prompts) contributes the SHA-256 of its ``user`` text to an
exclusion set; ingested records matching one are dropped.

Bucket-holdout guard: every record whose (lands, on_play, colors) bucket is
held out for unseen-bucket evaluation is dropped from training data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from arenamcp.action_planner import AUTOPILOT_SYSTEM_PROMPT, game_action_to_schema_json

logger = logging.getLogger("tools.training.ingest_17lands")


def _user_hash(user: str) -> str:
    return hashlib.sha256(user.strip().encode("utf-8")).hexdigest()


def is_bucket_held_out(key: tuple[int, bool, int]) -> bool:
    """Deterministically hold out ~20% of (lands, on_play, colors) buckets for unseen-bucket evaluation."""
    key_str = f"{key[0]}_{key[1]}_{key[2]}"
    h = int(hashlib.md5(key_str.encode("utf-8")).hexdigest(), 16)
    return (h % 5) == 0


def load_held_out_buckets(gate_v2_path: Path | None) -> set[tuple[int, bool, int]]:
    """Collect bucket keys marked bucket_held_out from gate_prompts_v2.jsonl."""
    held_out = set()
    if gate_v2_path and gate_v2_path.exists():
        with open(gate_v2_path, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    meta = rec.get("meta") or {}
                    b = meta.get("bucket") or {}
                    b_key = (b.get("num_lands_in_hand"), b.get("on_play"), b.get("color_count"))
                    if all(k is not None for k in b_key) and meta.get("bucket_held_out") is True:
                        held_out.add(b_key)
                except Exception:
                    pass
    return held_out


def load_exclusion_hashes(paths: list[Path]) -> set[str]:
    """Collect user-text hashes from frozen eval corpora and gate prompts (JSON/JSONL)."""
    hashes: set[str] = set()
    for path in paths:
        if not path.exists():
            logger.warning(f"exclusion file missing, skipping: {path}")
            continue
        with open(path, encoding="utf-8") as f:
            if path.suffix == ".json":
                try:
                    data = json.load(f)
                    if isinstance(data, list):
                        for item in data:
                            u = item.get("user") or item.get("prompt_user")
                            if u:
                                hashes.add(_user_hash(u))
                except Exception as e:
                    logger.warning(f"Error loading exclusion file {path}: {e}")
            elif path.suffix == ".jsonl":
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        u = rec.get("user") or rec.get("prompt_user")
                        if u:
                            hashes.add(_user_hash(u))
                    except json.JSONDecodeError:
                        continue
    return hashes


def _mulligan_action(correct: str, bucket: dict, stats: dict | None = None) -> dict:
    """Build the ACTION_SCHEMA action dict for a bucket-correct decision.

    Reasoning is strictly grounded in hand properties present in the prompt,
    omitting ungrounded statistical claims.
    """
    lands = bucket.get("num_lands_in_hand")
    play = "on the play" if bucket.get("on_play") else "on the draw"
    colors = bucket.get("color_count")
    reasoning = f"{lands} lands {play}, {colors} colors"
    if correct == "keep":
        return {
            "action_type": "mulligan_keep",
            "reasoning": reasoning,
        }
    return {
        "action_type": "mulligan_mull",
        "reasoning": reasoning,
    }


def turn_action_to_schema(played_action: Any) -> str:
    """Convert played turn action tags into valid ACTION_SCHEMA JSON string."""
    if isinstance(played_action, list):
        tags = played_action
    elif isinstance(played_action, str):
        tags = [t.strip() for t in played_action.split(",")]
    elif isinstance(played_action, dict):
        return game_action_to_schema_json(played_action)
    else:
        tags = []

    if "PLAY_LAND" in tags:
        act = {"action_type": "play_land", "reasoning": "Play land"}
    elif "CAST_CREATURE" in tags:
        act = {"action_type": "cast_spell", "reasoning": "Cast creature"}
    elif "CAST_SPELL" in tags:
        act = {"action_type": "cast_spell", "reasoning": "Cast spell"}
    elif "ATTACK" in tags:
        act = {"action_type": "declare_attackers", "reasoning": "Attack"}
    elif "ACTIVATE" in tags:
        act = {"action_type": "activate_ability", "reasoning": "Activate ability"}
    else:
        act = {"action_type": "pass_priority", "reasoning": "Pass priority"}
    return game_action_to_schema_json(act)


def parse_turn_action_jsonl(
    paths: list[Path], exclude_hashes: set[str]
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Convert turn_action_prompts output into SFT records."""
    records: list[dict[str, str]] = []
    stats = {"read": 0, "no_action": 0, "excluded": 0, "kept": 0}
    seen: set[str] = set()

    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stats["read"] += 1
                user = rec.get("user") or ""
                meta = rec.get("meta") or {}
                played_action = meta.get("actually_did") or meta.get("played_action") or rec.get("action")
                if not user or not played_action:
                    stats["no_action"] += 1
                    continue
                h = _user_hash(user)
                if h in exclude_hashes or h in seen:
                    if h in exclude_hashes:
                        stats["excluded"] += 1
                    continue
                seen.add(h)

                records.append(
                    {
                        "system": AUTOPILOT_SYSTEM_PROMPT,
                        "user": user,
                        "response": turn_action_to_schema(played_action),
                        "fallback": False,
                        "source": "17lands",
                        "kind": "turn_action",
                    }
                )
                stats["kept"] += 1
    return records, stats


def parse_prompts_jsonl(
    paths: list[Path],
    exclude_hashes: set[str],
    held_out_buckets: set[tuple[int, bool, int]],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    """Convert build_mulligan_prompts output into SFT records with bucket holdout."""
    records: list[dict[str, str]] = []
    stats = {"read": 0, "no_correct": 0, "excluded": 0, "held_out_bucket": 0, "kept": 0}
    seen: set[str] = set()

    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stats["read"] += 1
                user = rec.get("user") or ""
                meta = rec.get("meta") or {}
                bucket = meta.get("bucket") or {}
                bucket_stats = meta.get("bucket_stats") or {}
                correct = bucket_stats.get("correct")
                if not user or correct not in ("keep", "mull"):
                    stats["no_correct"] += 1
                    continue

                b_key = (
                    bucket.get("num_lands_in_hand"),
                    bucket.get("on_play"),
                    bucket.get("color_count"),
                )
                if all(k is not None for k in b_key):
                    if is_bucket_held_out(b_key) or b_key in held_out_buckets:
                        stats["held_out_bucket"] += 1
                        continue

                h = _user_hash(user)
                if h in exclude_hashes:
                    stats["excluded"] += 1
                    continue
                if h in seen:
                    continue
                seen.add(h)

                action = _mulligan_action(correct, bucket, bucket_stats)
                records.append(
                    {
                        "system": AUTOPILOT_SYSTEM_PROMPT,
                        "user": user,
                        "response": game_action_to_schema_json(action),
                        "fallback": False,
                        "source": "17lands",
                        "kind": "mulligan",
                    }
                )
                stats["kept"] += 1
    return records, stats


def main():
    p = argparse.ArgumentParser(description="Ingest 17lands match dataset into Stage 0 v2 SFT dataset.")
    p.add_argument(
        "--prompts-jsonl",
        type=Path,
        nargs="+",
        help="build_mulligan_prompts output files (bucket-labeled prompts)",
    )
    p.add_argument(
        "--turn-action-jsonl",
        type=Path,
        nargs="+",
        help="turn_action_prompts output files (played turn actions)",
    )
    p.add_argument(
        "--exclude-prompts",
        type=Path,
        nargs="*",
        default=[],
        help="Frozen eval corpora and gate files whose user texts must not enter training",
    )
    p.add_argument(
        "--gate-v2-path",
        type=Path,
        default=None,
        help="Path to gate_prompts_v2.jsonl for held-out bucket exclusion",
    )
    p.add_argument("--output", required=True, type=Path, help="Output SFT dataset JSON file")
    p.add_argument(
        "--max-keep-mull-ratio",
        type=float,
        default=None,
        help="Downsample mulligan_keep records to at most RATIO x the mulligan_mull count",
    )
    p.add_argument("--seed", type=int, default=13, help="Downsampling RNG seed")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    if not args.prompts_jsonl and not args.turn_action_jsonl:
        p.error("one of --prompts-jsonl / --turn-action-jsonl is required")

    exclude = load_exclusion_hashes(list(args.exclude_prompts))
    logger.info(f"Loaded {len(exclude)} eval-corpus / gate exclusion hashes.")

    held_out_buckets = load_held_out_buckets(args.gate_v2_path)
    logger.info(f"Loaded {len(held_out_buckets)} held-out bucket keys for exclusion.")

    records: list[dict[str, str]] = []
    if args.prompts_jsonl:
        recs, stats = parse_prompts_jsonl(list(args.prompts_jsonl), exclude, held_out_buckets)
        logger.info(
            f"prompts-jsonl ingest: read={stats['read']} kept={stats['kept']} "
            f"no_correct={stats['no_correct']} eval_excluded={stats['excluded']} "
            f"held_out_bucket_excluded={stats['held_out_bucket']}"
        )
        records.extend(recs)
    if args.turn_action_jsonl:
        ta_recs, ta_stats = parse_turn_action_jsonl(list(args.turn_action_jsonl), exclude)
        logger.info(
            f"turn-action-jsonl ingest: read={ta_stats['read']} kept={ta_stats['kept']} "
            f"no_action={ta_stats['no_action']} eval_excluded={ta_stats['excluded']}"
        )
        records.extend(ta_recs)

    if args.max_keep_mull_ratio:
        import random

        rng = random.Random(args.seed)
        keeps = [r for r in records if '"mulligan_keep"' in r["response"]]
        mulls = [r for r in records if '"mulligan_mull"' in r["response"]]
        others = [
            r
            for r in records
            if '"mulligan_keep"' not in r["response"] and '"mulligan_mull"' not in r["response"]
        ]
        cap = int(len(mulls) * args.max_keep_mull_ratio)
        n_keeps_before = sum(1 for r in records if '"mulligan_keep"' in r["response"])
        if len(keeps) > cap:
            rng.shuffle(keeps)
            keeps = keeps[:cap]
            logger.info(
                f"Downsampled keeps from {n_keeps_before} to {len(keeps)} "
                f"(ratio {args.max_keep_mull_ratio}x of {len(mulls)} mulls)."
            )
        records = keeps + mulls + others
        rng.shuffle(records)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)

    logger.info(f"✓ Saved {len(records)} Stage 0 v2 SFT prompts to {args.output}")


if __name__ == "__main__":
    main()
