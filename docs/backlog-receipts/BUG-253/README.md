# BUG-253 — a schema-valid empty answer is not a plan, and a skipped call is not a failure

Verdict: **no production change needed; the merged controls answer this ID.**
The retained trace's "schema-shaped empty role" is a JSON object that parses and
carries a role with no plan content. On current master `_usable_plan` rejects it
exactly like an unparseable answer (`plan.is_empty()`), keeps the prior plan,
logs the rejected text, and records `no usable plan from the model` as the
reason — which then reaches both `plan_status()` and the decision prompt as
`PLAN NOT REFRESHED: … (no usable plan from the model)`. A call the background
lane never made (server down, circuit open, dropped/superseded) is classified
separately as `call not made (model server unavailable)` and is never retried,
and a reform whose turn has passed discards its answer instead of overwriting
the plan. The "initial plan is missing" half is bounded by the fast first-plan
path: while no plan exists every hosted attempt runs at effort `none` inside
`_INITIAL_HOSTED_PLAN_TIMEOUT_S` (20 s), so a missing first plan cannot escalate
into a 75 s deep call. What was missing was a regression that pins those four
answers and the reported reason. New test file only:
`tests/test_game_plan_empty_and_busy_253.py`. No source commit; merged to local
master as `43679e3` (source commit `813f252`).

## Symptom and evidence

Queue row 15: "Planning can return an empty role while the coaching loop stalls
or initial plan is missing." Acceptance: replay schema-valid empty output and
busy/degraded planning; retain a valid prior plan or bounded fallback, cancel
stale work and report the true reason; inspect current master and existing tests
before editing, and record a narrow reproduction or a clearly bounded remaining
gap.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md` line 412 — `| #253 | PARTIAL | Autopilot /
  LLM latency |`.
* `docs/latency-222-triage-20261101.md` line 143 — `| #253 | cd0b1978,
  e53b79f6: Long decision chains plus schema-shaped empty role and initial-plan
  lag; follow-up opinions need one shared budget. | T, F2, F3, F4, J, F5 |
  Execution f9b88c8 owns Arena-clock/chain deadline changes. Verify empty-plan
  rejection and first availability without conflating it with service TTFT. |`
  — i.e. this ID is explicitly *verification* work on top of merged controls,
  and the triage warns against reading first-token time as first-answer time.
* The queue's third evidence pointer, `backlog-evidence-2026-11-01.md: #253`,
  resolves to nothing: that document keeps 263 locator rows for other IDs and
  states that 87 records have no row at all. Recorded here rather than treated
  as a missing trace.
* The two cited commits (`cd0b1978`, `e53b79f6`) are not objects in this
  checkout (`git log -1 cd0b1978` → `unknown revision`), so the original table
  row cannot be replayed from this clone. The reproduction below therefore pins
  the *mechanism* the triage row names, not the archived transcript.

Controls already on master that this item must not duplicate:

| Control | Where | What it does |
|---|---|---|
| empty-plan rejection | `game_plan.py:5199` (`plan is None or plan.is_empty()`) | a schema-valid answer with no plan content is unusable, logged with its text, and recorded as `no usable plan from the model` |
| skip classification | `game_plan.py:157` `is_skipped_call_text`, used at `5177` | server-down / circuit-open / cancelled-dropped sentinels are "call not made", never "call failed", and are not retried |
| superseded discard | `game_plan.py:5171` | a reform whose cancel token fired discards its answer and keeps the prior plan |
| fast bounded first plan (#141/#223) | `game_plan.py:5020–5045` | while no plan exists (or after a supersede) a hosted attempt runs at effort `none` inside the 20 s initial budget |
| shared JSON-retry budget (#141/#223) | `game_plan.py:5056`, `5209` | one bounded retry for just the JSON, sharing the reform's deadline |
| stale-plan reporting | `game_plan.py:3177`, `4654`, `4220` | `_refresh_failed` becomes a `PLAN NOT REFRESHED: … (<why>)` line in the prompt and `last_outcome` in `plan_status()` |

## Mechanism on current master (reproduced)

Deterministic probe against a fake backend whose model name is `current` (the
shipped hosted model). Prior plan formed on turn 3, then a forced reform on turn
4 answers with `{"role": "aggressor", "role_reason": "we are ahead on board"}`
twice:

```
== A: schema-valid empty answer, prior plan exists ==
calls: [('none', 20.0), ('high', 75.0), ('low', 75.0)]
kept prior: True | outcome: no usable plan from the model | refresh_failed: (4, 'no usable plan from the model')
status: {'state': 'ready', 'last_outcome': 'no usable plan from the model'}
```

So the empty answer costs one bounded low-effort retry (not a third full call),
the prior plan survives, and both the status and the prompt name the real
reason. The same probe over the three skip sentinels:

```
== B: busy/degraded lane (skip sentinel) ==
  [BACKEND ERROR] model server unavailable (backend do   calls=2 kept=True outcome='call not made (model server unavailable)'
  [BACKEND ERROR] model server unavailable (circuit op   calls=2 kept=True outcome='call not made (model server unavailable)'
  [BACKEND ERROR] request cancelled (dropped)            calls=2 kept=True outcome='call not made (model server unavailable)'
```

No retry, prior plan kept, and the reason distinguishes "not made" from "failed".
With no plan yet formed:

```
== C: no plan yet (first availability) ==
plan: None | calls: [('none', 20.0), ('none', 20.0)]
status: {'state': 'none', 'last_outcome': 'no usable plan from the model'}
deep budget: 75.0 | initial hosted budget: 20.0
```

and a reform whose turn passed while the model was answering:

```
== D: superseded reform ==
calls: 2 | kept prior: True | outcome: ok | refresh_failed: None
```

The contrast that makes C meaningful — the same backend without a hosted model
name takes the deep path instead:

```
== E: contrast — non-hosted model takes the deep path ==
calls: [('low', 75.0), ('low', 75.0)]
```

## Mutation checks (the guards are load-bearing)

Two one-line mutations of master, run against the same probe:

```
M1 empty accepted: True calls: 1 outcome: ok          # GamePlan.is_empty -> False
M2 skip retried: False calls: 2 outcome: call failed   # is_skipped_call_text -> False
```

M1 shows that without the empty-plan rejection the role-only answer becomes the
plan in a single call with outcome `ok` — tests 1–2 fail. M2 shows that without
the skip classification the same sentinel is reported as `call failed` instead of
`call not made (model server unavailable)` — test 3 fails on its reason
assertion. (M2 does not add a call, because a backend-error text is not retried;
the skip guard's observable effect is the reported reason, which is what test 3
pins.)

## What changed

`tests/test_game_plan_empty_and_busy_253.py` only — eight tests:

* `test_a_schema_valid_empty_answer_is_rejected_and_retried_once` — one bounded
  low-effort retry, then a usable plan; the rejected text appears in the log.
* `test_a_schema_valid_empty_answer_keeps_the_prior_plan_and_names_the_reason` —
  two unusable answers buy no third call; prior plan kept; `last_outcome`,
  `_refresh_failed` and the prompt's `PLAN NOT REFRESHED` line all name it.
* `test_a_busy_or_degraded_lane_keeps_the_prior_plan_without_a_retry`
  (parametrized over backend-down / circuit-open / dropped) — no retry, prior
  plan kept, reason is "call not made".
* `test_the_first_plan_is_requested_fast_and_reports_no_plan` — effort `none`
  inside 20 s for both attempts, retry shares the budget, status is `none`, and
  the warning names the reason.
* `test_a_superseded_reform_discards_its_answer_and_keeps_the_prior_plan` — the
  late answer is discarded, outcome stays `ok`, no refresh failure recorded.
* `test_no_plan_still_gives_the_planner_a_bounded_strategy_block` — with no plan
  at all the block still renders deterministic board math.

## Acceptance map

| Acceptance clause | Where it lives | Pinned by |
|---|---|---|
| Replay schema-valid empty output | probe A; rejection at `game_plan.py:5199` | tests 1–2 |
| Replay busy/degraded planning | probe B; skip classification at `game_plan.py:157/5177` | test 3 |
| Retain a valid prior plan or bounded fallback | prior plan kept in A/B/D; bounded first-plan path in C; board-math block with no plan | tests 1–6 |
| Cancel stale work | superseded reform discards its answer (`game_plan.py:5171`) | test 5 |
| Report the true reason | `last_outcome`, `_refresh_failed`, prompt line, warning text | tests 2–4 |
| Inspect master/tests before editing; narrow reproduction or bounded gap | controls table + probes A–E + mutation checks above; no source change claimed | this receipt |

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest -q tests/test_game_plan_empty_and_busy_253.py \
    tests/test_game_plan.py tests/test_game_plan_unusable_and_kills_20261019.py \
    tests/test_latency_budget_222_followup.py tests/test_game_plan_staleness_20261019.py \
    tests/test_separate_strategy_planning.py tests/test_game_plan_lethal_flip_midturn.py \
    tests/test_latency_249_first_token.py
99 passed in 10.36s

$ .venv_mac_311/bin/python -m ruff check tests/test_game_plan_empty_and_busy_253.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_game_plan_empty_and_busy_253.py
1 file already formatted
```

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* No replay of the archived transcript: both cited commits are absent from this
  checkout and the queue's evidence-document pointer has no #253 row.
* These are deterministic reproductions of the logged mechanism on current
  master, not live qualification against api.mtgacoach.com.
* Service-load TTFT under real gateway saturation is not qualified here; this ID
  deliberately does not conflate it with first availability.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
