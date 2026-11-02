# BUG-248 — a streaming budget cut ends in a legal play, not a stalled loop

Verdict: **no production change needed; the merged controls answer this ID.**
The retained trace's "LLM streaming time budget exhausted" is the proxy's own
budget cut. On current master that cut is bounded by the request budget, never
retried, classified as a slow-but-alive timeout (so the circuit stays closed and
health degrades), turned into `llm_unavailable` by the planner inside its
deadline, and replaced by the autopilot with the board-math line without
escalating the planning budget. What was missing was a regression that pins that
composition on this exact error. New test file only:
`tests/test_latency_248_budget_cut.py`. No source commit; merged to local master
as `acee2d6`.

## Symptom and evidence

Queue row 11: "Streaming budget exhaustion stalls the coaching loop despite a
benign fallback." Acceptance: locate this ID's retained trace, reproduce its
current-path mechanism, record queue/first-token/generation/cancellation timing
and final submission, preserve the original deadline, freshness and legal
fallback; if an earlier queue item already fixes it, verify this specific
regression and reference that same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md` line 407 — `| #248 | OPEN | Autopilot /
  LLM latency |`.
* `docs/latency-222-triage-20261101.md` line 141 — `| #248 | 50668fe5,
  447ae530: Budget exhaustion and 16–31 s loop delays accompanied by post-cast
  six-second waits. | P, T, F5 | Separate post-cast waits (#206/#243) from model
  duration; obtain post-install per-call and chain timings. |` — i.e. the ID is
  covered by existing proxy budget/health/stream-closure controls (P), typed
  first-usable-answer/bounded-doubt controls (T) and the F5 cancellation gap,
  with the post-cast waits called out as a *separate* prerequisite.
* The queue's `../autopilot-improvements.md` evidence path is not in this
  checkout, so the retained trace is the triage rows above.

Controls already on master that this item must not duplicate:

| Control | Commit | What it does |
|---|---|---|
| #222 budget-cut health and no-retry | `d27b878` (merge `9f0a328`) | our own stream cut is never retried, keeps the circuit closed, marks health degraded, records `first_token_seen` |
| F5 cancellation / bounded wait | `70d9e2f`, `b352b8a` | each planner call owns a cancel token set when the wait exits exceptionally; repair keeps the original deadline |
| #206 post-cast priority polling | `1adc79d` | polls priority after `shouldStop`, so the six-second post-cast waits are not model duration |
| #243 live-window classification | `9a321bc` | classifies bridge failures by live window instead of treating them as stalls |
| #231 decision trace | merged earlier in this queue | records queue wait, first token, generation and cancellation per request plus the submission suffix |

## Mechanism on current master (reproduced)

Direct probe of one decision call against a stream that yields a first token and
then keeps generating past a 0.4 s budget:

```
[PROXY] Request metrics: {'status': 'error', 'stream_chunks': 15,
 'ttft_ms': 0.2, 'total_ms': 416.9, 'attempts': 1, 'budget_s': 0.4,
 'call_class': 'decision.plan', 'reasoning_effort': 'low', 'max_tokens': 64}
API error: LLM streaming time budget exhausted
err: LLM streaming time budget exhausted timeout True
```

So the cut is one attempt at the budget with a first token already seen — slow,
not dead — which is exactly what `is_llm_unavailable_error` keys on.

Planner path with a stream that blocks inside `next()` past the budget:

```
Action planning timed out after 1192ms (limit 0.4s)
```

→ `ActionPlan(actions=[], fallback_reason='llm_unavailable',
fallback_detail='timeout')`, one backend call, diagnostics record
`failure='timeout'` and `elapsed_ms`. The caller's deadline wins that race; when
chunks keep arriving the proxy's own cut wins and the detail carries
`llm_error: LLM streaming time budget exhausted`. Both end in the same benign
plan.

Autopilot path: `llm_unavailable` → board-math line (`CAST_SPELL`), zero
consecutive plan failures, planning timeout still 15.0 s. The contrast that makes
that assertion meaningful already exists:
`test_empty_plans_do_not_escalate_the_budget_while_the_backend_is_failing`
asserts a plain empty plan escalates to 22.5 s.

## What changed

`tests/test_latency_248_budget_cut.py` only — five tests:

* `test_the_proxy_cuts_its_own_stream_and_records_the_decomposition` — one
  attempt, `kind == "timeout"`, `first_token_seen is True`, and the #231 trace
  entry carries `status="error"`, `attempts=1`, `budget_s=0.4`,
  `lane_wait_ms=0.0`, a first token, and generation cut at ≥350 ms.
* `test_a_stream_blocked_past_the_budget_is_one_bounded_call` — one backend call,
  benign fallback at the caller deadline, diagnostics recorded.
* `test_the_proxy_budget_cut_error_reaches_the_board_math_play_without_escalation`
  — the proxy-shaped error reaches the planner as `llm_unavailable` and the
  autopilot plays board math without escalating.
* `test_a_budget_cut_is_classified_as_the_model_being_unavailable` — both error
  shapes (`TimeoutError`, `BackendError`) classify as model-unavailable.

## Acceptance map

| Acceptance clause | Where it lives | Pinned by |
|---|---|---|
| Locate this ID's retained trace | triage rows above (hashes 50668fe5 / 447ae530) | this receipt |
| Reproduce its current-path mechanism | probe output above; blocked-stream planner run | tests 1–2 |
| Record queue/first-token/generation/cancellation timing | #231 `llm_trace` summary assertions | test 1 |
| Preserve the original deadline and freshness | one attempt, fallback at the caller deadline, no retry, no budget extension | tests 1–2 |
| Legal fallback / final submission | board-math `CAST_SPELL`, failures stay 0, timeout stays 15.0 s | test 3 |
| Reference an earlier fix instead of duplicating it | no source change; cites `d27b878`, `70d9e2f`, `b352b8a`, `1adc79d`, `9a321bc` | this receipt |

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest -q tests/test_latency_248_budget_cut.py \
    tests/test_llm_latency_222.py tests/test_latency_budget_222_followup.py \
    tests/test_action_plan_cancellation_222.py tests/test_llm_down_fallback.py \
    tests/test_latency_231_decision_trace.py
74 passed in 23.10s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_248_budget_cut.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_248_budget_cut.py
1 file already formatted
```

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* No replay of the retained live window; these are deterministic reproductions of
  the logged mechanism on current master.
* The post-cast six-second waits in the same trace are #206/#243 (`1adc79d`,
  `9a321bc`) and are not re-opened here.
* Which side wins the deadline race depends on whether generation blocks inside
  `next()`; both outcomes are pinned, but neither is a claim about a specific
  historical match.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item
can move past `mergedawaitingreview`. Nothing here was verified by its author.
