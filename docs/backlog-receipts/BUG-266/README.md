# BUG-266 — post-cast priority hop limits and stalls that hand work to the watchdog

Verdict: **no production change needed; the four latency controls this ID names
are merged and pinned, and the two remaining mechanisms belong to other owners.**
The retained window had two budget exhaustions, a first-plan lag, a hop-limit(1)
and a library-only strategy rejection. The second exhaustion is the one this ID
turns on: a malformed strategy answer used to start a fresh model request with a
fresh budget, so one reform could spend twice its allowance. On master the JSON
retry receives only what is left of the original reform budget, and an expired
reform starts no second request at all. The hop limit is deliberate bounded work
per cast and belongs to execution; the library-only strategy rejection belongs to
the strategy owner. Verification only: the 15 audit node IDs for this ID pass on
master (`24 passed`), together with the reform-budget file and the post-cast
priority file (`37 passed`). No source commit; merged to local master as `<merge>`
(receipt commit `<source>`).

## Symptom and evidence

Queue row 19: "Post-cast priority hop limits and stalls hand work to the
watchdog." Acceptance: locate the retained trace, reproduce its current-path
mechanism, record queue/first-token/generation/cancellation timing and final
submission, preserve the original deadline/freshness/legal fallback; if an
earlier queue item already fixes it, verify that regression and reference the
same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:425` — `| #266 | OPEN | Autopilot / LLM
  latency |`. Historical label only.
* `docs/latency-222-triage-20261101.md:148` — `| #266 | 04314b21, 8d26e22e,
  c06d3a82: Two budget exhaustions in one window, first-plan lag, hop-limit(1),
  and library-only strategy rejection. | P, T, F2, F4, F5 | Identify whether
  exhausted calls were hedge or serial work; execution owns hop limit and
  strategy owner owns unavailable-card rejection. |`
* `docs/latency-222-id-audit-20261101.json`, issue 266 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, and the exact pytest node IDs that pin each control key.
* The three retained traces (`04314b21`, `8d26e22e`, `c06d3a82`) are not objects
  in this checkout (`git log -1` reports unknown revision), so the archived
  episodes cannot be replayed here. The queue's other two pointers are also
  unusable: `../autopilot-improvements.md` is absent from this checkout and
  `backlog-evidence-2026-11-01.md` has no #266 row. Recorded rather than treated
  as a missing trace.

## The second exhaustion in the window

`test_strategy_json_retry_shares_the_original_reform_budget` drives a backend whose
first answer is malformed after spending part of a 10 s reform budget. On master
the retry is admitted with `request_timeout_s=3.0` — the remainder — not another
10 s; when the first attempt has already spent the whole budget the retry is never
started (`budgets == [10.0]`, plan `None`). That is the mechanism that turned one
reform into two exhaustions in the retained window.

## Controls on master

| Key | Control | Where | Merged as |
|---|---|---|---|
| F2 | a malformed strategy answer's JSON retry receives only the remaining reform budget; an expired reform starts no second request | `game_plan.py:5209` `_retry_for_json`, fast-recovery budget at `5020–5045` | `70d9e2f`, `b352b8a` |
| P | call budget clamped to the client hard timeout / caller's `request_timeout_s`, deadline fixed at request start; a cut stream closes instead of running on | `backends/proxy.py:1340–1343`, stream closure raising `LLM streaming time budget exhausted` | `d27b878` (#222), `87faace` (#248) |
| P | our own budget cut is never retried, retries share one budget, and an exhausted budget starts no further request | retry loop condition, metrics status map | `d27b878`, `87faace` |
| T | typed decision hedge: high and low at once, first usable answer wins, bounded doubt allowance, shared second-look budget | `action_planner.py:8845` `_hedged_decision`, `_CRITICAL_DECISION_BUDGET_S = 15.0` (`8829`) | `ec4836b` (#232), `b352b8a` |
| F4 | while no plan exists every hosted attempt runs at effort `none` inside a short initial budget before any ordinary deep refresh — the first-plan lag in this window | `game_plan.py:3733` `_INITIAL_HOSTED_PLAN_TIMEOUT_S = 20.0`, fast-recovery budget at `5020–5045` | `264334b` (#141), `409749f` (#223) |
| F5 | a planner call whose waiter exits exceptionally gets its own cancel token set; repair keeps a fresh token and the original deadline | `action_planner.py:1510–1523` `_bounded_complete` | `4359d67` |

All eight commits are ancestors of local master (`git merge-base --is-ancestor`
checked for each).

## Verification

The audit's node IDs for this ID were run unchanged on master:

```
$ .venv_mac_311/bin/python -m pytest <the 15 audit node IDs for #266> -q
24 passed in 14.95s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_budget_222_followup.py tests/test_priority_after_cast_206.py -q
37 passed in 11.42s
```

The acceptance points this ID turns on:

* **the second exhaustion** — the reform-budget test above pins that a retry shares
  the original budget and that an expired reform starts nothing new.
* **hedge versus serial work** — the audit's prerequisite ("identify whether
  exhausted calls were hedge or serial work") is answered by the decision trace:
  each request records its `call_class`, `status`, `attempts` and `budget_s`, so a
  hedged pair is distinguishable from serial retries in one decision.
* **post-cast priority** — the #206 file pins both halves of the post-cast path:
  the autopilot polls the bridge itself after a submitted `shouldStop` option until
  a new request appears (and answers it) or the window closes, bounded by
  `_POST_CAST_PRIORITY_POLL_S = 1.0`; and when Player.log says the decision is ours
  while our timer ticks, a stand-in request lets the watchdog act instead of
  standing down.

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **The hop limit is deliberate and owned by execution.** `autopilot.py:5172`
  `_MAX_POST_CAST_PRIORITY_HOPS = 1` bounds how much work one cast may chain; when it
  trips, the log says so explicitly (`post-cast priority: hop limit (%s) reached;
  leaving %s to the watchdog`, `6105–6108`) rather than stalling silently. Raising it
  is an execution decision, not a latency patch, so this receipt records it instead of
  changing it.
* **Library-only strategy rejection belongs to the strategy owner** (unavailable-card
  rejection, #240), which is outside this queue's scope.
* No exact historical replay: traces `04314b21`, `8d26e22e` and `c06d3a82` are absent
  from this checkout.
* No live qualification against api.mtgacoach.com under real gateway load.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
