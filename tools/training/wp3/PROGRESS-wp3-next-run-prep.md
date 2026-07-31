# WP3: Next-curriculum-run prep — Progress (MORNING PRIORITY items 3+4)

Branch: `wp3-next-run-prep`. Config + prototype + validation only — NO
training was run, NO live-run file was touched (the live run's
`configs/run.yml` in the magezero repo is unmodified; `run_diverse.yml` there
is also unmodified — deploy is a deliberate one-file copy, below).

## Deliverables

- [x] `tools/training/wp3/next_run_diverse.yml` — finalized, tracked config
      for the NEXT curriculum run
- [x] `tools/training/wp3/validate_run_config.py` — fail-closed permanent-
      holdout validator (HighNoonControl, BGRoots, BWBats)
- [x] `tests/test_validate_run_config.py` — 20 tests, wires the contract
- [x] `tools/training/wp3/build_mixture_manifest.py` — breadth-vs-depth
      mixture manifest PROTOTYPE (manifest only; refuses to write training
      data without `--emit`)
- [x] `tests/test_build_mixture_manifest.py` — 16 tests (fixtures only,
      no dependence on gitignored corpora)
- [x] Demo manifest at `--human-weight 0.2`:
      `tools/training/data/mixture_manifest_w020.json` (gitignored data dir;
      numbers reproduced below)

Tests: `pytest tests/test_validate_run_config.py tests/test_build_mixture_manifest.py -q`
→ **36 passed** (venv-train Python 3.12).

## 1. Diversity run config (item 3)

`tools/training/wp3/next_run_diverse.yml` finalizes the magezero-repo
`configs/run_diverse.yml` draft:

- **Opponents**: the 5 retained Standard-Mono baselines + the #438
  greedy-optimal five, exact `.dck` basenames verified against
  `deck_inventory.json`: `Oathbreaker_UR`, `GBLegends`, `EVG_Elves`,
  `EVG_Goblins`, `Mind(MindvsMight)`. Pool 83 → 235 distinct cards (2.8x,
  measured in #438).
- **Scale**: `games_per_gen: 550`, `generations: 6` — the draft still had the
  pre-bump 200; finalized to the scale the current run's 550-bump paid for.
- **No holdout decks** (validated, see below).
- `start_from_version: null` left as an explicit owner call at deploy time
  (fresh bootstrap vs resume from the current run's final checkpoint).

**Deploy step (the magezero repo is separate; run only BETWEEN curriculum
runs, never mid-run):**

```bash
cp tools/training/wp3/next_run_diverse.yml \
   /home/joshu/repos/magezero/configs/run_diverse.yml
```

Difference vs the current magezero draft: games_per_gen 200 → 550, plus the
holdout-contract provenance header. Opponent set is unchanged from the draft
(it already matched #438).

## 2. Permanent-holdout validator (the unseen-deck gate contract)

`validate_run_config.py` FAILS (exit 2) if any run config lists
**HighNoonControl, BGRoots, or BWBats** as `deck:` (primary) or any
`opponents[].deck`. Fail closed: unreadable file, YAML error, non-mapping top
level, missing `deck`/`opponents`, or an opponent entry without a `deck`
field are all violations — never skips. Matching is normalized
(case/whitespace/`.dck` suffix) so filename variants cannot dodge it.

- No-args default validates the tracked wp3 configs
  (`next_run_diverse.yml`, `run_baseline.yml`).
- `--configs-dir /home/joshu/repos/magezero/configs` scans the live configs
  (known non-run configs `curriculum.yml`/`game.yml`/`game_smoke.yml`
  excluded by name).

Measured run against the live magezero configs dir (2026-07-31):

```
[ok] /home/joshu/repos/magezero/configs/run.yml
[ok] /home/joshu/repos/magezero/configs/run_diverse.yml
OK: 2 config(s) clean of holdout decks (BGRoots, BWBats, HighNoonControl).
```

Wired as `tests/test_validate_run_config.py` (20 tests): the contract set is
pinned, every holdout is tested as primary and as opponent, normalization
variants are tested, all fail-closed branches are tested, and the tracked
`next_run_diverse.yml` is asserted clean + carrying the #438 five at
550×6.

## 3. Breadth-vs-depth mixture prototype (item 4 — owner decides weights)

### Corpora located on disk (blackwell, `tools/training/data/`, gitignored)

All share the record shape
`{system, user, response, source, attribution, kind, meta}` with response
`{"actions": [{"pick": N}]}`:

| file | size | records | shape | source |
|---|---|---|---|---|
| `wp3_v2_priority/train.jsonl` | 48 MB | 4,808 | 100% production (`Legal:`) | magezero_bridge (won games, priority) |
| `wp3_v2_combat/train.jsonl` | 61 MB | — | production | magezero_bridge + combat micro |
| `play_decisions.jsonl` | 223 MB | — | Candidate: (synthetic menus) | 17lands-replay_data |
| `play_decisions_nontrivial.jsonl` | 141 MB | 13,671 | Candidate: | 17lands, trivial-only-menu records dropped |
| `play_decisions_cast.jsonl` | 58 MB | — | Candidate: | 17lands, cast-only |
| `play_decisions_bridge.jsonl` | 12 MB | 988 | 100% production (real MTGA menus) | manasight/replay ground truth |
| `play_decisions_mixed.jsonl` | 153 MB | 14,659 | 6.74% production | bridge 988 + nontrivial 13,671 |
| `stage0_prompts_{EOE,OTJ,TDM,WOE}.jsonl`, `stage0_turnaction_*`, `stage0v2_*` | ~0.8 GB | — | pre-WP3 Phase-0 prompt shapes | 17lands |

The demo uses `wp3_v2_priority/train.jsonl` (depth) ×
`play_decisions_mixed.jsonl` (breadth; the blunder-filtered human mix).

### Prototype semantics

`build_mixture_manifest.py --magezero-corpus … --human-corpus …
--human-weight W --out manifest.json`:

- Keeps ALL magezero records (depth anchor); samples human records
  deterministically (`--seed`) so human/(human+magezero) = W; capping by
  availability is reported, never silent.
- **Manifest only** — no training data written without `--emit` (and `--emit`
  refuses to overwrite, plus optional `--min-production-share` fail-closed
  floor per the rl-pipeline-fix mixture-manifest requirement).
- Production-shape heuristic is the repo's `Legal:` heuristic from
  `build_bridge_dataset.py`: user contains `"Legal: (pick by number)"` and
  not `"Candidate:"`.
- Distinct-card coverage validated against the MTGJSON name index
  (`~/.arenamcp/mtgjson/name_index.json`, 34,633 names + DFC-face aliases);
  unresolvable tokens are counted and top-20 reported (drop-and-count),
  never silently included.
- Trivial-policy floors computed per source (same reflex families as
  `gate_play_decisions.py` G6); records with `meta.gold_equivalent_picks`
  (the 988 bridge records) use it, others use the single gold pick — a lower
  bound, stated in the manifest.

### Demo manifest — MEASURED, `--human-weight 0.2`, seed 20260731 (4.0 s)

Mixture: **4,808 magezero + 1,202 sampled human = 6,010 records**, realized
human share 0.2000 (not capped; 14,659 available).

| metric | magezero (wp3_v2_priority/train) | human (play_decisions_mixed) | mixture |
|---|---|---|---|
| records | 4,808 (0 drops) | 14,659 (0 drops) | 6,010 |
| production-shape fraction | 1.0000 | 0.0674 (988/14,659) | **0.8128** (4,885/6,010) |
| distinct cards (vocab-validated) | 66 | 1,721 | **1,198** (union; 1,132 new vs magezero) |
| menu size median/mean | 3 / 3.18 | 7 / 7.17 | — |
| gold-action class histogram | pass 2,026 · cast 1,389 · play 882 · activate 511 | play_land 13,397 · cast 1,158 · pass 76 · activate 24 · other 4 | — |
| best trivial policy | always_land_else_first **0.7633** | always_land_else_first **0.4424** | — |
| unrecognized card tokens (distinct / occurrences) | 10 / 610 | 1,468 / 6,009 | — |

Full-union ceiling (all human records, weight → 1): 1,753 distinct cards.
Human-sampled subset alone at W=0.2 covers 1,150 of the corpus's 1,721.

Notable, for the owner's weight decision:

1. **Breadth is cheap**: at W=0.2 the card pool grows 66 → 1,198 (18x) while
   production-shape fraction stays at 0.81.
2. **The human slice is land-drop heavy**: 91.4% of its gold labels are
   `Play Land:` picks (13,397/14,659) — the mixed corpus is main-1-window
   data. This is the 69%-land-drop lesson shape; a taxonomy/land-drop cap
   knob on the human slice is probably wanted before any training build.
3. **The magezero corpus has its own reflex ceiling**: always_land_else_first
   scores 0.7633 on it, and always_first_option scores 0.8032. Verified
   against raw records: gold pick is menu entry 1 in 3,862/4,808 records, and
   in 2,026 of those entry 1 is literally "Pass" (priority windows where the
   agent passes render Pass first). Any G6-style floor on a mixture
   containing it must use these measured numbers.
4. Remaining unrecognized tokens are real non-cards (Clue/Spirit/Lander
   tokens) plus XMage short-name renderings ("Malcolm", "Skrelv"); DFC face
   aliasing, `x2`, P/T and `,attacking` suffixes are already handled
   (unrecognized occurrences fell 1,496 → 610 on magezero after those fixes).

**No weight is recommended here** — that is the owner's call, per the work
order. The tool makes any candidate weight's numbers reproducible in ~4 s.
