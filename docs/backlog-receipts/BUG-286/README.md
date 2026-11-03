# BUG-286 — a failed high-effort declaration does not stall the Sneak guard

## Verdict

Verification only: **no production source change**. The retained recurrence is
already covered on current master by two merged mechanisms, and this item pins
the specific regression with a new test file:

1. The deterministic Sneak guard runs **before any model call**
   (`ActionPlanner.sneak_policy`, `src/arenamcp/action_planner.py:4800`, called
   at `action_planner.py:4970` after `filter_play_options`/`drain_loop_pick`),
   so a slow or failed declaration cannot delay it.
2. The declaration hedge is bounded by the planner deadline
   (`model_deadline` at `action_planner.py:1486`, hedge call at
   `action_planner.py:1537`); when both legs fail the plan returns
   `FALLBACK_LLM_UNAVAILABLE` with no actions, and the bridge's authoritative
   attack override (`_authoritative_attack_action`,
   `src/arenamcp/autopilot_bridge.py:807` → `_all_in_attack_action` at
   `autopilot_bridge.py:830`) recovers the lethal Sneak line in milliseconds.

Merged to local master as MERGE COMMIT (receipt commit SOURCE COMMIT).

## Symptom and evidence

Queue row (verbatim): "A failed high-effort declaration stalls before a
deterministic guard recovers." Acceptance asks to locate the retained trace,
reproduce the current-path mechanism, record queue/first-token/generation/
cancellation timing and the final submission, and to reference an earlier
master commit instead of duplicating a patch when one already fixes it.

Evidence located:

- `docs/backlog-triage-20261101.md:445` — `|#286|OPEN|Autopilot / LLM latency|`
  (the only triage row for this ID).
- `docs/latency-222-triage-20261101.md:155` — `|#286|012cb47: High declaration
  exhausted budget before deterministic Sneak guard converted lethal; illegal
  pass/fallback retry is distinct.|F1,P,F5| Verify accepted timely declaration
  without weakening lethal guard; parser/legality retry requires exact-case
  owner qualification.|`
- `docs/latency-222-id-audit-20261101.json` id 286 — source ledger line 892,
  historical evidence `012cb47`, disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, exact historical outcome
  replayed `false`.
- `docs/backlog-evidence-20261101.md` — no row for #286.
- `docs/autopilot-improvements.md` — absent from this checkout (the queue's
  evidence path does not resolve).

## What current master does (probe, `/tmp/probe286.py`)

Board: Leonardo T12 declare-attackers window from ledger #156/#159
(`tests/leonardo_boards_20261019.py`), 20 power on the menu plus Sneak Leonardo
(14) against their 17 life.

| Case | Setup | Observed |
|---|---|---|
| A timely low declaration | low answers immediately, high streams | low won in **25 ms**; plan declares all **15** attackers; elapsed 1.11 s (prompt build) |
| B failed high declaration | high streams past the deadline, low raises | plan returns `llm_unavailable`, no actions, at **0.60 s** (the 0.6 s planner budget); guard then returns the all-in Sneak line in **2 ms** with all 15 IDs |
| C guard not weakened | their life 60, our life 40 | guard returns `None` — no lethal line, no all-in verdict |
| D cancellation accounting | proxy backend, high leg cancelled at 0.2 s | trace records `decision.plan cancelled ttft=None total=505.2` plus `decision.plan ok ttft=0.1 total=0.2`; cancelled leg returns the `[BACKEND ERROR] request cancelled (cancelled)` sentinel |

The guard names each attacker by instance ID (`autopilot_bridge.py:855–860`,
ledger #208), so the recovered declaration matches the menu.

## Regression pinned

`tests/test_latency_286_sneak_guard.py` (new):

- `test_a_failed_high_declaration_still_reaches_the_sneak_guard` — both legs
  fail inside a 0.6 s budget; the plan is `llm_unavailable` with no actions and
  the guard still returns all 15 attackers with "Sneak" in its reasoning.
- `test_a_timely_low_declaration_is_not_overridden_by_the_guard` — a fast low
  answer declares all 15; the override names the same attackers, never fewer.
- `test_the_guard_still_declines_when_no_line_is_lethal` — at 60/40 life the
  guard declines, so the recovery is not an unconditional all-in.

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q <ten audit node IDs> tests/test_latency_286_sneak_guard.py
17 passed in 9.37s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_286_sneak_guard.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_286_sneak_guard.py
1 file already formatted
```

The ten audit node IDs are the ones listed for id 286 in
`docs/latency-222-id-audit-20261101.json` (hedge cancellation, budget-cut
health failure, retry budget sharing, streaming budget stop, plan cancellation);
they expand to fourteen tests because several are parametrized.

## Remaining limits

- The audit's unresolved prerequisite stands: an **illegal pass/fallback retry**
  (parser or legality rejection of an otherwise timely declaration) is a
  distinct case that needs its own exact-case reproduction and owner
  qualification. This item does not claim it.
- Live timing distribution is unqualified: no live match exercised this path,
  and there is no Linux CI receipt for this checkout.
- The board is reconstructed from ledger #156/#159; the original #286 packet is
  not in the repo.
