"""Master pipeline runner for MTG Arena recursive self-improvement.

Automates the iterative loop:
  1. Data Generation (Self-play bot battles via self_play.py)
  2. Dataset compilation (build_dataset.py)
  3. Fine-tuning (train.py)
  4. Gating evaluation (bot battles: Challenger vs Champion)
  5. Promotion of Challenger to Champion if win rate > 55%.

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
import logging
import os
import shutil
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
    ):
        self.champion_backend = champion_backend
        self.challenger_backend = challenger_backend
        self.iterations = iterations
        self.matches_per_iter = matches_per_iter
        self.gate_matches = gate_matches
        self.sets = sets
        self.license_key = license_key

        self.trajectories_path = REPO / "tools/eval/data/self_play_trajectories.jsonl"
        self.sft_path = REPO / "tools/training/data/sft_dataset.json"
        self.dpo_path = REPO / "tools/training/data/dpo_dataset.json"
        self.checkpoint_dir = REPO / "tools/training/checkpoints/challenger_adapter"
        self.champion_dir = REPO / "tools/training/checkpoints/champion_adapter"

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

            if win_rate > 0.55:
                logger.info(f"✓ Challenger promoted! Win rate is {win_rate * 100:.1f}% (> 55%). Overwriting Champion baseline.")
                # Copy Challenger adapter directory as the new Champion
                if self.champion_dir.exists():
                    shutil.rmtree(self.champion_dir)
                shutil.copytree(self.checkpoint_dir, self.champion_dir)
            else:
                logger.info(f"✗ Challenger rejected. Win rate is {win_rate * 100:.1f}% (<= 55%). Retaining Champion baseline.")

        logger.info("\nSelf-improvement pipeline execution complete!")


def main():
    p = argparse.ArgumentParser(description="Iterative recursive self-improvement pipeline driver.")
    p.add_argument("--champion-backend", required=True, help="Baseline model spec (Ollama, online, etc.)")
    p.add_argument("--challenger-backend", required=True, help="Target Challenger backend spec")
    p.add_argument("--iterations", type=int, default=1, help="Number of self-improvement epochs")
    p.add_argument("--matches-per-iter", type=int, default=10, help="Matches per SFT/DPO iteration")
    p.add_argument("--gate-matches", type=int, default=6, help="Evaluation matches for gating promotion")
    p.add_argument("--sets", default="EOE", help="MTGA set codes for random decks")
    p.add_argument("--license-key", default=os.environ.get("MTGACOACH_LICENSE_KEY", ""))
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
    )
    runner.run()


if __name__ == "__main__":
    main()
