"""Model performance benchmarking framework.

Compares LLM model quality across standardized MTG coaching scenarios
to identify the best cost-quality tradeoff for cloud models and the
ideal local model option.
"""

from arenamcp.model_benchmark.scenarios import EvalScenario, load_scenarios, save_scenarios
from arenamcp.model_benchmark.runner import BenchmarkRunner
from arenamcp.model_benchmark.metrics import score_response, QualityScore
from arenamcp.model_benchmark.report import generate_report

__all__ = [
    "EvalScenario",
    "load_scenarios",
    "save_scenarios",
    "BenchmarkRunner",
    "score_response",
    "QualityScore",
    "generate_report",
]
