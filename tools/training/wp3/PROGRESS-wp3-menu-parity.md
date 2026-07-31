# PROGRESS: wp3-menu-parity — chosen_not_in_menu diagnosis and fix

Date: 2026-07-30
Branch: wp3-menu-parity
Source log: /home/joshu/mz_train_smoke.log (201 MB, 594 games)
Pipeline command (both runs):
`run_wp3_pipeline.py --log /home/joshu/mz_train_smoke.log --balance downsample-pass`
(without `--balance` the run aborts at the 40% pass-rate tripwire before the
render stage, so the drop is only observable on the balanced path).

## Reproduction (current numbers — the 745 measurement had shifted)

| Metric | Before fix | After fix |
|---|---|---|
| Raw decisions parsed | 21,609 | 21,609 |
| Post-filter rows | 5,835 | 5,835 |
| chosen_not_in_menu drops | **816** (14.0% of post-filter) | **0** |
| decision_kind_binary drops | 359 | 359 |
| Training records (train+val+test) | 4,660 | 5,476 (+816) |
| Pass rate | 40.0% | 40.0% |
| Leak scan | PASS | PASS |

The task brief's 745 (12.8%) was measured before the recovered-pass and
combat-classifier changes landed; the reproduced baseline on current master
(48a47ac) is 816 (14.0%).

## Root cause (single bucket — 816 of 816 drops)

`parse_magezero_log.py` split the `playable abilities: [...]` payload on
commas. XMage prints that list as Java `List.toString()` — entries joined
with `", "` and no quoting — while menu entries themselves contain `", "`:

- comma-named cards: `Cast Malcolm, Alluring Scoundrel`, `Cast Skrelv,
  Defector Mite`, `Cast Kitsa, Otterball Elite`
- ability text: `{T}: Draw a card, then discard a card.`

The split shattered those entries, and `build_magezero_bridge._resolve_answer`
does an exact `menu.index(chosen)` — so every `chose action` whose name
contains a comma failed the match and was dropped.

### Bucketing method

Every one of the 816 dropped rows was re-segmented using the row's own
MCTS-pool action names (bracket-delimited in the log, therefore comma-safe)
as anchors; the chosen action then appeared verbatim in the re-segmented
menu in **816/816 cases**. Buckets that came back EMPTY: pass-not-in-menu
(0), menu truncation (0), cross-thread window bleed (0), land-play phrasing
(0), mana-ability phrasing (0).

### Three raw-log examples (grep -n citations into mz_train_smoke.log)

1. Lines 1747-1748 (thread-1, game 1, turn 2):
   `playable abilities: [Cast Skrelv, Defector Mite, Cast Skrelv, Defector Mite, Pass]`
   `chose action:Cast Skrelv, Defector Mite`
   Old menu: `['Cast Skrelv', 'Defector Mite', 'Cast Skrelv', 'Defector Mite', 'Pass']` — chosen not found.
   New menu: `['Cast Skrelv, Defector Mite', 'Cast Skrelv, Defector Mite', 'Pass']` (two castable copies).
2. Lines 3368-3370 (thread-6, game 1, turn 5):
   `playable abilities: [Cast Bounce Off, Cast Combat Research, Cast Kitsa, Otterball Elite, Cast Skrelv, Defector Mite, {1}{U}: Untap {this}., Pass]`
   `chose action:Cast Skrelv, Defector Mite`
   New menu (verified in decisions.jsonl): `['Cast Bounce Off', 'Cast Combat Research', 'Cast Kitsa, Otterball Elite', 'Cast Skrelv, Defector Mite', '{1}{U}: Untap {this}.', 'Pass']`.
3. Line 2747 (thread-5, turn 6): `chose action:Cast Kitsa, Otterball Elite`
   with menu line `playable abilities: [... Cast Kitsa, Otterball Elite, ...]` —
   same shatter, same recovery.

## Fix (parser-side, fail closed)

`parse_magezero_log.segment_menu(raw, vocab)`: the raw (unsplit) menu payload
is now stored per thread and segmented at decision-emission time using the
paired window's pool action names (plus the chose-action name) as anchors,
longest-first, only at `", "` boundaries. Any stretch matching no anchor
falls back to the old plain comma split — a merge is only ever produced when
the pool confirms the merged form verbatim. No guessing; pairing stays
thread-tag + same-window, per the existing pending_pool/pending_menu model
(log order per window is pool -> playable -> chose; pass windows emit only a
pool line and take pool names as the menu, unchanged).

## Guardrail (records only grow, no kept record changes its choice)

Compared rendered corpora before/after keyed on
`(game_id, turn, phase, session, outcome, chosen-action-string)` as a
multiset, with the chosen string re-derived from `meta.answer_pick` against
the numbered menu inside the final rendered prompt:

- records before: 4,660; after: 5,476
- previously-kept records missing after fix: **0**
- gained: **816**, of which 816/816 have a comma in the chosen action

Note `answer_pick` indices legitimately shift for kept records whose menus
contained shattered third-party entries (the menu is now shorter/correct);
the chosen action string at the picked index is unchanged for all 4,660.

## Secondary measured effect

Suspect shattered fragments across post-filter menus (entries with no
action-shaped prefix, e.g. orphaned `then discard a card.` / `Alluring
Scoundrel`): 1,841 of 20,044 entries before -> 336 of 18,142 after.

## Not fixed (documented, needs a judgement call)

1. **336 residual shattered fragments** in kept rows' menus: windows where
   the comma-named entry was never expanded by the paired pool (or the paired
   pool is a sub-decision pool), so no anchor confirms the merge. A global
   (cross-window) vocabulary of pool-confirmed action names would merge most
   of them, but that trades the tight same-window pairing for a corpus-wide
   assumption; recommend deciding explicitly whether menu cosmetics justify
   it. The chosen action is unaffected (it is always pool-confirmed).
2. **Sub-decision pools** (`PHASE1 (top: X)pool=`): a `chose action` that
   follows a sub-pool gets that sub-pool as its `mcts_counts` (targets, not
   top-level actions), so `meta.mcts_chosen_visits` is 0 for those rows.
   Pre-existing, out of scope here; menu segmentation defends against it by
   adding the chosen name to the anchor set.

## Verification

- tests/test_parse_magezero_log.py: 59 passed (50 existing + 9 new:
  TestSegmentMenu x7, TestCommaMenuEndToEnd x2).
- Full WP-3 suite (test_parse_magezero_log, test_run_wp3_pipeline,
  test_magezero_bridge_leaks, test_magezero_card_map, test_magezero_combat,
  test_magezero_filters): 156 passed, 2 skipped.
- Rendered records eyeballed against raw log lines 1747-1748 and 3368-3370:
  menus match the log verbatim.
