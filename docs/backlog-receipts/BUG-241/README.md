# BUG-241 — a timed-out reversal re-check preserved a fallback pass

Verdict: **already fixed on local master; this item adds the missing end-to-end
regression and changes no production code.** The two controls that answer #241
are `DecisionMemory` provenance (`70d9e2f`) and the board signature (`eb72e38`),
both merged before this queue started. New file:
`tests/test_reversal_recheck_241.py`.

## Symptom and evidence

Queue row: "A timed-out reversal re-check preserves a fallback pass over an
otherwise usable action."

`docs/latency-222-triage-20261101.md` (row #241, controls R/T/P) records the
mechanism and the evidence commits `4106994f`, `c7b1185c`, `0a0b80ab`: while the
model server was failing, the planner's deterministic fallback passed; the next
decision on the same board chose a legal action; the reversal re-check ran out
its budget; `_reversal_check_failed` then kept the *fallback* pass instead of
the fresh choice. The triage row also states what was already in place —
"Outage fallback pass was treated as strategic hold during a timed-out reversal
recheck; already addressed by provenance and board signature controls" — and
what was still missing: a replay of that card state rather than mechanism tests.

`docs/autopilot-improvements.md` (the queue's `../autopilot-improvements.md`
evidence path) is not in this checkout, so the retained trace is the triage row
plus the two controls' own commits.

## Mechanism on current master

`ActionPlanner.plan_decision_options` asks `DecisionMemory.reversal(...)` whether
the model's fresh pick reverses an earlier pass this turn. If it does, it sends a
high-effort re-check; if that re-check fails, `_reversal_check_failed` keeps the
earlier pass unless the fresh pick is line-best or lethal.

`DecisionMemory.reversal` (`src/arenamcp/decision_memory.py`) now refuses two
kinds of earlier pass:

* `record.model_unavailable` — a pass recorded while the model call failed
  (`last_llm_failure == "unavailable"`, which includes timeouts and circuit-open)
  is remembered for the log but is never a strategic commitment, so no re-check
  is sent at all;
* `record.signature != board_signature(state)` — a pass only counts on the board
  it was made on.

Both are exactly the two failure modes in the symptom: an outage fallback pass
being treated as a hold, and a stale board being treated as unchanged.

## Acceptance map

| Acceptance clause | Where it lives | Pinned by |
|---|---|---|
| Replay completed legal low-effort action versus timed-out re-check | outage pass recorded with `model_unavailable=True`, then a fresh legal land play at low effort | `test_an_outage_pass_is_not_a_reversal_and_the_fresh_choice_stands` |
| Genuine model hold and changed state | same board with a genuine hold still gets its high-effort re-check; a new creature on the board is not a reversal | `test_a_genuine_model_hold_still_gets_the_recheck`, `test_a_changed_board_is_not_a_reversal` |
| Keep a fresh valid choice without renewing the deadline | the re-check's budget is clamped to the first call's `decision_deadline`, so it gets only what is left | `test_the_recheck_cannot_renew_the_first_calls_deadline` |
| Inspect current master before patching | both controls read on master before writing any test; no source change was needed | this receipt |

## What changed

`tests/test_reversal_recheck_241.py` only (new). No production file was touched:
the fix for this ID is already on master, and re-implementing it would have been
a parallel fix for an assumed-fixed item.

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_reversal_recheck_241.py -q
4 passed in 8.07s

$ .venv_mac_311/bin/python -m pytest tests/test_reversal_recheck_241.py \
    tests/test_decision_memory.py tests/test_reversal_recheck_failure_20261019.py \
    tests/test_optional_reservation_306.py -q
33 passed in 6.21s

$ .venv_mac_311/bin/python -m pytest tests/test_burn_timing_20261019.py -q
29 passed in 7.20s

$ .venv_mac_311/bin/python -m ruff check tests/test_reversal_recheck_241.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_reversal_recheck_241.py
1 file already formatted
```

## The new regression bites

With pre-fix behaviour restored (a throwaway pytest plugin that clears
`model_unavailable` and equalises every record's signature, i.e. every earlier
pass looks like a genuine hold on this board):

```
FAILED test_an_outage_pass_is_not_a_reversal_and_the_fresh_choice_stands
FAILED test_a_changed_board_is_not_a_reversal
2 failed, 2 passed in 8.64s
```

The failure output is the live symptom reproduced end to end:

```
typed-decision: ['idx:4'] reverses an earlier pass this turn (Holding Proft: ...); asking again at high effort
typed-decision: reversal re-check failed (LLM streaming time budget exhausted); keeping the earlier pass,
    not Play Land: Island (no line tag) (neither the line's best nor lethal) [model: The Island is our land drop]
typed-decision choice ['pass']: Keeping the earlier pass this turn: Holding Proft: ...
```

The fresh legal land play is discarded for the outage fallback pass — exactly
what #241 reports.

## Remaining limits

* No replay of the original Gaea's Gift window with its original memory
  provenance; these are deterministic reproductions of the logged mechanism on
  current master, not that exact card state.
* The provenance gate keys off `last_llm_failure == "unavailable"`. A malformed
  answer (`bad_answer`) still counts as a strategic hold, which is deliberate:
  only a model-server failure makes a pass non-strategic.
* Total decision-chain elapsed time for #241 remains unqualified (triage row).
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item
can move past `mergedawaitingreview`. Nothing here was verified by its author.
