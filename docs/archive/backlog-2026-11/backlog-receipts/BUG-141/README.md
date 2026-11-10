# BUG-141 — game plan arrives too late / reforms cancel each other

Queue item: `BUG-141` ("Game plan"). Base commit: `636459f` (local master after
CI-PERF). Worktree: `/tmp/wt-141-base` (detached at `636459f`) for the baseline,
`/tmp/wt-141-patched` (same tree plus this change) for the patched side.

## Symptom being repaired

The archived recurrences (`autopilot-improvements.md`, matches `c667eb4d` and
`4d999502`) show the first plan landing, then **no usable plan for 120–170 s**:
each reform outlives the turn it was asked for, is superseded at the next turn
boundary, and its replacement is another full-budget call that is superseded
again. Three attempts and two cancellations produced a plan for a turn that had
already ended.

`b352b8a` already bounded the *first* plan on hosted models (effort `none`,
20 s budget). The remaining gap was every reform **after a cancellation**: it
still ran at full effort with the full 75 s budget, so on a hosted model it
could never land inside its own turn.

## Reproduction

[replay_timeline.py](replay_timeline.py) drives a real `GamePlanManager` against
a hosted-model backend (`model = "current"`) through turns 3–22 with two reform
triggers per turn, alternating active player, and a new opposing creature on
turns 7/11/15/19 so the threat-supersede path is exercised too. Time is scaled
~100× so one simulated second is ~100 real seconds: turn 0.12 s (~12 s),
cooldown 0.15 s (~15 s), first-plan budget 0.20 s (~20 s), patience 0.45 s
(~45 s), full budget 0.75 s (~75 s), and a hosted `low`/`high` answer takes
0.45 s (~45 s) while `none` takes 0.20 s (~20 s).

Both sides run from local-disk worktrees with the same interpreter
(`.venv_mac_311`, Python 3.11.15); full output in
[replay-before.txt](replay-before.txt) and [replay-after.txt](replay-after.txt).

| run | baseline `636459f` | patched |
| --- | --- | --- |
| 1 | plans landed `(3→4) (10→13) (18→21)`; 8 attempts, 4 cancels | `(3→4) (8→9) (12→13) (16→17) (20→21)`; 10 attempts, 4 cancels |
| 2 | only `(3→4)`; 7 attempts, **5 cancels**, ends mid-reform | same as run 1 |
| 3 | `(3→4) (10→13)`; 8 attempts, 5 cancels, ends mid-reform | same as run 1 |

Baseline call sequence (run 1): one cheap first plan, then seven full-budget
calls — `high`, `high`, `low`, `high`, `high`, `low`, `high`. Every one of them
outlived its turn; two landed three turns late and the rest were discarded.
Patched call sequence: `none`, `high`, `none`, `high`, … — each cancelled deep
reform is replaced by a bounded call that lands on the next turn.

What the replay shows, in the terms of the acceptance criterion:

- **A usable bounded plan arrives.** Baseline landed 1–3 plans in 20 turns and
  twice ended with a reform still in flight; patched lands a plan every four
  turns, each one turn after it was requested (worst staleness 3 turns → 1).
- **Urgent decisions retain their deadlines.** The yield path is untouched:
  `yield_for_decision()` still cancels a routine reform inside its grace window,
  keeps running past it for a newer threat or a planned attacker that is gone,
  and never cancels the first plan.
- **Stale plans never execute.** Unchanged: prompts flag lines that predate an
  opponent board change, and a threat re-plan still supersedes on their turn.
  The fix only changes *which* reform is allowed to finish, never whether a
  stale plan is used as if it were current.

## What changed (`src/arenamcp/game_plan.py`)

1. **Recovery reforms are bounded.** `_reform` now computes
   `fast_recovery = hosted and (self._plan is None or self._consecutive_supersedes >= 1)`
   and uses effort `none` with `min(self._timeout, _INITIAL_HOSTED_PLAN_TIMEOUT_S)`
   for it — the same bounded mode `b352b8a` gave the first plan. The JSON retry
   and mana-plan repair share that deadline and effort. A reform that follows no
   cancellation keeps full effort and budget, so "later refreshes think more
   deeply" (`b352b8a`) still holds.
2. **A late answer after one supersede is kept.** The stale-turn rule now runs
   on after one supersede in a row (`_keep_late_after_supersede`) instead of
   only after two, and the apply path keeps that late answer rather than
   discarding it. The threat rule (`_keep_late_plan`: two supersedes, or one in
   the late game with a sparse hand) is unchanged.
3. **Low-effort selection follows the same threshold** (`_low_effort_reform`
   uses one supersede in a row), so non-hosted backends also stop paying for a
   second full-effort attempt after a cancellation.

Why not make *every* hosted reform bounded: `b352b8a` deliberately seeds a cheap
first plan and then lets normal refreshes think more deeply. This change keeps
that intent and only bounds the attempt that replaces a cancelled one — the one
that provably cannot land inside its turn at full budget.

## Behaviour-preservation checks

- **Focused regressions** ([focused-tests.txt](focused-tests.txt)): **116 passed**
  for the seven game-plan files, then **473 passed** for the wider focused batch
  (game-plan, plan card/ledger/lines/mana-repair/dependencies, strategic plan,
  separate strategy planning, latency budget #222 follow-up, integrated repair
  budget, critical-decision budget, action-plan cancellation #222, LLM call
  discipline, proxy call classes/thinking control, startup resume, terminal during
  planning, plan availability feedback, hold-plan chip #228, deck strategy,
  unusable answers #214).
- **New regression** in `tests/test_autopilot_fixes_20261019.py`:
  `test_reform_after_a_cancelled_reform_is_bounded_and_kept()` asserts an
  uncancelled reform keeps full effort/budget, then a cancelled reform's
  replacement runs at `none` with the initial-plan budget and its late answer is
  kept. The existing two-supersede semantics test is preserved as an early-game
  variant plus a sparse-late-hand variant.
- **Lint**: `ruff check` and `ruff format --check` clean on both changed files;
  `py_compile` clean.

## Limits of this receipt

- No live qualification: this is a scaled-time replay of the manager's own state
  machine, not a real match against api.mtgacoach.com.
- One deep refresh per cycle is still cancelled at its turn boundary — by design,
  because a stale reform must not block the new turn's plan. The fix makes its
  replacement cheap enough to land; it does not remove that cancellation.
- No Linux CI receipt: this session may not push or deploy. The item stays at
  `merged_awaiting_review`.
