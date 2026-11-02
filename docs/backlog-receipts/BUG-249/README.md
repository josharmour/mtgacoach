# BUG-249 — a first token is not a first answer, and a cancelled plan must not compound

Verdict: **no production change needed; the merged controls answer this ID.**
The retained trace's "no first token / budget timeout" pair is the proxy's own
budget cut on a stream that was *alive* but never produced an answer, plus a
first plan that outlived its turn. On current master that cut is bounded by the
request budget, never retried, classified as a slow-but-alive timeout (circuit
stays closed, health degrades), and every hosted plan attempt made while no plan
exists runs at effort `none` inside `_INITIAL_HOSTED_PLAN_TIMEOUT_S` (20 s) — so
repeated cuts cannot escalate into another full-effort call that will be
cancelled again. What was missing was a regression that pins the distinction the
trace turns on (reasoning satisfies the first-token deadline while producing no
content) and the no-escalation invariant across repeated attempts. New test file
only: `tests/test_latency_249_first_token.py`. No source commit; merged to local
master as `6a40f07` (source commit `93bd0bf`).

## Symptom and evidence

Queue row 12: "Repeated first-token delays and cancelled background plans
persist across matches." Acceptance: locate this ID's retained trace, reproduce
its current-path mechanism, record queue/first-token/generation/cancellation
timing and final submission, preserve the original deadline, freshness and legal
fallback; if an earlier queue item already fixes it, verify this specific
regression and reference that same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md` line 408 — `| #249 | OPEN | Autopilot /
  LLM latency |`.
* `docs/latency-222-triage-20261101.md` line 142 — `| #249 | 0620a891,
  a627d4fe: No-first-token/budget timeouts, first-plan delay, benign recovered
  malformed blocker JSON. | P, T, F1, F4, F5 | Service-load and first-plan live
  qualification; malformed answer handling belongs to parser stream and is not
  evidence of universal parse failure. |` — i.e. the ID is covered by existing
  proxy budget/health controls (P), typed first-usable-answer/bounded-doubt
  controls (T), the F1/F4 first-plan and cancellation controls and F5's
  cancellation gap; the malformed blocker JSON in the same trace belongs to the
  parser stream and is explicitly *not* evidence of a universal parse failure.
* The same document's 650ae555 record (lines ~60–75) is the concrete instance:
  the first game plan "report[s] 45.300 seconds, low effort, 12,439 reasoning
  characters and zero content characters", its replacement was cancelled at game
  end with 59 content characters, and the doc states the conclusion this item
  pins: "First-token time is not first-answer time: reasoning arrived well before
  any usable content in the failed initial plan."
* The queue's `../autopilot-improvements.md` evidence path is not in this
  checkout, so the retained trace is the triage rows above.

Controls already on master that this item must not duplicate:

| Control | Commit | What it does |
|---|---|---|
| #222 budget-cut health and no-retry | `d27b878` (merge `9f0a328`) | our own stream cut is never retried, keeps the circuit closed, marks health degraded, records `first_token_seen` |
| #141/#223 fast bounded first plan | merged before this queue | a hosted plan with no prior plan runs at effort `none` inside `_INITIAL_HOSTED_PLAN_TIMEOUT_S`; a reform after a cancellation is bounded the same way and its late answer is kept |
| #223 lane release | merged before this queue | a cancelled background call releases the strategic lane for the next match |
| F5 cancellation / bounded wait | `70d9e2f`, `b352b8a` | each planner call owns a cancel token set when the wait exits exceptionally; repair keeps the original deadline |
| #231 request metrics | merged earlier in this queue | records queue wait, first token, first content, generation and cancellation per request |

## Mechanism on current master (reproduced)

Direct probe of one decision call against a stream that yields **only reasoning**
and keeps generating past a 0.4 s budget:

```
err: LLM streaming time budget exhausted | kind: timeout | first_token_seen: True
metrics: {'status': 'error', 'attempts': 1, 'budget_s': 0.4, 'ttft_ms': 25.9,
 'first_content_ms': None, 'stream_content_chars': 0,
 'stream_reasoning_chars': 285, 'total_ms': 409.6,
 'call_class': 'decision.plan', 'reasoning_effort': 'low'}
circuit consecutive_failures: 0
health: HealthState.DEGRADED
client calls: 1
```

So the cut is one attempt at the budget with a first token already seen — slow,
not dead — and the metrics record separates `ttft_ms` (25.9 ms) from
`first_content_ms` (`None`) with zero content characters. That is exactly the
650ae555 shape (45.3 s, 12,439 reasoning characters, zero content) reduced to a
deterministic reproduction.

Plan-manager path with no plan yet formed and three consecutive cuts:

```
reform turn 6 -> None
reform turn 7 -> None
reform turn 8 -> None
calls (effort, timeout_s): [('none', 20.0), ('none', 20.0), ('none', 20.0)]
plan: None | reform_failure: call failed
deep budget: 75.0 | initial hosted budget: 20.0
```

The contrast that makes those assertions meaningful: the same backend with a
non-hosted model name takes the deep path instead —
`calls: [('low', 74.99995579198003)]` — so the test discriminates the fast
bounded mode from the full-effort one rather than asserting a constant.

## What changed

`tests/test_latency_249_first_token.py` only — two tests:

* `test_reasoning_without_content_is_a_first_token_but_not_a_first_answer` —
  one attempt, `kind == "timeout"`, `first_token_seen is True`, metrics carry
  `status="error"`, `attempts=1`, a small `ttft_ms`, `first_content_ms is None`,
  `stream_content_chars == 0`, `stream_reasoning_chars > 0`; health degrades and
  the circuit's consecutive failures stay at zero.
* `test_repeated_cuts_with_no_plan_stay_in_the_fast_bounded_mode` — three
  consecutive failed reforms with no plan each run at effort `none` inside the
  20 s initial hosted budget (never the 75 s deep budget), leave `_plan is None`
  and record `_reform_failure == "call failed"`.

## Acceptance map

| Acceptance clause | Where it lives | Pinned by |
|---|---|---|
| Locate this ID's retained trace | triage rows above (hashes 0620a891 / a627d4fe) plus the 650ae555 metrics record | this receipt |
| Reproduce its current-path mechanism | probe outputs above (reasoning-only cut; three bounded reforms) | tests 1–2 |
| Record queue/first-token/generation/cancellation timing | metrics assertions: `ttft_ms`, `first_content_ms`, content/reasoning chars, `attempts`, `budget_s`, status | test 1 |
| Preserve the original deadline and freshness | one attempt at the budget; every attempt while no plan exists stays inside the initial hosted budget; no escalation to the deep budget | tests 1–2 |
| Legal fallback / final submission | unchanged by this item: a cut reaches the planner as `llm_unavailable` and the autopilot plays board math (`tests/test_latency_248_budget_cut.py`, master `acee2d6`) | cross-reference |
| Reference an earlier fix instead of duplicating it | no source change; cites `d27b878`, `70d9e2f`, `b352b8a`, and the merged #141/#223 plan-budget controls | this receipt |

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest -q tests/test_latency_249_first_token.py \
    tests/test_latency_248_budget_cut.py tests/test_latency_223_background_contention.py \
    tests/test_proxy_request_metrics.py tests/test_proxy_reasoning_salvage.py \
    tests/test_game_plan.py tests/test_autopilot_fixes_20261019.py \
    tests/test_llm_down_fallback.py
132 passed in 17.97s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_249_first_token.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_249_first_token.py
1 file already formatted
```

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* No replay of the retained live windows; these are deterministic reproductions of
  the logged mechanism on current master.
* Service-load TTFT under real gateway saturation is not qualified here — that is
  live qualification, not a code path.
* The malformed blocker JSON in the same trace belongs to the parser stream and is
  not re-opened by this item.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
