# Tactical followup and attack-tax integration

Baseline: installed `f4aaacd571645a20f2052caf2811fcbcd773286d`.
Prepared on isolated `codex/next-tactical`. No live app, settings/history, native bridge, or GitHub changes.

## Included

- **#329** (`f1ae38c` → `694c00c`): recheck explicit cast/hold rationale contradictions against current card rules within the existing decision deadline. Reconcile modal double-faced land actions before cache/memory so a land play is not remembered as a commander cast. Preserve legitimate protection/lifegain, cast-trigger, search, and entwine considerations.
- **#84/#262** (`0229849` → `32b8fa3`): model bounded simultaneous fight damage and challenge false surviving-blocker premises. Preserve a truthful same-action trade; current stats, damage, deathtouch and indestructible are considered. Unsupported prevention/protection, unknown identity/stats or intervening effects abstain. Does not establish that a historical trade was strategically wrong.
- **#308** (`bb776ce` → `106edda`): rank a single fixed-damage cast against a public zero-mana discard-for-indestructible response. The opponent selects the worse branch for us after our adaptive attack choice. Retain spell expenditure, unconditional lifegain, discard, tap/damage and temporary protection cleanup. Unsupported future/multiple-spell responses remain unmodelled and cannot license exhaustive forced-line claims. This is bounded one-turn policy, not horizon-wide minimax or a hidden-card forecast.
- **#210** (`78a15d7` → `5ccc99e`): preserve public Enduring Story facts, budget supported per-attacker taxes before selection/projection/submission, and exclude mana-payment creatures from crackback defense. Preserve payable intended selections and mandatory free-recipient alternatives; stale larger retained selections use acknowledged withdrawal. Current supported mana is a conservative lower bound; unknown and multi-turn tax/payment effects are not treated as free attacks.

Integration preserved both existing sequence-value prompt facts and new fight facts. The shared unsupported status now says `public forecast unsupported`, covering sacrifice/response/tax boundaries. Existing haste/duplicate-copy repairs are retained.

## Verification

One combined run across 39 affected files: **1,034 passed in 32.12s**, including the selected latency tests. Log: `/tmp/coach-tactical-210-results.log`; manifest: `/tmp/coach-tactical-210-tests.txt`.

The run logged a sandbox-blocked ephemeral bridge connection from a test constructing an executor before replacing its bridge. Existing test guards block live ports. The fixture now injects its dummy before construction. Focused bridge/attack-tax rerun: **51 passed in 0.58s**, with no connection warning. Counts overlap. No live game command or permission bypass.

Ruff, formatting and whitespace checks pass across all 23 changed Python files. Formatting was verified AST-equivalent; coach.py CRLF preserved. No full suite, hosted CI, external private-capture replay or live-match qualification.

## Remaining boundaries

Retained choices/raw facts are replayed through bounded reconstructions; complete original per-decision enriched snapshots are unavailable. No alternate-win or loss-causation claim. #329 Drannith commander availability remains a separate dependency. #308 future/multi-spell response ordering and the Innocence/Bowmasters draw sequence remain outside the main search. #210 omits untyped floating pools, restricted/conditional/costly/sacrifice payment sources, some bonuses and vigilance-payment alternatives; it can conservatively select fewer attackers than a different legal sequence. Arena remains authoritative.

## Activation

Freeze this batch; do not add more work while awaiting closure. Parent coordinates Coach closure, then verify process absence and acquire the startup lock before fast-forwarding clean master. Arena may remain open for manual play. No native bridge update, automatic shutdown/launch or push is required.
