"""Benchmark runner — executes scenarios across multiple models and collects results.

Handles backend instantiation, latency measurement, cost estimation,
retries, and result persistence.
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from arenamcp.model_benchmark.scenarios import EvalScenario
from arenamcp.model_benchmark.metrics import QualityScore, score_response

logger = logging.getLogger(__name__)


# Approximate per-token costs (USD) for popular models.
# Input / output pricing per 1M tokens. Updated as of early 2026.
# Users can override via model_costs parameter.
MODEL_COST_TABLE: dict[str, dict[str, float]] = {
    # Cloud — Anthropic
    "claude-opus-4": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4": {"input": 3.0, "output": 15.0},
    "claude-haiku-3.5": {"input": 0.80, "output": 4.0},
    # Cloud — OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "o3-mini": {"input": 1.10, "output": 4.40},
    # Cloud — Google
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    # Local — free to run (electricity only)
    "llama3.2": {"input": 0.0, "output": 0.0},
    "llama3.1": {"input": 0.0, "output": 0.0},
    "llama3.3": {"input": 0.0, "output": 0.0},
    "mistral": {"input": 0.0, "output": 0.0},
    "mixtral": {"input": 0.0, "output": 0.0},
    "phi3": {"input": 0.0, "output": 0.0},
    "phi4": {"input": 0.0, "output": 0.0},
    "qwen2.5": {"input": 0.0, "output": 0.0},
    "qwen3": {"input": 0.0, "output": 0.0},
    "gemma2": {"input": 0.0, "output": 0.0},
    "gemma3": {"input": 0.0, "output": 0.0},
    "deepseek-r1": {"input": 0.0, "output": 0.0},
    "command-r": {"input": 0.0, "output": 0.0},
}


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English."""
    return max(1, len(text) // 4)


def _lookup_cost(model: str) -> dict[str, float]:
    """Find cost entry by prefix match (e.g., 'gpt-4o-2024...' -> 'gpt-4o')."""
    model_lower = model.lower()
    # Exact match first
    if model_lower in MODEL_COST_TABLE:
        return MODEL_COST_TABLE[model_lower]
    # Prefix match
    for key, cost in MODEL_COST_TABLE.items():
        if model_lower.startswith(key):
            return cost
    return {"input": 0.0, "output": 0.0}


@dataclass
class ModelResult:
    """Result of running one scenario through one model."""

    scenario_id: str
    model: str
    backend: str
    response: str
    quality: QualityScore
    latency_s: float
    input_tokens_est: int
    output_tokens_est: int
    cost_usd_est: float
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class BenchmarkRun:
    """Full benchmark run across all models and scenarios."""

    run_id: str
    timestamp: str
    models: list[str]
    scenario_count: int
    results: list[ModelResult] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "models": self.models,
            "scenario_count": self.scenario_count,
            "results": [r.to_dict() for r in self.results],
            "metadata": self.metadata,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)
        logger.info(f"Saved benchmark run to {path}")


@dataclass
class ModelConfig:
    """Configuration for a model to benchmark.

    Attributes:
        name: Display name (e.g., "Claude Sonnet 4")
        backend: Backend type for create_backend() (e.g., "ollama", "proxy", "api")
        model: Model identifier passed to the backend (e.g., "llama3.2", "gpt-4o")
        cost_override: Optional per-token cost override {"input": x, "output": y}
    """

    name: str
    backend: str
    model: str
    cost_override: Optional[dict[str, float]] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


class BenchmarkRunner:
    """Orchestrates benchmark execution across models and scenarios.

    Usage:
        models = [
            ModelConfig("Llama 3.2 (local)", "ollama", "llama3.2"),
            ModelConfig("GPT-4o", "api", "gpt-4o"),
        ]
        runner = BenchmarkRunner(models)
        run = runner.execute(scenarios)
        run.save(Path("results.json"))
    """

    def __init__(
        self,
        models: list[ModelConfig],
        style: str = "concise",
        retries: int = 1,
        model_costs: Optional[dict[str, dict[str, float]]] = None,
        progress_callback: Optional[Any] = None,
    ):
        """
        Args:
            models: List of model configurations to benchmark.
            style: Coaching style passed to CoachEngine ("concise" or "normal").
            retries: Number of retry attempts per scenario on failure.
            model_costs: Optional cost table overrides.
            progress_callback: Optional callback(status_str) for progress reporting.
        """
        self.models = models
        self.style = style
        self.retries = retries
        self.progress_callback = progress_callback

        if model_costs:
            MODEL_COST_TABLE.update(model_costs)

    def _create_backend(self, config: ModelConfig):
        """Instantiate an LLM backend from a ModelConfig."""
        from arenamcp.coach import create_backend

        return create_backend(
            config.backend,
            model=config.model,
            progress_callback=self.progress_callback,
        )

    def _run_single(
        self, scenario: EvalScenario, config: ModelConfig
    ) -> ModelResult:
        """Run a single scenario through a single model."""
        from arenamcp.coach import CoachEngine

        backend = self._create_backend(config)
        coach = CoachEngine(backend=backend)

        error = None
        response = ""
        latency = 0.0

        for attempt in range(1 + self.retries):
            try:
                start = time.perf_counter()
                response = coach.get_advice(
                    scenario.game_state,
                    trigger=scenario.trigger,
                    style=self.style,
                )
                latency = time.perf_counter() - start
                if response and response.strip():
                    error = None
                    break
                error = "empty_response"
            except Exception as e:
                latency = time.perf_counter() - start
                error = str(e)
                logger.warning(
                    f"[{config.name}] Scenario {scenario.id} attempt {attempt+1} failed: {e}"
                )
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))  # backoff

        # Close backend if it has a close method (CLI backends)
        if hasattr(backend, "close"):
            try:
                backend.close()
            except Exception:
                pass

        # Score the response
        quality = score_response(scenario, response)

        # Estimate cost
        # Build approximate prompt from system prompt + game context
        from arenamcp.coach import CONCISE_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT
        sys_prompt = CONCISE_SYSTEM_PROMPT if self.style == "concise" else DEFAULT_SYSTEM_PROMPT
        input_tokens = _estimate_tokens(sys_prompt + json.dumps(scenario.game_state))
        output_tokens = _estimate_tokens(response)

        cost_entry = config.cost_override or _lookup_cost(config.model)
        cost_usd = (
            input_tokens * cost_entry.get("input", 0.0) / 1_000_000
            + output_tokens * cost_entry.get("output", 0.0) / 1_000_000
        )

        return ModelResult(
            scenario_id=scenario.id,
            model=config.model,
            backend=config.backend,
            response=response,
            quality=quality,
            latency_s=round(latency, 3),
            input_tokens_est=input_tokens,
            output_tokens_est=output_tokens,
            cost_usd_est=round(cost_usd, 6),
            error=error,
        )

    def execute(
        self,
        scenarios: list[EvalScenario],
        run_id: Optional[str] = None,
    ) -> BenchmarkRun:
        """Execute all scenarios across all models.

        Args:
            scenarios: List of evaluation scenarios.
            run_id: Optional custom run identifier.

        Returns:
            BenchmarkRun with all results.
        """
        import uuid
        from datetime import datetime, timezone

        if run_id is None:
            run_id = f"bench_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        run = BenchmarkRun(
            run_id=run_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            models=[c.model for c in self.models],
            scenario_count=len(scenarios),
        )

        total = len(scenarios) * len(self.models)
        completed = 0

        for config in self.models:
            logger.info(f"Benchmarking model: {config.name} ({config.backend}/{config.model})")
            if self.progress_callback:
                self.progress_callback(f"Starting {config.name}...")

            for scenario in scenarios:
                completed += 1
                status = f"[{completed}/{total}] {config.name}: {scenario.name}"
                logger.info(status)
                if self.progress_callback:
                    self.progress_callback(status)

                result = self._run_single(scenario, config)
                run.results.append(result)

                if result.error:
                    logger.warning(
                        f"  -> ERROR: {result.error}"
                    )
                else:
                    logger.info(
                        f"  -> score={result.quality.composite:.2f} "
                        f"latency={result.latency_s:.2f}s "
                        f"cost=${result.cost_usd_est:.6f}"
                    )

        return run


def load_model_configs(path: Path) -> list[ModelConfig]:
    """Load model configurations from a JSON file.

    Expected format:
    {
        "models": [
            {
                "name": "Llama 3.2 (local)",
                "backend": "ollama",
                "model": "llama3.2"
            },
            ...
        ]
    }
    """
    with open(path) as f:
        data = json.load(f)

    models_data = data if isinstance(data, list) else data.get("models", [])
    return [ModelConfig.from_dict(m) for m in models_data]
