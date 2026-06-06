"""Master pipeline runner for MTG Arena recursive self-improvement.

Automates the iterative loop:
  1. Data Generation (Self-play bot battles via self_play.py)
  2. Dataset compilation (build_dataset.py)
  3. Fine-tuning (train.py)
  4. Gating evaluation (bot battles: Challenger vs Champion)
  4.5. Quality evaluation (eval harness: run + LLM-as-judge on Challenger)
  5. Multi-dimensional promotion gate:
       * Hard minimums on Legality and Reasoning (from the eval harness).
       * Win-rate as a secondary driver.
       * A normalized composite score is logged for visibility.
     If the eval harness can't run, the pipeline falls back to the legacy
     binary win-rate gate so it still functions.

Usage:
    python -m tools.training.run_pipeline \\
        --champion-backend ollama:gemma4:latest \\
        --challenger-backend ollama:gemma4:challenger \\
        --iterations 3 \\
        --matches-per-iter 10 \\
        --gate-matches 6
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Ensure in-repo src is importable
REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

logger = logging.getLogger("tools.training.run_pipeline")


def _run_cmd(cmd: list[str]) -> bool:
    """Run a subcommand and stream output. Returns True on success (exit 0)."""
    logger.info(f"Running command: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, cwd=REPO, check=True)
        return res.returncode == 0
    except subprocess.CalledProcessError as e:
        logger.error(f"Command failed with exit code {e.returncode}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to execute command: {e}")
        return False


class PipelineRunner:
    """Manages the recursive self-improvement iterations."""

    def __init__(
        self,
        champion_backend: str,
        challenger_backend: str,
        iterations: int,
        matches_per_iter: int,
        gate_matches: int,
        sets: str,
        license_key: str = "",
        eval_corpus: Path | None = None,
        judge_backend: str = "online:gpt-5.4",
        legality_min: float = 4.0,
        reasoning_min: float = 3.5,
        win_rate_min: float = 0.45,
        fallback_win_rate: float = 0.55,
    ):
        self.champion_backend = champion_backend
        self.challenger_backend = challenger_backend
        self.iterations = iterations
        self.matches_per_iter = matches_per_iter
        self.gate_matches = gate_matches
        self.sets = sets
        self.license_key = license_key

        # Multi-dimensional gate configuration.
        self.eval_corpus = eval_corpus or (REPO / "tools/eval/data/seed_prompts.jsonl")
        self.judge_backend = judge_backend
        self.legality_min = legality_min
        self.reasoning_min = reasoning_min
        self.win_rate_min = win_rate_min
        # Legacy binary gate used only when the eval harness can't run.
        self.fallback_win_rate = fallback_win_rate

        self.trajectories_path = REPO / "tools/eval/data/self_play_trajectories.jsonl"
        self.sft_path = REPO / "tools/training/data/sft_dataset.json"
        self.dpo_path = REPO / "tools/training/data/dpo_dataset.json"
        self.checkpoint_dir = REPO / "tools/training/checkpoints/challenger_adapter"
        self.champion_dir = REPO / "tools/training/checkpoints/champion_adapter"
        self.gating_responses_path = REPO / "tools/eval/data/gating_responses.jsonl"
        self.gating_scores_path = REPO / "tools/eval/data/gating_scores.jsonl"

    @staticmethod
    def _iter_jsonl(path: Path):
        """Yield parsed JSON records from a JSONL file.

        Reuses the eval harness reader when importable (so behavior matches
        the rest of the eval tooling); falls back to a local parser so the
        gate keeps working even if the eval package can't be imported.
        """
        try:
            from tools.eval.run import _read_jsonl  # type: ignore
            yield from _read_jsonl(path)
            return
        except Exception:
            pass
        if not path.exists():
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def _aggregate_eval_scores(self, scores_path: Path) -> dict | None:
        """Compute per-dimension mean scores from a judge scores JSONL.

        Mirrors report.py: ignore non-positive scores (errors / unscored).
        Returns None when no usable scores are present.
        """
        dims: dict[str, list[float]] = {
            "legality": [],
            "reasoning": [],
            "correctness": [],
        }
        for rec in self._iter_jsonl(scores_path):
            for k in dims:
                v = rec.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    dims[k].append(float(v))

        if not any(dims.values()):
            return None
        return {k: (statistics.fmean(v) if v else 0.0) for k, v in dims.items()}

    def _evaluate_challenger_quality(self, challenger_spec: str) -> dict | None:
        """Run the eval harness (run + judge) on the Challenger.

        Returns a dict of per-dimension mean scores (legality / reasoning /
        correctness) or None if the harness can't be run (missing corpus,
        unreachable backend, judge failure, or no usable scores). A None
        return signals the caller to fall back to the legacy win-rate gate.
        """
        if not self.eval_corpus.exists():
            logger.warning(
                f"Eval corpus not found at {self.eval_corpus}; "
                f"skipping quality gate (falling back to win-rate)."
            )
            return None

        if "seed_prompts" in self.eval_corpus.name:
            logger.warning(
                "Gating on the synthetic seed corpus (not rules-verified). "
                "For production gating, capture real prompts via "
                "MTGACOACH_PROMPT_DUMP_PATH and pass --eval-corpus."
            )

        # Clear stale gating artifacts so we never score a previous
        # Challenger (the eval steps are append-only / idempotent and would
        # otherwise skip a new challenger sharing the same backend label).
        for p in (self.gating_responses_path, self.gating_scores_path):
            if p.exists():
                try:
                    p.unlink()
                except OSError as e:
                    logger.warning(f"Could not remove stale {p.name}: {e}")

        logger.info("Step 4.5a: Generating Challenger responses on eval corpus...")
        ok = _run_cmd([
            sys.executable, "-m", "tools.eval.run",
            "--prompts", str(self.eval_corpus),
            "--responses", str(self.gating_responses_path),
            "--backend", challenger_spec,
            *(["--license-key", self.license_key] if self.license_key else []),
        ])
        if not ok:
            logger.warning("eval.run failed; skipping quality gate (falling back to win-rate).")
            return None

        logger.info(f"Step 4.5b: Judging Challenger responses with {self.judge_backend}...")
        ok = _run_cmd([
            sys.executable, "-m", "tools.eval.judge",
            "--prompts", str(self.eval_corpus),
            "--responses", str(self.gating_responses_path),
            "--scores", str(self.gating_scores_path),
            "--judge-backend", self.judge_backend,
            *(["--license-key", self.license_key] if self.license_key else []),
        ])
        if not ok:
            logger.warning("eval.judge failed; skipping quality gate (falling back to win-rate).")
            return None

        metrics = self._aggregate_eval_scores(self.gating_scores_path)
        if metrics is None:
            logger.warning(
                "No usable judge scores produced; skipping quality gate "
                "(falling back to win-rate)."
            )
        return metrics

    def run(self):
        logger.info(f"Starting self-improvement pipeline: {self.iterations} iteration(s)")

        for iter_idx in range(self.iterations):
            logger.info(f"\n==========================================")
            logger.info(f"  ITERATION {iter_idx + 1} / {self.iterations}")
            logger.info(f"==========================================")

            # Step 1: Self-play Data Generation (Champion vs Champion to collect base trajectories)
            logger.info("Step 1: Running self-play matches...")
            # If the champion adapter directory exists, we point the backend to it;
            # otherwise, we use the default base champion spec.
            champ_spec = self.champion_backend
            if self.champion_dir.exists():
                # If using local adapters, we construct the spec accordingly
                champ_spec = f"openai-compatible|http://localhost:8000/v1|{self.champion_dir}"

            success = _run_cmd([
                sys.executable, "-m", "arenamcp.self_play",
                "--local-backend", champ_spec,
                "--opponent-backend", champ_spec,
                "--matches", str(self.matches_per_iter),
                "--sets", self.sets,
                "--out-trajectories", str(self.trajectories_path),
                *(["--license-key", self.license_key] if self.license_key else []),
            ])
            if not success:
                logger.error("Self-play data generation failed. Aborting iteration.")
                sys.exit(1)

            # Step 2: Build SFT and DPO Datasets
            logger.info("Step 2: Building training datasets...")
            success = _run_cmd([
                sys.executable, "-m", "tools.training.build_dataset",
                "--trajectories", str(self.trajectories_path),
                "--out-sft", str(self.sft_path),
                "--out-dpo", str(self.dpo_path),
            ])
            if not success:
                logger.error("Dataset building failed. Aborting iteration.")
                sys.exit(1)

            # Step 3: Model Training (Fine-tuning Challenger on DPO dataset)
            logger.info("Step 3: Fine-tuning Challenger model...")
            # Use SFT if DPO file doesn't exist or is empty
            method = "dpo" if self.dpo_path.exists() and self.dpo_path.stat().st_size > 10 else "sft"
            dataset_path = self.dpo_path if method == "dpo" else self.sft_path

            success = _run_cmd([
                sys.executable, "-m", "tools.training.train",
                "--model_id", "google/gemma-4-E2B-it",
                "--dataset", str(dataset_path),
                "--output_dir", str(self.checkpoint_dir),
                "--method", method,
                "--epochs", "1",
                "--batch_size", "2",
                "--load_in_4bit",
            ])
            if not success:
                logger.error("Fine-tuning failed. Aborting iteration.")
                sys.exit(1)

            # Step 4: Gating Validation (Challenger vs Champion bot battles)
            logger.info("Step 4: Running gating evaluation matches...")
            challenger_spec = f"openai-compatible|http://localhost:8000/v1|{self.checkpoint_dir}"
            
            # Clear previous evaluation trajectories
            eval_trajectories = REPO / "tools/eval/data/eval_gating_trajectories.jsonl"
            if eval_trajectories.exists():
                eval_trajectories.unlink()

            success = _run_cmd([
                sys.executable, "-m", "arenamcp.self_play",
                "--local-backend", challenger_spec,
                "--opponent-backend", champ_spec,
                "--matches", str(self.gate_matches),
                "--sets", self.sets,
                "--out-trajectories", str(eval_trajectories),
                *(["--license-key", self.license_key] if self.license_key else []),
            ])
            if not success:
                logger.error("Gating evaluation matches failed. Aborting iteration.")
                sys.exit(1)

            # Step 5: Score Gating and Promote Champion
            logger.info("Step 5: Gating promotion scoring...")
            challenger_wins = 0
            total_matches = 0
            if eval_trajectories.exists():
                with open(eval_trajectories, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line.strip())
                            winner = rec.get("winner")
                            seat = rec.get("seat")
                            # We only count once per match (e.g. when seat == "local")
                            if seat == "local" and winner:
                                total_matches += 1
                                if winner == "local":  # Challenger won (since Challenger was local)
                                    challenger_wins += 1
                        except Exception:
                            continue

            win_rate = (challenger_wins / total_matches) if total_matches > 0 else 0.0
            logger.info(f"Gating outcomes: Challenger won {challenger_wins} of {total_matches} matches ({win_rate * 100:.1f}% win rate)")

            # Step 4.5: Quality evaluation via the eval harness (run + judge).
            logger.info("Step 4.5: Evaluating Challenger advice quality (eval harness)...")
            quality = self._evaluate_challenger_quality(challenger_spec)

            # Step 5: Multi-dimensional promotion gate.
            logger.info("Step 5: Multi-dimensional promotion gate...")
            promote = self._gate_decision(win_rate, quality)

            if promote:
                logger.info("✓ Challenger promoted! Overwriting Champion baseline.")
                # Copy Challenger adapter directory as the new Champion
                if self.champion_dir.exists():
                    shutil.rmtree(self.champion_dir)
                shutil.copytree(self.checkpoint_dir, self.champion_dir)
            else:
                logger.info("✗ Challenger rejected. Retaining Champion baseline.")

        logger.info("\nSelf-improvement pipeline execution complete!")

    def _gate_decision(self, win_rate: float, quality: dict | None) -> bool:
        """Decide whether to promote the Challenger.

        When eval-harness quality metrics are available, enforce hard
        minimums on Legality and Reasoning plus a secondary win-rate floor,
        and log a normalized composite score for visibility. When metrics
        are unavailable, fall back to the legacy binary win-rate gate so the
        pipeline still functions.
        """
        if quality is None:
            # Degraded path: quality couldn't be measured. Demand the legacy
            # (stricter) win-rate so we don't promote unverified models.
            passed = win_rate > self.fallback_win_rate
            verdict = "passes" if passed else "fails"
            logger.warning(
                f"Quality gate unavailable; using legacy win-rate gate: "
                f"{win_rate * 100:.1f}% {verdict} (> {self.fallback_win_rate * 100:.0f}%)."
            )
            return passed

        legality_mean = quality.get("legality", 0.0)
        reasoning_mean = quality.get("reasoning", 0.0)
        correctness_mean = quality.get("correctness", 0.0)

        # Normalize 1-5 rubric scores to 0-1; win-rate is already 0-1.
        legality_norm = (legality_mean - 1) / 4 if legality_mean > 0 else 0.0
        reasoning_norm = (reasoning_mean - 1) / 4 if reasoning_mean > 0 else 0.0
        correctness_norm = (correctness_mean - 1) / 4 if correctness_mean > 0 else 0.0
        win_rate_norm = min(max(win_rate, 0.0), 1.0)

        # Weighted composite: 30% legality, 30% reasoning, 20% correctness, 20% win-rate.
        if legality_mean > 0 and reasoning_mean > 0:
            composite_score = (
                0.30 * legality_norm
                + 0.30 * reasoning_norm
                + 0.20 * correctness_norm
                + 0.20 * win_rate_norm
            )
        else:
            composite_score = 0.0

        logger.info(
            f"Quality metrics: legality={legality_mean:.2f}, "
            f"reasoning={reasoning_mean:.2f}, correctness={correctness_mean:.2f}, "
            f"win-rate={win_rate * 100:.1f}%"
        )
        logger.info(f"Composite score: {composite_score:.3f}/1.0")

        # Hard minimums are blocking; win-rate is a secondary driver.
        failures = []
        if legality_mean < self.legality_min:
            failures.append(f"legality {legality_mean:.2f} < {self.legality_min}")
        if reasoning_mean < self.reasoning_min:
            failures.append(f"reasoning {reasoning_mean:.2f} < {self.reasoning_min}")
        if win_rate < self.win_rate_min:
            failures.append(
                f"win-rate {win_rate * 100:.1f}% < {self.win_rate_min * 100:.0f}%"
            )

        if not failures:
            logger.info(
                f"Hard minimums satisfied: legality >= {self.legality_min}, "
                f"reasoning >= {self.reasoning_min}, "
                f"win-rate >= {self.win_rate_min * 100:.0f}%."
            )
            return True

        logger.info(f"Failed gates: {', '.join(failures)}")
        return False


def main():
    p = argparse.ArgumentParser(description="Iterative recursive self-improvement pipeline driver.")
    p.add_argument("--champion-backend", required=True, help="Baseline model spec (Ollama, online, etc.)")
    p.add_argument("--challenger-backend", required=True, help="Target Challenger backend spec")
    p.add_argument("--iterations", type=int, default=1, help="Number of self-improvement epochs")
    p.add_argument("--matches-per-iter", type=int, default=10, help="Matches per SFT/DPO iteration")
    p.add_argument("--gate-matches", type=int, default=6, help="Evaluation matches for gating promotion")
    p.add_argument("--sets", default="EOE", help="MTGA set codes for random decks")
    p.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
    p.add_argument("--eval-corpus", type=Path, default=None,
                   help="Prompt corpus for the quality gate (default: tools/eval/data/seed_prompts.jsonl)")
    p.add_argument("--judge-backend", default="online:gpt-5.4",
                   help="Judge model spec for scoring Challenger responses (default: online:gpt-5.4)")
    p.add_argument("--legality-min", type=float, default=4.0,
                   help="Hard minimum mean Legality score (1-5) to promote (default: 4.0)")
    p.add_argument("--reasoning-min", type=float, default=3.5,
                   help="Hard minimum mean Reasoning score (1-5) to promote (default: 3.5)")
    p.add_argument("--win-rate-min", type=float, default=0.45,
                   help="Secondary win-rate floor to promote (default: 0.45)")
    p.add_argument("--fallback-win-rate", type=float, default=0.55,
                   help="Legacy binary win-rate gate used only when the eval harness can't run (default: 0.55)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    runner = PipelineRunner(
        champion_backend=args.champion_backend,
        challenger_backend=args.challenger_backend,
        iterations=args.iterations,
        matches_per_iter=args.matches_per_iter,
        gate_matches=args.gate_matches,
        sets=args.sets,
        license_key=args.license_key,
        eval_corpus=args.eval_corpus,
        judge_backend=args.judge_backend,
        legality_min=args.legality_min,
        reasoning_min=args.reasoning_min,
        win_rate_min=args.win_rate_min,
        fallback_win_rate=args.fallback_win_rate,
    )
    runner.run()


if __name__ == "__main__":
    main()
