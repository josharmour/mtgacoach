# WP3: Deck Diversity — Progress

## Deliverables
- [x] `tools/training/wp3/deck_inventory.py` — parses all 29 .dck files
- [x] `tools/training/wp3/deck_inventory.json` — machine-readable inventory
- [x] `tools/training/wp3/deck_inventory.md` — markdown summary table
- [x] `configs/run_diverse.yml` — recommended opponent set
- [x] `tests/test_deck_inventory.py` — parser tests
- [x] `configs/run_diverse.yml` deployed to `/Volumes/repos/magezero/configs/run_diverse.yml`

## Key Findings

### Card Count Reconciliation
- **Union (all 29 decks): 473** distinct card names
- **Claimed: 471** — delta of +2 explained by:
  - The original count likely used the simpler regex from `resolve_cards.py`
    (`\w+:\w+` for set:num), which misses:
    - Alphanumeric collector numbers (`LCI:410a` → Cavern of Souls)
    - `*` foil markers (`WAR:221*` → Teferi, Time Raveler)
    - `SB:` sideboard lines (Oathbreaker_UR has 2 SB cards)
  - After handling all three edge cases, the count is 473

### Current Pool
- UWTempo + 5 Standard-Mono opponents: **83 distinct cards**

### Optimal Opponent Set (5 additional)
Exhaustive enumeration of all 23 candidate decks finds the greedy-optimal 5:

| Rank | Deck | Cards | New vs Current | Colour | Archetype |
|------|------|-------|---------------|--------|-----------|
| 1 | Oathbreaker_UR | 46 | 41 | UR | Storm/Combo |
| 2 | GBLegends | 37 | 31 | WUBRG | 5c Goodstuff |
| 3 | EVG_Elves | 28 | 27 | G | Tribal Aggro |
| 4 | EVG_Goblins | 28 | 27 | R | Tribal Aggro |
| 5 | Mind(MindvsMight) | 29 | 27 | UR | Spellslinger |

**Total new cards added**: 152
**New pool**: 235 distinct cards (2.8× current)

Role diversity gained: combo, tribal, 5c goodstuff, spellslinger —
none of which the current 5 mono opponents expose Gemma to.

### Wall-Clock Cost
- Throughput: ~120 games/hr
- Per opponent per generation: 200 games / 120 gph ≈ 1.67 hr
- With 6 generations: 6 × 1.67 = **10 hr per opponent in serial**
- **Running in parallel (10 opponents)**: wall-clock is still 1.67 hr/gen × 6 = **10 hr total**
- Adding 5 opponents instead of 0 adds **zero wall-clock cost** when parallelized
