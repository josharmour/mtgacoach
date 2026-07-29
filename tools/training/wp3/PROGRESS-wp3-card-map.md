# PROGRESS: wp3-card-map — XMage↔Scryfall Card + Action Mapping

## What was done

1. **Parsed 6 XMage deck files** — extracted 83 unique card names:
   - UWTempo.dck (60 cards, 17 named cards)
   - Standard-MonoR.dck, Standard-MonoG.dck, Standard-MonoB.dck, Standard-MonoW.dck, Standard-MonoU.dck

2. **Scryfall resolution** — all 83 names resolved EXACT:
   - Used `api.scryfall.com/cards/named?exact=...` (with fuzzy fallback)
   - Cache at `tools/training/wp3/scryfall_cache.json` (gitignored)
   - 0 missing, 0 fuzzy needed for the deck card names
   - 2 extra entries from layout-only refs (`Lost Jitte` [BIG:23], `Plaza of Heroes` [DMU:252]) resolved via set/num lookup

3. **Card map** — `tools/training/magezero_card_map.json` (TRACKED, 85 entries):
   - Keys: XMage original names → canonical Scryfall name, type_line, mana_cost
   - Double-faced cards registered with // separator (Brutal Cathar // Moonrage Brute, etc.)

4. **Action classifier** — `tools/training/magezero_actions.py`:
   - `classify_action(s)` → `{verb, card}` for 6 verbs: play, cast, pass, activate, attack, block, other
   - Comma-in-name safe (longest-match from card map)
   - Handles [OK] affordability suffix on cast actions
   - Batch variant `classify_actions(list)` available

5. **Tests** — `tests/test_magezero_card_map.py`, 25 tests all passing:
   - Card map completeness (83/83 names resolved)
   - Classify action edge cases (comma names, DFC, [OK] suffix, unknown cards, unknown actions)
   - Card name resolution accuracy (DFC, basics, exact match count)

## Decisions

- Layout-only card refs (Lost Jitte, Plaza of Heroes) included in card_map even though they may be sideboard — better to have them and not need them.
- Brutal Cathar and Cecil are DFCs in Scryfall but single-name entries in XMage — card map keys use XMage name, scryfall field shows canonical "//" name.
- `Activate Ability:` prefix added as alternate for "use" prefix.
- Scryfall cache saved every 10 entries to avoid loss on rate-limit.

## Test output

```
$ uv run pytest tests/test_magezero_card_map.py -v
... 25 passed ...
```

## Known gaps

- Action classifier only knows cards from the 6 deck files. Additional cards from game log data would need Scryfall resolution at inference time or a broader card map.
- The `activate` verb uses `^use\s+` prefix — XMage may also use "Activate: " or similar; the `^Activate\s+` pattern was added as a fallback.
- Attack/block with multiple creatures ("Attack with A and B") returns verb=attack, card=None — multi-attacker parsing could be added.
