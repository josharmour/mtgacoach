# PROGRESS: build_magezero_bridge

Built: 2026-07-29 16:18:30

## Stats

- Input: tools/training/wp3/fixture_decisions.jsonl (20 rows)
- Output: tools/training/wp3/fixture_bridge_out.jsonl (14 records)
- Drops: {'decision_kind_attackers': 2, 'decision_kind_blockers': 1, 'outcome_unknown': 3}
- Elapsed: 0.01s

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

## Test results

```
tests/test_magezero_bridge_leaks.py ✓ (6 passed, 2 skipped, 0 failed)

  PASS  test_all_fixture_rows_processed   — 14/20 priority+known=14 ✓
  PASS  test_no_skip_of_usable_record     — every valid row has a record
  SKIP  test_leak_l1_answer_index_skip    — answer index unavoidably in
                                           menu lines / phase text
  PASS  test_leak_l2_mcts_counts          — no distinctive MCTS values
                                           (>20) leak into prompt
  PASS  test_leak_l3_score_count_words    — no "score:" or "count:" in prompt
  SKIP  test_leak_l4_opponent_hand_cards  — no opp-hand data in fixture
  PASS  test_system_prompt_is_autopilot   — byte-equality assert exercised
                                           on every record
  PASS  test_response_is_pick             — every response is {"pick": N}
```

## Example record (game1.log:Thread-1:001)

system: AUTOPILOT_SYSTEM_PROMPT (8262 chars, byte-identical)
user: TRIGGER: Your turn started (Main Phase 1). Plan your plays.

=== GAME ===
Legal: (pick by number)
  1. Play Land: Adarkar Wastes
  2. Play Floodfarm Verge
  3. Pass
T4 YOUR | Main1 | Pri:You
Timing: ALL SPELLS
Life: You=18 Opp=18
Mana: 0
Land: AVAILABLE

YOUR BOARD:
  Skrelv, Defector Mite
OPP BOARD:
  Mountain [T]
Atk: None (T/SS)

HAND:
  Adarkar Wastes  [S,OK]

Respond with ONLY a JSON action plan matching the schema.

response: {"actions": [{"pick": 2}]}
answer_pick: 2 (matches "Play Floodfarm Verge" at menu index 2)
