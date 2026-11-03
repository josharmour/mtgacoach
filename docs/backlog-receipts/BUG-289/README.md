# BUG-289 — latency fallback and stale facts during a lethal window

## Verdict

Verification only: no production source change. Both halves of the retained
recurrence are already on local master and are pinned by a new regression file.

- Latency half — the typed-decision hedge returns a usable low answer at its own
  latency and cancels the high leg instead of waiting for its budget
  (`action_planner.py` `_hedged_declaration` / `_hedged_dec...`, merged as
  BUG-265 `d94cf56`, BUG-269 `34f12f8`, typed budget BUG-253 `43679e3`,
  declaration budget BUG-276 `855129d`).
- Freshness half — a model answer naming an option that is no longer legal is
  filtered against the current decision's option ids (`action_planner.py:5049`)
  and replaced by a deterministic legal fallback (`action_planner.py:5103–5120`),
  so a stale availability fact cannot reach submission.

## Symptom and evidence

- `docs/latency-222-triage-20261101.md:156` — `|#289|220b8546|Typed low reported
  4.051 s while high exhausted budget; loop stalled 19 s; big-board fallback and
  stale strategy compounded outcome|T,P,F5|Current usable-low race test covers
  wait mechanism, not historical solver choice. Combat owner must replay #288;
  strategic owner #240/#287|`
- `docs/latency-222-id-audit-20261101.json` id 289 — disposition
  `PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`, `exact_historical_outcome_replayed:
  false`, unresolved prerequisite naming the combat owner (#288) and strategic
  owner (#240/#287).
- `docs/backlog-evidence-2026-11-01.md` has no #289 row; the cited
  `../autopilot-improvements.md` is absent from the repo.

## What current master does (probe, reconstructed decision)

Probe `/tmp/probe289b.py` drives `ActionPlanner._plan_decision_options` with a
duck-typed backend on a two-target `SelectTargets` decision:

| Case | Setup | Result |
| --- | --- | --- |
| A | critical decision (`critical_decision` → "removal"), low leg answers at 205–405 ms, high leg blocks until cancelled, budget 1.0 s | chosen `["tgt:812"]`, elapsed **0.41 s**, trace `{"winner": "low", "low_ms": 405, "wait_after_low_ms": 0}`; high leg cancelled, never awaited |
| B | model answers a stale id `tgt:999` that is not in the decision's options | log `plan_decision_options: LLM answer had no valid ids for SelectTargets` → controller-aware fallback picks legal `["tgt:812"]`; stale id never returned |

The historical "low reported at 4.051 s while high exhausted its budget" shape is
therefore bounded by the hedge's own deadline, and the stale-fact shape is
filtered before submission.

## Regression pinned

`tests/test_latency_289_stale_facts.py`:

1. `test_a_usable_low_typed_answer_is_not_held_for_the_high_leg` — critical hedge,
   low answers in ~0.2 s against a 1.0 s budget; asserts the chosen id, elapsed
   < 0.9 s, both legs attempted, `winner == "low"`, `wait_after_low_ms == 0`.
2. `test_a_stale_option_id_is_replaced_by_a_legal_fallback` — asserts the stale id
   is dropped and the returned ids are a non-empty subset of the decision's legal
   option ids.

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q <eleven audit node IDs> \
    tests/test_latency_289_stale_facts.py
...............                                                          [100%]
15 passed in 15.52s

$ .venv_mac_311/bin/python -m ruff check tests/test_latency_289_stale_facts.py
All checks passed!
$ .venv_mac_311/bin/python -m ruff format --check tests/test_latency_289_stale_facts.py
1 file already formatted
```

The eleven audit node IDs are the ones listed for id 289 in
`docs/latency-222-id-audit-20261101.json`.

## Remaining limits

- The historical solver choice (combat owner #288) and the strategic compounding
  (#240/#287) are not replayed here; this item pins the latency and freshness
  mechanisms only.
- Live timing distribution remains unqualified (`PARTIAL_MECHANISM_COVERED_LIVE_UNQUALIFIED`);
  one quiet game would not close it.
- The probe uses a reconstructed two-target decision, not the original packet.
- No Linux CI receipt was produced locally; independent review is still required.
