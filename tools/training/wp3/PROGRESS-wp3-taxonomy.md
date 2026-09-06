# WP3-Taxonomy Progress

## Status: BUILDING — taxonomy JSON generated, annotate + tests pending

## Plan
1. `build_taxonomy.py` — parse 33,402 Forge scripts ⇒ `card_taxonomy.json` ✅
2. `annotate.py` — pure `annotate_menu_row(name, taxonomy)` function
3. `tests/test_taxonomy.py` — pytest coverage for taxonomy + annotation
4. Run tests, fix, push, PR

## Key findings
- **15,798 (47.3%)** cards have A: ability lines (activated/spell abilities)
- The plan's ~92% claim is for A+T+S+R combined (actual: 93.4%), not A: alone
- 17,604 cards (52.7%) are creatures/lands with keyword/static/triggered abilities only
- 169 distinct primitives found in A: lines
- Top unmapped primitives: Cleanup, Effect, PutCounter, GainLife, Animate, Charm
- Deck coverage: 35/78 unique non-basic cards (44.9%) resolve to ≥1 role via A: lines
- DFC aliases: 1,794 registered (handles // names automatically)
