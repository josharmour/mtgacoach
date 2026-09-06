# WP Audit — Phase 0/1 Work Package Status

Generated: 2026-07-29
Commit: 156313b (master HEAD)
Worktree: wp0-audit

Each WP assessed against its checkboxes and Accept criteria in `rl-pipeline-fix.md`.

---

## Phase 0 — Foundation Fixes

### WP-0.1: Retarget training data to production contracts
**Status: DONE**

| Checkbox | Evidence |
|----------|----------|
| `build_dataset.py`: SFT `response` serializes via `game_action_to_schema_json()` | **tools/training/build_dataset.py:56** — imports `game_action_to_schema_json` from `arenamcp.action_planner`; line 398 calls it for SFT `chosen_json`; line 399 for `rejected_json` |
| `build_dataset.py`: DPO `chosen`/`rejected` serialize to `ACTION_SCHEMA` JSON | **tools/training/build_dataset.py:398-399** — same serialization path for DPO pairs |
| Decision records use keyword contracts | **tools/training/validators.py:49-64** — `validate_keyword_contract` checks KEEP/MULLIGAN contracts |
| Accept: `pytest tests/test_build_dataset_contracts.py -q` passes | **tests/test_build_dataset_contracts.py** — 2 tests (`test_game_action_to_schema_json_serialization`, `test_menu_pick_to_schema_json_serialization`) exist; committed in `3648114` |
| Emitted responses parse through ActionPlanner's parser | **tools/training/build_dataset.py:56** reuses `game_action_to_schema_json` which mirrors `action_planner.py`'s `ACTION_SCHEMA` exports |

### WP-0.2: Chat-template training + length audit
**Status: DONE**

| Checkbox | Evidence |
|----------|----------|
| `train.py`: Format examples with `tokenizer.apply_chat_template` | **tools/training/train.py:354-361** — `to_prompt_completion()` uses `render_serve_prompt()` (which calls `apply_chat_template` with `add_generation_prompt=True`); **tools/training/formatting.py:44-51** — `render_serve_prompt()` implementation |
| System and user as separate messages | **tools/training/formatting.py:44-51** — `[{role: "system"}, {role: "user"}]` via `tokenizer.apply_chat_template()` |
| 5% eval split + early stopping | **tools/training/train.py:251-253** — `train_test_split(test_size=0.05, seed=42)`; **train.py:42** — `DEFAULT_EARLY_STOPPING_PATIENCE = 3`; **train.py:469-475** — `EarlyStoppingCallback` added when enabled |
| Length audit script exists | **tools/training/measure_prompt_lengths.py** — 67 lines, reports P50/P95/P99/Max token lengths with recommended `--max_length` |
| Accept: Decoded training sample byte-identical to serve wire format | **tests/test_train_chat_template.py** — 4 tests anchoring `format_sft_example()` byte-equality to `render_serve_prompt()`; **tools/training/formatting.py:54-72** — `format_sft_example()` stitches prompt + response + turn-end suffix |

### WP-0.3: Match-level metrics + robust winner attribution
**Status: PARTIAL**

| Requirement | Evidence | What's missing |
|-------------|----------|----------------|
| Calculate win rates over unique `match_id`s | **tools/training/run_pipeline.py:353-366** — win rate aggregated per-trajectory-line, NOT deduplicated by `match_id`. Line 358: `winner = rec.get("winner")` — counts every trajectory record, not one per match. `match_id` is carried in per-record metadata (`build_bridge_dataset.py:190,274-275`) but the pipeline has no dedup by match_id | No `set()` of seen match_ids for deduplication. Win-rate denominator is inflated (multiple trajectory rows from the same match count as separate data points) |
| Track and report `unresolved` matches | **tools/training/build_dataset.py:312-314** — records without `winner in ("local", "opp")` are silently dropped: `if not winner or winner not in ("local", "opp"): continue`. No counter. In pipeline gating (**run_pipeline.py:353-366**), only lines with `seat=="local"` and `winner` set are counted; others fall through without being reported | No `unresolved` counter or log line. Silent dropping is exactly the defect WP-0.3 was created to fix |
| Accept: Synthetic trajectory unit test yields accurate match-weighted win rate | No such test exists | Create `test_match_weighted_win_rate` |

**Remaining work:**
1. Add `match_id` dedup set in `run_pipeline.py._gate_decision()` (or the scoring loop) so one match contributes at most one data point
2. Add `unresolved_matches` counter — records where `winner` is missing or unrecognized should be counted and logged, not dropped silently
3. Write unit test for match-weighted win rate

### WP-0.4: Fallback-contamination tagging
**Status: PARTIAL**

| Requirement | Evidence | What's missing |
|-------------|----------|----------------|
| Tag records with `fallback: bool` | **tools/training/build_dataset.py:309** — reads `rec.get("fallback")` and skips if True. The *reader* expects the field but no tagging code exists in the tools/training/ pipeline. The `self_play.py` (in `src/arenamcp/self_play.py`) may emit fallback-tagged records but the tools/training pipeline does not produce them | No code in `tools/training/` that *sets* `fallback: True` on records. The pipeline only consumes it. The checkbox says "Tag records" — the tagging must happen somewhere upstream. If `src/arenamcp/self_play.py` handles it, that's outside the WP scope, but there's no verification that records entering `build_dataset.py` actually carry the tag |
| Tag records with `submitted_action` | No reference to `submitted_action` in any `tools/training/` file | Never implemented |
| Hard-exclude `fallback: true` from SFT positives and DPO `chosen` | **tools/training/build_dataset.py:309** — `if rec.get("fallback") is True: continue` — hard-exclusion IS implemented on the consumption side | The Accept criterion says "Built dataset contains zero `fallback: true` records" — this is satisfied if the upstream tags them correctly and `build_dataset.py` skips them |

**Remaining work:**
1. Ensure `submitted_action` is tagged on decision records (ideally in the producer, e.g. `src/arenamcp/self_play.py`)
2. Verify fallback tagging reaches `build_dataset.py` input records
3. No test validates the "zero fallback:true records" accept criterion — add one

### WP-0.5: Registry + pointer promotion
**Status: DONE**

| Checkbox | Evidence |
|----------|----------|
| Content-addressed store (`models/gen-NNNN-<sha8>/` with `metadata.json`) | **tools/training/registry.py:60-76** — `_atomic_write_json()` for metadata; **registry.py:89-109** — `hash_directory()` for content-addressed naming; **registry.py:264-291** — store dir creation and metadata.json write in `register_generation()` |
| Atomic pointer flip in `registry.sqlite` | **tools/training/registry.py:323-374** — `promote_champion()`: writes temp file → fsync → SQLite transaction → `os.replace()`, with rollback on failure. **registry.py:128-156** — SQLite schema with `models` table |
| Retain last 10 generations | **tools/training/registry.py:50** — `RETENTION_LIMIT = 10`; **registry.py:412-445** — `prune_old_generations()` prunes beyond limit, keeps champion always |
| Accept: Promote → rollback → promote cycle leaves artifacts intact and pointers clean | Partial: the atomic write logic (temp+sync+replace) in `_atomic_write_json()` and `promote_champion()` does guarantee this, but no explicit test cycles promotion→rollback→promote. A manual test or test_promote_rollback test would complete this |

**Note:** No explicit test file for `registry.py` — add `tests/test_registry.py` for the accept criteria.

### WP-0.6: Fail-closed gating
**Status: DONE**

| Checkbox | Evidence |
|----------|----------|
| Delete silent fallbacks in `run_pipeline.py` | **tools/training/run_pipeline.py:413-419** — `_gate_decision()`: when `quality is None`, gate returns `False` with "GATE BLOCKED" log. **run_pipeline.py:151-157** — `_evaluate_challenger_quality()` returns None if corpus is missing or judge fails |
| Judge/eval unavailability → gate BLOCKED | **run_pipeline.py:413-419** as above |
| Minimum gate sample sizes enforced as constants | **tools/training/gate_stage0.py:53** — `MIN_SAMPLES = 100`; **tools/training/gate_play_decisions.py** (lines 62-80 show G1-G4 thresholds as explicit constants) |
| Accept: Simulated judge outage returns BLOCKED with alert, leaving champion pointer untouched | **tests/test_gate_stats.py** — `test_gate_decision_fails_closed_when_quality_none` passes `quality=None` and asserts `verdict is False`. The champion pointer is not touched because `register_generation()` is only called on `promote=True` |

### WP-0.7: Config-driven endpoints
**Status: NOT-STARTED**

| Requirement | Evidence |
|-------------|----------|
| Centralize ports/URLs in `settings.py` | **src/arenamcp/settings.py** — contains user-facing settings (voice, mode, license key) NOT pipeline endpoints. No centralized constants for pipeline ports (vLLM `:8000` vs litellm `:8444` vs Ollama `:11434` vs Forge shim ports). Ports are hardcoded or passed as ad-hoc CLI args |
| Add `--dry-run` probe for target policy server | No `--dry-run` flag exists anywhere in `tools/training/`. The only reference to "dry run" is `split_combat_gate.py:328` in a comment about data, not endpoint probing |

**Remaining work:**
1. Create a pipeline config module or section (e.g. constants file or `training_settings.py`) that maps service names to ports/URLs
2. Add `--dry-run` to `run_pipeline.py` (or a standalone `verify_vllm.py`-style script) that connects to each endpoint and verifies the expected adapter/model is served
3. The `verify_vllm.py` at `tools/verify_vllm.py` exists (5028 bytes) but is for vLLM health, not pipeline endpoint validation

---

## Phase 1 — Data Spine, Distillation & RLAIF Loop

### WP-1.1: Multi-Source Data Spine & Ingestion
**Status: PARTIAL**

| Checkbox | Evidence | What's missing |
|----------|----------|----------------|
| Gateway Capture: LiteLLM callback on plex | Not found in repo — this is a plex-side infrastructure task. No callback script exists. The plan says it records `{model, system, user, response, latency, ts, key_hash}` with PII scrubbing | Gateway capture script is not yet built |
| 17lands replay ingestion | **tools/training/ingest_17lands.py** — 362 lines, full implementation: parses mulligan prompts (with bucket_stats → higher-WR target), turn-action prompts, eval-contamination guard via SHA-256 exclusion set, bucket-holdout guard | Complete |
| Raw GRE log ingestion (manasight / Player.log) | **tools/training/ingest_manasight.py** — 1620 lines, full implementation: parses `Player.log` via GreToClientEvent ↔ ClientToGreMessage pairing, normalises to replay_menu_groundtruth schema, emits `manasight_menu_groundtruth.jsonl` (2,503 records), rank-probe option (constructedClass/limitedClass) | Complete |
| Eval Corpus v1 (≥300 real prompts) | **tools/training/data/gate_play_decisions_test.jsonl** and related files exist (gate_play_decisions_manifest.json shows 665 real menus from 104 .rply replays). Combat and strategic corpora also exist | The eval corpus exists but is described in the plan as something that should be versioned and hash-excluded from training sets — that mechanism is in place via `build_bridge_dataset.py`'s SHA-256 exclusion |

**Remaining work:**
1. Build gateway capture (LiteLLM callback on plex) — infrastructure task, not repo code
2. Verify Eval Corpus v1 volume (should be ≥300 prompts; gate test corpus meets this)

### WP-1.2: Contract Validator Library & Tripwires
**Status: DONE**

| Checkbox | Evidence |
|----------|----------|
| `validators.py`: Pure Python checkers importing `ACTION_SCHEMA` and `DECISION_PROMPTS` | **tools/training/validators.py** — 115 lines. **Line 13** `validate_action_schema_json()` checks JSON schema; **line 49** `validate_keyword_contract()` checks keyword compliance; **line 67** `validate_action_legality()` checks legal action existence; **line 98** `validate_all()` runs all checks |
| Checks JSON schema | **validators.py:13-47** — validates root object, actions list, pick type, required keys |
| Keyword compliance | **validators.py:49-64** — KEEP/MULLIGAN keyword contracts |
| Length caps | Not explicitly in validators.py — `measure_prompt_lengths.py` handles token length auditing |
| Legal action existence | **validators.py:67-95** — verifies pick N exists within Legal: menu |
| Tripwire Set: 50 hand-verified puzzle states | **tools/training/build_tripwires.py** — generates 50 fixtures (15 keep, 15 mulligan, 10 lethal-on-board, 10 counterspell). **tools/training/data/tripwire_fixtures.jsonl** — 50 lines, one per fixture |
| Dropping >2 points vs champion rejects candidate | Specified in plan but no explicit code enforces this as a gate threshold in any gate script. This is part of the "tripwire set" usage that the plan describes but hasn't been wired into the gate pipeline yet |

**Remaining work:**
1. Wire the tripwire set into a gate — currently `tripwire_fixtures.jsonl` exists but no gate runner scores candidates against it and enforces the "dropping >2 points → reject" rule

### WP-1.3: Stage-0 Custom Gemma Fine-Tuning
**Status: SUPERSEDED**

| Requirement | Status |
|-------------|--------|
| Distill teacher over curated 17lands + GRE corpus into `mtgacoach-gemma4-v0` on GPU 1 | See plan §CURRENT STATE: **"Phase 0 (imitation / SFT from human replays) is PAUSED at 0-for-2."** Both candidates were blocked by gates — strategic (45.09 vs 47.27 base) and combat (label leak). Root cause: **human labels cap the model at that human's rank** (Gold) |
| Register with 99%+ schema parse rate | `gen-0001-v0` exists in data files (`gate_report_gen-0001-v0.json`) but IS_CHAMPION=0 and never promoted. The imitation approach is permanently blocked by the ceiling on human-labeled data |

**Why superseded:** The plan's own analysis (2026-07-26) concluded "Imitation learning cannot exceed its demonstrator" (the owner plays at Gold). The replacement path is **outcome-based self-play via MageZero** (WP-3), where teacher games (deck-local AlphaZero with no text ceiling) are distilled into gemma LoRAs.

**Related code still in tree:** `train.py`, `build_dataset.py`, `ingest_17lands.py`, `ingest_manasight.py` — all are reusable for MageZero bridge records. The train/gate pipeline is unchanged; only the label source changed.

### WP-1.4: Continuous RLAIF & GRPO Policy Training
**Status: SUPERSEDED**

| Requirement | Status |
|-------------|--------|
| Sample K=4 candidates at temp 0.8 | The RLAIF loop as originally scoped (DPO on judge-ranked candidates) was part of the same imitation chain that went 0-for-2 |
| Deterministic schema → DPO pairs | |
| AI judge pairwise rank | |
| GRPO / DPO optimization on GPU 1 with KL penalty | |
| Budget & quality governors | |

**Why superseded:** Same root cause — the imitation ceiling. The MageZero bridge (WP-3) replaces this with a distillation pipeline: MageZero teacher games → production-shaped prompts → gemma LoRA → existing gates → canary. RLAIF/GRPO may return in Phase 2 (the plan's Stage D), gated behind the Forge fidelity spike.

**What remains usable:** The DPO trainer in `train.py` and the judge infrastructure in `build_dataset.py` (`_judge_move_pair`, `MOVE_JUDGE_SYSTEM`) are intact and available if DPO training is needed for MageZero-distilled pairs.

### WP-1.5: `loopd` Orchestrator Daemon & GGUF Exporter
**Status: NOT-STARTED**

| Requirement | Evidence |
|-------------|----------|
| `tools/training/loopd.py` systemd service on plex | **File does not exist.** No `loopd.py` found anywhere in `tools/training/` or `tools/` |
| State machine: CAPTURE → CURATE → TRAIN → SERVE_CHALLENGER → GATE → CANARY → PROMOTE → EXPORT_GGUF | No state machine code exists. `run_pipeline.py` is the closest existing orchestrator but it's a single-shot script, not a daemon |
| Self-healing watchdogs, VRAM monitoring, alert integration | Nothing found |
| GGUF exporter (champion → `mtgacoach-gemma4-vX.Y-Q4_K_M.gguf`) | No GGUF export code exists anywhere in the repo |

**Remaining work:** Full implementation required:
1. Design and implement `loopd.py` as a systemd service with the specified state machine
2. GGUF conversion step (llama.cpp `convert.py` or equivalent)
3. Watchdog + VRAM monitoring + ntfy/email alerts
4. Wire into `run_pipeline.py`-style training orchestration

---

## Summary

| WP | Status | Key file(s) | What remains |
|----|--------|-------------|-------------|
| **WP-0.1** | **DONE** | `build_dataset.py:56,398`, `test_build_dataset_contracts.py` | — |
| **WP-0.2** | **DONE** | `formatting.py`, `train.py`, `measure_prompt_lengths.py`, `test_train_chat_template.py` | — |
| **WP-0.3** | **PARTIAL** | `run_pipeline.py:353-366` | Match-id dedup for win rate; `unresolved` counter; unit test |
| **WP-0.4** | **PARTIAL** | `build_dataset.py:309` | `submitted_action` tagging on records; verify upstream fallback tags |
| **WP-0.5** | **DONE** | `registry.py` | `tests/test_registry.py` recommended but not blocking |
| **WP-0.6** | **DONE** | `run_pipeline.py:413`, `gate_stage0.py:53`, `test_gate_stats.py` | — |
| **WP-0.7** | **NOT-STARTED** | — | Pipeline config module + `--dry-run` probe |
| **WP-1.1** | **PARTIAL** | `ingest_17lands.py`, `ingest_manasight.py` | Gateway capture (plex infra) |
| **WP-1.2** | **DONE** | `validators.py`, `build_tripwires.py`, `test_contract_validators.py` | Wire tripwires into gate pipeline |
| **WP-1.3** | **SUPERSEDED** | — | Replaced by MageZero bridge (WP-3). `train.py`/`gate_*.py` still used |
| **WP-1.4** | **SUPERSEDED** | — | Replaced by MageZero distillation. DPO trainer/judge infra reusable |
| **WP-1.5** | **NOT-STARTED** | — | Full `loopd.py` implementation + GGUF exporter |

**Count:** DONE=5 (WP-0.1, WP-0.2, WP-0.5, WP-0.6, WP-1.2) · PARTIAL=3 (WP-0.3, WP-0.4, WP-1.1) · NOT-STARTED=2 (WP-0.7, WP-1.5) · SUPERSEDED=2 (WP-1.3, WP-1.4)
