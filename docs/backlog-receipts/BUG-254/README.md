# BUG-254 — X values and activated/cast costs must not exceed the usable payment capacity

Verdict: **no production change needed; the merged counting controls answer this
ID.** Both reported boards are closed on the current path. The model never sees
an unpayable X because `filter_play_options` withholds every numeric
casting-time option above `affordable_x`, and `affordable_x` now prices the
board exactly: counted subtype mana (`{T}: Add {C} for each Halfling you
control`) is added once per ready source, a Channel self-discount is stripped
before the "costs less" abstention, and a fetch land is excluded from a cast
already in progress. Replayed against the two recorded boards the ceiling is 7
for Goldvein Hydra on 8 mana (the reported pick was X=10) and 0 for Nature's
Rhythm on 2 mana (the reported pick was X=1). What was missing was a regression
that pins those two boards through the live withholding path. Test-only change:
`tests/test_mana_recurrence_254.py`. No source commit; merged to local master as
`712db15` (source commit `6cddacc`).

## Symptom and evidence

Queue row 16: "X values or activated/cast costs exceed the usable payment
capacity." Acceptance: use X plus fixed and colored/restricted costs, tapped/sick
sources and actual offered payment capacity; accept payable alternatives and
never invent mana; inspect current master and existing tests before editing and
record a narrow reproduction or a clearly bounded remaining gap.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:413` — `| #254 | PARTIAL | P1 / mana
  affordability |`. PARTIAL, not OPEN: the triage already treats the counting
  repairs as landed and asks for current-path closure.
* The queue's first pointer, `../autopilot-improvements.md`, is not present in
  this checkout (the parent directory holds unrelated projects), so the archived
  original table row cannot be replayed from this clone.
* The third pointer, `backlog-evidence-2026-11-01.md: #254`, resolves to nothing:
  that document keeps locator rows for other IDs and states that many records
  have no row at all. Recorded here rather than treated as a missing trace.

Controls already on master that this item must not duplicate:

| Control | Where | What it does |
|---|---|---|
| exact board total | `play_safety.py:195` `exact_mana_total` | base pool counts each ready producer once; returns `None` (guard off) whenever any source is conditional, costly or otherwise inexact |
| counted subtype mana | `play_safety.py:239` | `{T}: Add {C} for each <type> you control` adds one per ready source of that subtype; a changeling / "all creature types" board abstains instead of undercounting |
| Channel self-discount | `play_safety.py:209` | a `Channel — …` sentence is stripped before the "costs … less" abstention, so Boseiju's own discount does not disable the guard |
| fetch lands | `play_safety.py:195` region | a fetch land cannot pay for a spell already being cast, so it is not counted |
| X ceiling | `play_safety.py:279` `affordable_x` | `(total - cmc) // x_count`, so `{X}{G}` on 8 mana pays X=7 and `{X}{G}{G}` on 2 mana pays X=0 |
| withholding | `play_safety.py:1834` `filter_play_options` → `x_choice_reason` (`309`) | every numeric casting-time option above the ceiling is dropped with a log line; payable alternatives stay |
| fixed activation cost | `activation_payment.py:13` `activation_payment_reason`, used at `play_safety.py:1807` | an activation whose fixed cost exceeds the exact total is withheld (the Dalkovan Encampment case, pinned by `tests/test_activation_payment_254.py`) |

## Reproduction

Both boards are rebuilt from the recorded games and pushed through the same
function the autopilot uses before it shows options to the model.

**match 566912c9 gsid335** — four untapped Forests, two The Notary Hobbits
(entered turn 8, turn 12), tapped Boseiju, Who Endures; Goldvein Hydra `{X}{G}`
on the stack:

```
exact_mana_total == 8      # 4 Forests + two Notary Hobbits x two Halflings each = 8;
                           # Boseiju is tapped and contributes nothing
affordable_x(hydra) == 7   # (8 - 1) // 1
kept X values == [1..7]    # X=10 withheld: "largest affordable X is 7"
```

**match 566912c9 gsid65** — two untapped Forests, untapped Misty Rainforest,
Kami of Bamboo Groves; Nature's Rhythm `{X}{G}{G}` on the stack:

```
exact_mana_total == 2      # fetch land excluded from a cast in progress
affordable_x(rhythm) == 0  # (2 - 2) // 1
kept X values == [0]       # X=1 withheld; X=0 is payable and stays offered
```

Mutation check (run against a patched copy of `play_safety.py`, not committed):
removing the Channel strip turns gsid335's total into `None`, and removing the
counted-mana branch does the same — either way the guard switches off and X=10
is offered again. The new tests therefore pin both mechanisms rather than just
the arithmetic.

## Tests run

```
$ .venv_mac_311/bin/python -m pytest tests/test_mana_recurrence_254.py \
    tests/test_x_spell_affordability.py tests/test_counted_mana_backlog.py \
    tests/test_activation_payment_254.py -q
40 passed in 6.99s

$ .venv_mac_311/bin/python -m ruff check tests/test_mana_recurrence_254.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_mana_recurrence_254.py
1 file already formatted
```

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* An **activated ability whose cost contains {X}** is still uncapped:
  `affordable_x` reads the permanent's printed `mana_cost`, which has no `{X}`,
  so it returns `None` and the numeric option passes. No reported case has that
  shape (the reported activation, Dalkovan Encampment, has a fixed cost and is
  covered), so this is recorded as a bounded gap rather than fixed here.
* These are deterministic replays of the recorded boards on current master, not
  live qualification against api.mtgacoach.com.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
