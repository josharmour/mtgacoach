"""Fail-closed Stage 0 gate (WP-0.6 / WP-1.3) + registry registration (WP-0.5).

Evaluates a candidate's responses on a frozen, training-excluded mulligan
corpus against production contracts and 17lands bucket ground truth:

  * schema_parse_rate   — validators.validate_action_schema_json
  * contract_compliance — parsed mulligan responses must be a
                          mulligan_keep / mulligan_mull action
  * decision_accuracy   — action agrees with the bucket's higher-WR option
  * baseline comparison — optional non-inferiority vs a baseline responses
                          file over the same prompts (accuracy delta)

Fail-closed semantics: ANY missing input, sample shortfall, or exception
yields verdict BLOCKED (exit 2). PASS (exit 0) requires every threshold to
hold on a sufficient sample. There is no degraded/fallback gate.

On PASS only (fail closed — no bypass flag),
the checkpoint can be registered into the content-addressed registry with
the gate report attached:

    python -m tools.training.gate_stage0 \\
        --prompts tools/training/data/gate_prompts.jsonl \\
        --responses tools/eval/data/gate_candidate_responses.jsonl \\
        --backend-label "openai-compat:mtgacoach-gemma4-v0" \\
        --baseline-responses tools/eval/data/gate_base_responses.jsonl \\
        --baseline-label "openai-compat:gemma-4-12b-it" \\
        --report tools/training/data/gate_report_gen0001.json \\
        --register --registry-dir models --gen-id gen-0001-v0 \\
        --base-model google/gemma-4-12B-it \\
        --adapter-path tools/training/checkpoints/stage0_gemma12b_lora
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tools.training.validators import validate_action_schema_json  # noqa: E402

logger = logging.getLogger("tools.training.gate_stage0")

MIN_SAMPLES = 100  # gate refuses to certify on less
MIN_PARSE_RATE = 0.99  # WP-1.3: 99%+ schema parse rate
MIN_CONTRACT_RATE = 1.00  # parsed mulligan responses must use the contract
MAX_ACCURACY_REGRESSION = 0.02  # vs baseline, when a baseline is provided


def _read_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _extract_json_decision(response: str) -> str | None:
    """Map a schema-JSON response to keep/mull, else None."""
    s = (response or "").strip()
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    s = re.sub(r",\s*([\]}])", r"\1", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list) or not actions:
        return None
    at = str(actions[0].get("action_type") or "")
    if at == "mulligan_keep":
        return "keep"
    if at == "mulligan_mull":
        return "mull"
    return None


def _extract_keyword_decision(response: str) -> str | None:
    """Keyword fallback (KEEP/MULLIGAN prose) — used so a non-schema baseline
    still yields a decision-accuracy comparison. Candidates are still held to
    the JSON contract via schema_parse_rate/contract_rate."""
    head = (response or "").strip().upper()[:40]
    if head.startswith("KEEP"):
        return "keep"
    if head.startswith("MULL"):
        return "mull"
    return None


import random


def paired_bootstrap_ci(
    pairs: list[dict],
    n_bootstraps: int = 10000,
    seed: int = 42,
) -> dict:
    """Compute paired-bootstrap 95% confidence intervals for raw accuracy delta
    and balanced accuracy delta between candidate and baseline predictions."""
    if not pairs:
        return {}

    rng = random.Random(seed)
    n = len(pairs)

    delta_raw_list = []
    delta_bal_list = []

    for _ in range(n_bootstraps):
        sample = [pairs[rng.randint(0, n - 1)] for _ in range(n)]

        cand_correct = sum(1 for p in sample if p["cand_correct"])
        base_correct = sum(1 for p in sample if p["base_correct"])
        delta_raw_list.append((cand_correct - base_correct) / n)

        keep_sample = [p for p in sample if p["truth"] == "keep"]
        mull_sample = [p for p in sample if p["truth"] == "mull"]

        if keep_sample and mull_sample:
            cand_keep_acc = sum(1 for p in keep_sample if p["cand_correct"]) / len(keep_sample)
            cand_mull_acc = sum(1 for p in mull_sample if p["cand_correct"]) / len(mull_sample)
            cand_bal = 0.5 * (cand_keep_acc + cand_mull_acc)

            base_keep_acc = sum(1 for p in keep_sample if p["base_correct"]) / len(keep_sample)
            base_mull_acc = sum(1 for p in mull_sample if p["base_correct"]) / len(mull_sample)
            base_bal = 0.5 * (base_keep_acc + base_mull_acc)

            delta_bal_list.append(cand_bal - base_bal)

    delta_raw_list.sort()
    raw_ci_lower = delta_raw_list[int(0.025 * n_bootstraps)]
    raw_ci_upper = delta_raw_list[int(0.975 * n_bootstraps)]

    bal_ci_lower = bal_ci_upper = None
    if delta_bal_list:
        delta_bal_list.sort()
        bal_ci_lower = delta_bal_list[int(0.025 * len(delta_bal_list))]
        bal_ci_upper = delta_bal_list[int(0.975 * len(delta_bal_list))]

    return {
        "raw_accuracy_delta_95ci": [round(raw_ci_lower, 4), round(raw_ci_upper, 4)],
        "balanced_accuracy_delta_95ci": [round(bal_ci_lower, 4), round(bal_ci_upper, 4)]
        if bal_ci_lower is not None
        else None,
    }


def evaluate(prompts_path: Path, responses_path: Path, backend_label: str) -> dict:
    prompts_list = list(_read_jsonl(prompts_path))
    prompts = {p.get("id"): p for p in prompts_list if p.get("id")}
    prompt_count = len(prompts_list)
    n = parsed = contract_ok = scorable = correct = errors = 0
    recorded_decoding_params: list = []
    arm_n = {"keep": 0, "mull": 0}
    arm_correct = {"keep": 0, "mull": 0}
    per_prompt_decisions: dict[str, dict] = {}
    seen_n = seen_correct = unseen_n = unseen_correct = 0

    for rec in _read_jsonl(responses_path):
        if backend_label and rec.get("backend") != backend_label:
            continue
        pid = rec.get("prompt_id")
        prompt = prompts.get(pid)
        if prompt is None:
            continue
        n += 1
        if rec.get("decoding_params"):
            recorded_decoding_params.append(rec.get("decoding_params"))
        elif "temperature" in rec:
            recorded_decoding_params.append({"temperature": rec.get("temperature")})
        if rec.get("error"):
            errors += 1
            continue
        response = rec.get("response") or ""
        ok, _ = validate_action_schema_json(response)
        json_decision = _extract_json_decision(response)
        if ok:
            parsed += 1
            if json_decision is not None:
                contract_ok += 1
        decision = json_decision or _extract_keyword_decision(response)
        truth = ((prompt.get("meta") or {}).get("bucket_stats") or {}).get("correct")
        is_held_out = bool((prompt.get("meta") or {}).get("bucket_held_out", False))
        if decision is not None and truth in ("keep", "mull"):
            scorable += 1
            arm_n[truth] += 1
            is_correct = decision == truth
            if is_correct:
                correct += 1
                arm_correct[truth] += 1

            if is_held_out:
                unseen_n += 1
                if is_correct:
                    unseen_correct += 1
            else:
                seen_n += 1
                if is_correct:
                    seen_correct += 1

            per_prompt_decisions[pid] = {
                "decision": decision,
                "truth": truth,
                "correct": is_correct,
                "bucket_held_out": is_held_out,
            }

    # Balanced accuracy (mean of per-arm accuracies) defends against
    # degenerate always-keep/always-mull policies on the keep-heavy corpus.
    balanced = None
    if arm_n["keep"] >= 5 and arm_n["mull"] >= 5:
        balanced = 0.5 * (arm_correct["keep"] / arm_n["keep"] + arm_correct["mull"] / arm_n["mull"])

    return {
        "backend": backend_label,
        "prompt_count": prompt_count,
        "n": n,
        "errors": errors,
        "schema_parse_rate": parsed / n if n else 0.0,
        "contract_rate": contract_ok / parsed if parsed else 0.0,
        "scorable": scorable,
        "decision_accuracy": correct / scorable if scorable else 0.0,
        "seen_bucket_n": seen_n,
        "seen_bucket_accuracy": seen_correct / seen_n if seen_n else None,
        "unseen_bucket_n": unseen_n,
        "unseen_bucket_accuracy": unseen_correct / unseen_n if unseen_n else None,
        "keep_n": arm_n["keep"],
        "mull_n": arm_n["mull"],
        "keep_accuracy": arm_correct["keep"] / arm_n["keep"] if arm_n["keep"] else None,
        "mull_accuracy": arm_correct["mull"] / arm_n["mull"] if arm_n["mull"] else None,
        "balanced_accuracy": balanced,
        "per_prompt_decisions": per_prompt_decisions,
        "recorded_decoding_params": recorded_decoding_params[:5]
        if recorded_decoding_params
        else [{"temperature": 0.0}],
    }


def main():
    p = argparse.ArgumentParser(description="Fail-closed Stage 0 promotion gate.")
    p.add_argument("--prompts", required=True, type=Path)
    p.add_argument("--responses", required=True, type=Path)
    p.add_argument("--backend-label", required=True)
    p.add_argument("--baseline-responses", type=Path)
    p.add_argument("--baseline-label", default="")
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--min-samples", type=int, default=MIN_SAMPLES)
    p.add_argument("--min-parse-rate", type=float, default=MIN_PARSE_RATE)
    p.add_argument("--min-contract-rate", type=float, default=MIN_CONTRACT_RATE)
    p.add_argument("--max-accuracy-regression", type=float, default=MAX_ACCURACY_REGRESSION)
    p.add_argument(
        "--tripwire-fixtures",
        type=Path,
        default=None,
        help="Path to tripwire puzzle fixtures (inactive floor check)",
    )
    # Registration
    p.add_argument("--register", action="store_true")
    p.add_argument("--registry-dir", type=Path, default=REPO / "models")
    p.add_argument("--gen-id", default="")
    p.add_argument("--base-model", default="")
    p.add_argument("--adapter-path", type=Path)
    p.add_argument("--parent-gen", default=None)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s | %(message)s")

    verdict = "BLOCKED"
    failures: list[str] = []
    candidate: dict = {}
    baseline: dict = {}
    paired_bootstrap_metrics: dict = {}

    try:
        if not args.prompts.exists():
            failures.append(f"gate corpus missing: {args.prompts}")
        if not args.responses.exists():
            failures.append(f"candidate responses missing: {args.responses}")

        if not failures:
            candidate = evaluate(args.prompts, args.responses, args.backend_label)
            logger.info(f"candidate metrics: {candidate}")

            if candidate["n"] != candidate["prompt_count"]:
                failures.append(
                    f"candidate response count ({candidate['n']}) != prompt count ({candidate['prompt_count']}) — fail closed"
                )
            if candidate["n"] < args.min_samples:
                failures.append(f"insufficient samples: {candidate['n']} < {args.min_samples}")
            if candidate["schema_parse_rate"] < args.min_parse_rate:
                failures.append(
                    f"schema_parse_rate {candidate['schema_parse_rate']:.4f} < {args.min_parse_rate}"
                )
            if candidate["contract_rate"] < args.min_contract_rate:
                failures.append(f"contract_rate {candidate['contract_rate']:.4f} < {args.min_contract_rate}")
            if candidate["scorable"] < args.min_samples // 2:
                failures.append(f"insufficient scorable decisions: {candidate['scorable']}")
            if candidate.get("balanced_accuracy") is None:
                failures.append(
                    "candidate balanced accuracy incomputable (insufficient keep/mull samples) — fail closed"
                )

            if args.baseline_responses is not None:
                if not args.baseline_responses.exists():
                    failures.append(
                        f"baseline responses missing: {args.baseline_responses} "
                        "(fail closed — no gate without the declared baseline)"
                    )
                else:
                    baseline = evaluate(
                        args.prompts,
                        args.baseline_responses,
                        args.baseline_label,
                    )
                    logger.info(f"baseline metrics: {baseline}")
                    if baseline["n"] != baseline["prompt_count"]:
                        failures.append(
                            f"baseline response count ({baseline['n']}) != prompt count ({baseline['prompt_count']}) — fail closed"
                        )
                    if baseline.get("balanced_accuracy") is None:
                        failures.append(
                            "baseline balanced accuracy incomputable (insufficient keep/mull samples) — fail closed"
                        )

                    if baseline.get("scorable", 0) >= args.min_samples // 2:
                        delta = candidate["decision_accuracy"] - baseline["decision_accuracy"]
                        candidate["accuracy_delta_vs_baseline"] = round(delta, 4)
                        cand_bal = candidate.get("balanced_accuracy")
                        base_bal = baseline.get("balanced_accuracy")
                        if cand_bal is not None and base_bal is not None:
                            bal_delta = cand_bal - base_bal
                            candidate["balanced_delta_vs_baseline"] = round(bal_delta, 4)

                        # Build paired observations
                        cand_dec_map = candidate.get("per_prompt_decisions", {})
                        base_dec_map = baseline.get("per_prompt_decisions", {})
                        paired_data = []
                        for pid, c_item in cand_dec_map.items():
                            if pid in base_dec_map:
                                b_item = base_dec_map[pid]
                                paired_data.append(
                                    {
                                        "prompt_id": pid,
                                        "truth": c_item["truth"],
                                        "cand_correct": c_item["correct"],
                                        "base_correct": b_item["correct"],
                                    }
                                )

                        if paired_data:
                            paired_bootstrap_metrics = paired_bootstrap_ci(paired_data)
                            logger.info(f"paired bootstrap metrics: {paired_bootstrap_metrics}")

                            raw_ci = paired_bootstrap_metrics.get("raw_accuracy_delta_95ci")
                            if raw_ci and raw_ci[0] < -args.max_accuracy_regression:
                                failures.append(
                                    f"decision accuracy regressed under paired bootstrap 95% CI: lower bound {raw_ci[0]:+.4f} < -{args.max_accuracy_regression} "
                                    f"(point delta: {delta:+.4f}, 95% CI: {raw_ci})"
                                )

                            bal_ci = paired_bootstrap_metrics.get("balanced_accuracy_delta_95ci")
                            if bal_ci and bal_ci[0] < -0.05:
                                failures.append(
                                    f"balanced accuracy regressed under paired bootstrap 95% CI: lower bound {bal_ci[0]:+.4f} < -0.05 "
                                    f"(point delta: {candidate.get('balanced_delta_vs_baseline', 0.0):+.4f}, 95% CI: {bal_ci})"
                                )
                    else:
                        failures.append("baseline responses unscorable — fail closed")

        if not failures:
            verdict = "PASS"
    except Exception as e:  # any evaluation error blocks promotion
        failures.append(f"gate exception: {type(e).__name__}: {e}")
        verdict = "BLOCKED"

    git_sha = ""
    try:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:
        pass

    report = {
        "gate": "stage0",
        "verdict": verdict,
        "failures": failures,
        "candidate": candidate,
        "baseline": baseline,
        "paired_bootstrap": paired_bootstrap_metrics,
        "thresholds": {
            "min_samples": args.min_samples,
            "min_parse_rate": args.min_parse_rate,
            "min_contract_rate": args.min_contract_rate,
            "max_accuracy_regression": args.max_accuracy_regression,
        },
        "prompts": str(args.prompts),
        "responses": str(args.responses),
        "git_sha": git_sha,
        "ts": time.time(),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"gate verdict: {verdict}; report written to {args.report}")
    if failures:
        for msg in failures:
            logger.error(f"  gate failure: {msg}")

    if args.register:
        if verdict != "PASS":
            logger.error("Refusing to register: gate verdict is not PASS (fail closed).")
            sys.exit(2)
        if not (args.gen_id and args.base_model and args.adapter_path):
            logger.error("--register requires --gen-id, --base-model, --adapter-path")
            sys.exit(2)
        from tools.training.registry import ModelRegistry

        reg = ModelRegistry(args.registry_dir)
        reg.register_generation(
            gen_id=args.gen_id,
            base_model=args.base_model,
            adapter_path=args.adapter_path,
            parent_gen=args.parent_gen,
            git_sha=git_sha,
            metadata={
                "stage": "stage0-sft",
                "gate_prompts": str(args.prompts),
                "backend_label": args.backend_label,
            },
            gate_report=report,
        )
        logger.info(f"✓ Registered {args.gen_id} in {args.registry_dir}/registry.sqlite")

    sys.exit(0 if verdict == "PASS" else 2)


if __name__ == "__main__":
    main()
