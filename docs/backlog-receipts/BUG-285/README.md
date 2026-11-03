# BUG-285 — Autopilot LLM latency: budget exhaustion and circuit degradation overlap planning windows

**Verdict: VERIFICATION-ONLY for the latency mechanism** (the declaration hedge,
budget-cut classification and hop limit are already merged by #232/#265/#277/#283),
**plus one instrumentation fix** so a decision's trace cannot absorb a request that
belongs to an earlier decision.

- Base commit: `6cb1f1c`
- Source commit: `8f595f3`
- Merge commit: `6ad4b01`
- Receipt commit: `8f595f3`

## Symptom and evidence

Queue row (verbatim): *"Budget exhaustion and circuit degradation overlap important
planning windows"*; existing work *"Shared deadline cancellation and fallback repairs
are merged; this retained recurrence is not independently closed"*.

- `docs/backlog-triage-20261101.md:444` — `|285|OPEN|Autopilot / LLM latency|`.
- `docs/backlog-evidence-20261101.md` — no `285` row.
- `docs/latency-222-id-audit-20261101.json` issue `285` (verbatim):
  source ledger line 891, historical evidence `78904ea7 fd68fa31 53bdd320 10f24c09`,
  observed mechanisms *"High declaration budget cut while low stood; other budget
  failures/model-unavailable fallback; and hop-limit(1)"*, control keys `F1 P T F5`,
  disposition **PARTIAL MECHANISM COVERED / LIVE UNQUALIFIED**, unresolved prerequisite
  *"Qualify declaration speed and service timeouts separately; execution owner handles
  hop progression"*, *"No proven caused loss"*, exact historical outcome replayed false.
- `docs/latency-222-triage-20261101.md:17` — the declaration hedge that waited for high
  after low answered is listed as reproduced on current code and fixed here (#232/#265/
  #277/#285/#286); validation and deployment qualification remain separate.
- `docs/latency-222-triage-20261101.md:154` — the same issue row as the audit.

The retained traces are absent from this checkout, so this is a deterministic
reproduction of the mechanism on current master, not a live latency sample.

## What current master already does

| Mechanism | Where | Effect on this signature |
|---|---|---|
| Bounded declaration hedge returns the first usable answer | `action_planner.py:1162` (`_hedged_declaration`) | a standing low declaration wins at once; the high leg is cancelled instead of being awaited to its budget |
| Per-leg cancel events | same method, `cancels = [Event(), Event()]` | the losing leg stops promptly |
| Budget-cut classification | `proxy.py:444` (`_budget_cut_stream`), used at `1533`, mapped at `1618–1625` | our own budget cut is never retried |
| Legal hold fallback | `action_planner.py:1233` (`_llm_unavailable_plan`) | both legs failing yields `llm_unavailable` with no invented action |
| Post-cast priority hop limit | `autopilot.py:5172` (`_MAX_POST_CAST_PRIORITY_HOPS = 1`) | deeper requests go to the watchdog instead of chaining |

## Probe on current master (`/tmp/probe285.py`, duck-typed backend)

| Mode | Elapsed | Efforts attempted | Fallback | Actions |
|---|---|---|---|---|
| high leg cut at its budget, low answers | 0.60 s | `['high', None]` | `''` | `DECLARE_ATTACKERS` |
| both legs cut | 0.00 s | `['high', None]` | `llm_unavailable` | none (legal hold) |
| model-unavailable sentinel on both legs | 0.00 s | `['high', None]` | `llm_unavailable` | none (legal hold) |

The high-cut case shows the low declaration standing and the plan submitted without
waiting out the high leg's budget — the historical "high cut while low stood" ordering
no longer degrades the plan.

## New regression

`tests/test_latency_285_budget_cut_hedge.py` (5 tests):

1. `test_a_high_budget_cut_keeps_the_standing_low_declaration` — high raises
   `TimeoutError("LLM streaming time budget exhausted")`, low answers after 50 ms:
   both legs attempted, no fallback, one `DECLARE_ATTACKERS`, elapsed < 1 s.
2. `test_both_legs_cut_falls_back_to_the_legal_hold` — both cut → `llm_unavailable`,
   no actions.
3. `test_model_unavailable_sentinel_falls_back_to_the_legal_hold` — sentinel on both
   legs → same legal hold.
4. `test_post_cast_priority_hops_stay_capped_at_one` — pins the hop-limit(1) half of
   the signature.
5. `test_a_request_from_an_earlier_decision_is_not_attributed_to_this_trace` — pins the
   instrumentation fix below.

## Instrumentation fix (why this item touched source)

Running the audit's node IDs together with the newer latency regressions exposed a real
attribution error in the #231 trace: a cancelled leg from the previous decision finished
after the next trace had opened and was recorded into it. The failing assertion showed a
1.0 s decision whose trace held a cancelled entry with `budget_s = 2.0` and
`total_ms = 55.2` — the previous test's high leg. That misreports exactly the timing this
item is asked to record.

Fix (`src/arenamcp/llm_trace.py`, instrumentation only): `RequestTrace.__init__` records
`started_unix_s`, and `add()` ignores metrics whose `request_started_unix_s` predates it.
Metrics without that field are still recorded, so existing callers are unaffected.
After the fix the combined set ran twice at **64 passed** with no bleed.

## Verification

See [focused-tests.txt](focused-tests.txt):

- 14 audit-listed node IDs + this item's file + #276/#277/#283 regressions → **31 passed in 12.73 s**
- `tests/test_latency_231_decision_trace.py` → **7 passed in 6.93 s**
- combined audit set run twice → **64 passed** each run (20.13 s / 19.88 s)
- `ruff check` → all checks passed; `ruff format --check` → already formatted

## Remaining limits

- **Live qualification is not exercised.** The audit disposition stays PARTIAL MECHANISM
  COVERED / LIVE UNQUALIFIED; its unresolved prerequisite (qualify declaration speed and
  service timeouts separately) needs live timing distribution, which this checkout cannot
  produce.
- No Linux CI receipt was produced locally; CI still has to run on push.
- Independent review is still required — this receipt is the author's record, not a
  verification pass.
