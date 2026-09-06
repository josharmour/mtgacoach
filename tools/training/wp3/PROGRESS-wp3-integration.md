# PROGRESS: wp3-integration

## Pipeline: run_wp3_pipeline.py — chain all WP-3 modules end-to-end

### What was done
Created `tools/training/run_wp3_pipeline.py` that:
  1. Parses MageZero XMage logs → decisions JSONL (via `parse_magezero_log`)
  2. Filters: drop_single_option → outcome_filter(won_only) → dedupe (via `magezero_filters`)
  3. Pass rate tripwire (0.40) — PASS (0.0% pass rate)
  4. Split by game (seed=7) — game-level split to prevent train/val leakage
  5. Renders priority rows per split via `build_magezero_bridge.build_record`
  6. Attackers/blockers excluded and counted (621 total)
  7. Leak scan: 0 violations (checks for "score:", "count:", and MCTS integers ≥30)
  8. Writes corpus to `tools/training/data/wp3/`
  9. Writes manifest.json and REPORT.md

### Test output (real corpus — 2 logs)
```
Input: /home/joshu/mz_train_smoke.log (201 MB, 9789 decisions)
       /home/joshu/mz_logs/mz_train.manual-20260729-160933.log (806 MB, 30439 decisions)

Stage        | In     | Out    | Drop
-------------|--------|--------|------
parse        | 0      | 40228  | 0
drop_single  | 40228  | 40228  | 0
outcome_filt | 40228  | 14591  | 25637
dedupe       | 14591  | 11265  | 3326
render(train)| 10167  | 7202   | 2965 (561 AB + 2404 other)
render(val)  | 519    | 370    | 149 (31 AB + 118 other)
render(test) | 579    | 419    | 160 (29 AB + 131 other)

Pass rate: 0.0% (tripwire PASS)
Attackers/blockers excluded: 621
Total training records: 7991 (7202 train, 370 val, 419 test)
Leak scan: 0 violations
Elapsed: 58.8s
```

### Decisions made
- Leak scan uses two-tier approach: (1) "score:" and "count:" substring checks (definitive MCTS markers), (2) bare integer check for MCTS values > 30 with context validation (not preceded by =, :, >, < to exclude life totals/turn numbers/menu indices). The full adjacency check (action_name × count pairs) was rejected due to combinatorial explosion (115K pairs → 230K regex patterns).
- `drop_single_option` filtered 0 rows — MageZero's MCTS pool always has multiple options at priority decisions.
- Rendering drops 3274 rows: 621 attackers/blockers and 2653 from menu parity issues (chosen action not in menu due to action string normalisation differences between XMage and MTGA naming).

### Known gaps
- Card facts: only basic land type lines are resolved; non-basic permanents render without full type/oracle info. Mana computation for non-basic lands without basic-land-name-in-name contributes 0 mana.
- Attackers/blockers rows: excluded and counted only — a parallel agent builds the combat corpus.
- Session labels from the manual log use "sessionN" fallback naming (no opponent detection for non-smoke logs).
- Leak scan's MCTS integer check only flags values > 30 to avoid false positives from life totals (10-20).

### Verified
- [x] Parse: both logs parsed successfully (40228 decisions)
- [x] Filter: single-option, outcome, dedupe all pass
- [x] Tripwire: 0.0% pass rate (PASS)
- [x] Split: game-level split, 3 splits
- [x] Render: production-shaped prompts via gate_play_decisions.build_user_message
- [x] R1: system prompt identity (AUTOPILOT_SYSTEM_PROMPT)
- [x] R2: attackers/blockers excluded and counted
- [x] R3: unknown-outcome rows excluded by pre-filter
- [x] R4: answer index from menu match
- [x] Leak scan: 0 violations
- [x] Manifest: per-split counts, sha256, filter_counts
- [x] REPORT.md: full pipeline report with sample records

### File sizes
- decisions.jsonl: 49 MB (40228 rows)
- filtered.jsonl: 14 MB (11265 rows)
- train.jsonl: 567 MB (7202 records)
- val.jsonl: 30 MB (370 records)
- test.jsonl: 33 MB (419 records)
