"""One-command eval automation.

Wraps the multi-step eval pipelines into a single command per target. Useful
for prompt-iteration loops (change the system prompt -> ``python -m
tools.eval.auto general --upload`` -> check the admin Eval tab) and for
scheduling a periodic regression run via Windows Task Scheduler / cron.

Subcommands:

  general
      Replay an existing prompts.jsonl through one or more backends, judge,
      report --json, and (optionally) upload to the proxy admin endpoint.

  mulligan
      Same shape, but for the 17lands mulligan target. If the per-set CSV
      isn't cached locally, pulls it first.

Examples:

    # Quick general eval against the seed corpus, upload:
    python -m tools.eval.auto general \\
        --prompts tools/eval/data/seed_prompts.jsonl \\
        --backends online:deepseek-v4-flash \\
        --upload

    # 17lands EOE mulligan eval, full pipeline, upload:
    python -m tools.eval.auto mulligan \\
        --set EOE --n 200 \\
        --backends online:deepseek-v4-flash \\
        --upload

Env vars:
    MTGACOACH_LICENSE_KEY  — used by online: backends
    MTGACOACH_ADMIN_KEY    — required for --upload
    MTGACOACH_PROXY_URL    — defaults to https://api.mtgacoach.com
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run(cmd: list[str], *, env: dict | None = None) -> int:
    """Run a child process, streaming output. Returns its exit code."""
    pretty = " ".join(shlex.quote(str(c)) for c in cmd)
    print(f"\n$ {pretty}", flush=True)
    return subprocess.call(cmd, env=env or os.environ.copy(), cwd=REPO)


def _require_zero(rc: int, step: str) -> None:
    if rc != 0:
        print(f"\nstep '{step}' failed (exit {rc}); aborting", file=sys.stderr)
        sys.exit(rc)


def _data_dir() -> Path:
    d = REPO / "tools" / "eval" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _admin_upload(json_path: Path) -> None:
    admin_key = os.environ.get("MTGACOACH_ADMIN_KEY", "")
    if not admin_key:
        print("MTGACOACH_ADMIN_KEY not set; skipping --upload", file=sys.stderr)
        sys.exit(2)
    proxy = os.environ.get("MTGACOACH_PROXY_URL", "https://api.mtgacoach.com")
    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.upload_results",
            "--json",
            str(json_path),
            "--proxy-url",
            proxy,
            "--admin-key",
            admin_key,
        ]
    )
    _require_zero(rc, "upload")


# -- general subcommand ------------------------------------------------------


def cmd_general(args: argparse.Namespace) -> None:
    data = _data_dir()
    prompts = Path(args.prompts).resolve()
    if not prompts.exists():
        print(f"prompts file not found: {prompts}", file=sys.stderr)
        sys.exit(2)
    responses = data / args.responses_name
    scores = data / args.scores_name
    summary = data / args.json_name

    backend_args: list[str] = []
    for b in args.backends:
        backend_args.extend(["--backend", b])

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.run",
            "--prompts",
            str(prompts),
            "--responses",
            str(responses),
            *backend_args,
        ]
    )
    _require_zero(rc, "run")

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.judge",
            "--prompts",
            str(prompts),
            "--responses",
            str(responses),
            "--scores",
            str(scores),
            "--judge-backend",
            args.judge_backend,
        ]
    )
    _require_zero(rc, "judge")

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.report",
            "--responses",
            str(responses),
            "--scores",
            str(scores),
            "--json",
            str(summary),
        ]
    )
    _require_zero(rc, "report")

    print(f"\n[auto] general summary written to {summary}")
    if args.upload:
        _admin_upload(summary)


# -- mulligan subcommand -----------------------------------------------------


def cmd_mulligan(args: argparse.Namespace) -> None:
    data = _data_dir()

    csv_path = data / "17lands" / f"replay_data_public.{args.set_code}.{args.event}.csv.gz"
    if not csv_path.exists() or args.redownload:
        rc = _run(
            [
                sys.executable,
                "-m",
                "tools.eval.seventeenlands.download",
                "--set",
                args.set_code,
                "--event",
                args.event,
                *(["--force"] if args.redownload else []),
            ]
        )
        _require_zero(rc, "download")

    prompts = data / args.prompts_name
    if not prompts.exists() or args.rebuild_prompts:
        rc = _run(
            [
                sys.executable,
                "-m",
                "tools.eval.seventeenlands.build_mulligan_prompts",
                "--csv",
                str(csv_path),
                "--out",
                str(prompts),
                "--n",
                str(args.n_samples),
                "--min-rank",
                args.min_rank,
                "--min-bucket-n",
                str(args.min_bucket_n),
                "--seed",
                str(args.seed),
            ]
        )
        _require_zero(rc, "build_mulligan_prompts")
    else:
        print(f"[auto] reusing existing prompts: {prompts} (--rebuild-prompts to refresh)")

    responses = data / args.responses_name
    summary = data / args.json_name

    backend_args: list[str] = []
    for b in args.backends:
        backend_args.extend(["--backend", b])

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.run",
            "--prompts",
            str(prompts),
            "--responses",
            str(responses),
            *backend_args,
        ]
    )
    _require_zero(rc, "run")

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.seventeenlands.score_mulligan",
            "--prompts",
            str(prompts),
            "--responses",
            str(responses),
            "--json",
            str(summary),
            "--set",
            args.set_code,
        ]
    )
    _require_zero(rc, "score_mulligan")

    print(f"\n[auto] mulligan summary written to {summary}")
    if args.upload:
        _admin_upload(summary)


# -- turn-action subcommand --------------------------------------------------


def cmd_turn_action(args: argparse.Namespace) -> None:
    data = _data_dir()

    csv_path = data / "17lands" / f"replay_data_public.{args.set_code}.{args.event}.csv.gz"
    if not csv_path.exists() or args.redownload:
        rc = _run(
            [
                sys.executable,
                "-m",
                "tools.eval.seventeenlands.download",
                "--set",
                args.set_code,
                "--event",
                args.event,
                *(["--force"] if args.redownload else []),
            ]
        )
        _require_zero(rc, "download")

    prompts = data / args.prompts_name
    if not prompts.exists() or args.rebuild_prompts:
        rc = _run(
            [
                sys.executable,
                "-m",
                "tools.eval.seventeenlands.build_turn_action_prompts",
                "--csv",
                str(csv_path),
                "--out",
                str(prompts),
                "--n",
                str(args.n_samples),
                "--min-rank",
                args.min_rank,
                "--seed",
                str(args.seed),
            ]
        )
        _require_zero(rc, "build_turn_action_prompts")
    else:
        print(f"[auto] reusing existing prompts: {prompts} (--rebuild-prompts to refresh)")

    responses = data / args.responses_name
    summary = data / args.json_name

    backend_args: list[str] = []
    for b in args.backends:
        backend_args.extend(["--backend", b])

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.run",
            "--prompts",
            str(prompts),
            "--responses",
            str(responses),
            *backend_args,
        ]
    )
    _require_zero(rc, "run")

    rc = _run(
        [
            sys.executable,
            "-m",
            "tools.eval.seventeenlands.score_turn_actions",
            "--prompts",
            str(prompts),
            "--responses",
            str(responses),
            "--json",
            str(summary),
            "--set",
            args.set_code,
        ]
    )
    _require_zero(rc, "score_turn_actions")

    print(f"\n[auto] turn-action summary written to {summary}")
    if args.upload:
        _admin_upload(summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_g = sub.add_parser("general", help="run/judge/report the general eval")
    p_g.add_argument("--prompts", required=True, help="Prompts JSONL (e.g. seed corpus or captured)")
    p_g.add_argument(
        "--backends",
        nargs="+",
        required=True,
        help="Backend specs (online:deepseek-v4-flash, ollama:qwen2.5:14b, ...)",
    )
    p_g.add_argument("--judge-backend", default="online:deepseek-v4-flash")
    p_g.add_argument("--responses-name", default="responses.jsonl")
    p_g.add_argument("--scores-name", default="scores.jsonl")
    p_g.add_argument("--json-name", default="general_summary.json")
    p_g.add_argument("--upload", action="store_true")
    p_g.set_defaults(func=cmd_general)

    p_m = sub.add_parser("mulligan", help="full 17lands mulligan eval pipeline")
    p_m.add_argument("--set", required=True, dest="set_code", help="Arena set code (EOE, OTJ, ...)")
    p_m.add_argument("--event", default="PremierDraft")
    p_m.add_argument("--backends", nargs="+", required=True)
    p_m.add_argument("--n", dest="n_samples", type=int, default=200)
    p_m.add_argument("--min-rank", default="diamond")
    p_m.add_argument("--min-bucket-n", type=int, default=20)
    p_m.add_argument("--seed", type=int, default=42)
    p_m.add_argument("--prompts-name", default="mulligan_prompts.jsonl")
    p_m.add_argument("--responses-name", default="mulligan_responses.jsonl")
    p_m.add_argument("--json-name", default="mulligan_summary.json")
    p_m.add_argument("--rebuild-prompts", action="store_true")
    p_m.add_argument("--redownload", action="store_true")
    p_m.add_argument("--upload", action="store_true")
    p_m.set_defaults(func=cmd_mulligan)

    p_t = sub.add_parser("turn-action", help="full 17lands turn-action eval pipeline")
    p_t.add_argument("--set", required=True, dest="set_code")
    p_t.add_argument("--event", default="PremierDraft")
    p_t.add_argument("--backends", nargs="+", required=True)
    p_t.add_argument("--n", dest="n_samples", type=int, default=200)
    p_t.add_argument("--min-rank", default="diamond")
    p_t.add_argument("--seed", type=int, default=42)
    p_t.add_argument("--prompts-name", default="turn_action_prompts.jsonl")
    p_t.add_argument("--responses-name", default="turn_action_responses.jsonl")
    p_t.add_argument("--json-name", default="turn_action_summary.json")
    p_t.add_argument("--rebuild-prompts", action="store_true")
    p_t.add_argument("--redownload", action="store_true")
    p_t.add_argument("--upload", action="store_true")
    p_t.set_defaults(func=cmd_turn_action)

    args = parser.parse_args()
    started = time.time()
    args.func(args)
    elapsed = time.time() - started
    print(f"\n[auto] {args.cmd} done in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
