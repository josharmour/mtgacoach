# PROGRESS — wp3-combat-adapter2 (MageZero COMBAT micro-decision adapter)

Date: 2026-07-30. Source log: /home/joshu/mz_train_smoke.log (210MB, 594 games,
6 XMage threads). Context: #453 found 89/90 fabricated combat labels in the old
adapter (build_magezero_combat.py, now fail-closed to zero); the retraction in
rl-pipeline-fix.md ("COMBAT IS BLOCKED UPSTREAM — RETRACTED") identified the
real per-creature emitters this adapter consumes.

## What was built

- `tools/training/magezero_combat_micro.py` — streaming scanner + renderer.
  - Attack micro-decisions: `base choose use attack with: <name>?` opener ->
    binary `DECLARE_ATTACKERS*pool= actions: [false ...] [true ...]` ->
    `use attack with: <name>?: true|false` resolution, all paired by thread
    tag with a 12 same-thread-line pool window. Kind `attack_commit`.
  - Block micro-decisions: `choose which creature to block for <blocker>`
    opener gates the `DECLARE_BLOCKERS*pool=` and the `Targeting <attacker>` /
    `Targeting Stop Choosing` resolution. A bare `Targeting` line is NEVER
    a block (4,296 spell-targeting lines correctly bypassed). Kind
    `block_assign`.
  - Board/hand context from the thread's own battlefield state machine
    (same grammar as parse_magezero_log, incl. the #430 emitter-attribution
    rules); a record requires a complete MCTS printBattlefieldScore block on
    the same thread, same game, within 800 same-thread lines — else dropped
    and counted.
  - `verify_accounting()` asserts the closed ledger (every seen line is
    emitted or counted) and runs on every scan, including in the pipeline.
  - Renderer emits production-shaped records (AUTOPILOT_SYSTEM_PROMPT +
    gate_play_decisions.build_user_message, `combat_attackers` /
    `combat_blockers` triggers, `{"pick": N}` answers). MCTS counts/scores
    stay in `meta`; an assert plus the pipeline leak scan enforce that no
    `score:`/`count:` text reaches a prompt. OPP cards are never marked
    is_attacking (MageZero has no P/T; the production formatter would run its
    block solver on fabricated 0/0s and could restate a no-block answer —
    a label leak). The attacker roster reaches the model via the menu rows,
    which are log-authoritative.
- `tools/training/run_wp3_pipeline.py` — `--include-combat` flag, default
  OFF (default behavior unchanged; a combat row appearing with the flag off
  is counted, not rendered). Combat rows are filtered separately (the
  pass-rate tripwire stays priority-only as pre-registered), split together
  with priority rows by game (cross-kind game-level leak safety), rendered,
  leak-scanned, and reported in manifest + REPORT.md.
- Tests: `tests/test_magezero_combat_micro.py` (27 tests) on
  `tools/training/wp3/fixtures/fixture_combat_interleaved.log` — a REAL,
  unmodified 261-line excerpt of the smoke log with all 6 threads
  interleaved (13/13 sampled lines verified verbatim against the source) —
  plus synthetic orphan fixtures proving fail-closed drops (orphan
  resolution, opener-name mismatch, missing/previous-game board context,
  cross-thread pool, intervening priority decision, spell-targeting
  bypass, chosen-not-in-pool).
- `tools/training/wp3/sample_combat_records.py` — eyeball helper (renders
  sampled records next to the raw log lines their provenance points at).

## Measured numbers (full smoke log)

Scanner (standalone, `--report`):

- 3,213 attack_commit + 788 block_assign = 4,001 rows.
- Accounting ledger (all identities asserted):
  - seen_attack_openers 3,352; seen_attack_resolutions 3,352;
    seen_attack_pools_binary 3,289; emitted_attack 3,213;
    drop_attack_resolution_without_pool 139
    (pool abandoned: 41 at_priority_decision, 29 by_noncombat_pool,
    4 by_block_opener, 2 replaced_by_new_pool = 76; plus 63 searches that
    never printed a binary pool, see reconciliation).
  - seen_block_openers 1,088; seen_targeting_total 5,384 =
    4,296 nonblock (spell targeting) + 1,088 block-gated;
    seen_block_pools_gated 788; emitted_block 788;
    drop_block_targeting_without_pool 300.
  - Zero drops for missing/stale board context (MCTS prints the battlefield
    at every search; context gap p50/p90/p99/max = 45/111/223/578
    same-thread lines, window 800).

Class histograms (raw scan) vs pre-registered trivial-policy floors:

- attack records: {attack: 2,296, no_attack: 917} — attack share 71.5%.
- attack chains: 2,355 {all_in: 1,558, no_attack: 428, proper_subset: 369}
  — **all-in share 66.2% vs 38.6% floor: BREACHED.**
- block records: {no_block: 398, block: 390} — no-block share 50.5% vs 69%
  floor: not breached.
- Post-filter (won_only + dedupe, the corpus that trains): all-in 74.4%
  (still breached), no-block 51.1% (not breached). NOT rebalanced;
  `combat.floor_breach: true` recorded in the manifest, loud banner in the
  report. Gate decision is the owner's.

Pipeline end-to-end (`--include-combat --balance downsample-pass`):

- 21,609 priority decisions + 4,001 combat rows scanned; outcome join
  4,001/4,001 by game_id (game numbering is shared with the priority parser
  by construction).
- Combat post-filter: 1,478 (2,505 dropped by won_only, 18 dedupe, 0
  single-option). All 1,478 rendered: 1,304 attack_commit + 174
  block_assign, distributed train/val/test by game together with priority
  rows. Leak scan: 0 violations. Manifest carries the full accounting,
  filter counts, both histograms, floor_breach, and per-split rendered
  combat counts.
- Without `--balance downsample-pass` the pipeline aborts on the
  pre-existing PRIORITY pass-rate tripwire (43.8% > 40%) — unrelated to
  combat (combat rows never enter that computation).

## Reconciliation vs raw grep (every gap explained)

- `grep -ac "attack with"` = 6,705 = 3,352 openers + 3,352 resolutions + 1
  card-text mention ("Whenever you attack with one or more Lizards" inside a
  DECLARE_ATTACKERS2 pool line, L~..., thread-5 21:58:22) — verified by
  inverse grep.
- 3,352 resolutions -> 3,213 emitted + 139 dropped without a usable pool:
  76 pools were invalidated by an intervening decision-class event on the
  same thread (counted per reason above); the remaining 63 searches never
  printed a binary pool at all. Eyeballed instances show the MCTS reusing a
  cached tree ("required visits reached, ending search", "Ran 1
  simulations", COMPOSITE CHILDREN: [999] — single root child), so
  MCTSNode.bestChild has no alternatives to print. No pool = no MCTS
  stats = fail-closed drop.
- `grep -ac "choose which creature to block"` = 1,088 openers -> 788
  emitted + 300 dropped without a DECLARE_BLOCKERS pool. Measured on the
  raw log: 172 of the 300 had `possible targets: 1` (XMage auto-picks a
  forced choice without running a search — no pool exists); the other 128
  are the same cached-tree/no-bestChild-print pattern as the attacks
  (verified on raw excerpts, e.g. thread-5 L3894-3906).
- `Targeting` grep total 5,384: 1,088 attributed to gated block windows,
  4,296 correctly rejected as spell targeting.

## Manual record validation (requirement 8)

Rendered 5 attack + 5 block records (seed 42, sample_combat_records.py) and
checked each against the raw log lines in its provenance:

- All 10: opener/pool/resolution on the same thread, adjacent, creature name
  identical, pick matches the logged verdict/Targeting, and the pool argmax
  agrees with the resolution (incl. a declined attack: false 0.546 > true
  0.525 -> pick 2, and a chosen block: 0.571 > 0.533 -> pick 1).
- Board owner correct in all 10 (blocker under YOUR BOARD, attacker
  candidates under OPP BOARD / menu; block records set active_player=OPP).
- No label leak: no `score:`/`count:` text in any prompt; the answer is not
  recoverable from the prompt (both/all menu options are present by design —
  that IS the pick mechanism).
- One soft spot found and quantified: board context can be stale within the
  window — in one sampled block record the blocker was missing from the
  printed YOUR BOARD (cast after the last battlefield print). Rates on the
  full corpus: creature_on_board true for 3,209/3,213 attacks (99.9%) and
  750/788 blocks (95.2%); every record carries `provenance.creature_on_board`
  (and blocks `attackers_on_board`/`attackers_total`) so downstream can
  filter harder if desired. `possible_targets_matches_pool` true for
  634/788.

## Tests

- `tests/test_magezero_combat_micro.py`: 27/27 pass
  (`/home/joshu/venv-train/bin/python3 -m pytest`).
- Full suite (`PYTHONPATH=src:. pytest tests -q --ignore
  tests/test_draft_guidance_wiring.py`): 1,422 passed, 4 failed, 28
  skipped. The 4 failures are environment-only in modules this branch does
  not touch (missing `PIL` and `mcp` packages in venv-train; the FastMCP
  mocks are test-order dependent) — the same modules error at collection on
  a clean checkout of master in this venv. test_draft_guidance_wiring is
  excluded for the same missing-`mcp` reason.

## Not verified / known limits

- Chain-level "all_in" classification uses emitted rows only: a chain that
  lost a micro-decision to a fail-closed drop is classified from the
  surviving rows (drops are ~4% of resolutions, so the effect is small,
  but it is not zero).
- Board context freshness is bounded (800 same-thread lines + same-game),
  not exact: ~0.1% of attack and ~4.8% of block records name a creature not
  present in the printed board snapshot (flagged per-record, see above).
- Duplicate attacker names in a block pool cannot be mapped to instances
  (the log has names only); menus disambiguate with "#k" and `chosen` maps
  to the first occurrence, which is name-accurate. `duplicate_candidates`
  is set on those records.
- Outcome for combat rows joins the priority parser's calibrated outcome by
  game_id (4,001/4,001 joined on the smoke log); a hypothetical game with
  zero priority decisions would fall back to the scanner's life-inferred
  outcome.
