#!/usr/bin/env python3
"""Model Performance Benchmark CLI.

Compare quality, latency, and cost across local and cloud LLM models
for MTG coaching advice.

Usage:
    # Run with default scenarios against Ollama models
    python benchmark_models.py --backend ollama --models llama3.2,mistral,phi3

    # Run with a models config file
    python benchmark_models.py --config models.json

    # Run specific scenario categories
    python benchmark_models.py --config models.json --categories combat,sequencing

    # Run only basic difficulty scenarios
    python benchmark_models.py --config models.json --difficulty basic

    # List available scenarios
    python benchmark_models.py --list-scenarios

    # Use custom scenarios file
    python benchmark_models.py --config models.json --scenarios my_scenarios.json

    # Save results to a specific directory
    python benchmark_models.py --config models.json --output-dir ./results
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from arenamcp.model_benchmark.scenarios import load_scenarios, SCENARIOS_DIR
from arenamcp.model_benchmark.runner import BenchmarkRunner, ModelConfig, load_model_configs
from arenamcp.model_benchmark.report import generate_report


def list_scenarios(args: argparse.Namespace) -> int:
    """List all available evaluation scenarios."""
    scenarios_path = Path(args.scenarios) if args.scenarios else None
    scenarios = load_scenarios(path=scenarios_path)

    if not scenarios:
        print("No scenarios found.")
        return 1

    print(f"\nAvailable Scenarios ({len(scenarios)} total)")
    print("=" * 70)

    # Group by category
    by_category: dict[str, list] = {}
    for s in scenarios:
        by_category.setdefault(s.category, []).append(s)

    for category, items in sorted(by_category.items()):
        print(f"\n  [{category.upper()}] ({len(items)} scenarios)")
        for s in items:
            tags = ", ".join(s.tags) if s.tags else ""
            print(f"    {s.id:<35} {s.difficulty:<14} {s.name}")
            if tags:
                print(f"    {'':35} tags: {tags}")

    return 0


def run_benchmark(args: argparse.Namespace) -> int:
    """Execute the benchmark."""
    # Load model configurations
    models: list[ModelConfig] = []

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            print(f"Error: Config file not found: {config_path}")
            return 1
        models = load_model_configs(config_path)
        print(f"Loaded {len(models)} model configurations from {config_path}")

    elif args.backend and args.models:
        # Quick mode: --backend ollama --models llama3.2,mistral
        for model_name in args.models.split(","):
            model_name = model_name.strip()
            models.append(ModelConfig(
                name=f"{model_name} ({args.backend})",
                backend=args.backend,
                model=model_name,
            ))

    elif args.backend:
        # Single backend, default model
        models.append(ModelConfig(
            name=f"default ({args.backend})",
            backend=args.backend,
            model="",
        ))

    if not models:
        print("Error: No models specified. Use --config, or --backend with --models.")
        print("Example: python benchmark_models.py --backend ollama --models llama3.2,mistral")
        return 1

    # Load scenarios
    scenarios_path = Path(args.scenarios) if args.scenarios else None
    categories = args.categories.split(",") if args.categories else None
    difficulties = args.difficulty.split(",") if args.difficulty else None
    tags = args.tags.split(",") if args.tags else None

    scenarios = load_scenarios(
        path=scenarios_path,
        categories=categories,
        difficulties=difficulties,
        tags=tags,
    )

    if not scenarios:
        print("Error: No scenarios matched the filters.")
        return 1

    print(f"\nBenchmark Configuration:")
    print(f"  Models:    {len(models)}")
    for m in models:
        print(f"    - {m.name} ({m.backend}/{m.model})")
    print(f"  Scenarios: {len(scenarios)}")
    if categories:
        print(f"  Categories: {', '.join(categories)}")
    if difficulties:
        print(f"  Difficulties: {', '.join(difficulties)}")
    print(f"  Total evaluations: {len(models) * len(scenarios)}")
    print()

    # Progress callback
    def progress(status: str) -> None:
        if status:
            print(f"  {status}")

    # Run benchmark
    runner = BenchmarkRunner(
        models=models,
        style=args.style,
        retries=args.retries,
        progress_callback=progress if not args.quiet else None,
    )

    print("Starting benchmark...")
    print("=" * 60)
    run = runner.execute(scenarios, run_id=args.run_id)
    print("=" * 60)
    print("Benchmark complete.\n")

    # Generate report
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / f"report_{run.run_id}.txt"
    report = generate_report(run, scenarios=scenarios, output_path=report_path)

    # Save raw results
    results_path = output_dir / f"results_{run.run_id}.json"
    run.save(results_path)

    # Print report to stdout
    print(report)

    print(f"\nFiles saved:")
    print(f"  Report:  {report_path}")
    print(f"  Results: {results_path}")
    print(f"  Summary: {report_path.with_suffix('.json')}")

    return 0


def generate_sample_config(args: argparse.Namespace) -> int:
    """Generate a sample models.json configuration file."""
    sample = {
        "models": [
            {
                "name": "Llama 3.2 (Ollama local)",
                "backend": "ollama",
                "model": "llama3.2"
            },
            {
                "name": "Mistral (Ollama local)",
                "backend": "ollama",
                "model": "mistral"
            },
            {
                "name": "Phi-4 (Ollama local)",
                "backend": "ollama",
                "model": "phi4"
            },
            {
                "name": "Qwen 2.5 (Ollama local)",
                "backend": "ollama",
                "model": "qwen2.5"
            },
            {
                "name": "GPT-4o (API)",
                "backend": "api",
                "model": "gpt-4o"
            },
            {
                "name": "GPT-4o-mini (API)",
                "backend": "api",
                "model": "gpt-4o-mini"
            },
            {
                "name": "Claude Sonnet 4 (Proxy)",
                "backend": "proxy",
                "model": "claude-sonnet-4"
            }
        ]
    }

    output = Path(args.output) if args.output else Path("models_benchmark.json")
    with open(output, "w") as f:
        json.dump(sample, f, indent=2)
    print(f"Sample config written to: {output}")
    print("Edit this file to add/remove models, then run:")
    print(f"  python benchmark_models.py --config {output}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark LLM models for MTG coaching quality, latency, and cost",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick benchmark of local Ollama models
  python benchmark_models.py --backend ollama --models llama3.2,mistral,phi4

  # Full benchmark with config file
  python benchmark_models.py --config models_benchmark.json

  # Only combat scenarios, basic difficulty
  python benchmark_models.py --backend ollama --models llama3.2 --categories combat --difficulty basic

  # Generate sample config file
  python benchmark_models.py --generate-config

  # List all available test scenarios
  python benchmark_models.py --list-scenarios
        """,
    )

    # Mode flags
    parser.add_argument("--list-scenarios", action="store_true",
                        help="List available evaluation scenarios and exit")
    parser.add_argument("--generate-config", action="store_true",
                        help="Generate a sample models.json config file")

    # Model selection
    parser.add_argument("--config", type=str,
                        help="Path to models JSON config file")
    parser.add_argument("--backend", type=str,
                        help="Backend type (ollama, api, proxy, claude-code, gemini-cli)")
    parser.add_argument("--models", type=str,
                        help="Comma-separated model names (used with --backend)")

    # Scenario filtering
    parser.add_argument("--scenarios", type=str,
                        help="Path to custom scenarios JSON file")
    parser.add_argument("--categories", type=str,
                        help="Comma-separated category filter (combat,sequencing,mana,timing,decision)")
    parser.add_argument("--difficulty", type=str,
                        help="Comma-separated difficulty filter (basic,intermediate,advanced)")
    parser.add_argument("--tags", type=str,
                        help="Comma-separated tag filter")

    # Execution options
    parser.add_argument("--style", default="concise",
                        choices=["concise", "normal"],
                        help="Coaching style (default: concise)")
    parser.add_argument("--retries", type=int, default=1,
                        help="Retry count on failure (default: 1)")
    parser.add_argument("--run-id", type=str, default=None,
                        help="Custom run identifier")

    # Output
    parser.add_argument("--output-dir", type=str, default="benchmark_results",
                        help="Output directory for results (default: benchmark_results)")
    parser.add_argument("--output", type=str,
                        help="Output file path (for --generate-config)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress progress output")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    # Dispatch to subcommand
    if args.list_scenarios:
        return list_scenarios(args)
    elif args.generate_config:
        return generate_sample_config(args)
    else:
        return run_benchmark(args)


if __name__ == "__main__":
    sys.exit(main())
