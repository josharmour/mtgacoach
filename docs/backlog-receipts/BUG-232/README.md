# BUG-232 — Autopilot / LLM latency: a combat declaration must not fall back

**Verdict:** no production source change is required on master `f91dabf`. The
retained #232 mechanism (a DeclareBlockers plan that times out and hands the
combat to SAFE-DEFAULT BLOCKS) is already repaired by two merged commits; this
item adds the missing combat-specific regression and the timing record.

## Retained evidence

| Source | What it says |
|---|---|
| `autopilot-improvements.md:902` | c63678d8 10:04:26 — "DeclareBlockers planning timed out and SAFE-DEFAULT BLOCKS threw Surrak and Goreclaw + both Notary bodies under a 26-power board (vs 12 life)"; backend degraded, background.game_plan cancelled twice; loop stalled 15/16/~20 s |
| `docs/latency-222-id-audit-20261101.json` `.issues[232]` | mechanisms: no first token + declare-blocker timeout; late declaration hedge could wait despite a ready low; harmful fallback strategy is a separate cause. disposition `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED` |
| `docs/latency-222-triage-20261101.md` | bucket "declaration hedge waits for high after low answered" (#232 #265 #277 #285 #286); status PARTIAL |

## Already-merged repairs this item reuses

| Commit | Date | Repair |
|---|---|---|
| `70d9e2f` | 2026-10-26 | per-leg cancel events forwarded through `complete()` to the backend call site; every leg signalled on exit (the ledger's "declaration hedge cleanup (#231/#232)") |
| `b352b8a` | 2026-10-31 | `_hedged_declaration` returns the first *usable* declaration (parser-checked) and cancels the other leg instead of waiting for high to exhaust its budget |
| `d27b878` / `9f0a328` | 2026-10-23 | #222 budget-cut health, shared deadline, cancellation of cooperative backend calls |

No patch is duplicated here: the queue asks for the retained trace to be located
and the original deadline/freshness/legal fallback preserved, which is what the
regression below pins.

## Acceptance map

| Queue acceptance | Where it is satisfied |
|---|---|
| Locate this ID's retained trace | `autopilot-improvements.md:902` (c63678d8), audit `.issues[232]`, triage row #232 — quoted above |
| Preserve the original deadline | `test_both_block_legs_timing_out_still_yield_the_legal_fallback`: both legs stall, the hedge still returns at its own deadline (`timeout=0.4`, elapsed < 2 s) with `fallback_detail == "timeout"` |
| Preserve freshness and legal fallback | same test: empty plan + `FALLBACK_LLM_UNAVAILABLE`, so the autopilot's own legal safe-default blocks still run (`autopilot_bridge.py:2797`) |
| Record queue/first-token/generation/cancellation timing and final submission | `test_a_ready_block_declaration_is_submitted_before_the_slow_high_leg`: the planner diagnostic carries `effort` ("5 attacking against our 3 life") and `elapsed_ms`; the losing high leg observes its cancel event; the submitted action is `DECLARE_BLOCKERS`. Request-level timing for the same decision comes from BUG-231's decision trace (`llm_trace.py`) |
| Reference that same master commit instead of duplicating a patch | this receipt cites `b352b8a` / `70d9e2f`; no source file changed |

## What changed

- `tests/test_latency_232_combat_declaration.py` (new, 2 tests). No production
  source change.

## Commands and results

```
$ .venv_mac_311/bin/python -m pytest tests/test_latency_232_combat_declaration.py -q
2 passed in 7.08s

$ .venv_mac_311/bin/python -m pytest tests/test_latency_232_combat_declaration.py \
    tests/test_declaration_budget_232.py tests/test_latency_budget_222_followup.py \
    tests/test_action_plan_cancellation_222.py tests/test_block_assignment_completeness.py -q
45 passed in 8.20s

$ .venv_mac_311/bin/python -m pytest tests/test_commander_combat.py \
    tests/test_latency_231_decision_trace.py -q
23 passed in 7.39s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_232_combat_declaration.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_232_combat_declaration.py
1 file already formatted
```

## Remaining limits

- No live replay of c63678d8 against api.mtgacoach.com: the regression drives a
  fake backend, so it proves the planner mechanism, not gateway latency.
- The harmful fallback *strategy* (safe-default blocks throwing creatures into a
  lethal board) is a separate cause and stays out of this item; only the
  planner's bounded chance before that fallback is pinned.
- The hedge still waits for high when low's answer is unusable — deliberate
  (#222 F1), covered by
  `test_fast_invalid_declaration_does_not_displace_the_valid_other_leg`.
- No Linux CI receipt.

## BLOCKER

Independent review and a real Linux CI run are still required before this item
can be called verified; the implementer may not claim either.
