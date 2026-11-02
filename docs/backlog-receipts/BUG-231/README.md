# BUG-231 — Autopilot / LLM latency: decision-scoped request trace

Verdict: **bounded instrumentation added on local master; no behavioural change
to deadlines, freshness or fallback.** The retained recurrence is a 15–31 s
whole-loop stall that the per-request `[PROXY] Request metrics` lines could not
attribute: one decision can issue a hedged pair, a repair retry and a second
opinion, and those lines interleave with background work. This change ties the
requests of one decision together and prints them next to the action that was
finally submitted.

## Acceptance map

| Acceptance point | Where it is met |
|---|---|
| Locate this ID's retained trace | `docs/latency-222-id-audit-20261101.json` id `231` (`source_ledger_line` 837, evidence `d90d0eae`, `e276ca14`); `docs/latency-222-triage-20261101.md` bucket "budget exhaustion / no first token" and its per-ID supplement row |
| Reproduce its current-path mechanism | `tests/test_latency_231_decision_trace.py::test_a_stalled_decision_records_queue_first_token_and_generation` drives a real `ProxyBackend` through an answered call and a first-token timeout inside one open trace; `::test_a_cancelled_request_is_recorded_with_the_decision` covers the cancellation path |
| Record queue / first-token / generation / cancellation timing | `src/arenamcp/llm_trace.py` keeps `lane_wait_ms`, `client_setup_ms`, `ttft_ms`, `first_content_ms`, `total_ms`, `status`, `attempts`, `budget_s` per request; `RequestTrace.summary()` adds the decision's wall clock and a cancellation count |
| Record the final submission | `AutopilotEngine._plan_with_trace` logs `[AUTOPILOT] Decision trace: … fallback=… actions=…`; `_log_execution_path` appends `(fallback=…, decision=…ms, requests=…, cancelled=…)` to every submitted action line |
| Preserve deadline, freshness and legal fallback | No planner, proxy or autopilot decision logic changed: the trace only observes. The existing deadline/cancel/fallback tests still pass (below) |
| If an earlier queue item already fixes it, reference that commit | The merged #222 repairs (hedged declaration, shared repair deadline, reform cooldown, hosted first-plan effort) are reused unchanged; this item adds the missing attribution, not another patch |

## What changed

- `src/arenamcp/llm_trace.py` (new): one process-wide open trace; every finished
  request appends its timing fields while a decision is open. Recording never
  raises into a request or a plan, and no prompt or model output is stored.
- `src/arenamcp/backends/proxy.py`: `_record_request_metrics` also hands the
  metrics dict to `llm_trace.record(...)` — the same record that is logged.
- `src/arenamcp/autopilot.py`: both planning call sites go through
  `_plan_with_trace`, which opens/closes the trace and logs the decision line;
  `_log_execution_path` appends the decision suffix to each submission line.

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_latency_231_decision_trace.py -q
7 passed in 1.55s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_231_decision_trace.py \
    tests/test_proxy_request_metrics.py tests/test_proxy_cancellation.py \
    tests/test_action_plan_cancellation_222.py tests/test_llm_latency_222.py \
    tests/test_latency_223_background_contention.py tests/test_proxy_call_classes.py -q
60 passed in 49.85s

$ .venv_mac_311/bin/python -m ruff check src/arenamcp/llm_trace.py \
    src/arenamcp/autopilot.py src/arenamcp/backends/proxy.py \
    tests/test_latency_231_decision_trace.py
All checks passed!

$ .venv_mac_311/bin/python -m ruff format --check <same files>
4 files already formatted
```

## Remaining limits

- **No live qualification.** The historical stall (`d90d0eae`, `e276ca14`) was
  not replayed against the gateway; the audit's unresolved prerequisite —
  matching request start/end spans and chain waits to that stall — still needs a
  live loaded session. This change makes that match possible from one log line;
  it does not perform it.
- **One decision at a time.** The trace is process-wide and single-slot: a
  second decision opening while one is planning replaces it (the first keeps its
  own summary). Autopilot plans one decision at a time, so this is not expected
  in play.
- **Background work is only visible when it lands inside the decision window.**
  A game plan that started before planning and finished after it is not in the
  trace; its own metrics line still carries its lane wait.
- **No Linux CI receipt.** Only the macOS Python 3.11 venv ran these tests.

BLOCKER: independent review and a real Linux CI run are still required before
this item can be called verified; the implementer may not claim either.

## Follow-up repair (cf7ad72)

The suffix read `_last_decision_trace` and `last_plan_fallback_reason` directly.
Both are set in `AutopilotEngine.__init__`, so the twenty test files that build an
engine with `AutopilotEngine.__new__` raised `AttributeError` from
`_log_execution_path`; eight block-declaration tests failed on master after this
merge. `_decision_suffix` now reads both attributes defensively — the suffix is
log decoration and must never break a submission. Production still sets them in
`__init__`.

```
$ .venv_mac_311/bin/python -m pytest -q tests/test_block_declaration_20261019.py \
    tests/test_attack_tax_loop_210.py tests/test_combat_safe_defaults.py \
    tests/test_select_from_groups_203.py tests/test_stack_order_20261019.py
88 passed in 10.81s
```
