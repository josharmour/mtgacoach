# PROGRESS: build_magezero_bridge

Built: 2026-08-13 21:19:12

## Stats

- Input: tools/training/wp3/fixture_decisions.jsonl (20 rows)
- Output: tools/training/wp3/fixture_bridge_out.jsonl (14 records)
- Drops: {'decision_kind_attackers': 2, 'decision_kind_blockers': 1, 'outcome_unknown': 3}
- Elapsed: 0.47s

## Drops

- outcome_unknown: 3
- decision_kind_attackers: 2
- decision_kind_blockers: 1

## Known gaps

- Card facts: only basic land type lines are resolved; non-basic
  permanents render without type/oracle info in the prompt.
- Mana computation: non-basic lands without basic-land-name-in-name
  contribute 0 mana to the pool.
- Leak scan: fixture-only test (no real gate corpus to check against).

## Verified

- R1: system prompt identity assert exercised.
- R2: attackers/blockers skipped and counted.
- R3: unknown-outcome rows skipped and counted.
- R4: answer index from menu match.
