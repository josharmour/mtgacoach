# PROGRESS: wp3-oracle-coverage — oracle-text coverage audit + enforcement

Branch: `wp3-oracle-coverage`. MORNING PRIORITY item 2 (rl-pipeline-fix.md):
the transfer medium for card generalization is TEXT — a bare card name in a
prompt teaches name->action; oracle text teaches role->action. #461 fixed
non-basic lands; this measures and fixes everything else in the WP-3 path.

All numbers below are MEASURED on this machine (blackwell), 2026-07-31.

## 1. Coverage BEFORE (committed corpus `wp3_v2_combat`, built from
## mz_train_smoke.log at a9a3f23 with --include-combat --balance downsample-pass)

Mention = one distinct card name per context per rendered prompt.
Coverage = covered / (covered + uncovered + unresolved); `keyword_only` and
`no_text` are excluded from the denominator (nothing to attach); `unresolved`
(no LOCAL oracle source) counts AGAINST coverage.

| Context     | Coverage | Covered | Uncovered | Unresolved | Keyword-only | No-text |
|-------------|----------|---------|-----------|------------|--------------|---------|
| menu        | 0.0%     | 0       | 8,046     | 166        | 1            | 671     |
| hand        | 0.0%     | 0       | 17,761    | 630        | 0            | 1,285   |
| your_board  | 0.0%     | 2       | 38,457    | 349        | 0            | 5,878   |
| opp_board   | 0.0%     | 0       | 10,886    | 169        | 83           | 6,629   |
| **overall** | **0.0%** (2 / 76,466) | | | | | |

5 concrete missing examples per context (from the audit):
- menu: Adarkar Wastes, Skrelv Defector Mite, Shardmage's Rescue, Bounce Off, Floodfarm Verge
- hand: Floodfarm Verge, Meticulous Archive, Seachrome Coast, No More Lies, Soul Partition
- your_board: Seachrome Coast, Skrelv Defector Mite, Combat Research, Meticulous Archive, Sheltered by Ghosts
- opp_board: Burnout Bashtronaut, Rockface Village, Nova Hellkite, Hired Claw, Emberheart Challenger

Root cause: `build_magezero_bridge._build_card`/`_build_hand_card` hardcoded
`oracle_text: ""` (MZ logs carry names only, and `magezero_card_map.json`
never carried oracle text), and the planner formatter suppresses board oracle
for long-resident permanents (`turn_entered_battlefield=-1` made every MZ
card look long-resident).

## 2. Three corpus bugs found by the audit (all fixed, drop-and-count)

1. **Hand score suffixes**: XMage's `GameStateEvaluator2.printBattlefield`
   emits hand entries as `Name:5`; `parse_card_list` kept the suffix while
   `parse_permanents` already stripped it. Measured: 8,185 of 18,943
   `-> Hand:` lines in mz_train_smoke.log carry it; 2,412 of 9,489 combat-row
   hand mentions rendered as nonexistent names ("Negate:5").
2. **Combat markers as names**: `parse_permanents` extracted `,tapped` but
   not `,attacking`/`,blocking`, so priority prompts rendered names like
   "Skrelv, Defector Mite,attacking". Now extracted into flags; the bridge
   honours them for the LOCAL seat only ([ATK]/[BLK] flags) — an
   is_attacking-marked OPPONENT card would make the production formatter run
   block analysis on fabricated 0/0 stats (same rule magezero_combat_micro
   already enforced).
3. **False [NO TARGETS] tags**: with oracle text attached, the production
   formatter's removal analysis computes target pools from the opponent's
   TYPED board. MZ boards are untyped, so the tag comes out false for any
   removal spell whenever the opponent actually has creatures — while the
   MageZero gold action frequently casts exactly that spell (contradictory
   label). The bridge sanitizer strips the tag and counts it
   (5,529 strips across 6,921 rendered records). `[RM:...]` tags are kept:
   they derive from the card's own oracle text.

## 3. The fix

- `tools/training/wp3/enrich_card_map.py` (new): enriches
  `magezero_card_map.json` with `oracle_text` (+ printed power/toughness,
  stored not rendered) from the LOCAL Scryfall bulk
  (`~/.arenamcp/cache/scryfall/default_cards.json`), no network. Adds entries
  for token/DFC names found in decisions files. Result: **98 entries, 98
  resolved locally, 0 unresolved** ("Incubator Token" resolves via
  double-faced-token face names). Names with no local source would be tagged
  `oracle_source: none_local` and rendered bare — never fabricated.
- `build_magezero_bridge.py`: attaches the map's oracle text to hand and
  battlefield card dicts; `turn_entered_battlefield` set to the current turn
  so the planner formatter actually renders the text (entry turn is not
  recoverable from MZ logs; with untyped cards the field's only
  prompt-visible effect is that render gate). Deliberately does NOT attach
  type_line/power/toughness: typing MZ cards as creatures makes the formatter
  print fabricated 0/0 stats and run the combat solver, whose
  "Computed optimal" lines are fail-closed-rejected by the combat adapter.
- `run_wp3_pipeline.py`: new stage measures coverage on every rendered
  corpus; `oracle_text_coverage` (per-context + overall + examples +
  mention accounting) lands in `manifest.json` and `REPORT.md`;
  `--min-oracle-coverage FRAC` is a fail-closed floor (exit 43; also trips
  when there are zero measurable mentions). Default OFF.
- `tools/training/oracle_coverage.py` (new): the audit module + CLI
  (`--bulk` for real-replay gate corpora). Fail closed: unparseable lines
  are counted per context, never skipped.

## 4. Coverage AFTER (same log, same flags, fixed code;
## outdir wp3_smoke_oracle, 6,921 records: 5,459 priority + 1,291 attack + 171 block)

| Context     | Coverage | Covered | Uncovered | Unresolved | Keyword-only | No-text |
|-------------|----------|---------|-----------|------------|--------------|---------|
| menu        | 88.1%    | 7,234   | 815       | 166        | 1            | 671     |
| hand        | 100.0%   | 18,355  | 0         | 0          | 0            | 1,316   |
| your_board  | 100.0%   | 38,821  | 0         | 0          | 0            | 5,874   |
| opp_board   | 100.0%   | 11,041  | 0         | 0          | 83           | 6,627   |
| **overall** | **98.7%** (75,451 / 76,432) | | | | | |

Mention accounting from the build itself: 122,948 oracle attachments, 30
resolved-but-textless (basics/vanilla), **0 unresolved**, 5,529 [NO TARGETS]
strips. Unfixable cards (no local oracle source): **0**.

Residual menu gap (11.9%): production menus are bare by design — a menu
mention counts as covered when the card's text renders in hand/board of the
same prompt. The remaining 815 uncovered are menu-only mentions (mostly
stale/incomplete MZ hand attribution windows); the 166 unresolved are
comma-split fragments of the known `segment_menu` fail-closed fallback
("Cast Malcolm" + "Alluring Scoundrel" when the MCTS pool didn't confirm the
merged name); 314 unparsed menu lines are ability-text fragments of the same
split. These are log-fidelity limits, not renderer gaps.

Side effects, measured:
- Prompt length: train median 789 -> 2,505 chars, mean 815 -> 2,583, max
  1,425 -> 5,216 (oracle text is the payload; system prompt unchanged).
- `Mana:` lines gain source colors for suffix-typed lands (oracle mana
  abilities now visible to the formatter).
- Corpus deltas vs the committed build (same log): dedupe 1,598 -> 1,617
  drops (cleaned names collapse more duplicates); records 6,924 -> 6,921.
  Combat class histograms unchanged (all-in 74.5% floor breach still
  flagged, per addendum 19 postscript).
- Every corpus rebuilt after this prompt change must be regenerated (the
  system-prompt staleness guard is unchanged; the USER shape changed).

## 5. Gate corpora (real-replay, audited with --bulk; measurement only)

| Corpus | Records | menu | hand | your_board | opp_board | attacking | overall |
|--------|---------|------|------|------------|-----------|-----------|---------|
| gate_play_decisions_test.jsonl | 206 | 74.5% | 95.5% | 12.0% | 6.6% | n/a | 59.7% |
| gate_strategic_decisions_test.jsonl | 550 | 55.1% | 95.5% | 9.0% | 5.5% | n/a | 41.6% |
| combat_gate_test.jsonl | 6,848 | 97.8% | n/a | 95.8% | 96.0% | 96.9% | 96.4% |

The `gate_b5_*_test.jsonl` files matched by the task's glob are RESPONSE
files (model outputs, no prompts) — nothing to audit there; the prompt-side
gate corpora are the three above.

Reading: the low board coverage in the two priority-gate corpora is the
production planner's deliberate for_planner trim (long-resident permanents
render flags, not text — real entry turns are known there). Whether that trim
is still right given the role->action transfer argument is a PRODUCT prompt
decision (coach.py), deliberately not changed here; flagged for the owner.

## 6. Verification

- 232 tests pass: full touched-module batch (`test_parse_magezero_log`,
  `test_magezero_combat_micro`, `test_magezero_bridge_leaks`,
  `test_run_wp3_pipeline`, `test_magezero_combat`, `test_magezero_filters`,
  `test_battlefield_land_oracle`, `test_magezero_card_map`) + 26 new in
  `tests/test_oracle_coverage.py` (parser strips, oracle attachment,
  opp-never-attacking rule, [NO TARGETS] strip + count, audit
  classification incl. {oT}/{T} dialect match, floor trips at exit 43,
  floor-off default, no-mentions-with-floor fails closed).
- Pipeline end-to-end on mz_train_smoke.log (201 MB): 32s, leak scan clean,
  pass-rate tripwire 40.0% after downsample (43.1% raw — same as the
  committed run), rendered records eyeballed against the raw log.
