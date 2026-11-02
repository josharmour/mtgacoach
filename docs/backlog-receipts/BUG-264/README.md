# BUG-264 — a stall incident that needs client versus backend timing separation

Verdict: **no production change needed; the timing separation this ID asks for
is merged, and the three controls it names are pinned.** The retained mechanism
is the opposite of #256/#260: "milder loop stalls with no first-token or budget
failures". A stall with no failed model call cannot be attributed to the service,
and the triage says so explicitly — a watchdog warning alone does not prove a
service delay. What this ID needs is the ability to tell a queue wait from a slow
first token from slow generation from client-side setup, per request, inside one
decision. That is what `llm_trace` records on master: `client_setup_ms` and
`lane_wait_ms` are kept separate from `ttft_ms` and `total_ms`, scoped to the
decision, next to what was finally submitted. The one first-plan-start lag in the
trace is covered by the fast initial plan. Verification only: the 9 audit node
IDs for this ID pass on master (`16 passed`), together with the decision-trace
file (`7 passed`). No source commit; merged to local master as `<merge>`
(receipt commit `<source>`).

## Symptom and evidence

Queue row 17: "A recurring stall incident needs client versus backend timing
separation." Acceptance: locate the retained trace, reproduce its current-path
mechanism, record queue/first-token/generation/cancellation timing and final
submission, preserve the original deadline/freshness/legal fallback; if an
earlier queue item already fixes it, verify that regression and reference the
same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:423` — `| #264 | OPEN | Autopilot / LLM
  latency |`. The OPEN label is historical; the latency triage below already
  maps this ID to merged controls.
* `docs/latency-222-triage-20261101.md:146` — `| #264 | 90b6a56c, 94f1043a,
  edfd8db0: Milder loop stalls with no first-token or budget failures; one
  first-plan-start lag. | T, F4, F5 | Need end-to-end spans to explain stalls;
  cannot infer service delay merely from watchdog warning. First-plan live
  completion pending. |`
* `docs/latency-222-id-audit-20261101.json`, issue 264 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, and the exact pytest node IDs that pin each control key.
* The three retained traces (`90b6a56c`, `94f1043a`, `edfd8db0`) are not objects
  in this checkout, so the archived episodes cannot be replayed here. The queue's
  other two pointers are also unusable: `../autopilot-improvements.md` is absent
  from this checkout and `backlog-evidence-2026-11-01.md` has no #264 row.
  Recorded rather than treated as a missing trace.

## Why timing separation is the answer here

The trace has no first-token timeout and no budget failure, so the model call
completed inside its limits and the wall clock went somewhere else. Per-request
log lines cannot answer that: one decision can issue a hedged pair, a repair retry
and a second opinion, and those lines interleave with background work. `llm_trace`
(`src/arenamcp/llm_trace.py`) keeps one process-wide trace that every finished
request appends to while a decision is open, and it keeps the fields that separate
the layers:

| Field | What it separates |
|---|---|
| `client_setup_ms` | time spent before the request left the client |
| `lane_wait_ms` | time queued behind background work (a decision records `0.0`) |
| `ttft_ms` / `first_content_ms` | time to first token / first content from the service |
| `total_ms`, `attempts`, `budget_s`, `finish_reason` | generation and whether a retry or budget cut shaped it |

Instrumentation only: recording never raises into a request or a plan, and the
trace holds counts and timings, never prompts or model output.

## Controls on master

| Key | Control | Where | Merged as |
|---|---|---|---|
| T | typed decision hedge: high and low at once, first usable answer wins, bounded doubt allowance for a questioned low answer, shared second-look budget | `action_planner.py:8845` `_hedged_decision`, `_CRITICAL_DECISION_BUDGET_S = 15.0` (`8829`), `_DOUBT_AFTER_LOW_S = 6.0` (`8843`) | `ec4836b` (#232), `b352b8a` |
| T | decision-scoped trace separating client setup, lane wait, first token and generation, plus the submission line | `llm_trace.py:24–33`, `describe()` at `103` | `429fac1` (#231) |
| F4 | while no plan exists every hosted attempt runs at effort `none` inside a short initial budget before any ordinary deep refresh — the first-plan-start lag in this trace | `game_plan.py:3733` `_INITIAL_HOSTED_PLAN_TIMEOUT_S = 20.0`, fast-recovery budget at `5020–5045` | `264334b` (#141), `409749f` (#223) |
| F5 | a planner call whose waiter exits exceptionally gets its own cancel token set; repair keeps a fresh token and the original deadline | `action_planner.py:1510–1523` `_bounded_complete` | `4359d67` |

All five commits are ancestors of local master (`git merge-base --is-ancestor`
checked for each).

## Verification

The audit's node IDs for this ID were run unchanged on master:

```
$ .venv_mac_311/bin/python -m pytest <the 9 audit node IDs for #264> -q
16 passed in 13.96s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_231_decision_trace.py -q
7 passed in 6.61s
```

The second run is the acceptance point this ID turns on:

* **timing record** — one decision's requests are attributable end to end:
  queue wait (`lane_wait_ms == 0.0`: a decision never queues behind background
  work), first token (`ttft_ms`) or its absence (`ttft_ms is None` for the stalled
  call), generation (`total_ms >= ttft_ms`), cancellation count, and the
  submission line naming each request's class and status.
* **client versus backend** — the same summary keeps `client_setup_ms` apart from
  `ttft_ms`, so a future stall with no failed call can be attributed to setup,
  queueing or generation instead of being read as a service delay.

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **End-to-end spans are still missing.** The triage's prerequisite — spans that
  explain a client-side stall without a failed model call — is not in this queue.
  The trace attributes one decision's requests; it does not span the coaching loop
  between decisions.
* **First-plan live completion pending.** The fast initial plan is pinned by unit
  tests; no live run has confirmed a first plan completing under real gateway load.
* No retained trace in this checkout (`90b6a56c`, `94f1043a`, `edfd8db0` absent),
  so this is mechanism verification on current master, not an exact replay.
* No live qualification against api.mtgacoach.com under real gateway load.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
