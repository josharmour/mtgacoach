# PROGRESS — wp3-hand-attribution

Remove the LAST positional assumption in `tools/training/parse_magezero_log.py`:
hand sourcing. Hands previously came from positional `-> Hand:` block lines
(attributed by header adjacency — the #452 board-swap defect family, plus a
finalize-time "latest hand seen" guess). They now come ONLY from
name-attributed `ComputerPlayer.logList` lines:

    [1:Beginning:UPKEEP]PlayerA hand: : Island,Soul Partition, =>[pool-3-thread-4] ComputerPlayer.logList

keyed by player NAME + THREAD + TURN (the line carries turn and phase itself;
`named_hands` is reset at every die-roll game boundary). A row whose
(thread-game, turn) has no such line is DROPPED and counted — never guessed,
never emitted with a fabricated empty hand (an empty hand in a prompt is an
observation, "you hold nothing"). An empty logList line (624 of 9,063 on the
smoke log) IS a valid attribution: the hand really is empty at that upkeep.

## What changed

- `parse_magezero_log.py`:
  - `RE_NAMED_HAND` + `parse_named_hand_cards` (separator commas carry no
    trailing space; in-name commas — "Kitsa, Otterball Elite" — do; verified
    against magezero_card_map.json: 0 of 510 names contain `,<non-space>`).
  - `_ThreadState.named_hands: dict[turn -> cards]`, reset per game.
  - Rows are appended to the output list at `_finalize_game` (not at
    creation), so an unattributable-hand row is really dropped, and counted
    (`hand_dropped_unattributed` in `LAST_PARSE_STATS` / `--report`).
  - Lines naming any player other than PlayerA are ignored (hidden
    information, leak class L4). The smoke log contains 0 such hand lines,
    but the gate is by name, not by assumption.
  - The old positional flow survives ONLY as an internal `_hand_positional`
    shadow (stripped on write) so `--report` can print the
    positional-vs-named disagreement rate.
- `tests/test_parse_magezero_log.py`: fixtures gained named hand lines;
  new `TestNamedHandAttribution` (8 tests: regex, comma names, empty hand,
  fail-closed drop + count, PlayerB ignore, game-boundary reset, recovered
  pass rows, underscore hygiene) and `TestHandDeckSignature` (2 fixture
  guardrails + a real-smoke-log guardrail gated on `MZ_SMOKE_LOG`).

## Measured numbers (mz_train_smoke.log, 210MB, 594 games)

Named-source inventory: 9,063 `hand: :` lines, ALL `[N:Beginning:UPKEEP]PlayerA`
(0 other phases, 0 other players); 624 with an empty list.

Parser rows:
- before: 21,609 emitted (18,644 with non-empty hand, 2,965 empty)
- after:  21,557 emitted with attributed hands + 52 dropped fail-closed (0.24%)
- decision_kind histogram before: priority 20,182 / binary 1,424 / blockers 3
- decision_kind histogram after:  priority 20,130 / binary 1,424 / blockers 3
  (all 52 drops were priority rows)

Deck-signature guardrail (hand must be a subset of the 17 UWTempo names):
- violations BEFORE: 5,422 rows (of 18,644 non-empty-hand rows, 29.1%) carried
  non-deck entries — every one of them `:N` score-suffix pollution
  ("Negate:5") from GameStateEvaluator2 block hands backfilled positionally.
  After stripping `:N` for the check, off-DECK names before: 0 — the existing
  PlayerB-block skip already stopped true off-color leaks; the residual
  positional damage was format pollution + temporal misattribution.
- violations AFTER: 0 rows (no normalization applied).
  `MZ_SMOKE_LOG=... pytest tests/test_parse_magezero_log.py::TestHandDeckSignature`
  = 3 passed (includes the full-log scan).

Reconciliation old-vs-new (rows where the old source produced a hand; old
side normalized by stripping `:N` so only CONTENT differences count):
- rows with both sources: 18,592; differing: 15,770 → mismatch rate 84.8%
- direction: 9,444 mixed / 4,691 named-superset / 1,635 positional-superset
- 10 sampled diffs verified against raw log context (thread + game seq +
  turn): in 10/10 the new hand matched the game's raw
  `[N:Beginning:UPKEEP]PlayerA hand:` line EXACTLY — attribution correct.
  In at least 4/10 (incl. an UPKEEP "Cast Bounce Off" row whose positional
  hand lacked Bounce Off, and a turn-3 row whose positional hand contained
  two Skrelvs impossible from that turn's card flow) the positional hand was
  demonstrably wrong or post-decision. In the remainder the positional hand
  was the fresher intra-turn snapshot — the named hand is the turn's UPKEEP
  snapshot (see caveat below).
- coherence check, chosen `Cast/Play X` with X present in hand:
  before 1,075/7,649 = 14.1%; after 5,247/7,649 = 68.6%. The old source was
  usually a POST-action hand (printBattlefieldScore prints after execution);
  the new one is pre-action for upkeep decisions and turn-start for the rest.
- VERDICT: the named source is the correct attribution in every verified
  sample and far more decision-time-coherent; the 84.8% row-level mismatch is
  dominated by the old source being post-action/misattributed, not by upkeep
  staleness.

run_wp3_pipeline.py (--balance downsample-pass; default run trips the 40%
pass tripwire at 43.8% on both before and after):
- before: 21,609 decisions → 5,835 filtered → 4,660 records
  (train 4,061 / val 282 / test 317), pass rate 40.0%, leak scan 0
- after:  21,557 decisions → 5,848 filtered → 4,649 records
  (train 4,092 / val 265 / test 292), pass rate 40.0%, leak scan 0
- net −11 records (−0.24%): no silent collapse. Split-level shifts are
  dedupe-key churn (hand is part of the dedupe key).
- 3 rendered train records eyeballed against the raw log: hands are
  UWTempo-only, no `:N` suffixes, consistent with their game's upkeep lines.

Tests:
- tests/test_parse_magezero_log.py: 61 passed, 1 skipped (the smoke-log test
  without MZ_SMOKE_LOG); with MZ_SMOKE_LOG the guardrail class is 3 passed.
- Related files (magezero_filters/bridge_leaks/combat/run_wp3_pipeline/
  card_map/deck_inventory/corpus_selfcheck): 134 passed, 2 skipped.
- Full `pytest tests` under venv-train + PYTHONPATH=src: 1,405 passed,
  5 failed, 29 skipped — the 5 failures (dynamic_cards, proxy_thinking,
  server_bridge_overlay, settings_scryfall) are missing-dependency issues in
  app modules (e.g. `No module named 'PIL'`) and reproduce with this branch's
  changes stashed; not touched by this diff.

## Known caveat / could not verify

- The named hand is captured at the turn's UPKEEP, before the draw step: rows
  later in the turn can show a hand missing cards drawn or still holding
  cards already played that turn (31.4% of Cast/Play rows still lack the
  chosen card in `hand`; the rendered menu's [OK] tags carry the truth about
  castability). Reconstructing intra-turn hands from the upkeep snapshot plus
  the turn's chose-action deltas would be a derivation (not a guess) and is a
  possible follow-up; it was NOT done here.
- Whether XMage can emit `hand: :` lines in other phases or for other player
  names in other configurations was not verifiable from this log (this log
  has only UPKEEP/PlayerA); the parser keys by the turn on the line and gates
  by name, so either case is handled or safely ignored.
