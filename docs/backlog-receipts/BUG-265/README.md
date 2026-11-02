# BUG-265 — a high-effort declaration that fails at budget while low could stand

Verdict: **no production change needed; the declaration hedge this ID names is
merged and pinned.** The retained mechanism is precise: the high-effort
declaration leg ran out its budget while the low-effort leg had an answer that
could stand, and the old hedge waited for high anyway — so the combat window
closed with a usable answer already in hand. On master both legs are submitted at
once and the first answer the ordinary parser accepts as legal actions wins, with
the losing leg's network work cancelled rather than left running. A fast but
invalid answer does not displace the valid other leg, and if neither is usable a
response is retained for the caller's bounded repair path instead of raising.
Verification only: the 10 audit node IDs for this ID pass on master (`14 passed`),
together with the two declaration files (`27 passed`). No source commit; merged to
local master as `<merge>` (receipt commit `<source>`).

## Symptom and evidence

Queue row 18: "Planner failure and fallback behavior recur during latency
pressure." Acceptance: locate the retained trace, reproduce its current-path
mechanism, record queue/first-token/generation/cancellation timing and final
submission, preserve the original deadline/freshness/legal fallback; if an
earlier queue item already fixes it, verify that regression and reference the
same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:424` — `| #265 | OPEN | Autopilot / LLM
  latency |`. Historical label only.
* `docs/latency-222-triage-20261101.md:17` — "Declaration hedge waits for high
  after low answered | #232, #265, #277, #285, #286 | Reproduced on current code.
  Previously `_hedged_declaration` waited only on high, then used low after
  timeout. Fixed here; validation and deployment qualification remain separate."
* `docs/latency-222-triage-20261101.md:147` — `| #265 | e756afa4, 15eaac1a:
  High-effort declaration fails at budget while low could stand; separate
  malformed typed output and illegal declaration pass. | F1, F5, P | Qualify
  faster accepted declaration live; parser/legality owner retains
  malformed/illegal-output work. No exact historical full-game replay. |`
* `docs/latency-222-id-audit-20261101.json`, issue 265 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, and the exact pytest node IDs that pin each control key.
* The two retained traces (`e756afa4`, `15eaac1a`) are not objects in this
  checkout (`git log -1` reports unknown revision), so the archived episodes
  cannot be replayed here. The queue's other two pointers are also unusable:
  `../autopilot-improvements.md` is absent from this checkout and
  `backlog-evidence-2026-11-01.md` has no #265 row. Recorded rather than treated
  as a missing trace.

## What the hedge does now

`action_planner.py:1162` `_hedged_declaration` (docstring cites `#222/#265/#277`):

* both legs are submitted to a two-worker pool at once, each with its own cancel
  token passed as `cancel_event=`;
* the loop returns the first answer where `usable(answer)` is true — the call site
  at `1535–1549` passes a parser-based predicate over the effective legal actions,
  so "usable" means "the ordinary parser accepts it as legal actions", not merely
  "non-empty";
* a winner logs `Declaration plan: %s won in %.0f ms; other effort not awaited`
  and returns immediately, so a ready low answer no longer waits for high to
  exhaust its budget;
* a fast but invalid answer is recorded and the loop keeps waiting for the other
  leg — it cannot displace a valid answer;
* if neither leg is usable, a retained response is returned for the caller's
  bounded repair path rather than raising;
* the `finally` block sets every cancel token before shutting the pool down, so
  the losing leg's network work stops instead of running on after a winner,
  timeout or request change.

## Controls on master

| Key | Control | Where | Merged as |
|---|---|---|---|
| F1 | declaration hedge returns the first answer the parser accepts as legal actions and cancels the other leg | `action_planner.py:1162` `_hedged_declaration`, call site `1535–1549` | `70d9e2f`, `b352b8a` |
| F5 | a planner call whose waiter exits exceptionally gets its own cancel token set; repair keeps a fresh token and the original deadline | `action_planner.py:1510–1523` `_bounded_complete` | `4359d67` |
| P | call budget clamped to the client hard timeout / caller's `request_timeout_s`, deadline fixed at request start; a cut stream closes instead of running on | `backends/proxy.py:1340–1343`, stream closure raising `LLM streaming time budget exhausted` | `d27b878` (#222), `87faace` (#248) |
| P | our own budget cut is never retried, and retries share one budget instead of restarting it | retry loop condition, metrics status map | `d27b878`, `87faace` |

All four commits are ancestors of local master (`git merge-base --is-ancestor`
checked for each).

## Verification

The audit's node IDs for this ID were run unchanged on master:

```
$ .venv_mac_311/bin/python -m pytest <the 10 audit node IDs for #265> -q
14 passed in 8.61s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_budget_222_followup.py tests/test_declaration_budget_232.py -q
27 passed in 7.81s
```

The acceptance points this ID turns on:

* **the reported mechanism** — `test_ready_low_declaration_cancels_high_before_its_budget_expires`
  drives a high leg that would block until its budget expires and asserts the low
  answer returns first *and* that high observed its cancel token.
* **invalid output does not win** — `test_fast_invalid_declaration_does_not_displace_the_valid_other_leg`
  pins that a fast unusable answer leaves the valid leg to win.
* **cancellation** — `test_declaration_hedge_cancels_outstanding_calls` pins that no
  leg is left running after the hedge returns.
* **budget** — the three proxy metrics tests pin that retries share one budget, an
  exhausted budget starts no further request, and a continuously streaming model
  stops at the budget and closes.

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **Live qualification of a faster accepted declaration is still pending.** The
  hedge is pinned by deterministic reproductions; no live run has shown a combat
  declaration landing sooner under real gateway load.
* **Malformed typed output and illegal declarations belong to the parser/legality
  owner**, per the triage. This receipt verifies that an unusable answer cannot win
  the hedge; it does not claim those outputs are now well-formed or legal.
* No exact historical full-game replay: traces `e756afa4` and `15eaac1a` are absent
  from this checkout.
* No live qualification against api.mtgacoach.com under real gateway load.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
