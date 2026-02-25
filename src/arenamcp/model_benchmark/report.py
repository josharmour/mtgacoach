"""Report generation for benchmark results.

Produces human-readable comparison reports and machine-readable summaries
for identifying the best cost-quality tradeoffs.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from arenamcp.model_benchmark.runner import BenchmarkRun, ModelResult

logger = logging.getLogger(__name__)


@dataclass
class ModelSummary:
    """Aggregated performance summary for one model across all scenarios."""

    model: str
    backend: str
    scenarios_run: int = 0
    scenarios_correct: int = 0
    scenarios_failed: int = 0

    # Quality averages (0-1)
    avg_composite: float = 0.0
    avg_action_correctness: float = 0.0
    avg_rule_compliance: float = 0.0
    avg_completeness: float = 0.0
    avg_hallucination_penalty: float = 0.0
    avg_conciseness: float = 0.0

    # Performance
    avg_latency_s: float = 0.0
    p95_latency_s: float = 0.0
    min_latency_s: float = 0.0
    max_latency_s: float = 0.0

    # Cost
    total_cost_usd: float = 0.0
    avg_cost_per_scenario_usd: float = 0.0
    cost_per_quality_point: float = 0.0  # cost / composite — lower is better

    # Category breakdowns
    category_scores: dict = field(default_factory=dict)
    difficulty_scores: dict = field(default_factory=dict)


def _percentile(values: list[float], pct: float) -> float:
    """Compute percentile from a sorted list."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    idx = int(len(sorted_v) * pct / 100)
    return sorted_v[min(idx, len(sorted_v) - 1)]


def _aggregate_model(
    model: str,
    results: list[ModelResult],
    scenarios_by_id: dict,
) -> ModelSummary:
    """Aggregate results for a single model."""
    summary = ModelSummary(
        model=model,
        backend=results[0].backend if results else "",
        scenarios_run=len(results),
    )

    if not results:
        return summary

    composites = []
    action_scores = []
    rule_scores = []
    completeness_scores = []
    hallucination_scores = []
    conciseness_scores = []
    latencies = []
    costs = []
    category_results: dict[str, list[float]] = defaultdict(list)
    difficulty_results: dict[str, list[float]] = defaultdict(list)

    for r in results:
        if r.error:
            summary.scenarios_failed += 1
            continue

        q = r.quality
        composites.append(q.composite)
        action_scores.append(q.action_correctness)
        rule_scores.append(q.rule_compliance)
        completeness_scores.append(q.completeness)
        hallucination_scores.append(q.hallucination_penalty)
        conciseness_scores.append(q.conciseness)
        latencies.append(r.latency_s)
        costs.append(r.cost_usd_est)

        if q.action_correctness >= 0.8:
            summary.scenarios_correct += 1

        # Category/difficulty breakdowns
        scenario = scenarios_by_id.get(r.scenario_id)
        if scenario:
            category_results[scenario.category].append(q.composite)
            difficulty_results[scenario.difficulty].append(q.composite)

    n = len(composites) or 1

    summary.avg_composite = sum(composites) / n
    summary.avg_action_correctness = sum(action_scores) / n
    summary.avg_rule_compliance = sum(rule_scores) / n
    summary.avg_completeness = sum(completeness_scores) / n
    summary.avg_hallucination_penalty = sum(hallucination_scores) / n
    summary.avg_conciseness = sum(conciseness_scores) / n

    summary.avg_latency_s = sum(latencies) / n if latencies else 0.0
    summary.p95_latency_s = _percentile(latencies, 95)
    summary.min_latency_s = min(latencies) if latencies else 0.0
    summary.max_latency_s = max(latencies) if latencies else 0.0

    summary.total_cost_usd = sum(costs)
    summary.avg_cost_per_scenario_usd = summary.total_cost_usd / n
    if summary.avg_composite > 0:
        summary.cost_per_quality_point = summary.total_cost_usd / (summary.avg_composite * n)
    else:
        summary.cost_per_quality_point = float("inf")

    for cat, scores in category_results.items():
        summary.category_scores[cat] = round(sum(scores) / len(scores), 3)
    for diff, scores in difficulty_results.items():
        summary.difficulty_scores[diff] = round(sum(scores) / len(scores), 3)

    return summary


def generate_report(
    run: BenchmarkRun,
    scenarios: Optional[list] = None,
    output_path: Optional[Path] = None,
) -> str:
    """Generate a formatted comparison report from benchmark results.

    Args:
        run: The completed benchmark run.
        scenarios: Optional list of EvalScenario objects for category breakdowns.
        output_path: Optional path to write the report.

    Returns:
        Formatted report string.
    """
    # Group results by model
    by_model: dict[str, list[ModelResult]] = defaultdict(list)
    for r in run.results:
        by_model[r.model].append(r)

    # Build scenario lookup
    scenarios_by_id = {}
    if scenarios:
        scenarios_by_id = {s.id: s for s in scenarios}

    # Compute summaries
    summaries = []
    for model, results in by_model.items():
        summaries.append(_aggregate_model(model, results, scenarios_by_id))

    # Sort by composite score descending
    summaries.sort(key=lambda s: s.avg_composite, reverse=True)

    lines: list[str] = []

    lines.append("=" * 80)
    lines.append("MODEL PERFORMANCE BENCHMARK REPORT")
    lines.append(f"Run ID: {run.run_id}")
    lines.append(f"Timestamp: {run.timestamp}")
    lines.append(f"Scenarios: {run.scenario_count}")
    lines.append(f"Models: {len(summaries)}")
    lines.append("=" * 80)

    # --- Ranking table ---
    lines.append("")
    lines.append("OVERALL RANKING (by composite quality score)")
    lines.append("-" * 80)
    lines.append(
        f"{'Rank':<5} {'Model':<28} {'Quality':>8} {'Correct':>8} "
        f"{'Latency':>9} {'Cost/Scn':>10} {'Cost/Qual':>10}"
    )
    lines.append("-" * 80)

    for i, s in enumerate(summaries, 1):
        cost_qual = f"${s.cost_per_quality_point:.4f}" if s.cost_per_quality_point < 1000 else "N/A"
        lines.append(
            f"{i:<5} {s.model:<28} {s.avg_composite:>7.3f} "
            f"{s.scenarios_correct:>4}/{s.scenarios_run:<3} "
            f"{s.avg_latency_s:>7.2f}s "
            f"${s.avg_cost_per_scenario_usd:>8.5f} "
            f"{cost_qual:>10}"
        )

    # --- Detailed per-model breakdown ---
    lines.append("")
    lines.append("=" * 80)
    lines.append("DETAILED MODEL BREAKDOWNS")
    lines.append("=" * 80)

    for s in summaries:
        lines.append("")
        lines.append(f"--- {s.model} ({s.backend}) ---")
        lines.append(f"  Scenarios: {s.scenarios_run} run, {s.scenarios_correct} correct, {s.scenarios_failed} failed")
        lines.append(f"  Quality Scores (0-1 scale):")
        lines.append(f"    Composite:        {s.avg_composite:.3f}")
        lines.append(f"    Action Correct:   {s.avg_action_correctness:.3f}")
        lines.append(f"    Rule Compliance:  {s.avg_rule_compliance:.3f}")
        lines.append(f"    Completeness:     {s.avg_completeness:.3f}")
        lines.append(f"    Hallucination:    {s.avg_hallucination_penalty:.3f}  (lower=better)")
        lines.append(f"    Conciseness:      {s.avg_conciseness:.3f}")
        lines.append(f"  Latency:")
        lines.append(f"    Avg: {s.avg_latency_s:.2f}s  P95: {s.p95_latency_s:.2f}s  Range: {s.min_latency_s:.2f}-{s.max_latency_s:.2f}s")
        lines.append(f"  Cost:")
        lines.append(f"    Total: ${s.total_cost_usd:.6f}  Per scenario: ${s.avg_cost_per_scenario_usd:.6f}")
        cost_qual = f"${s.cost_per_quality_point:.6f}" if s.cost_per_quality_point < 1000 else "N/A"
        lines.append(f"    Per quality point: {cost_qual}")

        if s.category_scores:
            lines.append(f"  By Category:")
            for cat, score in sorted(s.category_scores.items()):
                lines.append(f"    {cat:<20} {score:.3f}")

        if s.difficulty_scores:
            lines.append(f"  By Difficulty:")
            for diff, score in sorted(s.difficulty_scores.items()):
                lines.append(f"    {diff:<20} {score:.3f}")

    # --- Cost-quality analysis ---
    lines.append("")
    lines.append("=" * 80)
    lines.append("COST-QUALITY ANALYSIS")
    lines.append("=" * 80)

    cloud_models = [s for s in summaries if s.total_cost_usd > 0]
    local_models = [s for s in summaries if s.total_cost_usd == 0]

    if cloud_models:
        lines.append("")
        lines.append("Cloud Models (by cost efficiency — lower cost/quality is better):")
        cloud_sorted = sorted(cloud_models, key=lambda s: s.cost_per_quality_point)
        for s in cloud_sorted:
            cost_qual = f"${s.cost_per_quality_point:.6f}" if s.cost_per_quality_point < 1000 else "N/A"
            lines.append(
                f"  {s.model:<28} quality={s.avg_composite:.3f}  "
                f"cost/scn=${s.avg_cost_per_scenario_usd:.5f}  "
                f"cost/qual={cost_qual}"
            )

        # Best value pick
        best_value = cloud_sorted[0]
        best_quality = max(cloud_models, key=lambda s: s.avg_composite)
        lines.append("")
        lines.append(f"  Best Value:   {best_value.model} (cost/qual={best_value.cost_per_quality_point:.6f})")
        lines.append(f"  Best Quality: {best_quality.model} (composite={best_quality.avg_composite:.3f})")

    if local_models:
        lines.append("")
        lines.append("Local Models (free — ranked by quality):")
        local_sorted = sorted(local_models, key=lambda s: s.avg_composite, reverse=True)
        for s in local_sorted:
            lines.append(
                f"  {s.model:<28} quality={s.avg_composite:.3f}  "
                f"latency={s.avg_latency_s:.2f}s"
            )

        best_local = local_sorted[0]
        lines.append("")
        lines.append(f"  Best Local: {best_local.model} (composite={best_local.avg_composite:.3f})")

    if cloud_models and local_models:
        best_cloud = max(cloud_models, key=lambda s: s.avg_composite)
        best_local = max(local_models, key=lambda s: s.avg_composite)
        quality_gap = best_cloud.avg_composite - best_local.avg_composite
        lines.append("")
        lines.append(f"  Cloud vs Local Quality Gap: {quality_gap:+.3f}")
        if quality_gap > 0.15:
            lines.append(f"  -> Cloud models provide meaningfully better advice quality.")
        elif quality_gap > 0.05:
            lines.append(f"  -> Cloud models are slightly better. Local may be acceptable for casual use.")
        else:
            lines.append(f"  -> Local models are competitive with cloud. Consider local for cost savings.")

    # --- Per-scenario details ---
    lines.append("")
    lines.append("=" * 80)
    lines.append("PER-SCENARIO RESULTS")
    lines.append("=" * 80)

    scenario_ids = list(dict.fromkeys(r.scenario_id for r in run.results))
    for sid in scenario_ids:
        scenario = scenarios_by_id.get(sid)
        scenario_name = scenario.name if scenario else sid
        lines.append("")
        lines.append(f"  [{sid}] {scenario_name}")

        scenario_results = [r for r in run.results if r.scenario_id == sid]
        scenario_results.sort(key=lambda r: r.quality.composite, reverse=True)
        for r in scenario_results:
            status = "OK" if r.quality.action_correctness >= 0.8 else "MISS"
            lines.append(
                f"    {r.model:<28} {status:<5} "
                f"score={r.quality.composite:.3f}  "
                f"latency={r.latency_s:.2f}s"
            )
            # Show first 100 chars of response for quick review
            snippet = (r.response or "")[:100].replace("\n", " ")
            if snippet:
                lines.append(f"      -> {snippet}")

    lines.append("")
    lines.append("=" * 80)
    lines.append("END OF REPORT")
    lines.append("=" * 80)

    report = "\n".join(lines)

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            f.write(report)
        logger.info(f"Report saved to {output_path}")

        # Also save machine-readable summary
        json_path = output_path.with_suffix(".json")
        summary_data = {
            "run_id": run.run_id,
            "timestamp": run.timestamp,
            "models": [
                {
                    "model": s.model,
                    "backend": s.backend,
                    "composite": round(s.avg_composite, 4),
                    "action_correctness": round(s.avg_action_correctness, 4),
                    "rule_compliance": round(s.avg_rule_compliance, 4),
                    "avg_latency_s": round(s.avg_latency_s, 3),
                    "p95_latency_s": round(s.p95_latency_s, 3),
                    "total_cost_usd": round(s.total_cost_usd, 6),
                    "cost_per_quality_point": round(s.cost_per_quality_point, 6) if s.cost_per_quality_point < 1000 else None,
                    "scenarios_correct": s.scenarios_correct,
                    "scenarios_run": s.scenarios_run,
                    "category_scores": s.category_scores,
                    "difficulty_scores": s.difficulty_scores,
                }
                for s in summaries
            ],
        }
        with open(json_path, "w") as f:
            json.dump(summary_data, f, indent=2)
        logger.info(f"Summary JSON saved to {json_path}")

    return report
