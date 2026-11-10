# BUG-256 — a recurring latency incident that consumed most of a decision window

Verdict: **no production change needed; the five controls this ID names are
merged and pinned.** The retained trace is a compound episode: first-token and
budget delays, plus a rejected unavailable-card win condition and a closed
attack window. Only the latency half belongs to this queue, and on current
master it is bounded at every layer — the proxy clamps the call budget and
closes a cut stream, a first-token timeout is never retried, the typed hedge
returns the first usable answer and cancels the loser, the declaration hedge
does the same for combat, the first hosted plan runs fast before any deep
refresh, and a planner call whose waiter gives up has its own cancel token set
so it stops competing with the next live request. The two strategy halves are
owned by #240 and #230/#243, which are not in this queue. Verification only:
the 17 audit node IDs for this ID pass on master (`27 passed`), together with
the decision-trace and model-down fallback files (`38 passed`). No source
commit; merged to local master as `5a38627` (receipt commit `dadd9bc`).

## Symptom and evidence

Queue row 17: "A recurring latency incident still consumes most of a decision
window." Acceptance: locate the retained trace, reproduce its current-path
mechanism, record queue/first-token/generation/cancellation timing and final
submission, preserve the original deadline/freshness/legal fallback; if an
earlier queue item already fixes it, verify that regression and reference the
same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:415` — `| #256 | OPEN | Autopilot / LLM
  latency |`. The OPEN label is historical; the latency triage below already
  maps this ID to merged controls.
* `docs/latency-222-triage-20261101.md:144` — `| #256 | 9dc1c55c:
  First-token/budget delays; rejected unavailable-card win conditions and closed
  attack window compounded the episode. | P, T, F1, F4, F5 | Unavailable-card
  strategy (#240) and closed-window combat (#230/#243) need their owners;
  service timings alone cannot establish causal loss. |`
* `docs/latency-222-id-audit-20261101.json`, issue 256 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, and the exact pytest node IDs that pin each control key.
* The retained trace commit `9dc1c55c` is not an object in this checkout
  (`git log -1 9dc1c55c` → unknown revision), so the archived episode cannot be
  replayed here. The queue's other two pointers are also unusable:
  `../autopilot-improvements.md` is absent from this checkout and
  `backlog-evidence-2026-11-01.md` has no #256 row. Recorded rather than treated
  as a missing trace.

## Controls on master

| Key | Control | Where | Merged as |
|---|---|---|---|
| P | call budget clamped to the client hard timeout / caller's `request_timeout_s`, deadline fixed at request start; a cut stream closes instead of running on | `backends/proxy.py:1340–1343`, stream closure raising `LLM streaming time budget exhausted` | `d27b878` (#222), `87faace` (#248) |
| P | a first-token timeout is never retried and is recorded as `ttft_timeout`, not as a generic error | retry loop condition `err.kind != "first_token_timeout"`, metrics status map | `d27b878`, `87faace` |
| T | typed decision hedge: high and low at once, first usable answer wins, bounded doubt allowance for a questioned low answer, shared second-look budget | `action_planner.py:8845` `_hedged_decision`, `_CRITICAL_DECISION_BUDGET_S = 15.0` (`8829`), `_DOUBT_AFTER_LOW_S = 6.0` (`8843`) | `ec4836b` (#232), `b352b8a` |
| F1 | declaration hedge returns the first answer the parser accepts as legal actions and cancels the other leg | `action_planner.py:1162` `_hedged_declaration` | `70d9e2f`, `b352b8a` |
| F4 | while no plan exists every hosted attempt runs at effort `none` inside a short initial budget before any ordinary deep refresh | `game_plan.py:3733` `_INITIAL_HOSTED_PLAN_TIMEOUT_S = 20.0`, fast-recovery budget at `5020–5045` | `264334b` (#141), `409749f` (#223) |
| F5 | a planner call whose waiter exits exceptionally gets its own cancel token set, so it stops competing with the next live request; repair keeps a fresh token and the original deadline | `action_planner.py:1510–1523` `_bounded_complete` | `4359d67` |

All six commits are ancestors of local master (`git merge-base --is-ancestor`
checked for each).

## Verification

The audit's node IDs for this ID were run unchanged on master:

```
$ .venv_mac_311/bin/python -m pytest <the 17 audit node IDs for #256> -q
27 passed in 15.01s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_231_decision_trace.py \
    tests/test_llm_down_fallback.py -q
38 passed in 13.64s
```

The second run covers the two acceptance points that are not per-control unit
tests:

* **timing record** — `tests/test_latency_231_decision_trace.py` pins one
  decision's queue wait (`lane_wait_ms == 0.0`: a decision never queues behind
  background work), first token (`ttft_ms`) or its absence (`ttft_ms is None`
  for the stalled call), generation (`total_ms >= ttft_ms`), cancellation count,
  and the submission line naming the fallback and decision time.
* **legal fallback preserved** — `tests/test_llm_down_fallback.py` pins that a
  timed-out or failed planner still plays the board-math line (land first, then
  its spells), never passes while that line casts something, keeps attacks,
  blocks and targets on their safe classifiers, makes no model call with the
  circuit open, and never escalates the planner budget on failure.

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **Strategy halves are out of queue scope.** The rejected unavailable-card win
  condition belongs to #240 and the closed attack window to #230/#243; neither
  ID is in this queue's active set. Service timings alone cannot establish that
  they caused the loss, so this receipt does not claim them.
* **Decision-chain accumulation** — sequential decisions can still add up even
  when each single call is bounded; Arena-clock/chain deadline changes belong to
  execution follow-up f9b88c8 per the triage.
* No retained trace in this checkout (`9dc1c55c` absent), so this is mechanism
  verification on current master, not an exact historical replay.
* No live qualification against api.mtgacoach.com under real gateway load.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
