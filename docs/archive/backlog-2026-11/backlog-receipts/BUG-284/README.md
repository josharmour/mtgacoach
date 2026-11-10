# BUG-284 — exactly-lethal attack forecast vs blockers and lifelink

## Verdict

Verification only: no production source change. Current master already counts their
blockers, damage order and net lifelink in the declaration forecast, so the reported
"exactly lethal on paper, opponent survives" claim is not reproducible on the current
code. A new regression file pins the forecast against the combat resolver so the
claim cannot come back silently.

- Base commit: `0fde30a` (local master before this item)
- Source commit: `e78789a`
- Merged to local master as `4be2eaf` (receipt commit)
- Receipt: this file + `focused-tests.txt`

## Symptom and evidence

Queue row: "An exactly-lethal attack forecast ignores blockers or lifelink and the
opponent survives."

- `docs/backlog-triage-2026-11-01.md:443` — `| #284 | OPEN | Strategy / combat math |`
  (the only occurrence of #284 in the triage file).
- `docs/autopilot-improvements.md` — absent from this checkout; the queue's evidence
  path cannot be read here.
- `docs/backlog-evidence-2026-11-01.md` — no #284 row.
- No historical match packet for #284 exists in the repo, so the boards below are
  reconstructed from the symptom, not replayed.

## What current master does

The declaration forecast is `board_assessment.assess`, which computes `lethal_now`
from `simulate_attacks` against their untapped blockers (their best blocks), and the
combat solver's `optimal_blocks` / `_resolve_attacker` carry `damage_through`,
`blocker_life_gained` and damage order. Probes on current master:

1. Keyword grid (`/tmp/probe284b.py`): 576 combinations of attacker keyword × blocker
   keyword × attacker P/T × blocker P/T at 4 opponent life. Zero mismatches between
   `optimal_blocks` totals and `_resolve_attacker`, and zero mismatches between
   `assess(...).lethal_now` and "defender's best blocks lose them their life total".
2. Multi-blocker damage order (`/tmp/probe284c.py`): four two-blocker boards (trample,
   deathtouch, double lifelink, first strike) — forecast totals equal resolver totals
   in every case.
3. Contrast pair (`/tmp/probe284.py`): 6/6 trample against 4 life is `lethal_now=True`
   with a plain 2/2 blocker and `lethal_now=False` with a 2/2 lifelink blocker; the
   posture follows (`lethal` vs `attack`).

## New regression

`tests/test_lifelink_lethal_forecast_284.py` (27 tests):

- `test_lifelink_blocker_turns_exact_lethal_into_a_nonlethal_attack` — the contrast
  pair above: no lethal claim when their lifelink makes the swing short.
- `test_single_blocker_totals_match_the_resolver` — 16 keyword/P-T combinations:
  forecast `damage_through` and `blocker_life_gained` equal what combat deals.
- `test_multi_blocker_damage_order_totals_match_the_resolver` — four two-blocker
  boards, damage order included.
- `test_lethal_now_matches_the_defender_best_net_life_loss` — six boards where the
  lethal claim must equal "their best blocks lose them their life total".

## Bounded remaining gap (not fixed here)

`combat_strategy._best_through` (`src/arenamcp/combat_strategy.py:1033`) returns the
plan's raw `damage_through` without subtracting the blockers' lifelink, and the
losing-attack guard's lethal test (`combat_strategy.py:1571`) uses it. That is an
unsupported exact-lethal claim inside the guard, but it errs toward keeping attackers
rather than thinning them, and a probe (`/tmp/probe284d.py`) found no board where
subtracting lifelink changes the guard's outcome: dropping an attacker never raises
the net damage through, so the thinned attack never beats the declaration on these
boards. Left as-is rather than changing five call sites of a shared helper without a
reproduction that shows a wrong user-facing claim.

## Verification

```
$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q tests/test_lifelink_lethal_forecast_284.py
27 passed in 7.01s

$ PYTHONPATH=. .venv_mac_311/bin/python -m pytest -q \
    tests/test_lifelink_lethal_forecast_284.py tests/test_blocker_lifelink_284.py \
    tests/test_extra_combat_damage_284.py tests/test_combat_persistence_284.py \
    tests/test_false_lethal_claim.py tests/test_defender_hold_278.py \
    tests/test_combat_strategy.py tests/test_combat_solver_budget.py \
    tests/test_combat_double_strike_lifelink.py
112 passed in 2.01s

$ .venv_mac_311/bin/python -m ruff check src tests
All checks passed!

$ .venv_mac_311/bin/python -m ruff format --check src tests
1008 files already formatted
```

## Limits

- Reconstructed boards; no historical #284 packet to replay.
- No live qualification and no Linux CI run from this machine (macOS venv, Python
  3.11.15; CI uses Python 3.12).
- Independent review is still required; nothing here is marked verified by the author.
