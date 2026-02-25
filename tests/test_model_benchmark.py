"""Tests for the model benchmark framework.

Validates scenario loading, metrics scoring, and report generation
without requiring any LLM backends.

Uses importlib spec loading to bypass arenamcp.__init__ which has
heavy system dependencies (sounddevice/PortAudio, watchdog, mcp).
"""

import json
import importlib
import importlib.util
import types
import tempfile
from pathlib import Path

import pytest

import sys

_SRC = str(Path(__file__).parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _load_module(dotted_name: str, file_path: str) -> types.ModuleType:
    """Load a module by file path, registering it in sys.modules.

    This bypasses the parent arenamcp.__init__ import chain.
    """
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


# Register a minimal stub for 'arenamcp' package so submodule imports work
# without triggering the real __init__ (which pulls in watchdog, mcp, sounddevice).
if "arenamcp" not in sys.modules:
    _stub = types.ModuleType("arenamcp")
    _stub.__path__ = [str(Path(__file__).parent.parent / "src" / "arenamcp")]
    _stub.__package__ = "arenamcp"
    sys.modules["arenamcp"] = _stub

# Register stub for arenamcp.model_benchmark subpackage
_bench_pkg_path = str(Path(__file__).parent.parent / "src" / "arenamcp" / "model_benchmark")
if "arenamcp.model_benchmark" not in sys.modules:
    _bench_stub = types.ModuleType("arenamcp.model_benchmark")
    _bench_stub.__path__ = [_bench_pkg_path]
    _bench_stub.__package__ = "arenamcp.model_benchmark"
    sys.modules["arenamcp.model_benchmark"] = _bench_stub

_base = Path(__file__).parent.parent / "src" / "arenamcp" / "model_benchmark"
_scenarios_mod = _load_module(
    "arenamcp.model_benchmark.scenarios", str(_base / "scenarios.py")
)
_metrics_mod = _load_module(
    "arenamcp.model_benchmark.metrics", str(_base / "metrics.py")
)
_runner_mod = _load_module(
    "arenamcp.model_benchmark.runner", str(_base / "runner.py")
)
_report_mod = _load_module(
    "arenamcp.model_benchmark.report", str(_base / "report.py")
)

EvalScenario = _scenarios_mod.EvalScenario
load_scenarios = _scenarios_mod.load_scenarios
save_scenarios = _scenarios_mod.save_scenarios
score_response = _metrics_mod.score_response
QualityScore = _metrics_mod.QualityScore
BenchmarkRun = _runner_mod.BenchmarkRun
ModelResult = _runner_mod.ModelResult
ModelConfig = _runner_mod.ModelConfig
_estimate_tokens = _runner_mod._estimate_tokens
_lookup_cost = _runner_mod._lookup_cost
load_model_configs = _runner_mod.load_model_configs
generate_report = _report_mod.generate_report


# --- Fixtures ---

def _make_scenario(**overrides) -> EvalScenario:
    """Create a minimal test scenario with overrides."""
    defaults = {
        "id": "test_01",
        "name": "Test scenario",
        "category": "combat",
        "difficulty": "basic",
        "game_state": {"turn": {"turn_number": 1}},
        "trigger": "new_turn",
        "correct_actions": ["attack with all"],
    }
    defaults.update(overrides)
    return EvalScenario(**defaults)


# --- Scenario Tests ---


class TestScenarioLoading:
    def test_load_builtin_scenarios(self):
        """Built-in scenarios file should load successfully."""
        scenarios = load_scenarios()
        assert len(scenarios) > 0
        for s in scenarios:
            assert s.id
            assert s.name
            assert s.category
            assert s.correct_actions

    def test_load_with_category_filter(self):
        scenarios = load_scenarios(categories=["combat"])
        assert all(s.category == "combat" for s in scenarios)
        assert len(scenarios) > 0

    def test_load_with_difficulty_filter(self):
        scenarios = load_scenarios(difficulties=["basic"])
        assert all(s.difficulty == "basic" for s in scenarios)

    def test_load_with_tag_filter(self):
        scenarios = load_scenarios(tags=["lethal"])
        assert all(any("lethal" in t for t in s.tags) for s in scenarios)

    def test_load_missing_file_returns_empty(self):
        scenarios = load_scenarios(path=Path("/nonexistent/file.json"))
        assert scenarios == []

    def test_save_and_reload(self, tmp_path):
        original = [_make_scenario(id="save_test_01")]
        path = tmp_path / "test_scenarios.json"
        save_scenarios(original, path)

        loaded = load_scenarios(path=path)
        assert len(loaded) == 1
        assert loaded[0].id == "save_test_01"

    def test_scenario_roundtrip_dict(self):
        s = _make_scenario(
            tags=["tag1", "tag2"],
            must_mention=["card_name"],
            weight=2.0,
        )
        d = s.to_dict()
        restored = EvalScenario.from_dict(d)
        assert restored.id == s.id
        assert restored.tags == s.tags
        assert restored.weight == s.weight


# --- Metrics Tests ---


class TestMetricsScoring:
    def test_perfect_response(self):
        """Response matching correct action gets full action score."""
        scenario = _make_scenario(
            correct_actions=["attack with all"],
            incorrect_actions=["pass priority"],
            must_mention=["Goblin Guide"],
        )
        response = "Attack with all creatures. Goblin Guide leads the charge."
        score = score_response(scenario, response)

        assert score.action_correctness == 1.0
        assert score.rule_compliance == 1.0
        assert score.completeness == 1.0
        assert score.composite > 0.8

    def test_incorrect_action_penalty(self):
        """Response with incorrect action gets rule compliance penalty."""
        scenario = _make_scenario(
            correct_actions=["attack with all"],
            incorrect_actions=["pass priority"],
        )
        response = "You should pass priority here and wait."
        score = score_response(scenario, response)

        assert score.rule_compliance < 1.0
        assert score.action_correctness == 0.0

    def test_empty_response(self):
        scenario = _make_scenario()
        score = score_response(scenario, "")
        assert score.composite == 0.0
        assert "error" in score.details

    def test_hallucination_penalty(self):
        """Response mentioning forbidden content gets hallucination penalty."""
        scenario = _make_scenario(
            correct_actions=["Cast Llanowar Elves"],
            must_not_mention=["Craterhoof Behemoth"],
        )
        response = "Cast Llanowar Elves to ramp into Craterhoof Behemoth."
        score = score_response(scenario, response)

        assert score.hallucination_penalty > 0
        assert score.action_correctness == 1.0

    def test_partial_credit_card_name_match(self):
        """Response mentioning the card name but not exact action gets partial credit."""
        scenario = _make_scenario(
            correct_actions=["Cast Lightning Bolt targeting opponent"],
        )
        # Doesn't say "Cast Lightning Bolt targeting opponent" exactly
        # but mentions "Lightning Bolt" — partial credit
        response = "Lightning Bolt the face for 3 damage."
        score = score_response(scenario, response)

        assert score.action_correctness == 0.5

    def test_conciseness_ideal_range(self):
        """Response in 10-60 word range gets full conciseness score."""
        scenario = _make_scenario(correct_actions=["attack"])
        response = "Attack with all creatures. " * 3  # ~12 words
        score = score_response(scenario, response)
        assert score.conciseness == 1.0

    def test_conciseness_too_long(self):
        """Very long response gets conciseness penalty."""
        scenario = _make_scenario(correct_actions=["attack"])
        response = "Attack with creatures. " * 50  # ~150 words
        score = score_response(scenario, response)
        assert score.conciseness < 1.0

    def test_conciseness_too_short(self):
        """Very short response gets conciseness penalty."""
        scenario = _make_scenario(correct_actions=["attack"])
        response = "Attack."
        score = score_response(scenario, response)
        assert score.conciseness < 1.0

    def test_multiple_correct_actions_any_match(self):
        """Any matching correct action gives full credit."""
        scenario = _make_scenario(
            correct_actions=["attack with all", "go all in", "alpha strike"],
        )
        response = "Go all in with the attack."
        score = score_response(scenario, response)
        assert score.action_correctness == 1.0

    def test_completeness_partial(self):
        """Partial must_mention matches give proportional credit."""
        scenario = _make_scenario(
            correct_actions=["attack"],
            must_mention=["Goblin Guide", "Lightning Bolt", "Mountain"],
        )
        response = "Attack with Goblin Guide after playing Mountain."
        score = score_response(scenario, response)
        # 2 of 3 mentioned
        assert abs(score.completeness - 2 / 3) < 0.01

    def test_case_insensitive_matching(self):
        """Matching should be case-insensitive."""
        scenario = _make_scenario(
            correct_actions=["Cast Lightning Bolt"],
            incorrect_actions=["Pass Priority"],
        )
        response = "cast lightning bolt at the opponent's face"
        score = score_response(scenario, response)
        assert score.action_correctness == 1.0

    def test_custom_weights(self):
        """Custom weights should change composite calculation."""
        scenario = _make_scenario(correct_actions=["attack"])
        response = "Attack with all. "
        # Weight everything toward action_correctness
        weights = {
            "action_correctness": 1.0,
            "rule_compliance": 0.0,
            "completeness": 0.0,
            "hallucination_penalty": 0.0,
            "conciseness": 0.0,
        }
        score = score_response(scenario, response, weights=weights)
        assert score.composite == 1.0  # Full credit from action_correctness alone


# --- Runner Utility Tests ---


class TestRunnerUtils:
    def test_estimate_tokens(self):
        assert _estimate_tokens("hello world") > 0
        assert _estimate_tokens("a" * 100) == 25

    def test_lookup_cost_exact(self):
        cost = _lookup_cost("gpt-4o")
        assert cost["input"] > 0
        assert cost["output"] > 0

    def test_lookup_cost_prefix(self):
        cost = _lookup_cost("gpt-4o-2024-11-20")
        assert cost["input"] > 0

    def test_lookup_cost_local(self):
        cost = _lookup_cost("llama3.2")
        assert cost["input"] == 0.0
        assert cost["output"] == 0.0

    def test_lookup_cost_unknown(self):
        cost = _lookup_cost("totally-unknown-model")
        assert cost["input"] == 0.0
        assert cost["output"] == 0.0

    def test_model_config_roundtrip(self):
        mc = ModelConfig("Test", "ollama", "llama3.2", {"input": 0, "output": 0})
        d = mc.to_dict()
        restored = ModelConfig.from_dict(d)
        assert restored.name == mc.name
        assert restored.model == mc.model

    def test_load_model_configs(self, tmp_path):
        config = {
            "models": [
                {"name": "Test Model", "backend": "ollama", "model": "llama3.2"},
            ]
        }
        path = tmp_path / "models.json"
        with open(path, "w") as f:
            json.dump(config, f)

        models = load_model_configs(path)
        assert len(models) == 1
        assert models[0].name == "Test Model"


# --- Report Tests ---


class TestReportGeneration:
    def _make_run(self) -> tuple[BenchmarkRun, list[EvalScenario]]:
        """Create a fake benchmark run for report testing."""
        scenarios = [
            _make_scenario(id="s1", name="Scenario 1", category="combat", difficulty="basic"),
            _make_scenario(id="s2", name="Scenario 2", category="sequencing", difficulty="intermediate"),
        ]

        results = []
        for model, backend in [("llama3.2", "ollama"), ("gpt-4o", "api")]:
            for s in scenarios:
                q = QualityScore(
                    action_correctness=0.8 if model == "gpt-4o" else 0.5,
                    rule_compliance=1.0,
                    completeness=0.7,
                    hallucination_penalty=0.0,
                    conciseness=0.9,
                    composite=0.75 if model == "gpt-4o" else 0.55,
                )
                results.append(ModelResult(
                    scenario_id=s.id,
                    model=model,
                    backend=backend,
                    response="Attack with creatures.",
                    quality=q,
                    latency_s=1.5 if model == "gpt-4o" else 0.5,
                    input_tokens_est=500,
                    output_tokens_est=20,
                    cost_usd_est=0.001 if model == "gpt-4o" else 0.0,
                ))

        run = BenchmarkRun(
            run_id="test_run_001",
            timestamp="2026-02-24T00:00:00+00:00",
            models=["llama3.2", "gpt-4o"],
            scenario_count=2,
            results=results,
        )
        return run, scenarios

    def test_report_generation(self):
        run, scenarios = self._make_run()
        report = generate_report(run, scenarios=scenarios)

        assert "MODEL PERFORMANCE BENCHMARK REPORT" in report
        assert "OVERALL RANKING" in report
        assert "gpt-4o" in report
        assert "llama3.2" in report
        assert "COST-QUALITY ANALYSIS" in report

    def test_report_with_file_output(self, tmp_path):
        run, scenarios = self._make_run()
        output_path = tmp_path / "report.txt"
        report = generate_report(run, scenarios=scenarios, output_path=output_path)

        assert output_path.exists()
        assert output_path.with_suffix(".json").exists()

        # Check JSON summary is valid
        with open(output_path.with_suffix(".json")) as f:
            summary = json.load(f)
        assert summary["run_id"] == "test_run_001"
        assert len(summary["models"]) == 2

    def test_report_cloud_vs_local(self):
        """Report should identify cloud vs local models."""
        run, scenarios = self._make_run()
        report = generate_report(run, scenarios=scenarios)

        assert "Cloud Models" in report
        assert "Local Models" in report
        assert "Cloud vs Local Quality Gap" in report

    def test_benchmark_run_save(self, tmp_path):
        run, _ = self._make_run()
        path = tmp_path / "results.json"
        run.save(path)

        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert data["run_id"] == "test_run_001"
        assert len(data["results"]) == 4
