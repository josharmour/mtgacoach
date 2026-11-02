# BUG-269 — a doubted low answer whose high leg never arrived

Verdict: **no production change needed; the bounded doubt wait and the
board-math failover this ID names are merged and pinned.** The retained mechanism
is narrow: the low-effort answer was specifically doubted for a false lethal
claim, the high-effort leg did not arrive, and the hedge correctly fell over to
board math instead of firing the unsafe low answer. That is the intended
behaviour, not a stall to remove — the triage explicitly forbids dropping the
false-lethal guard to reduce latency. On master a doubted low answer waits only
for the bounded doubt allowance (`_DOUBT_AFTER_LOW_S = 6.0`, clamped by the high
leg's own deadline), and without a high answer it raises `SecondLookError` so the
caller's board-math fallback decides. Verification only: the 11 audit node IDs
for this ID pass on master (`13 passed`), together with the two hedge files
(`12 passed`). No source commit; merged to local master as `<merge>` (receipt
commit `<source>`).

## Symptom and evidence

Queue row 20: "A recurring stall incident remains open despite successful fallback
decisions." Acceptance: locate the retained trace, reproduce its current-path
mechanism, record queue/first-token/generation/cancellation timing and final
submission, preserve the original deadline/freshness/legal fallback; if an
earlier queue item already fixes it, verify that regression and reference the
same master commit instead of duplicating a patch.

Evidence read before touching code:

* `docs/backlog-triage-2026-11-01.md:428` — `| #269 | OPEN | Autopilot / LLM
  latency |`. Historical label only.
* `docs/latency-222-triage-20261101.md:149` — `| #269 | 211d4ccc, 87cac475: Low
  answer specifically doubted for false lethal; high did not arrive. Bounded wait
  correctly falls to board math rather than accepting unsafe low. | T, P, F5 |
  Replay doubt/fallback outcome on exact board; do not remove false-lethal guard
  to reduce latency. Qualify actual fallback time versus Arena clock. |`
* `docs/latency-222-id-audit-20261101.json`, issue 269 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, and the exact pytest node IDs that pin each control key.
* The two retained traces (`211d4ccc`, `87cac475`) are not objects in this
  checkout (`git log -1` reports unknown revision), so the archived episodes
  cannot be replayed here. The queue's other two pointers are also unusable:
  `../autopilot-improvements.md` is absent from this checkout and
  `backlog-evidence-2026-11-01.md` has no #269 row. Recorded rather than treated
  as a missing trace.

## What the doubt path does now

`action_planner.py:8845` `_hedged_decision` (docstring cites the false-lethal and
face-burn doubts):

* a usable low answer returns immediately — including within lethal reach, because
  lethal proximity alone is not evidence that a completed answer needs a second
  opinion;
* a low answer that `low_doubt` questions waits for the high leg only for the
  bounded doubt allowance: `doubt_deadline = min(high_deadline, time.monotonic() +
  self._DOUBT_AFTER_LOW_S)` (`8955`), with `_DOUBT_AFTER_LOW_S = 6.0` (`8843`);
* without a high answer the decision fails over rather than firing the doubted
  pick: `raise SecondLookError(f"low answer set aside ({doubt}); {why}")` (`9023`),
  logged as `typed-decision hedge: low answer %s set aside (%s; %s)`;
* the caller records `last_llm_failure = "set_aside"` (`5140`) and lets the
  board-math fallback decide — `5357`: "if that call fails, the board-math fallback
  decides (:class:`SecondLookError`), not the doubted pick";
* `patient` is retained for diagnostic compatibility and never extends a wait.

## Controls on master

| Key | Control | Where | Merged as |
|---|---|---|---|
| T | doubted low answer waits only for the bounded doubt allowance, then fails over to board math instead of firing | `action_planner.py:8845` `_hedged_decision`, `_DOUBT_AFTER_LOW_S = 6.0` (`8843`), doubt deadline (`8955`), raise (`9023`) | `ec4836b` (#232), `b352b8a` |
| T | a usable low answer never waits for high, including within lethal reach | same method, winner path before the doubt branch | `ec4836b`, `b352b8a` |
| P | call budget clamped to the client hard timeout / caller's `request_timeout_s`, deadline fixed at request start; a cut stream closes instead of running on | `backends/proxy.py:1340–1343`, stream closure raising `LLM streaming time budget exhausted` | `d27b878` (#222), `87faace` (#248) |
| P | our own budget cut is never retried, retries share one budget, and an exhausted budget starts no further request | retry loop condition, metrics status map | `d27b878`, `87faace` |
| F5 | a planner call whose waiter exits exceptionally gets its own cancel token set; repair keeps a fresh token and the original deadline | `action_planner.py:1510–1523` `_bounded_complete` | `4359d67` |

All five commits are ancestors of local master (`git merge-base --is-ancestor`
checked for each).

## Verification

The audit's node IDs for this ID were run unchanged on master:

```
$ .venv_mac_311/bin/python -m pytest <the 11 audit node IDs for #269> -q
13 passed in 14.77s

$ .venv_mac_311/bin/python -m pytest tests/test_llm_latency_222.py tests/test_typed_budget_253.py -q
12 passed in 15.35s
```

The acceptance points this ID turns on:

* **the reported mechanism** — `test_doubted_low_answer_hands_over_after_a_bounded_wait`
  drives an instant low answer that a doubt function questions and a high leg that
  sleeps far past the doubt window, then asserts `SecondLookError` is raised after
  5–9.5 s rather than after the old ~15 s + grace wait. The docstring records why:
  "The high leg used to be waited out to its full 15 s + 2 s grace (#222)."
* **the guard stays** — the same test's doubt function returns a face-burn reason,
  so passing it requires keeping the false-lethal guard rather than deleting it to
  save time.
* **patient does not extend** — `test_patient_window_does_not_extend_the_doubt_budget`
  pins that a patient window still cancels the high leg at the doubt allowance.
* **budget and cancellation** — the remaining node IDs pin that a second opinion only
  receives the remaining budget, a slow legacy backend is bounded, our own budget cut
  is never retried, and an outstanding call is cancelled.

Full output: [focused-tests.txt](focused-tests.txt).

## Remaining limits

* **No exact board replay.** The triage asks to "replay doubt/fallback outcome on
  exact board"; traces `211d4ccc` and `87cac475` are absent from this checkout, so this
  receipt verifies the mechanism on synthetic boards instead.
* **Fallback time versus Arena clock is not qualified live.** The bounded wait is
  6 s plus whatever the low leg already spent; whether that lands inside a real Arena
  rope under gateway load has not been measured.
* **The false-lethal guard must not be removed to reduce latency**, per the triage.
  This receipt records that constraint; it does not weaken or bypass it.
* No live qualification against api.mtgacoach.com under real gateway load.
* No Linux CI receipt for this commit yet.

## BLOCKER

Independent review and a real Linux CI run are still required before this item can
move past `merged_awaiting_review`. Nothing here was verified by its author.
