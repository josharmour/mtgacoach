# CI-PERF — slow-board latency repair

Queue item: `CI-PERF` ("Repair both slow-board CI performance failures").
Base commit: `c31000589d5a0f38680855ac07c3f16580635694` (master `34ee704` at the
time of the work). Worktree: `/tmp/wt-ciperf` (detached at `34ee704`).

## Symptom being repaired

`tests/test_line_search.py::test_the_slowest_real_boards_stay_within_the_latency_target`
failed on Linux CI (run 41402478076) with `SLOW_212608` at 491.433 ms and
`SLOW_202111` at 493.853 ms against the unchanged 480 ms bound
(`latency_bound(80)` × 6 under `CI`). The bound, the search budgets and every
assertion in that test are unchanged by this work.

## What changed

All three files are hot paths reached from one `board_assessment.assess()` call:
`assess` → `line_search.search_lines` → `Moves.finish`/`their_attack` →
`Moves.combat_detail` → `board_assessment._combat_life` → `combat_solver._search_blocks`.

1. `src/arenamcp/combat_solver.py` — the block search no longer rebuilds work it
   already has:
   - per-search content keys (`_fight_key`) are built once per body instead of
     once per fight (`keys`), and material/death-trigger swings and hit flags are
     likewise cached per body (`mats`, `swings`, `flags`);
   - every assignment starts from a precomputed "nobody blocks" baseline per
     attacker and adds only the deltas of the attackers a choice actually
     touches, instead of re-summing all attackers for all ~341k choices;
   - the per-choice scratch state is reused (`buckets`, `masks`, `touched`) and
     the menace check is skipped entirely when no attacker has menace;
   - the wall-clock backstop is checked every 256 choices against a local
     `time.monotonic` binding.
2. `src/arenamcp/line_search_moves.py` — `Moves.combat_detail` builds its memo key
   through a new `Moves._protection_part`, which returns `()` when no body in the
   fight can be affected by protection at all (no protection quality and no
   unpreventable damage). Such fights would all key to the plain default, so the
   tuple only split the memo across fights that resolve identically.
3. `src/arenamcp/combat_protection.py` — new `protection_matters()` plus a
   "plain" fast path in `protection_signature()`: a body with no protection
   quality and no unpreventable damage can only change its key through its color
   fields, so the other ten field reads are skipped. The fast path also takes the
   slow path for bodies whose oracle text says damage can't be prevented, because
   `damage_is_unpreventable()` reads those printed lines.

No fixture-specific shortcut, no budget change, no assertion change: the node and
combat counts of both slow boards are byte-identical before and after.

## Matched before/after profiling

Same machine, same interpreter (`.venv_mac_311`, Python 3.11.15), same script
(`cpu_ms(run, times=7)`: fastest of seven process-CPU measurements with GC off,
cache cleared before each run). Baseline is a clean worktree at `c310005`;
patched is this worktree. Full output: [bench.txt](bench.txt).

| board | baseline (3 runs) | patched (3 runs) | change |
| --- | --- | --- | --- |
| SLOW_212608 | 103.2 / 103.3 / 103.5 ms | 87.6 / 87.1 / 86.7 ms | −15.7 % |
| SLOW_202111 | 101.5 / 102.3 / 101.9 ms | 87.4 / 88.0 / 86.9 ms | −14.2 % |

Payload check — identical search payload on both sides:

```
baseline SLOW_212608: nodes=90 combats=396 bounded=True truncated=False
patched  SLOW_212608: nodes=90 combats=396 bounded=True truncated=False
baseline SLOW_202111: nodes=185 combats=310 bounded=True truncated=False
patched  SLOW_202111: nodes=185 combats=310 bounded=True truncated=False
```

Where the time went (cProfile of one patched `SLOW_212608` assess, warm cache):
52 % of the call tree is `_search_blocks`, and inside it the per-choice loop,
fight resolution and fight-key construction were the three costs this change
removes or amortises; protection key construction was ~6 % before the fast path.

## Behaviour-preservation checks

- **Differential block search** ([differential-block-search.txt](differential-block-search.txt)):
  every `_search_blocks` call made by an `assess()` of all 29 strategic boards is
  replayed against the unmodified `c310005` implementation and compared on
  rounded score *and* evaluated-choice count — **2,771 calls, 0 mismatches**.
  This is what caught an earlier draft whose menace check broke out of the
  assignment loop early (125 mismatched calls on BRAWL_T7); the shipped version
  assigns every blocker first and then tests menace over the touched attackers,
  matching the original semantics exactly.
- **Mutation checks**: `tests/test_combat_memo_invalidation_20261101.py` passes,
  including the parametrised matrix asserting every field read by
  `protection_key` is present in `protection_signature`, so an in-place refresh of
  a body or its nested `_card` still invalidates both memos.
- **Focused regressions** ([focused-tests.txt](focused-tests.txt)): **501 passed**
  — the slow-board latency test plus board assessment, line search, block search,
  block assignment completeness, deadline, big-board, trick-risk, blocker caps,
  color protection (#273), combat loss (#281), cant-be-blocked, damage doubler,
  death drain and blocker lifelink (#284) suites.
- **Lint**: `ruff check` and `ruff format --check` clean on all three files.

## Blocker — no Linux CI receipt

Acceptance criterion 3 requires a real Linux CI receipt to claim CI is green.
This session is not permitted to push or deploy, so that receipt cannot be
produced here; local timings are not a substitute. Extrapolating from the failing
run's own ratio (491.4 ms CI / 103.3 ms local ≈ 4.76×), the patched local numbers
predict ≈415 ms for SLOW_212608 and ≈424 ms for SLOW_202111 against the unchanged
480 ms bound — roughly a 12–14 % margin rather than the previous failure.

The item therefore stays at `merged_awaiting_review`: merged into local master,
not independently verified, and still needing a pushed run to confirm green.
