# BUG-276 — Autopilot / LLM latency

## Verdict

Verification-only: **no production source change**. The retained recurrence —
"high-effort planning exceeds its budget and resumes only after the decision
delay" — is already covered on current master by the bounded declaration hedge
(`b352b8a`, per-leg cancel events from `70d9e2f`) together with the proxy budget
cut (`d27b878`/`9f0a328`). The new regression pins the half that was not pinned
before: a high leg that has *already produced its first token* and keeps
streaming cannot hold a ready low declaration, and when no leg yields a usable
answer the streaming leg is cut at its own budget and replaced by the
deterministic legal fallback.

Merged to local master as 855129d; receipt commit 8ff78b7.

## Symptom and evidence

- Queue row: `docs/autopilot-active-work.json` item `BUG-276`.
- Triage row: `docs/backlog-triage-2026-11-01.md:435` — `| #276 | OPEN | Autopilot / LLM latency |`.
- Audit entry: `docs/latency-222-id-audit-20261101.json` id 276 —
  source ledger line 882, historical evidence `228fed6f`, `750806ad`,
  observed mechanisms "decision.plan high used full15s despite first token at5.4s;
  loop resumed around22s.", control keys P/F1/F5,
  disposition `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`,
  unresolved prerequisite "Check whether historical low leg was ready; validate
  first-usable declaration and total loop timing. No causal loss established.",
  `exact_historical_outcome_replayed: false`.
- Neither evidence hash appears anywhere under `src/`, `tests/` or `docs/`
  except the audit and triage rows themselves, so there is no retained trace to
  replay. The reproduction below is deterministic, not a replay of the incident.

## What master already does

1. **First usable answer wins** — `action_planner.py:1162` `_hedged_declaration`
   races a high and a low leg, checks each answer with the ordinary parser's
   `usable` predicate, and returns the first usable one immediately
   ("Declaration plan: low won in 27 ms; other effort not awaited"). Both legs
   carry per-leg cancel events that are set in the method's `finally` block.
2. **One shared deadline** — `action_planner.py:1486` sets
   `model_deadline = min(now + self._timeout, arena_deadline)`;
   `_bounded_call` (`1150`) and `_wait_for_model` (`1133`) poll the future in
   ≤50 ms slices against that deadline, and a post-response repair reuses the
   same deadline instead of extending it.
3. **Budget cut mid-stream** — `proxy.py:1340`
   `budget = 120.0 if background else self._CLIENT_HARD_TIMEOUT_S`, clamped by
   `request_timeout_s`. A cut after the first token is classified from the
   budget/stall path and is never retried (`proxy.py:1433–1512`).
4. **Legal fallback** — when no model answer parses, the planner returns the
   deterministic board-math line (`action_planner.py:2775`,
   `fallback_reason = "planner_free_attack"`) rather than an empty plan.

## Measured on current master (this checkout)

Both cases run through the real `ProxyBackend` with a fake OpenAI client whose
high leg streams a reasoning chunk immediately and then one content chunk every
50 ms for 2 s; the low leg answers at once.

| Case | Low leg | High leg | Decision |
| --- | --- | --- | --- |
| Streaming high + ready low (`timeout=2.0`) | `ok`, ttft 0.3 ms, total 0.4 ms | `cancelled`, ttft 0.9 ms, total 58.7 ms | low won in 27 ms; declaration returned |
| Streaming high + unusable low (`timeout=1.0`) | `ok`, total 0.2 ms (unparseable) | cut at total 1014.9 ms against a 1.0 s budget | deterministic free attack returned |

The first case's wall clock also includes ~0.9 s of cold prompt construction on
the first test in the process; the regression measures model time separately so
that setup cannot mask an overrun.

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest tests/test_latency_276_declaration_budget.py -q
2 passed in 7.79s

$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q tests/test_latency_276_declaration_budget.py \
    tests/test_latency_232_combat_declaration.py tests/test_declaration_budget_232.py \
    tests/test_latency_231_decision_trace.py tests/test_latency_248_budget_cut.py \
    tests/test_latency_249_first_token.py tests/test_proxy_request_metrics.py \
    tests/test_action_plan_cancellation_222.py tests/test_llm_latency_222.py
48 passed in 18.94s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_276_declaration_budget.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_276_declaration_budget.py
1 file already formatted
```

Full output: [focused-tests.txt](focused-tests.txt).

The hedge half was already pinned at planner level by
`tests/test_latency_budget_222_followup.py::test_ready_low_declaration_cancels_high_before_its_budget_expires`
(duck backend, no proxy). The new file adds the proxy-level streaming cut, the
first-token-seen classification, and the decision-trace accounting that the
audit's test list did not cover.

## Remaining limits

- **Live timing distribution unqualified.** No live-service trace and no Linux
  CI run; `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED` stands.
- **The ~22 s loop resume is not fully explained by the planner.** A 15 s budget
  plus ~1 s of prompt construction leaves roughly 6 s, which matches the
  post-cast priority waits already attributed to #206/#243 in the BUG-248
  receipt; that tail is not re-opened here.
- **Prompt construction is outside the model deadline** (~0.9 s cold, ~0.03 s
  warm in these runs). It is not bounded by this item.
- Independent review and Linux CI are still required; nothing here is verified
  by the author.
