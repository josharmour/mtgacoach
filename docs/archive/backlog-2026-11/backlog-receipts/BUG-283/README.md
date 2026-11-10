# BUG-283 — streaming budget exhaustion stall

## Verdict

Verification-only: **no production source change**. Current master already bounds
every planner model wait by its own deadline, so a stream that never finishes is
cut at the budget and the decision falls back to legal board math. The retained
"15–18 s" figure is the 15 s decision budget plus prompt construction (about one
second warm, measured for #275), not an unbounded stall.

Merged to local master as fa0e292 (receipt commit dde78f7).

## Symptom and evidence

- Queue row: "Streaming budget exhaustion still causes 15–18-second coaching stalls."
- `docs/latency-222-id-audit-20261101.json` entry `id: 283`: source ledger line
  889, historical evidence `341f42fc, 93237e88`, observed mechanisms "15–18s loop
  stalls and one budget exhaustion; correct board-math fallback in companion
  match", disposition `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, unresolved
  prerequisite "Per-call/chain correlation under load; correct fallback is not
  evidence the full latency defect is closed."
- `docs/backlog-triage-2026-11-01.md:442`: `| #283 | OPEN | Autopilot / LLM latency |`
  (the only occurrence in the triage file).
- `docs/backlog-evidence-2026-11-01.md`: no #283 row.
- `../autopilot-improvements.md`: absent from this checkout, so the archived
  original table row cannot be read here.

## Current-path mechanism (master)

1. `action_planner.py:1486` sets `model_deadline = time.monotonic() + self._timeout`
   **after** prompt construction, so prompt build sits outside the model budget.
2. `_bounded_call` / `_wait_for_model` (`action_planner.py:1133`, `1150`) poll the
   future every 50 ms and raise `TimeoutError` at that deadline; the pool is shut
   down with `wait=False`, so a slow backend is never joined.
3. Inside the proxy, `proxy.py:1995–1996` checks the streaming budget per chunk and
   raises `TimeoutError("LLM streaming time budget exhausted")`; `proxy.py:1618–1625`
   plus `_budget_cut_stream` mark that error non-retryable, so a cut stream costs
   exactly one attempt.
4. The planner's except chain maps the timeout to fallback reason `llm_unavailable`
   and returns the deterministic board-math plan (an empty declaration holds every
   attacker, which is legal).

## Reproduction recorded

Probe (`/tmp/probe283.py`, real `ActionPlanner` + `ProxyBackend` with a fake client
whose stream yields a first token then sleeps 0.4 s per chunk):

| case | timeout | elapsed | fallback | actions |
| --- | --- | --- | --- | --- |
| both legs stream forever | 3 s | 3.87 s | `llm_unavailable` | `[]` |

Elapsed decomposes as prompt build ≈0.87 s + the full 3.0 s budget — i.e. the wait
is bounded by the deadline, and the overrun past the budget is prompt construction,
not streaming. Scaled to production that is 15 s + ~1 s ≈ 16 s, inside the retained
15–18 s band.

Probe artifact recorded honestly: an earlier probe passed no legal-action menu, so
the parser rejected every answer (`legal_actions=[]`) and a single low leg consumed
its whole budget. With the real menu (`MENU = ["Attack with: Odin (5/5)", "Done"]`)
the low leg wins in 27 ms — see the second regression below.

## What the new regression pins

`tests/test_latency_283_stream_stall.py`:

1. `test_a_stream_that_never_finishes_is_cut_at_the_deadline` — both hedge legs
   stream past a 1 s budget; the plan returns at its deadline (≥0.9 s, < budget +
   3 s prompt allowance), fallback reason is `llm_unavailable`, and no partial
   stream text is submitted (`actions == []`, a legal hold).
2. `test_a_streaming_high_leg_cannot_outlive_a_ready_low_answer` — #277's hedge fix
   holds for this item too: low answers at once, high is cancelled, and the legal
   `DECLARE_ATTACKERS` action is returned well inside the 6 s budget.

## Verification

```
PYTHONPATH=. .venv_mac_311/bin/python -m pytest tests/test_latency_283_stream_stall.py -q
2 passed in 5.04s

PYTHONPATH=. .venv_mac_311/bin/python -m pytest tests/test_latency_248_budget_cut.py \
  tests/test_latency_249_first_token.py tests/test_proxy_request_metrics.py \
  tests/test_action_plan_cancellation_222.py tests/test_latency_276_declaration_budget.py \
  tests/test_latency_277_delayed_winner.py tests/test_latency_283_stream_stall.py \
  tests/test_coaching_stall_watchdog.py -q
28 passed in 10.84s

PYTHONPATH=. .venv_mac_311/bin/python -m ruff check tests/test_latency_283_stream_stall.py
All checks passed!
PYTHONPATH=. .venv_mac_311/bin/python -m ruff format --check tests/test_latency_283_stream_stall.py
1 file already formatted
```

## Remaining limits

- Live timing distribution is unqualified: these are deterministic fakes, not a
  measurement against api.mtgacoach.com under load (audit disposition unchanged).
- Prompt construction (~1 s warm) still sits outside the model deadline, so a full
  15 s budget can occupy ~16 s of the coaching thread. Shortening it would change
  the deadline semantics this item's acceptance asks to preserve, so it is left as-is.
- When the planner's bounded wait fires before the proxy records metrics, the
  decision trace can show `requests=0` for that decision — recorded as a bounded gap
  by #275 and not re-fixed here.
- Independent review and Linux CI are still required; nothing here is verified by
  the author.
