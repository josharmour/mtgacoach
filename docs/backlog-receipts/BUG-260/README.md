# BUG-260 — a timeout/circuit-degradation incident still leaves delayed decisions

Verdict: **no production change needed; the two controls this ID names are
merged and pinned.** The retained mechanism is "15–21 s stalls without a
first-token timeout". On current master every decision path passes a first-token
timeout, and the proxy's post-first-token stall guard deliberately does not
apply before the first token — so the first-token deadline is what bounds that
wait, and it is now always set. The typed hedge returns the first usable answer
and cancels the loser, and a planner call whose waiter gives up has its own
cancel token set. The other signals in the same trace are separate owners:
PayCosts/AutoTap request mismatch belongs to execution, and the contradictory
fallback rationale belongs to the claims/fallback owner. Verification only: the
6 audit node IDs for this ID pass on master (`8 passed`), together with the
decision-trace file and the test that pins the first-token timeout on decision
calls (`8 passed`). No source commit; merged to local master as `<merge>`
(receipt commit `<source>`).

## Symptom and evidence

Queue row 16: "A recurring timeout/circuit-degradation incident still leaves
delayed decisions." Acceptance: locate the retained trace, reproduce its
current-path mechanism, record queue/first-token/generation/cancellation timing
and final submission, preserve the original deadline/freshness/legal fallback;
if an earlier queue item already fixes it, verify that regression and reference
the same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:419` — `| #260 | OPEN | Autopilot / LLM
  latency |`. The OPEN label is historical; the latency triage below already
  maps this ID to merged controls.
* `docs/latency-222-triage-20261101.md:145` — `| #260 | 6bef1efb, 06bbdd81,
  b5f55777: 15–21s stalls without first-token timeout; fallback contradiction,
  post-cast wait and PayCosts/AutoTap mismatch are separate signals. | T, F5 |
  Correlate blocking frame/decision-chain time. Execution owns PayCosts request
  mismatch; claims/fallback owner handles contradictory rationale. |`
* `docs/latency-222-id-audit-20261101.json`, issue 260 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, and the exact pytest node IDs that pin each control key.
* The three retained traces (`6bef1efb`, `06bbdd81`, `b5f55777`) are not objects
  in this checkout, so the archived episodes cannot be replayed here. The queue's
  other two pointers are also unusable: `../autopilot-improvements.md` is absent
  from this checkout and `backlog-evidence-2026-11-01.md` has no #260 row.
  Recorded rather than treated as a missing trace.

## Why "without a first-token timeout" was the mechanism

`_CallHandle.read_limit()` (`backends/proxy.py:708`) applies `stall_s` only once
`token_seen` is true; before the first token only `first_token_deadline` (or the
overall budget deadline) limits a socket read. A decision call that passed no
first-token timeout therefore waited out its whole budget on a stream that never
started — the reported 15–21 s stall. Every decision path on master now sets one:

| Path | First-token limit | Where |
|---|---|---|
| typed decisions (priority, targets, casting) | `first_token_budget(_FIRST_TOKEN_TIMEOUT_S = 8.0, prompt chars)`, budget sized to keep answer time | `action_planner.py:9644`, passed at `9589` |
| plan and combat declarations | `_plan_first_token_timeout(...)` = min(12 s, max(8 s, budget − 2 s)), scaled for big prompts | `action_planner.py:1038`, passed at `1505` |
| advice calls | min(`_ADVICE_FIRST_TOKEN_TIMEOUT_S`, api timeout − 2 s) | `coach.py:4629` |

## Controls on master

| Key | Control | Where | Merged as |
|---|---|---|---|
| T | typed decision hedge: high and low at once, first usable answer wins, bounded doubt allowance for a questioned low answer, shared second-look budget | `action_planner.py:8845` `_hedged_decision`, `_CRITICAL_DECISION_BUDGET_S = 15.0` (`8829`), `_DOUBT_AFTER_LOW_S = 6.0` (`8843`) | `ec4836b` (#232), `b352b8a` |
| T | a slow legacy backend that ignores `request_timeout_s` cannot hold the coaching loop; the waiter is bounded independently of the socket | `action_planner.py:1150` `_bounded_call`, `1133` `_wait_for_model` | `d27b878` (#222) |
| F5 | a planner call whose waiter exits exceptionally gets its own cancel token set, so it stops competing with the next live request; repair keeps a fresh token and the original deadline | `action_planner.py:1510–1523` `_bounded_complete` | `4359d67` |
| F5 | declaration hedge cancels the losing leg after a usable answer instead of waiting out its budget | `action_planner.py:1162` `_hedged_declaration` | `70d9e2f`, `b352b8a` |

All four commits are ancestors of local master (`git merge-base --is-ancestor`
checked for each).

## Verification

The audit's node IDs for this ID were run unchanged on master:

```
$ .venv_mac_311/bin/python -m pytest <the 6 audit node IDs for #260> -q
8 passed in 13.77s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_231_decision_trace.py \
    tests/test_llm_down_fallback.py::test_decision_calls_carry_call_class_and_first_token_timeout -q
8 passed in 10.80s
```

The second run covers the two acceptance points that are not per-control unit
tests:

* **timing record** — `tests/test_latency_231_decision_trace.py` pins one
  decision's queue wait (`lane_wait_ms == 0.0`: a decision never queues behind
  background work), first token (`ttft_ms`) or its absence (`ttft_ms is None`
  for the stalled call), generation (`total_ms >= ttft_ms`), cancellation count,
  and the submission line naming the fallback and decision time.
* **the mechanism itself** —
  `test_decision_calls_carry_call_class_and_first_token_timeout` pins that a
  decision call reaches the backend with both its call class and its first-token
  timeout, which is what closes "stalls without first-token timeout".

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **Separate owners for the other signals.** The PayCosts/AutoTap request
  mismatch belongs to execution (per the triage row), and the contradictory
  fallback rationale belongs to the claims/fallback owner. Neither is in this
  queue's active set, so this receipt does not claim them.
* **Decision-chain accumulation** — sequential decisions can still add up even
  when each single call is bounded; Arena-clock/chain deadline changes belong to
  execution follow-up f9b88c8 per the triage.
* No retained trace in this checkout (`6bef1efb`, `06bbdd81`, `b5f55777`
  absent), so this is mechanism verification on current master, not an exact
  historical replay.
* No live qualification against api.mtgacoach.com under real gateway load.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
