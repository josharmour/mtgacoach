# WP-3 B1 Parser Fix: Issue #430

## Bug 1: Swapped battlefield_self / battlefield_opp

### Root Cause
Two interacting mechanisms:

1. **Stale `_perm_buffer` across decisions**: The `_perm_buffer` in `_ThreadState`
   was never cleared between `chose_action` lines. When the opponent's MCTS bot
   called `printBattlefieldScore` before the first PlayerA chose_action (showing
   the opponent's starting board), that permanents entry sat in the buffer and
   got paired with the first permanents of the actual decision.

2. **Turn-player ambiguity in `printBattlefieldScore`**: In MageZero self-play,
   both PlayerA and PlayerB are controlled by separate MCTS bots. When
   `ComputerPlayerMCTS.printBattlefieldScore` is called, it prints:
   - The HAND of the current turn player
   - The PERMANENTS of the current turn player (first)
   - The PERMANENTS of the other player (second)

   When PlayerA's bot called it, the first permanents was PlayerA (self). When
   PlayerB's bot called it, the first permanents was PlayerB (opponent).

### Fix
- **Clear `_perm_buffer` at each `chose_action`** so only permanents recorded
  after the decision belong to that decision.
- **Use hand-based player detection** (`_hand_player_is_a`) to determine whose
  turn it is: if the hand contains Island (absent from MonoR/G/B/W hands) it's
  PlayerA's turn; if it contains Mountain/Forest/Swamp/Plains it's PlayerB's.

### Verification
- Ran against `mz_train_smoke.log` (9789 decisions): **0 violations** where a
  UWTempo hand (2+ signature cards) has Mountain/Forest/Swamp/Plains in
  `battlefield_self`.
- Prior to fix: **2676 violations** with the original code (identical count
  to the issue description).

## Bug 2: Fabricated land availability

### Root Cause
Downstream `build_game_state` hard-coded `lands_played=0` and ignored the
play-land actions present in every decision's menu/pool.

### Fix
- Derive `lands_played` from the chosen action: if it starts with `Play `
  and names a land (basic or non-basic), increment a per-turn counter.
- Derive `land_playable`: true when the pool actions include a `Play <land>`
  action AND `lands_played_this_turn == 0`.
- Both fields are emitted on every decision record.

### Schema addition
Every decision dict now includes:
- `"lands_played": int` — number of land plays seen so far this turn (0 or 1)
- `"land_playable": bool` — whether the player can legally play a land this
  turn based on the menu and turn-1-per-turn constraint

(Count of excluded rows: all decisions are covered — no rows excluded.)

## Guardrail test
`tests/test_parse_magezero_deck_sanity.py` — three tests:
1. Synthetic fixture: asserts no UWTempo hand has opposing basics in self
2. Schema check: every decision has lands_played/land_playable
3. Full-log run: 0 violations across 9789 decisions
