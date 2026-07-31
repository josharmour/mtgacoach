# PROGRESS — wp3-gate-eval-prep

Goal: make the gate evaluation RUNNABLE for the B5 LoRA
(`tools/training/checkpoints/wp3_gemma12b_b5_smoke`, base
`google/gemma-4-12B-it`) finishing tonight, with zero GPU usage during prep.

## What the gate actually is (mapped, not assumed)

The pre-registered "strategic gate >47.27" traces to
`tools/training/data/gate_report_strategic_v1.json` (verdict BLOCKED,
git_sha 1644f26): baseline arm `openai-compat:gemma-4-31b-it-base` scored
**overall accuracy 0.4727 (260/550)** on
`tools/training/data/gate_strategic_decisions_test.jsonl` (n=550, real MTGA
menus, `#perm`-suffixed permuted twin alongside). The scorer is
`tools/training/gate_play_decisions.py gate` (G1-G7, fail-closed, exit 0/2).
`tools/training/build_gate_corpus_v2.py` / `gate_prompts.jsonl` (400 rows)
belong to the older mulligan Stage-0 gate (`gate_stage0.py`) — NOT the
strategic gate; not used tonight.

End-to-end command sequence (proven previously as the `pd-gate-lora` flow,
docs/rl-training-status.md sections 21 and 23.8):

1. **serve** — vLLM (`/home/joshu/venv-serve/bin/python3`, vllm
   0.22.1rc1.dev596) serves `google/gemma-4-12B-it` bf16 with
   `--enable-lora --max-lora-rank 8 --lora-modules b5=<adapter dir>`.
2. **generate** — `tools.eval.run` replays corpus + permuted twin through
   `openai-compatible|http://127.0.0.1:8003/v1|{b5, google/gemma-4-12B-it}`
   (temperature 0.0 and max_tokens 400 come from the corpus records).
3. **score** — `gate_play_decisions.py gate` on the three response files
   (candidate identity, candidate permuted, baseline identity — the gate
   takes no baseline-permuted arm).
4. **compare** — pre-registered legs L1/L2/L3 read from the report.

All four steps are wrapped in **`tools/training/wp3/run_b5_gate_eval.py`**
(new). One command after the GPU frees (~18:35):

    /home/joshu/venv-train/bin/python3 -m tools.training.wp3.run_b5_gate_eval --gpu 0

CPU-only preflight (safe any time):

    /home/joshu/venv-train/bin/python3 -m tools.training.wp3.run_b5_gate_eval --dry-run

## Can the scorer consume a PEFT adapter directory?

The scorer itself is model-free (consumes response JSONL). Generation is the
model-touching step, and **vLLM consumes the PEFT adapter dir directly — no
merge needed**. Verified compatibility evidence: the B5 checkpoints'
`adapter_config.json` (`peft_type` LORA, r=8, alpha=16, regex
`target_modules` `(.*language_model.*(q_proj|...|down_proj))`) is
byte-for-byte the same *shape* as
`/home/joshu/checkpoints/play_decisions_v2_gemma31b_lora/adapter_config.json`,
which was served exactly this way for the section-21 gate run. transformers+
peft direct load (venv-train has peft 0.19.1) would also work but is slower
and was not the proven path; not used.

Adapter dir layout expected by the runner (fail-closed checks):
`adapter_config.json` + `adapter_model.safetensors` (>= 1 MB), config
`base_model_name_or_path == google/gemma-4-12B-it`, `peft_type == LORA`.
Checkpoint resolution: candidates = root itself (final trainer save) plus
`checkpoint-N/` subdirs; a candidate is complete only if those files exist
and are untouched for `--min-age-s` (90 s) — this is how the runner avoids
grabbing a checkpoint mid-write while the trainer is live. Newest complete
`adapter_model.safetensors` mtime wins.

## Pre-registered legs, encoded in the runner

- **L1** candidate overall accuracy strictly > 0.4727. Recorded honestly in
  the summary: that floor was measured with the **31B** base arm; tonight's
  baseline arm is the 12B base (the LoRA's actual base), and the summary
  carries both the absolute floor check and the same-base paired-bootstrap
  delta.
- **L2** candidate `legality_rate >= baseline legality_rate`.
- **L3** combat slice: **VACUOUS tonight** — `combat_gate_*.jsonl` are stale
  (built from the old colliding-id corpus, section 23.8) and the runner
  prints VACUOUS, never PASS.
- The full G1-G7 verdict is produced alongside (it is stricter; e.g. G7
  requires the strategic-subset delta CI to exclude 0 vs the 12B base).
  Exit code of the runner reflects L1 and L2 only; both verdicts are in
  `gate_b5_smoke_summary.json`.

## Tripwires (#443)

`gate_play_decisions.py` does NOT support the tripwire fixtures (different
response contract: `action_type` schema, not `{"pick": N}`) — that remains
the "wire tripwires into a gate" backlog item as a *hard gate leg*. What IS
wired tonight: **`tools/training/wp3/score_tripwires.py`** (new) scores a
responses JSONL against the 55 fixtures by reusing the existing
`build_tripwires.run_tripwire_eval` (no scoring logic re-implemented;
ordering skew between fixtures and the evaluator is asserted). The runner
regenerates the fixtures (the checked-out `tripwire_fixtures.jsonl` had only
50 rows — stale; `build_tripwires.py` deterministically emits 55 to
`tripwire_fixtures_55.jsonl`), generates both arms, and records per-category
histograms in the summary as an ADVISORY slice. Missing/errored responses
are scored as failed AND counted separately (never silently dropped).

## Measured / verified (all CPU, no GPU touched)

- Dry-run preflight: PASS end-to-end. Resolved adapter live while the
  trainer was writing: picked `checkpoint-150` (complete), skipped the
  incomplete root dir. Corpora: 550 + 550 records, id sets equal modulo
  `#perm` suffix.
- `py_compile` clean on both new files; `--help` (full import chain incl.
  arenamcp) exits 0 for `tools.eval.run`, `tools.training.gate_play_decisions`,
  `tools.training.wp3.score_tripwires`.
- Real `gate` command executed on CPU with the runner's exact labels using
  `simulate`-generated responses (`land_else_first` candidate vs `first`
  baseline, n=550, 2000 bootstraps): verdict BLOCKED on G6/G7 as expected
  for reflex policies; every report field the verdict code reads
  (`candidate.overall.accuracy`, `legality_rate`, `paired_bootstrap*`,
  `verdict`, `failures`) present with expected shapes.
- Tripwire scorer: oracle arm 55/55 (100.0%), broken arm 0/55 with
  missing=3 / errored=2 accounted; category histogram printed
  (15 keep / 15 mull / 10 lethal / 10 counter / 2 develop / 2 attack /
  1 pass).
- Harness regression tests: `tests/test_gate_play_decisions.py`,
  `test_gate_stats.py`, `test_temperature_fallback.py` — 46 passed,
  4 skipped.
- vllm 0.22.1rc1.dev596 importable from venv-serve;
  `models--google--gemma-4-12B-it` present in the HF hub cache.

## Could NOT verify (needs the GPU)

- The single GPU step: vLLM serving `google/gemma-4-12B-it` +
  `--lora-modules b5=<checkpoint>` on this exact vllm build (LoRA loading of
  THIS adapter has not been exercised; the shape-identical 31B sibling was).
- `--max-model-len 49152` headroom: chosen to cover the 32.4k-token prompt
  that 400'd the v1 run; not re-measured against this vllm build.
- End-to-end generation latency (550 x 3 arms + 55 x 2 tripwires).

## Known concerns for whoever runs it

- The corpus `system` prompt (7907 chars) no longer equals the current
  `AUTOPILOT_SYSTEM_PROMPT` (8262 chars) — the prompt changed after the
  corpus was built at 1644f26. Generation uses the system stored IN the
  corpus records (comparable with the 47.27 measurement, which is what
  pre-registration requires), and the equality assert lives in `build`,
  not `gate`, so nothing blocks. A future corpus rebuild resets the floor.
- The 47.27 floor is a 31B-base number; beating it with a 12B+LoRA is a
  strictly harder ask than beating its own base. Both comparisons are in
  the summary so the verdict cannot be quoted without that context.
- The trainer's final top-level save (`adapter_model.safetensors` in the
  root) will outrank checkpoint-N by mtime once written — that is the
  intended behavior of "last complete checkpoint".
