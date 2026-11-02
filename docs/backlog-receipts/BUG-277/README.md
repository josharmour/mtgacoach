# BUG-277 — Autopilot / LLM latency: slow declaration leg vs a usable low answer

## Verdict

Verification-only. No production source change was needed: the delayed-winner
signature is already fixed on local master by the bounded declaration hedge
(`src/arenamcp/action_planner.py:1162`, b352b8a) with per-leg cancel events
(70d9e2f), the proxy budget cut (`src/arenamcp/backends/proxy.py:1340`,
d27b878 / 9f0a328) and the deterministic free-attack fallback
(`action_planner.py:2775`). This item references those same master commits
instead of duplicating a patch.

New regression: `tests/test_latency_277_delayed_winner.py` (3 tests) drives the
real `ProxyBackend` so queue/first-token/generation/cancellation timing is
recorded by `llm_trace` (#231) exactly as in production.

Merged to local master as MERGE COMMIT; receipt commit SOURCE COMMIT.

## Symptom and evidence

Queue row BUG-277: "A slow declaration leg exhausts budget although the
low-effort answer is usable."

- `docs/latency-222-id-audit-20261101.json` id 277: source ledger line 883,
  historical evidence `faa03ef3`, `bfd351f0`, observed mechanism "High-effort
  declaration exhausted budget then low stood and attack executed; a direct
  delayed-winner signature.", control keys F1/F5/P, disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, unresolved prerequisite "Live
  first-valid-winner/cancel confirmation and residual 33s chain timing.
  First-plan and hop-limit variants remain separate.", exact historical outcome
  replayed false.
- `docs/latency-222-triage-20261101.md`: #277 row.
- The queue's evidence paths (`../autopilot-improvements.md`,
  `backlog-evidence-2026-11-01.md`) do not exist in this checkout; both retained
  hashes appear only in the audit/triage rows, so there is no trace to replay.

## What the current path does

`declaration_effort` (`src/arenamcp/big_board.py:80`) raises the declaration to
high effort inside lethal reach or in danger. `plan_actions` then calls
`_hedged_declaration`, which submits both legs to a two-worker pool, checks each
completed answer with the ordinary parser (`usable`), returns the first usable
answer immediately and sets both cancel events in its `finally`. The losing
proxy request is cancelled by the proxy's cancel watcher (poll interval 0.1 s)
and recorded with status `cancelled`. If neither leg is usable before the shared
`model_deadline`, the deterministic free-attack line supplies a legal plan.

## Measured on current master

Each case: Odin (5/5) against their 5 life, so `declaration_effort` says high;
planner timeout 2.0 s; model time excludes prompt construction.

| Case | Model time | Low leg | High leg | Result |
| --- | --- | --- | --- | --- |
| slow but valid high answer (finishes at ~1.2 s) | 0.012 s | ok, ttft 0.3 ms, total 0.4 ms | cancelled, ttft 0.8 ms, total 1203.8 ms | attack executed (`declare_attackers`, Odin), no fallback |
| fast invalid high answer | 0.018 s | ok, ttft 0.2 ms, total 0.2 ms | ok, ttft 0.7 ms, total 0.8 ms (unparseable) | attack executed from low, no fallback |
| streaming high leg that never becomes usable | 0.001 s | ok, ttft 0.2 ms, total 0.3 ms | cancelled, ttft 0.3 ms, total 54.0 ms | attack executed from low, no fallback |

The delayed winner would show up as a model time near the high leg's own budget
(1.2 s+ in case one) with both legs `ok` and the high declaration used; none of
the three cases does.

## Controls

- Case one keeps the high leg *valid* on purpose: it would have been usable at
  ~1.2 s, so an implementation that waited for it would return late and use it.
- Case two keeps the high leg fast but unparseable: a fast invalid answer must
  not displace a valid low answer.
- Case three keeps the high leg streaming without ever being usable: only a
  budget/cancel cut can end it.
- All three assert the submitted plan (`declare_attackers`, attacker Odin) and
  an empty `fallback_reason`, so a silent fallback cannot pass as success.

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest tests/test_latency_277_delayed_winner.py -q
3 passed in 8.40s

$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q <nine files>
63 passed in 18.98s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_277_delayed_winner.py tests/test_latency_276_declaration_budget.py
All checks passed!

$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_277_delayed_winner.py tests/test_latency_276_declaration_budget.py
2 files already formatted
```

The nine-file set was `tests/test_latency_277_delayed_winner.py`,
`tests/test_latency_276_declaration_budget.py`,
`tests/test_latency_budget_222_followup.py`,
`tests/test_declaration_budget_232.py`,
`tests/test_action_plan_cancellation_222.py`, `tests/test_llm_latency_222.py`,
`tests/test_proxy_request_metrics.py`,
`tests/test_latency_231_decision_trace.py`,
`tests/test_latency_232_combat_declaration.py`.

## Test-only correction carried with this item

Running the new file before BUG-276's file exposed a brittle assertion in
`tests/test_latency_276_declaration_budget.py`: it required the hedge legs to
arrive as `["high", "low"]`, but both are submitted to a two-worker pool and
their arrival order is scheduling-dependent (`["low", "high"]` was observed).
The assertion now compares sorted legs; nothing about the behaviour under test
was weakened.

## Remaining limits

- Live first-valid-winner/cancel confirmation is still unqualified; this receipt
  is a deterministic reproduction of the mechanism on current master, not a live
  latency sample (`PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED` stands).
- The audit's residual "33 s chain timing" is not reproduced here. The planner's
  15 s budget plus ~1 s of prompt construction leaves ~6 s that matches the
  post-cast priority waits already attributed to #206/#243 in the BUG-248
  receipt; that chain was not re-measured for this item.
- Prompt construction sits outside the model deadline (~0.9 s cold, ~0.03 s warm).
- Probe artifact worth recording: reusing one mutated state dict across several
  decisions suppressed the high leg on the third decision; with fresh state per
  decision every case hedged. No production gap was found from it.
- Independent review and Linux CI are still required; nothing here is verified by
  its author.
