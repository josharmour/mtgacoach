"""Dataset builder for MTG coach models.

Parses self-play trajectories, identifies winning decisions for SFT,
and pairs differing model decisions for DPO preference tuning. Also supports
bootstrapping SFT data from 17lands mulligan and turn-action prompt corpora.

DPO pair selection supports two strategies:
  - "winner takes all" heuristic (default): the action played by the eventual
    game winner is "chosen", the alternative is "rejected".
  - optional LLM judge (``--judge-backend``): an expert judge picks the
    strategically superior move *independent of the game outcome*. The judge is
    optional; when not configured the deterministic heuristic is used and
    behavior is identical to the heuristic-only path.

Optionally, trajectory examples can be deduplicated by game-state signature and
prioritized via hard-example mining (``--dedup`` / ``--hard-mine`` /
``--max-examples``). These are off by default, so the default output is
unchanged.

Usage:
    python -m tools.training.build_dataset \\
        --trajectories tools/eval/data/self_play_trajectories.jsonl \\
        --seventeenlands-mulligan tools/eval/data/mulligan_prompts.jsonl \\
        --seventeenlands-turn-action tools/eval/data/turn_action_prompts.jsonl \\
        --out-sft tools/training/data/sft_dataset.json \\
        --out-dpo tools/training/data/dpo_dataset.json

    # With an LLM judge and diversity-aware sampling:
    python -m tools.training.build_dataset \\
        --trajectories tools/eval/data/self_play_trajectories.jsonl \\
        --judge-backend online:gpt-5.4 \\
        --dedup --hard-mine --max-examples 5000
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import sys
from pathlib import Path
from typing import Optional

# Make the in-repo src/ importable so the optional judge backend can be built
# without requiring the caller to set PYTHONPATH.
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

logger = logging.getLogger("tools.training.build_dataset")


# ---------------------------------------------------------------------------
# LLM judge for DPO pair selection (optional)
# ---------------------------------------------------------------------------

MOVE_JUDGE_SYSTEM = """You are an expert Magic: The Gathering Arena strategic evaluator.

You will be shown:
1. The game state context (system + user prompt shown to the coaching model)
2. Two candidate moves that were evaluated by a model

Your task: Determine which move is strategically superior given the game state,
ignoring the final game outcome. Consider:
  - Board control and tempo implications
  - Card advantage and resource management
  - Threat assessment (what threats does the opponent have?)
  - Win conditions and paths to victory
  - Risk/reward of each move

Output STRICT JSON:
{
  "superior_move": 1 or 2,
  "confidence": 1-5 (1=barely better, 5=clearly better),
  "reasoning": "one sentence explaining the choice"
}
"""

# Common, low-information request types used by hard-example mining.
_COMMON_REQUEST_TYPES = {
    "ActionType_Pass",
    "Mulligan_Keep",
    "Mulligan_Mull",
    "ActionsAvailable",
}


def _build_move_judge_message(prompt_system: str, prompt_user: str, action1: str, action2: str) -> str:
    """Build the judge message comparing two candidate actions."""
    return f"""Game Context:
=== SYSTEM ===
{prompt_system}

=== USER ===
{prompt_user}

=== MOVES TO EVALUATE ===
Move 1: {action1}
Move 2: {action2}

Which move is strategically superior? Output JSON only."""


def _parse_move_judge_response(text: str) -> Optional[dict]:
    """Extract judge JSON, handling markdown fences and prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:]
        if text.endswith("```"):
            text = text[:-3]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _build_judge_client(judge_backend_spec: Optional[str], license_key: str):
    """Build and return a judge client, or None if spec not provided / unavailable."""
    if not judge_backend_spec:
        return None
    try:
        # Imported lazily so that the default (no-judge) path and the test
        # suite do not require the arenamcp runtime dependencies to be
        # installed just to build heuristic datasets.
        from tools.eval.run import BackendSpec  # noqa: WPS433
        spec = BackendSpec.parse(judge_backend_spec, license_key=license_key)
        client = spec.build()
        logger.info(f"Judge backend initialized: {spec.label}")
        return client
    except Exception as e:  # pragma: no cover - depends on external runtime
        logger.error(f"Failed to initialize judge backend {judge_backend_spec}: {e}")
        return None


def _judge_move_pair(
    judge_client,
    prompt_system: str,
    prompt_user: str,
    action1: str,
    action2: str,
):
    """Call judge to compare two actions.

    Returns: (superior_action_index, confidence_1_to_5, reasoning) or None on error.
    superior_action_index: 1 or 2, indicating which action is better.
    """
    if not judge_client:
        return None

    try:
        user_msg = _build_move_judge_message(prompt_system, prompt_user, action1, action2)
        judge_text = judge_client.complete(
            MOVE_JUDGE_SYSTEM,
            user_msg,
            max_tokens=200,
            temperature=0.0,
            request_timeout_s=30.0,
        )
        result = _parse_move_judge_response(judge_text)
        if not result:
            logger.warning("Judge response could not be parsed")
            return None

        superior = int(result.get("superior_move", 0))
        confidence = min(5, max(1, int(result.get("confidence", 1))))
        reasoning = str(result.get("reasoning", ""))[:200]

        if superior not in (1, 2):
            logger.warning(f"Judge returned invalid superior_move: {superior}")
            return None

        return (superior, confidence, reasoning)
    except Exception as e:
        logger.warning(f"Judge call failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Diversity-aware sampling: state dedup + hard-example mining
# ---------------------------------------------------------------------------


def _compute_state_signature(rec: dict) -> str:
    """Compute a dedup signature for a trajectory record.

    Clusters similar decision contexts by turn + phase + request type, plus a
    short hash of the (truncated) user prompt so that near-identical board
    states collapse together while genuinely different ones do not.
    """
    turn = rec.get("turn", 0)
    phase = rec.get("phase", "")
    req_type = rec.get("request_type", "")
    prompt_user = (rec.get("prompt_user") or "")[:500]
    prompt_hash = hashlib.sha256(prompt_user.encode("utf-8")).hexdigest()[:16]
    return f"{turn}_{phase}_{req_type}_{prompt_hash}"


def _score_example_hardness(rec: dict) -> float:
    """Score an example's complexity/rarity in [0.0, 1.0] for hard-example mining."""
    score = 0.5
    planned = rec.get("planned_action")
    alt = rec.get("alt_planned_action")
    # Model had a meaningful choice between two distinct actions.
    if alt and planned and alt != planned:
        score += 0.2
    # Rare / high-information decision type.
    if rec.get("request_type") not in _COMMON_REQUEST_TYPES:
        score += 0.2
    # Model spent time, likely complex reasoning.
    try:
        if float(rec.get("latency_ms", 0) or 0) > 1500:
            score += 0.15
    except (TypeError, ValueError):
        pass
    # Trivial early game.
    try:
        if int(rec.get("turn", 999) or 999) <= 2:
            score -= 0.15
    except (TypeError, ValueError):
        pass
    return max(0.0, min(1.0, score))


def main():
    p = argparse.ArgumentParser(description="Build SFT and DPO datasets from self-play and 17lands.")
    p.add_argument("--trajectories", type=Path, help="Input self-play trajectories JSONL")
    p.add_argument("--seventeenlands-mulligan", type=Path, help="Input 17lands mulligan prompts JSONL")
    p.add_argument("--seventeenlands-turn-action", type=Path, help="Input 17lands turn action prompts JSONL")
    p.add_argument("--out-sft", type=Path, default=Path("tools/training/data/sft_dataset.json"))
    p.add_argument("--out-dpo", type=Path, default=Path("tools/training/data/dpo_dataset.json"))
    p.add_argument("--judge-backend", type=str, default=None,
                   help="Optional judge backend spec for DPO pair selection (e.g., 'online:gpt-5.4'). "
                        "When omitted, falls back to the deterministic winner-takes-all heuristic.")
    p.add_argument("--license-key", type=str, default=os.environ.get("MTGACOACH_LICENSE_KEY", ""),
                   help="License key for online judge access")
    p.add_argument("--dedup", action="store_true",
                   help="Deduplicate self-play trajectories by game-state signature, "
                        "keeping the hardest example per state.")
    p.add_argument("--hard-mine", action="store_true",
                   help="Sort deduplicated trajectories by hardness (descending) so the "
                        "hardest examples are processed first / survive --max-examples.")
    p.add_argument("--max-examples", type=int, default=0,
                   help="Cap the number of self-play trajectory examples (0 = no cap). "
                        "Hard examples are always kept; easier ones are subsampled.")
    p.add_argument("--seed", type=int, default=1234,
                   help="Random seed for --max-examples subsampling (deterministic).")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    # Initialize judge client if specified (optional).
    judge_client = _build_judge_client(args.judge_backend, args.license_key)

    sft_data = []
    dpo_data = []
    judge_stats = {"used": 0, "errors": 0, "fallback": 0}

    # 1. Process self-play trajectories if provided
    if args.trajectories and args.trajectories.exists():
        logger.info(f"Processing self-play trajectories: {args.trajectories}")

        # Collect valid records first so optional dedup / hard-mining can run
        # over the full set. When no sampling flags are set, records are
        # processed in original file order, identical to the legacy behavior.
        records = []
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

                if not rec.get("planned_action"):
                    continue

                records.append(rec)

        original_count = len(records)

        # Optional diversity-aware sampling (off by default).
        if args.dedup or args.hard_mine or args.max_examples:
            seen_states = {}
            hardness_scores = {}
            for rec in records:
                state_sig = _compute_state_signature(rec)
                hardness = _score_example_hardness(rec)
                if state_sig not in seen_states or hardness > hardness_scores[state_sig]:
                    seen_states[state_sig] = rec
                    hardness_scores[state_sig] = hardness

            dedup_records = [(rec, hardness_scores[sig]) for sig, rec in seen_states.items()]

            if args.dedup or args.max_examples:
                logger.info(
                    f"Dedup complete: {original_count} trajectories -> "
                    f"{len(dedup_records)} unique states"
                )
            elif args.hard_mine:
                # hard-mine alone still dedups by signature for stable ordering
                logger.info(
                    f"Hard-mine: {original_count} trajectories -> "
                    f"{len(dedup_records)} unique states"
                )

            # Hardness distribution (easy/medium/hard buckets).
            easy = sum(1 for _, h in dedup_records if h < 0.5)
            med = sum(1 for _, h in dedup_records if 0.5 <= h < 0.75)
            hard = sum(1 for _, h in dedup_records if h >= 0.75)
            total = max(1, len(dedup_records))
            logger.info(
                f"Hardness distribution: easy={easy} ({100.0 * easy / total:.1f}%), "
                f"medium={med} ({100.0 * med / total:.1f}%), "
                f"hard={hard} ({100.0 * hard / total:.1f}%)"
            )

            if args.hard_mine:
                dedup_records.sort(key=lambda x: x[1], reverse=True)

            # Optional cap: always keep the hardest half, probabilistically
            # subsample the rest (easier examples are dropped more often).
            if args.max_examples and len(dedup_records) > args.max_examples:
                rng = random.Random(args.seed)
                sorted_h = sorted(h for _, h in dedup_records)
                hard_threshold = sorted_h[len(sorted_h) // 2]
                sampled = [
                    (rec, h) for rec, h in dedup_records
                    if h >= hard_threshold or rng.random() < (1.0 - h)
                ]
                # If still over the cap, keep the hardest max_examples.
                if len(sampled) > args.max_examples:
                    sampled.sort(key=lambda x: x[1], reverse=True)
                    sampled = sampled[:args.max_examples]
                logger.info(
                    f"Sampled down to {len(sampled)} examples (cap={args.max_examples})"
                )
                dedup_records = sampled

            final_records = [rec for rec, _ in dedup_records]
        else:
            final_records = records

        # Process selected records into SFT/DPO.
        for rec in final_records:
            winner = rec.get("winner")
            seat = rec.get("seat")
            prompt_system = rec.get("prompt_system") or ""
            prompt_user = rec.get("prompt_user") or ""
            planned = rec.get("planned_action")
            alt = rec.get("alt_planned_action")

            # Winner-takes-all heuristic: used for SFT targets and as the
            # deterministic DPO fallback when no judge overrides the pair.
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
                dpo_entry = {
                    "system": prompt_system,
                    "user": prompt_user,
                    "chosen": chosen,
                    "rejected": rejected,
                }

                judge_result = _judge_move_pair(
                    judge_client,
                    prompt_system,
                    prompt_user,
                    planned,
                    alt,
                )

                if judge_result:
                    superior_idx, confidence, reasoning = judge_result
                    if superior_idx == 1:
                        dpo_entry["chosen"] = planned
                        dpo_entry["rejected"] = alt
                    else:
                        dpo_entry["chosen"] = alt
                        dpo_entry["rejected"] = planned
                    dpo_entry["judge_backend"] = getattr(judge_client, "model", None)
                    dpo_entry["judge_confidence"] = confidence
                    dpo_entry["judge_reasoning"] = reasoning
                    judge_stats["used"] += 1
                else:
                    # Fall back to winner-takes-all (already set above).
                    if judge_client:
                        judge_stats["errors"] += 1
                    else:
                        judge_stats["fallback"] += 1

                dpo_data.append(dpo_entry)

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

    # Judge statistics summary
    if judge_client:
        logger.info(
            f"Judge statistics: used={judge_stats['used']}, "
            f"errors={judge_stats['errors']}, fallback={judge_stats['fallback']}"
        )


if __name__ == "__main__":
    main()
