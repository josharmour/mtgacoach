# BUG-275 — Autopilot LLM latency (isolated long stall)

**Verdict: verification only — no production source change.** The three stall
signatures the audit lists are each covered by work already merged to local
master, and the decision-timing record the acceptance asks for already exists
(#231). The retained trace is not in this repo, so the exact historical stall
cannot be replayed; the audit's own disposition for this ID is
`PARTIAL MECHANISM COVERED / LIVE UNQUALIFIED`.

- Source commit: none (receipt only)
- Merged to local master as: `7693b3c`
- Receipt commit: `618ec02`
- Independent verification: **not reviewed** (author cannot close this)
- Live qualification: **not exercised**

## Symptom evidence

| Source | What it says |
|---|---|
| `docs/backlog-triage-2026-11-01.md:434` | `#275 \| OPEN \| Autopilot LLM latency` — the only occurrence in the triage file |
| `docs/latency-222-id-audit-20261101.json:530` | `historical_evidence: "788e9968, a2b16e80"`; observed mechanisms: *one budget-exhaustion/19 s loop stall, first-plan lag, hop-limit signature*; companion match had no stalls |
| same entry | disposition `PARTIAL MECHANISM COVERED`, qualification `LIVE UNQUALIFIED`, unresolved prerequisite *live timing distribution and execution hop progression* |
| `docs/latency-222-triage-20261101.md:150` | the triage narrative for the same ID |

A targeted grep for both hashes across `src/`, `tests/` and `docs/` finds only
those two documentation lines — there is no retained trace, replay fixture or
test that references them. `docs/backlog-evidence-2026-11-01.md` has no #275
row, and `docs/BACKLOG_STAGE_2026-11-01.md` does not mention it.

## What covers each signature on current master

1. **Budget exhaustion / 19 s loop stall.** The proxy computes a per-call budget
   and deadline (`src/arenamcp/backends/proxy.py:1340`), checks cancellation
   between stream chunks (`TimeoutError("LLM streaming time budget exhausted")`),
   and closes the socket from the cancel watcher (`_TrackingNetworkBackend`,
   `proxy.py:880`–`900`) so a stalled read is interrupted instead of outliving
   the decision. The planner bounds its wait without joining a slow backend
   (`_wait_for_model` `action_planner.py:1133`, `_bounded_call`
   `action_planner.py:1150`) and falls back through `_llm_unavailable_plan`
   (`action_planner.py:1233`). Merged under #248/#249/#256/#260/#264.
2. **First-plan lag.** The hosted first plan gets a fast 20 s mode
   (`game_plan.py:3733`) with a deep refresh behind it, and the plan call's
   first-token limit scales with prompt size inside the planner budget
   (`action_planner.py:1038`–`1051`). Merged under #235/#253.
3. **Hop-limit signature.** Post-cast priority hops are capped at one
   (`autopilot.py:5172`) and hand off to the watchdog with a named warning
   (`autopilot.py:6107`). Merged under #266.
4. **Recording queue / first-token / generation / cancellation and the final
   submission.** `llm_trace` (#231) is started around every plan
   (`autopilot.py:2974`) and fed by the proxy (`proxy.py:1887`); the submitted
   action line carries the summary through `_decision_suffix`
   (`autopilot.py:2953`).

## Probe evidence (this item)

Run against real code paths with a fake streaming backend:

```
--- healthy slow first token (delay=0.3s timeout=5.0s)
summary: {'elapsed_ms': 1390.2, 'cancelled': 0}
  request: {'call_class': 'decision.plan', 'status': 'ok', 'lane_wait_ms': 0.0,
            'ttft_ms': 309.3, 'first_content_ms': 309.3, 'total_ms': 309.6,
            'attempts': 1, 'budget_s': 5.0}
  request: {'call_class': 'decision.attack_repair', 'status': 'ok', 'lane_wait_ms': 0.0,
            'ttft_ms': 0.3, 'total_ms': 0.3, 'attempts': 1, 'budget_s': 4.6}
describe: elapsed=1390.2ms requests=2 [decision.plan ok lane=0.0 ttft=309.3 total=309.6;
          decision.attack_repair ok lane=0.0 ttft=0.3 total=0.3]
suffix:  (decision=1390.2ms, requests=2)

--- budget cut mid-stream (delay=0.6s timeout=0.25s)
Action planning timed out after 256ms (limit 0.25s)
summary: {'elapsed_ms': 255.8, 'cancelled': 0}
suffix:  (decision=255.8ms, requests=0)
fallback: llm_unavailable actions: 0
```

Timing decomposition of one decision (timestamped wrapper probe): ~1.04 s of
prompt construction happens before the model call starts, so under the
production 15 s planner cap (`autopilot.py` `_PLANNER_BUDGET_CAP_S`) roughly
14 s of model time remains. A profile of the same call attributes setup to
`_build_action_prompt` → `coach._format_game_context` → `mcts_evaluator.evaluate`
→ `board_assessment.assess`, plus cold imports on first use.

## Bounded gaps left as-is

- **The retained trace is absent.** Both hashes exist only in the audit docs;
  the exact 19 s stall cannot be replayed here.
- **Setup time sits outside the model budget by design.** `model_deadline`
  starts after prompt construction, so a decision can take setup + budget
  (~16 s at the 15 s cap). The planner's own log line reports elapsed against
  `self._timeout`, so the overrun is visible but not prevented.
- **A timed-out decision records `requests=0`.** The cancelled call's metrics
  land after `llm_trace.finish()`, so the trace misses it; the proxy log line
  still carries `status=cancelled` and `total_ms`.
- **Live timing distribution and hop progression remain unqualified** — the
  audit's unresolved prerequisite — and there is no Linux CI receipt.

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q \
    tests/test_llm_latency_222.py tests/test_proxy_request_metrics.py \
    tests/test_typed_budget_253.py tests/test_latency_budget_222_followup.py \
    tests/test_action_plan_cancellation_222.py tests/test_latency_231_decision_trace.py \
    tests/test_latency_248_budget_cut.py tests/test_latency_249_first_token.py \
    tests/test_latency_223_background_contention.py tests/test_latency_232_combat_declaration.py \
    tests/test_declaration_budget_232.py tests/test_critical_decision_budget.py \
    tests/test_integrated_plan_repair_budget.py tests/test_recovery_trace.py
101 passed in 22.03s

$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q tests/test_priority_after_cast_206.py
18 passed in 7.41s

$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q <the 14 audit-listed node IDs>
21 passed in 14.62s
```

No source file changed, so ruff has nothing new to check; the receipt commit is
documentation only.

## BLOCKER

Independent review and a Linux CI run are still required before this ID can be
called closed. Nothing here is verified by its author, and live timing remains
unqualified.
