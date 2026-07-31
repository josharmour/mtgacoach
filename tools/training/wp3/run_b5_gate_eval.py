#!/usr/bin/env python3
"""B5 gate evaluation runner — base vs LoRA adapter on the strategic gate.

Pre-registered gate for the wp3_gemma12b_b5_smoke LoRA
(google/gemma-4-12B-it, WP-3 priority decisions):

  L1  beat the base model on the strategic gate: candidate overall
      accuracy STRICTLY > 0.4727 (the 47.27% base-arm number from
      tools/training/data/gate_report_strategic_v1.json, n=550, corpus
      tools/training/data/gate_strategic_decisions_test.jsonl).
      NOTE recorded honestly: that 47.27 was measured with
      google/gemma-4-31B-it as the base arm. Tonight's baseline arm is
      google/gemma-4-12B-it (the LoRA's actual base), so the report
      carries BOTH comparisons: the pre-registered absolute floor and the
      same-base paired delta.
  L2  legality >= base: candidate legality_rate >= baseline legality_rate.
  L3  no combat-slice regression: candidate non-degenerate accuracy on
      the combat gate test corpus >= 0.393 (the 07-28 baseline arm's
      non_degenerate accuracy from
      gate_combat_report_combat_v1_gemma31b_lora.json, n=2,557). The
      combat gate corpus is CLEAN and current — rebuilt 2026-07-27
      (combat_gate_manifest.json: 96,131/96,131 distinct ids, leak check
      clean) and consumed 07-28; the earlier stale-corpus note in
      rl-training-status section 23.8 was superseded by 23.9. As with L1
      the floor arm was 31B-lora-era; the report also records the
      same-base 12B paired delta so neither number is quoted alone.
      --skip-combat marks the leg SKIPPED (loudly, never silently).

The full G1-G7 verdict from tools/training/gate_play_decisions.py is run
and reported alongside; it is a separate (stricter) verdict from the three
pre-registered legs above.

Pipeline (matches the proven pd-gate-lora flow, section 21/23.8):

  serve   vLLM (venv-serve) serves google/gemma-4-12B-it bf16 with
          --enable-lora --lora-modules b5=<adapter dir>. vLLM consumes the
          PEFT adapter directory DIRECTLY — no merge needed. The identical
          adapter_config shape (regex target_modules, r=8, from
          tools/training/train.py) was served this way for
          play_decisions_v2_gemma31b_lora.                    [GPU STEP]
  generate tools.eval.run replays the corpus through both arms
          (openai-compat|http://127.0.0.1:<port>/v1|{b5, base}).
  score   tools.training.gate_play_decisions gate  (CPU) +
          tools/training/wp3/score_tripwires.py for the 55 tripwire
          fixtures (#443) as an ADVISORY slice (not a pre-registered leg).
  verdict this script reads the gate report and prints/records L1-L3.

Checkpoint selection: --checkpoint-root may still be written by the live
trainer. The runner picks the LAST COMPLETE save: any of {root itself,
root/checkpoint-N} that contains adapter_config.json + a non-trivial
adapter_model.safetensors whose newest file is older than --min-age-s.
Among complete candidates the newest adapter_model.safetensors mtime wins
(the trainer writes checkpoints in order; a final top-level save is
written last). Fail closed if none qualify.

CPU-only preflight (safe while the GPU is busy):

    /home/joshu/venv-train/bin/python3 tools/training/wp3/run_b5_gate_eval.py --dry-run

Full run (needs the GPU — only after the training run frees it, ~18:35):

    /home/joshu/venv-train/bin/python3 tools/training/wp3/run_b5_gate_eval.py --gpu 0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

DEFAULT_CHECKPOINT_ROOT = REPO / "tools" / "training" / "checkpoints" / "wp3_gemma12b_b5_smoke"
DEFAULT_DATA_DIR = REPO / "tools" / "training" / "data"
DEFAULT_BASE_MODEL = "google/gemma-4-12B-it"
DEFAULT_SERVE_PYTHON = "/home/joshu/venv-serve/bin/python3"
DEFAULT_PORT = 8003
LORA_NAME = "b5"

# Pre-registered absolute floor (L1). Source: gate_report_strategic_v1.json,
# baseline arm openai-compat:gemma-4-31b-it-base, overall accuracy 0.4727
# (260/550) on gate_strategic_decisions_test.jsonl. Strictly greater-than.
PREREG_STRATEGIC_FLOOR = 0.4727

# Pre-registered combat floor (L3). Source:
# gate_combat_report_combat_v1_gemma31b_lora.json baseline.non_degenerate
# .accuracy = 0.393 (n=2,557 non-degenerate combat decisions). Candidate
# must be >= (no regression against the recorded slice baseline).
PREREG_COMBAT_FLOOR = 0.393

REQUIRED_ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors")
MIN_ADAPTER_BYTES = 1_000_000  # a real r=8 12B adapter is tens of MB; refuse stubs


def die(msg: str, code: int = 2) -> None:
    print(f"FAIL-CLOSED: {msg}", file=sys.stderr)
    sys.exit(code)


def log(msg: str) -> None:
    print(f"[b5-gate] {msg}", flush=True)


# --------------------------------------------------------------------------
# Checkpoint resolution
# --------------------------------------------------------------------------


def _is_complete_adapter(d: Path, min_age_s: float) -> bool:
    for name in REQUIRED_ADAPTER_FILES:
        if not (d / name).is_file():
            return False
    st = (d / "adapter_model.safetensors").stat()
    if st.st_size < MIN_ADAPTER_BYTES:
        return False
    newest = max((d / name).stat().st_mtime for name in REQUIRED_ADAPTER_FILES)
    if time.time() - newest < min_age_s:
        return False  # possibly still being written by the live trainer
    return True


def resolve_adapter(root: Path, explicit: Path | None, min_age_s: float, base_model: str) -> Path:
    if explicit is not None:
        if not _is_complete_adapter(explicit, min_age_s=0):
            die(
                f"--adapter {explicit} is not a complete PEFT adapter dir "
                f"(needs {REQUIRED_ADAPTER_FILES}, safetensors >= {MIN_ADAPTER_BYTES} B)"
            )
        chosen = explicit
    else:
        if not root.is_dir():
            die(f"checkpoint root does not exist: {root}")
        candidates = [root] + sorted(
            (p for p in root.glob("checkpoint-*") if p.is_dir()),
            key=lambda p: int(p.name.split("-")[-1]) if p.name.split("-")[-1].isdigit() else -1,
        )
        complete = [c for c in candidates if _is_complete_adapter(c, min_age_s)]
        if not complete:
            die(
                f"no COMPLETE adapter found under {root} "
                f"(complete = {REQUIRED_ADAPTER_FILES} present, safetensors >= "
                f"{MIN_ADAPTER_BYTES} B, untouched for {min_age_s:.0f}s). "
                "The trainer may still be mid-write — retry later or pass --adapter."
            )
        chosen = max(complete, key=lambda c: (c / "adapter_model.safetensors").stat().st_mtime)
        skipped = [str(c) for c in candidates if c not in complete]
        if skipped:
            log(f"skipped incomplete/too-fresh candidates: {skipped}")

    cfg = json.loads((chosen / "adapter_config.json").read_text(encoding="utf-8"))
    got_base = cfg.get("base_model_name_or_path", "")
    if got_base != base_model:
        die(f"adapter {chosen} was trained on {got_base!r}, expected {base_model!r}")
    if cfg.get("peft_type") != "LORA":
        die(f"adapter {chosen} peft_type={cfg.get('peft_type')!r}, expected LORA")
    log(f"resolved adapter: {chosen} (r={cfg.get('r')}, alpha={cfg.get('lora_alpha')})")
    return chosen


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Preflight (CPU-only)
# --------------------------------------------------------------------------


def check_corpora(
    corpus: Path,
    permuted: Path,
    perm_suffix: str = "#perm",
    require_menu_size: bool = True,
) -> int:
    def load_ids(p: Path) -> list[str]:
        if not p.is_file():
            die(f"gate corpus missing: {p}")
        ids = []
        with open(p, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as e:
                    die(f"malformed corpus line {p}:{i}: {e}")
                for key in ("id", "system", "user", "meta"):
                    if key not in rec:
                        die(f"corpus record {p}:{i} missing key {key!r}")
                if require_menu_size and "menu_size" not in rec["meta"]:
                    die(f"corpus record {p}:{i} meta missing menu_size")
                ids.append(str(rec["id"]))
        return ids

    ids_a, ids_b = load_ids(corpus), load_ids(permuted)
    if len(ids_a) != len(set(ids_a)):
        die(f"duplicate ids in {corpus}")
    # Permuted-twin ids carry a suffix by design (prevents response files
    # from cross-contaminating between the identity and permuted arms).
    # Strategic corpora use '#perm'; the combat gate corpora use '-perm'.
    not_suffixed = [i for i in ids_b if not i.endswith(perm_suffix)]
    if not_suffixed:
        die(f"{len(not_suffixed)} permuted ids lack the {perm_suffix!r} suffix in {permuted.name}")
    if set(ids_a) != {i[: -len(perm_suffix)] for i in ids_b}:
        die(f"id sets differ between {corpus.name} and {permuted.name} (modulo '#perm')")
    log(f"corpora OK: {len(ids_a)} decisions, identity/permuted id sets equal (modulo '#perm')")
    return len(ids_a)


def check_cli_importable(python: str, module: str, *help_args: str) -> None:
    r = subprocess.run(
        [python, "-m", module, *help_args, "--help"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode != 0:
        die(f"`{python} -m {module} --help` failed:\n{r.stderr[-2000:]}")
    log(f"CLI import OK: {module}")


def check_port_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            die(
                f"port {port} is already in use — is another vLLM running? "
                "Use --skip-serve to target it, or pick --port."
            )


def regen_tripwires(python: str, out: Path) -> None:
    r = subprocess.run(
        [python, str(REPO / "tools" / "training" / "build_tripwires.py"), "--output", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        die(f"tripwire fixture generation failed:\n{r.stderr[-2000:]}")
    n = sum(1 for line in open(out, encoding="utf-8") if line.strip())
    if n != 55:
        die(f"regenerated tripwire fixture count {n} != 55 ({out})")
    log(f"tripwire fixtures OK: 55 at {out}")


# --------------------------------------------------------------------------
# Serve (THE GPU STEP)
# --------------------------------------------------------------------------


def serve_cmd(args: argparse.Namespace, adapter: Path) -> list[str]:
    cfg = json.loads((adapter / "adapter_config.json").read_text(encoding="utf-8"))
    max_lora_rank = max(8, int(cfg.get("r", 8)))
    cmd = [
        args.serve_python,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        args.base_model,
        "--served-model-name",
        args.base_model,
        "--port",
        str(args.port),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        str(args.max_model_len),
        "--gpu-memory-utilization",
        str(args.gpu_mem_util),
        "--enable-lora",
        "--max-lora-rank",
        str(max_lora_rank),
        "--lora-modules",
        f"{LORA_NAME}={adapter}",
    ]
    cmd += args.extra_serve_arg or []
    return cmd


def wait_for_server(port: int, timeout_s: float) -> None:
    url = f"http://127.0.0.1:{port}/v1/models"
    deadline = time.time() + timeout_s
    last_err = "never reached"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            names = {m.get("id") for m in data.get("data", [])}
            if LORA_NAME in names:
                log(f"server up, models: {sorted(names)}")
                return
            last_err = f"lora module {LORA_NAME!r} not in served models {sorted(names)}"
        except Exception as e:  # noqa: BLE001 - poll loop
            last_err = str(e)
        time.sleep(5)
    die(f"vLLM server not healthy after {timeout_s:.0f}s: {last_err}")


# --------------------------------------------------------------------------
# Generation + accounting
# --------------------------------------------------------------------------


def run_arm(args: argparse.Namespace, prompts: Path, responses: Path, model: str) -> None:
    backend = f"openai-compatible|http://127.0.0.1:{args.port}/v1|{model}"
    cmd = [
        sys.executable,
        "-m",
        "tools.eval.run",
        "--prompts",
        str(prompts),
        "--responses",
        str(responses),
        "--backend",
        backend,
        "--timeout-s",
        str(args.request_timeout_s),
        "--concurrency",
        str(args.concurrency),
    ]
    log("RUN " + shlex.join(cmd))
    r = subprocess.run(cmd, cwd=REPO)
    if r.returncode != 0:
        die(f"generation failed for {model} on {prompts.name} (exit {r.returncode})")


def assert_arm_complete(responses: Path, label: str, expected_n: int) -> None:
    ok = errors = 0
    with open(responses, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("backend") != label:
                continue
            if row.get("error"):
                errors += 1
            else:
                ok += 1
    if errors or ok < expected_n:
        die(
            f"arm {label!r} in {responses.name}: {ok} ok / {errors} errored, "
            f"expected {expected_n} clean rows. tools.eval.run is idempotent — "
            "fix the server condition and re-run; errored rows must be "
            "investigated, not scored (a server outage must not read as a "
            "wrong answer)."
        )
    log(f"arm complete: {label} {ok}/{expected_n}, 0 errors")


# --------------------------------------------------------------------------
# Generalization (seen vs unseen-deck slice) — only runs with --unseen-corpus
# --------------------------------------------------------------------------


def _bootstrap_generalization(
    seen_pairs: list[tuple[int, int]],
    unseen_pairs: list[tuple[int, int]],
    bootstraps: int,
    seed: int = 20260731,
) -> tuple[list[float], list[float]]:
    """95% bootstrap CIs on the seen-minus-unseen accuracy gap.

    Each pair is (candidate_correct, baseline_correct) for ONE prompt. The two
    slices are DIFFERENT prompt populations, so the raw gap resamples them
    independently; the baseline-adjusted gap — (cand-base on seen) minus
    (cand-base on unseen) — preserves the per-prompt candidate/baseline
    pairing inside each slice so shared prompt difficulty cancels. That is
    the paired estimator: read it, not the raw gap, when deciding whether the
    candidate generalizes worse than its own base model.
    """
    import random

    rng = random.Random(seed)
    ns, nu = len(seen_pairs), len(unseen_pairs)

    def acc(pairs: list[tuple[int, int]], idx: int) -> float:
        return sum(p[idx] for p in pairs) / len(pairs)

    raw: list[float] = []
    adj: list[float] = []
    for _ in range(bootstraps):
        s = [seen_pairs[rng.randrange(ns)] for _ in range(ns)]
        u = [unseen_pairs[rng.randrange(nu)] for _ in range(nu)]
        raw.append(acc(s, 0) - acc(u, 0))
        adj.append((acc(s, 0) - acc(s, 1)) - (acc(u, 0) - acc(u, 1)))

    def ci(values: list[float]) -> list[float]:
        v = sorted(values)
        lo = v[int(0.025 * len(v))]
        hi = v[min(len(v) - 1, int(0.975 * len(v)))]
        return [round(lo, 4), round(hi, 4)]

    return ci(raw), ci(adj)


def compute_generalization(
    seen_corpus_path: Path,
    seen_cand_resp: Path,
    seen_base_resp: Path,
    unseen_corpus_path: Path,
    unseen_cand_resp: Path,
    unseen_base_resp: Path,
    unseen_perm_corpus_path: Path,
    unseen_perm_resp: Path,
    cand_label: str,
    base_label: str,
    bootstraps: int,
) -> dict:
    """Score candidate+baseline on both slices; return the summary section.

    Scoring reuses gate_play_decisions.evaluate VERBATIM (same pick
    extraction, same gold-equivalence rules) so the seen and unseen numbers
    are produced by one scorer, not two implementations.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from tools.training import gate_play_decisions as G

    seen_corpus = list(G._read_jsonl(seen_corpus_path))
    unseen_corpus = list(G._read_jsonl(unseen_corpus_path))
    perm_corpus = list(G._read_jsonl(unseen_perm_corpus_path))

    cand_seen = G.evaluate(seen_corpus, seen_cand_resp, cand_label)
    base_seen = G.evaluate(seen_corpus, seen_base_resp, base_label)
    cand_unseen = G.evaluate(unseen_corpus, unseen_cand_resp, cand_label)
    base_unseen = G.evaluate(unseen_corpus, unseen_base_resp, base_label)
    cand_unseen_perm = G.evaluate(perm_corpus, unseen_perm_resp, cand_label)

    def pairs(cand: dict, base: dict) -> list[tuple[int, int]]:
        out = []
        for pid, c in cand.get("per_id", {}).items():
            b = base.get("per_id", {}).get(pid)
            if b is not None:
                out.append((1 if c["correct"] else 0, 1 if b["correct"] else 0))
        return out

    seen_pairs = pairs(cand_seen, base_seen)
    unseen_pairs = pairs(cand_unseen, base_unseen)
    if not seen_pairs or not unseen_pairs:
        die(
            f"generalization scoring produced empty pair sets "
            f"(seen={len(seen_pairs)}, unseen={len(unseen_pairs)}) — response "
            "files do not match the corpora; refusing to report a gap"
        )

    raw_ci, adj_ci = _bootstrap_generalization(seen_pairs, unseen_pairs, bootstraps)
    c_seen = cand_seen["overall"]["accuracy"]
    c_unseen = cand_unseen["overall"]["accuracy"]
    b_seen = base_seen["overall"]["accuracy"]
    b_unseen = base_unseen["overall"]["accuracy"]
    perm_acc = cand_unseen_perm["overall"]["accuracy"]

    return {
        "seen": {
            "corpus": str(seen_corpus_path),
            "n_paired": len(seen_pairs),
            "candidate_overall": c_seen,
            "candidate_95ci": cand_seen["overall"]["accuracy_95ci"],
            "baseline_overall": b_seen,
            "provenance": "real MTGA replay menus, human picks",
        },
        "unseen": {
            "corpus": str(unseen_corpus_path),
            "n_paired": len(unseen_pairs),
            "candidate_overall": c_unseen,
            "candidate_95ci": cand_unseen["overall"]["accuracy_95ci"],
            "baseline_overall": b_unseen,
            "candidate_permuted_overall": perm_acc,
            "permutation_gap_candidate": (
                round(c_unseen - perm_acc, 4) if c_unseen is not None and perm_acc is not None else None
            ),
            "provenance": "MageZero MCTS teacher picks on permanent-holdout decks "
            "(name-only prompts, same shape as WP-3 training records)",
        },
        "gap_candidate_seen_minus_unseen": (
            round(c_seen - c_unseen, 4) if c_seen is not None and c_unseen is not None else None
        ),
        "gap_bootstrap_95ci": raw_ci,
        "baseline_adjusted_gap": (
            round((c_seen - b_seen) - (c_unseen - b_unseen), 4)
            if None not in (c_seen, b_seen, c_unseen, b_unseen)
            else None
        ),
        "baseline_adjusted_gap_95ci": adj_ci,
        "bootstraps": bootstraps,
        "notes": [
            "reporting only — NOT a pre-registered leg; never affects the exit code",
            "the two slices differ in prompt provenance AND label provenance; "
            "read the baseline-adjusted gap (difficulty cancels via the shared "
            "base model), not the raw gap alone",
        ],
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--checkpoint-root", type=Path, default=DEFAULT_CHECKPOINT_ROOT)
    p.add_argument(
        "--adapter",
        type=Path,
        default=None,
        help="explicit PEFT adapter dir (skips last-checkpoint resolution)",
    )
    p.add_argument(
        "--min-age-s",
        type=float,
        default=90.0,
        help="a checkpoint written more recently than this is treated as in-flight",
    )
    p.add_argument("--base-model", default=DEFAULT_BASE_MODEL)
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument(
        "--corpus", type=Path, default=None, help="default: <data-dir>/gate_strategic_decisions_test.jsonl"
    )
    p.add_argument("--permuted-corpus", type=Path, default=None)
    p.add_argument("--run-id", default="b5_smoke")
    p.add_argument("--serve-python", default=DEFAULT_SERVE_PYTHON)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--gpu", default="0", help="CUDA_VISIBLE_DEVICES for the vLLM server")
    p.add_argument(
        "--max-model-len",
        type=int,
        default=49152,
        help="must cover the longest corpus prompt (~33k tokens observed) + output",
    )
    p.add_argument("--gpu-mem-util", type=float, default=0.85)
    p.add_argument("--extra-serve-arg", action="append", default=None)
    p.add_argument("--serve-timeout-s", type=float, default=900.0)
    p.add_argument("--request-timeout-s", type=float, default=180.0)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--bootstraps", type=int, default=10000)
    p.add_argument(
        "--skip-serve",
        action="store_true",
        help="assume a healthy server on --port already serves base + lora",
    )
    p.add_argument("--keep-server", action="store_true", help="do not stop the vLLM server on exit")
    p.add_argument("--skip-tripwires", action="store_true")
    p.add_argument(
        "--combat-corpus", type=Path, default=None, help="default: <data-dir>/combat_gate_test.jsonl"
    )
    p.add_argument(
        "--combat-permuted-corpus",
        type=Path,
        default=None,
        help="default: <data-dir>/combat_gate_test_permuted.jsonl",
    )
    p.add_argument(
        "--skip-combat",
        action="store_true",
        help="skip the L3 combat leg; the verdict records SKIPPED "
        "loudly (never silently) and the exit code ignores L3",
    )
    p.add_argument(
        "--unseen-corpus",
        type=Path,
        default=None,
        help="OPTIONAL unseen-deck gate slice (built by tools/training/wp3/"
        "build_unseen_gate_corpus.py from permanent-holdout-deck MageZero "
        "logs). When given, candidate AND baseline are also scored on it and "
        "the summary json gains a 'generalization' section: seen score, "
        "unseen score, gap, and paired-bootstrap CIs on the gap. Reporting "
        "only — never a pre-registered leg, never changes the exit code. "
        "Without this flag, behavior is identical to before the option existed.",
    )
    p.add_argument(
        "--unseen-permuted-corpus",
        type=Path,
        default=None,
        help="permuted twin of --unseen-corpus "
        "(default: <unseen-corpus stem>_permuted.jsonl next to it)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="CPU-only preflight: resolve adapter, validate corpora/CLIs, "
        "print the exact GPU command, then exit",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data = args.data_dir
    corpus = args.corpus or (data / "gate_strategic_decisions_test.jsonl")
    permuted = args.permuted_corpus or (data / "gate_strategic_decisions_test_permuted.jsonl")

    cand_label = f"openai-compat:{LORA_NAME}"
    base_label = f"openai-compat:{args.base_model}"

    combat_corpus = args.combat_corpus or (data / "combat_gate_test.jsonl")
    combat_permuted = args.combat_permuted_corpus or (data / "combat_gate_test_permuted.jsonl")

    unseen_corpus = args.unseen_corpus
    unseen_permuted = args.unseen_permuted_corpus
    if unseen_corpus is not None and unseen_permuted is None:
        unseen_permuted = unseen_corpus.with_name(unseen_corpus.stem + "_permuted" + unseen_corpus.suffix)

    out = {
        "unseen_cand": data / f"gate_{args.run_id}_unseen_candidate_test.jsonl",
        "unseen_cand_perm": data / f"gate_{args.run_id}_unseen_candidate_test_permuted.jsonl",
        "unseen_base": data / f"gate_{args.run_id}_unseen_baseline_test.jsonl",
        "combat_cand": data / f"gate_{args.run_id}_combat_candidate_test.jsonl",
        "combat_cand_perm": data / f"gate_{args.run_id}_combat_candidate_test_permuted.jsonl",
        "combat_base": data / f"gate_{args.run_id}_combat_baseline_test.jsonl",
        "combat_report": data / f"gate_report_{args.run_id}_combat.json",
        "cand_test": data / f"gate_{args.run_id}_candidate_test.jsonl",
        "cand_perm": data / f"gate_{args.run_id}_candidate_test_permuted.jsonl",
        "base_test": data / f"gate_{args.run_id}_baseline_test.jsonl",
        "tripwires_fixtures": data / "tripwire_fixtures_55.jsonl",
        "tripwires_resp": data / f"gate_{args.run_id}_tripwires.jsonl",
        "tripwires_cand_score": data / f"gate_{args.run_id}_tripwires_candidate_score.json",
        "tripwires_base_score": data / f"gate_{args.run_id}_tripwires_baseline_score.json",
        "report": data / f"gate_report_{args.run_id}.json",
        "summary": data / f"gate_{args.run_id}_summary.json",
        "serve_log": data / f"gate_{args.run_id}_vllm.log",
    }

    # ---- preflight (CPU-only) --------------------------------------------
    adapter = resolve_adapter(args.checkpoint_root, args.adapter, args.min_age_s, args.base_model)
    n = check_corpora(corpus, permuted)
    check_cli_importable(sys.executable, "tools.eval.run")
    check_cli_importable(sys.executable, "tools.training.gate_play_decisions")
    check_cli_importable(sys.executable, "tools.training.wp3.score_tripwires")
    combat_n = 0
    if not args.skip_combat:
        check_cli_importable(sys.executable, "tools.training.gate_combat_decisions")
        combat_n = check_corpora(combat_corpus, combat_permuted, perm_suffix="-perm", require_menu_size=False)
        log(f"combat gate corpus: {combat_n} records ({combat_corpus.name})")
    unseen_n = 0
    if unseen_corpus is not None:
        unseen_n = check_corpora(unseen_corpus, unseen_permuted)
        log(f"unseen-deck gate corpus: {unseen_n} records ({unseen_corpus.name})")
    r = subprocess.run(
        [args.serve_python, "-c", "import vllm; print(vllm.__version__)"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        die(f"vllm not importable from {args.serve_python}:\n{r.stderr[-1000:]}")
    log(f"vllm {r.stdout.strip()} available in {args.serve_python}")
    hub = Path.home() / ".cache" / "huggingface" / "hub"
    cache_name = "models--" + args.base_model.replace("/", "--")
    if not (hub / cache_name).exists():
        log(f"WARNING: {args.base_model} not in HF cache ({hub / cache_name}); vLLM will try to download it")
    if not args.skip_tripwires:
        regen_tripwires(sys.executable, out["tripwires_fixtures"])
    if not args.skip_serve and not args.dry_run:
        check_port_free(args.port)

    gpu_cmd = serve_cmd(args, adapter)
    log("=" * 72)
    log(f"GPU STEP (the ONLY step that touches the GPU; CUDA_VISIBLE_DEVICES={args.gpu}):")
    log(f"  CUDA_VISIBLE_DEVICES={args.gpu} " + shlex.join(gpu_cmd))
    log("=" * 72)

    if args.dry_run:
        log("dry-run: preflight PASSED; stopping before the GPU step.")
        return 0

    # ---- serve -----------------------------------------------------------
    server = None
    if not args.skip_serve:
        # The serve venv's bin dir must lead PATH: vLLM's JIT shells out to
        # `ninja`, which lives there — invoking the venv python directly does
        # not bring it in, and the engine dies with FileNotFoundError('ninja')
        # during memory profiling (hit live 2026-07-30).
        serve_bin = str(Path(args.serve_python).resolve().parent)
        env = dict(
            os.environ,
            CUDA_VISIBLE_DEVICES=str(args.gpu),
            PATH=serve_bin + os.pathsep + os.environ.get("PATH", ""),
        )
        log(f"starting vLLM (log: {out['serve_log']})")
        server = subprocess.Popen(
            gpu_cmd,
            cwd=REPO,
            env=env,
            stdout=open(out["serve_log"], "ab"),
            stderr=subprocess.STDOUT,
        )
    try:
        wait_for_server(args.port, args.serve_timeout_s)

        # ---- generate ----------------------------------------------------
        run_arm(args, corpus, out["cand_test"], LORA_NAME)
        run_arm(args, permuted, out["cand_perm"], LORA_NAME)
        run_arm(args, corpus, out["base_test"], args.base_model)
        assert_arm_complete(out["cand_test"], cand_label, n)
        assert_arm_complete(out["cand_perm"], cand_label, n)
        assert_arm_complete(out["base_test"], base_label, n)

        if not args.skip_combat:
            run_arm(args, combat_corpus, out["combat_cand"], LORA_NAME)
            run_arm(args, combat_permuted, out["combat_cand_perm"], LORA_NAME)
            run_arm(args, combat_corpus, out["combat_base"], args.base_model)
            assert_arm_complete(out["combat_cand"], cand_label, combat_n)
            assert_arm_complete(out["combat_cand_perm"], cand_label, combat_n)
            assert_arm_complete(out["combat_base"], base_label, combat_n)

        if unseen_corpus is not None:
            run_arm(args, unseen_corpus, out["unseen_cand"], LORA_NAME)
            run_arm(args, unseen_permuted, out["unseen_cand_perm"], LORA_NAME)
            run_arm(args, unseen_corpus, out["unseen_base"], args.base_model)
            assert_arm_complete(out["unseen_cand"], cand_label, unseen_n)
            assert_arm_complete(out["unseen_cand_perm"], cand_label, unseen_n)
            assert_arm_complete(out["unseen_base"], base_label, unseen_n)

        tripwire_scores = {}
        if not args.skip_tripwires:
            run_arm(args, out["tripwires_fixtures"], out["tripwires_resp"], LORA_NAME)
            run_arm(args, out["tripwires_fixtures"], out["tripwires_resp"], args.base_model)
            for label, score_path in (
                (cand_label, out["tripwires_cand_score"]),
                (base_label, out["tripwires_base_score"]),
            ):
                rr = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "tools.training.wp3.score_tripwires",
                        "--fixtures",
                        str(out["tripwires_fixtures"]),
                        "--responses",
                        str(out["tripwires_resp"]),
                        "--backend-label",
                        label,
                        "--out",
                        str(score_path),
                    ],
                    cwd=REPO,
                )
                if rr.returncode != 0:
                    die(f"tripwire scoring failed for {label}")
                s = json.loads(score_path.read_text(encoding="utf-8"))
                tripwire_scores[label] = {
                    "pass_rate": s["pass_rate"],
                    "passed": s["passed"],
                    "total": s["total_fixtures"],
                    "categories": {k: v for k, v in s["categories"].items()},
                }
    finally:
        if server is not None and not args.keep_server:
            log("stopping vLLM server")
            server.terminate()
            try:
                server.wait(timeout=60)
            except subprocess.TimeoutExpired:
                server.kill()

    # ---- full G1-G7 gate (CPU) ------------------------------------------
    gate_cmd = [
        sys.executable,
        "-m",
        "tools.training.gate_play_decisions",
        "gate",
        "--corpus",
        str(corpus),
        "--permuted-corpus",
        str(permuted),
        "--responses",
        str(out["cand_test"]),
        "--permuted-responses",
        str(out["cand_perm"]),
        "--baseline-responses",
        str(out["base_test"]),
        "--backend-label",
        cand_label,
        "--baseline-label",
        base_label,
        "--bootstraps",
        str(args.bootstraps),
        "--report",
        str(out["report"]),
    ]
    log("RUN " + shlex.join(gate_cmd))
    gate_rc = subprocess.run(gate_cmd, cwd=REPO).returncode
    if gate_rc not in (0, 2):
        die(f"gate_play_decisions crashed (exit {gate_rc})")
    if not out["report"].exists():
        die("gate report was not written")
    report = json.loads(out["report"].read_text(encoding="utf-8"))

    # ---- pre-registered verdict L1-L3 ------------------------------------
    cand_overall = report["candidate"]["overall"]["accuracy"]
    cand_legality = report["candidate"]["legality_rate"]
    base_overall = report["baseline"]["overall"]["accuracy"]
    base_legality = report["baseline"]["legality_rate"]

    l1 = cand_overall is not None and cand_overall > PREREG_STRATEGIC_FLOOR
    l2 = cand_legality is not None and base_legality is not None and cand_legality >= base_legality

    # ---- L3 combat slice (scored with the dedicated combat gate) ---------
    l3 = None
    l3_detail: dict = {"status": "SKIPPED", "reason": "--skip-combat passed"}
    if not args.skip_combat:
        combat_gate_cmd = [
            sys.executable,
            "-m",
            "tools.training.gate_combat_decisions",
            "gate",
            "--corpus",
            str(combat_corpus),
            "--permuted-corpus",
            str(combat_permuted),
            "--responses",
            str(out["combat_cand"]),
            "--permuted-responses",
            str(out["combat_cand_perm"]),
            "--baseline-responses",
            str(out["combat_base"]),
            "--backend-label",
            cand_label,
            "--baseline-label",
            base_label,
            "--report",
            str(out["combat_report"]),
        ]
        log("RUN " + shlex.join(combat_gate_cmd))
        combat_rc = subprocess.run(combat_gate_cmd, cwd=REPO).returncode
        if combat_rc not in (0, 2):
            die(f"gate_combat_decisions crashed (exit {combat_rc})")
        if not out["combat_report"].exists():
            die("combat gate report was not written")
        combat_report = json.loads(out["combat_report"].read_text(encoding="utf-8"))
        cand_nd = combat_report["candidate"]["non_degenerate"]["accuracy"]
        base_nd = combat_report["baseline"]["non_degenerate"]["accuracy"]
        l3 = cand_nd is not None and cand_nd >= PREREG_COMBAT_FLOOR
        l3_detail = {
            "status": "PASS" if l3 else "FAIL",
            "floor": PREREG_COMBAT_FLOOR,
            "floor_provenance": "gate_combat_report_combat_v1_gemma31b_lora.json "
            "baseline.non_degenerate.accuracy (n=2,557)",
            "candidate_non_degenerate": cand_nd,
            "baseline_12b_non_degenerate": base_nd,
            "paired_bootstrap_non_degenerate": combat_report.get("paired_bootstrap_non_degenerate"),
            "full_combat_gate_verdict": combat_report.get("verdict"),
            "combat_report": str(out["combat_report"]),
        }

    # ---- generalization: seen vs unseen-deck slice (CPU, optional) -------
    generalization = None
    if unseen_corpus is not None:
        generalization = compute_generalization(
            seen_corpus_path=corpus,
            seen_cand_resp=out["cand_test"],
            seen_base_resp=out["base_test"],
            unseen_corpus_path=unseen_corpus,
            unseen_cand_resp=out["unseen_cand"],
            unseen_base_resp=out["unseen_base"],
            unseen_perm_corpus_path=unseen_permuted,
            unseen_perm_resp=out["unseen_cand_perm"],
            cand_label=cand_label,
            base_label=base_label,
            bootstraps=args.bootstraps,
        )

    summary = {
        "run_id": args.run_id,
        "adapter": str(adapter),
        "adapter_sha256": sha256_file(adapter / "adapter_model.safetensors"),
        "base_model": args.base_model,
        "corpus": str(corpus),
        "corpus_n": n,
        "prereg": {
            "L1_strategic_gt_floor": {
                "floor": PREREG_STRATEGIC_FLOOR,
                "floor_provenance": "gate_report_strategic_v1.json baseline arm "
                "(openai-compat:gemma-4-31b-it-base, 260/550)",
                "candidate_overall": cand_overall,
                "pass": l1,
            },
            "L2_legality_ge_base": {
                "candidate_legality": cand_legality,
                "baseline_legality": base_legality,
                "pass": l2,
            },
            "L3_combat_no_regression": l3_detail,
        },
        "same_base_comparison": {
            "baseline_overall_gemma12b": base_overall,
            "paired_bootstrap": report.get("paired_bootstrap"),
            "paired_bootstrap_strategic": report.get("paired_bootstrap_strategic"),
        },
        "full_gate_verdict_g1_g7": report.get("verdict"),
        "full_gate_failures": report.get("failures"),
        "tripwires_advisory": tripwire_scores if not args.skip_tripwires else "skipped",
        "gate_report": str(out["report"]),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if generalization is not None:
        summary["generalization"] = generalization
    out["summary"].write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    log("=" * 72)
    log(
        f"pre-registered L1 (overall > {PREREG_STRATEGIC_FLOOR}): "
        f"candidate {cand_overall} -> {'PASS' if l1 else 'FAIL'}"
    )
    log(
        f"pre-registered L2 (legality >= base): "
        f"{cand_legality} vs {base_legality} -> {'PASS' if l2 else 'FAIL'}"
    )
    if l3 is None:
        log(
            "pre-registered L3 (combat slice): SKIPPED (--skip-combat) — "
            "exit code ignores L3; do not read this run as a 3-leg verdict"
        )
    else:
        log(
            f"pre-registered L3 (combat non-degenerate >= {PREREG_COMBAT_FLOOR}): "
            f"candidate {l3_detail['candidate_non_degenerate']} "
            f"(12B base: {l3_detail['baseline_12b_non_degenerate']}) "
            f"-> {'PASS' if l3 else 'FAIL'}"
        )
    log(
        f"same-base (12B) comparison: base overall {base_overall}, "
        f"paired delta CI {report.get('paired_bootstrap')}"
    )
    if generalization is not None:
        log(
            "generalization (ADVISORY, not a leg): "
            f"seen {generalization['seen']['candidate_overall']} vs "
            f"unseen {generalization['unseen']['candidate_overall']} — "
            f"gap {generalization['gap_candidate_seen_minus_unseen']} "
            f"CI {generalization['gap_bootstrap_95ci']}, "
            f"baseline-adjusted {generalization['baseline_adjusted_gap']} "
            f"CI {generalization['baseline_adjusted_gap_95ci']}"
        )
    log(f"full G1-G7 gate verdict: {report.get('verdict')} (exit {gate_rc})")
    log(f"summary: {out['summary']}")
    log("=" * 72)

    legs = [l1, l2] + ([] if l3 is None else [l3])
    return 0 if all(legs) else 2


if __name__ == "__main__":
    sys.exit(main())
