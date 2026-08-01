# RL Training — Status
*Fully rewritten 2026-08-01. The complete execution log (addenda 1–23,
2026-07-26 → 2026-08-01) is preserved verbatim in
`rl-training-status-archive.md`. This document is the current state of the
world, updated in place.*

## Where things stand (one paragraph)
The distillation machinery is built and proven honest; the science says the
only thing that ever blocked it was teacher quality — and the prior teacher
was crippled by our own deployment bugs, not by its method. A fresh MageZero
run at upstream-faithful parameters, on clean data, with 10 diverse
opponents, started 2026-08-01 14:11 and is the current main experiment. In
parallel, a zero-training "teacher audition" showed the frontier model
(DeepSeek-V4-Flash-0731) is near-perfect on objective play puzzles,
making frontier distillation the leading product path while the curriculum
pursues the no-ceiling research path.

## Key measured results (the numbers that drive decisions)

### Distillation arms — strategic gate vs objective tripwires
| candidate | strategic gate (n=550) | tripwires (n=55) |
|---|---|---|
| best no-knowledge reflex | **0.3964** | — |
| base gemma-4-12B-it | 0.3909 | 0/55 (format-confounded) |
| dsv4 (0731, thinking off) | 0.3636 | **53/55 = 96.4%** |
| LoRA v3 (oracle-enriched) | 0.2636 | 18/55 |
| LoRA v2 arms / v1 | 0.249–0.262 / 0.240 | — |

Two conclusions, both load-bearing: (1) the strategic gate's Gold-click
reference **saturates ~0.40 and cannot rank at/above Gold** — it remains a
blunder/collapse detector only; (2) on objective positions the frontier model
is near-perfect while every LoRA to date learned format without judgment —
because every LoRA distilled a teacher weaker than the student's own prior.

### Why the old curriculum readouts were invalid (fidelity audit)
| defect | measured impact | status |
|---|---|---|
| device flapping crash (our regression) | gen-1 training NEVER completed (6 aborted attempts) | fixed (memoized) |
| replay-buffer glob pollution | gen-0 net trained 48.6% on orphan data; next step would have been ~66% polluted | quarantined; fresh ver2 lineage |
| search budget 300 vs upstream 1000 | 1/3 author's search depth; 34–60% of decisions timeout-capped | budget 1000 restored |
| scale 3.3K games planned vs "~20K is enough" | judged the method at 1/6 its documented requirement | 40-gen config, "enough" ≈ gen 20 |
| load-sensitive opponents | same config swung 24.6%→52.2% win rate across load regimes | fixed load for the whole run; head-to-head instrument queued |

Upstream's published trajectory (5 mono opponents): win rate ≈ .347 → .361 →
.439 → .502 → .507 → .524 → .543 over gens 0–6, inflecting when prior heads
activate at gens 2–3; long-run 16%→66% on UWTempo.

## LIVE: the diverse fidelity run (2026-08-01_14-11-20)
- UWTempo **ver2, fresh bootstrap** — clean lineage, polluted ver1 fossilized.
- **10 opponents** = 5 Standard-Mono (upstream-comparable subset) + 5 diverse
  (storm, 5-colour legends, tribal ×2, spellslinger); card pool 83 → 235+.
  Permanent holdouts (HighNoonControl, BGRoots, BWBats) excluded by validator.
- Budget 1000 · 10 threads · 64G heap · eval-server workers 20 ·
  100 games/arm · 1,000 games/gen · buffer 10 · ≤40 gens.
- Checkpoints: HEALTH-2 (first online arm — recalibrates ETAs) → SANITY
  (gen-1 in .30–.36 band) → LEARNING (gen2-vs-gen1, first clean pair) →
  TRAJECTORY (gen-3 vs upstream shape) → ENOUGH (gen ~20; ~Aug 8–12 solo,
  ~Aug 6–8 if the Mac's distributed arms enroll).
- Known small risk: a watchdog race at launch briefly ran two runners; check
  for duplicate MonoR session files before gen-0 training consumes them.

## Serving & hardware (as of 2026-08-01)
- **dsv4** (DeepSeek-V4-Flash-0731) serves both RTX 6000s at **1M context**
  (speculation off; env knobs can trade context down for +55% decode).
  Gateway names: `dsv4`, `deepseek-v4-flash`, `qwen3.6-fp8` (legacy). Qwen
  container retained as rollback.
- **R9700** is MageZero's: Ollama chat model evicted (8.4 GB freed);
  embeddings (all-minilm) retained and verified for sobriety-copilot, whose
  chat (incl. sc-generator) now serves from dsv4 — verified live.
- **Speed program assets** (built, verified, partially parked): CUDA serving
  for the mz net (6.9ms p50 vs 41–87ms; parked while DeepSeek holds both
  cards), distributed multi-host runner (1.73× smoke-verified; awaits Mac
  Remote Login), budget-sweep harness (needs its two-server fix re-run).

## Instruments and their validity rules
- Strategic gate: blunder/collapse detection only (saturates ~0.40).
- Tripwires (55 objective puzzles): the current ranking instrument; **owner
  hand-verification pending** — they are agent-authored.
- Head-to-head net-vs-net: the load-immune compounding measure; harness
  queued for gen boundaries.
- Unseen-deck slice (#468): generalization gap; wired, awaiting holdout logs.
- All comparisons: one config change at a time; adjacent-gen reads only
  within a fixed-config run.

## Next readouts
1. HEALTH-2 — Sat evening: real games/hr at budget 1000 online.
2. LEARNING — Sun/Mon: gen2-vs-gen1, first clean compounding pair.
3. TRAJECTORY — Mon/Tue: gen-3 shape vs upstream's inflection.
4. v4-from-dsv4 — on owner go: relabeled corpus, objective-slice gate.
5. ENOUGH — ~Aug 8–12: teacher-audition rematch; is the curriculum corpus
   finally worth distilling.
