# WP-3: wp3-combat-adapter — MageZero combat rows → combat-gate records

## Scope
decision_kind `attackers`/`blockers` rows from decisions JSONL → production-shaped
combat training records (declare_attackers / declare_blockers answer shape).

## What was built
- `tools/training/build_magezero_combat.py` — converter
- `tests/test_magezero_combat.py` — 9 tests (leak scan + acceptance)

## Design decisions

### Attack pool
Creatures on `battlefield_self` that are non-land, non-blocking, and not tapped
(unless the creature is marked `,attacking` — attacking taps in MTG). This
approximates the legal-attacker pool without a card DB.

### Blocker pool
Non-land self creatures not marked `,attacking`, with tapped creatures
excluded EXCEPT those marked `,blocking` (MZ "tapped" flag may reflect prior
combat sub-phase rather than current blocking ability).

### Blocking → attacker pairing
If exactly ONE opponent creature has `,attacking` marker, all self blockers
map to it. Multi-attacker rows are dropped (same constraint as 17lands source
in `build_combat_decisions.py` — pairing is unknowable without game state tracking).

### Reverse marker pattern (self_attacking + opp_blocking)
69/199 blockers rows have this pattern — the most common. Creatures on self have
`,attacking` from a prior declare_attackers sub-phase; opp creatures have
`,blocking`. These rows are dropped as ambiguous — we cannot determine which
blocker → which attacker from the single-row snapshot.

### Solver lines: OFF
Every record carries NO_SOLVER_ADDENDUM. Verified by `test_no_solver_line`.

### MCTS count leak guard
`test_no_mcts_count_leak` verifies no MCTS visit count > 20 appears as a bare
number in the prompt. `test_no_score_count_words` checks for "score:"/"count:".

### Name disambiguation
Positional matching (`_match_declared_to_pool`) ensures declared attacker/blocker
names match the disambiguated pool names (`#1`/`#2` suffixes), avoiding the
`"Forsaken Miner"` vs `"Forsaken Miner #1"` mismatch.

## Output counts (smoke log: 578 combat rows)

| Metric | Value |
|--------|-------|
| Input attackers | 379 |
| Input blockers | 199 |
| Attack records | 104 (6 all_in + 98 proper_subset) |
| Block records | 78 (56 no_block + 17 partial_block + 5 block_all) |
| Dropped (attack_no_declared) | 223 (MCTS chose spell/ability before declaring attackers) |
| Dropped (attack_no_creatures) | 52 (no non-land permanents on self) |
| Dropped (block_no_markers) | 109 (no clear combat state markers) |
| Dropped (multi-attacker cannot pair) | 12 |
| Solver line leaks | 0 |
| MCTS count leaks | 0 |

## How to run
```bash
# Build
python3 tools/training/build_magezero_combat.py \
    --in /tmp/combat_in.jsonl --out /tmp/combat_out.jsonl --report

# Test
python3 -m pytest tests/test_magezero_combat.py -v
```

## Known gaps
1. No P/T or oracle text in prompts (MZ data has no grp_ids or card facts).
   Pool/board lines show card names only.
2. Blocker pool cannot distinguish creatures from auras/enchantments without a
   card DB. Non-land permanents are included; some are auras that can't actually
   block.
3. 52 attack rows dropped because player had only lands on self (no creatures to
   attack with). These could become "no_attack" records with pool size 0.
4. 69 "reversed marker" blocker rows dropped as ambiguous — a future pass with
   cross-row game state tracking could recover these.
