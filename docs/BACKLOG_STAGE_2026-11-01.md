# Backlog integration: staged, not installed

## Closed integration batch

All currently accepted compatible work is combined on `codex/session-burndown`.
The final batch incorporates marked extra-combat damage (`390f3c8` → `8b108ee`),
retained-engine valuation (`76a50e0` → `51e26ac`), the reported Greaves equip
opportunity (`a34f936` → `c1ff241`), and grounded plan-name aliases
(`aafd354` → `d59ecb8`). Previously integrated prerequisites were not duplicated.

**1,704 tests passed in 48.67 seconds across 100 affected test files** after all
four changes and conflict resolutions. Ruff/format and whitespace checks passed
on 19 changed Python files. Formatting was checked for AST equivalence. This is
the combined affected batch, not a full repository suite, CI, or live match test.

The exact committed history above installed main is recorded in
`BACKLOG_INTEGRATED_COMMITS_2026-11-01.json`. The earlier prepared `cd9320f` and
all subsequently accepted batches are ancestors of this integration branch.
The conditional-keyword follow-up `52b23a7` is explicitly deferred to the next
batch. Parser capture `091bd189` remains held for privacy controls and bounds.

Normal restart currently loads `/Volumes/repos/mtgacoach/src`, main `07098e91`,
using `~/Library/Application Support/mtgacoach/venv/bin/python`. The verified
Python 3.12 editable-package path points there, not at this worktree. Updating
that checkout could affect lazy-loaded code in the running app, so activation
must occur only with safe app coordination after the deployment pause is lifted.
No restart alone will install these worktree changes.

Deployment restarts remain paused following the update-guard closure. The
installed checkout was verified at `07098e91` at 05:02 UTC, with Coach and Arena
running, Autopilot and Auto Queue enabled, and speech speed 1.5. No subsequent
deployment or game-control command was issued during this integration.

## Tested boundaries

- `cd9320f`: previous prepared batch, 510 affected tests; not installed.
- `ef9da20`: model-call cancellation, unknown draft context, nested activation
  intent, Traditional Limited sideboarding, and update coordination. 327
  affected tests plus 5 lock tests passed. These counts overlap earlier runs.
- `6ac6b10`: poison/combat-mill search and memoization optimization. 455 combined
  checks passed, including the existing load-adjusted timing assertions. This
  is not qualification against the separate idle 80 ms target.
- `53203e4`: opponent reanimation recipient attribution (#344). 118 combined
  decision/claim checks passed. It corrects the explanation, not the choice's
  strategic optimality.
- Tactical stream through source `50fab79`, then commander-zone source
  `e4b62a2`: 817 combined tests passed in 25.22 seconds after integration.
  This is an affected regression batch, not the full repository suite or CI.
- Ward/short-name mana follow-ups and the three combat-core commits: one
  combined run across 80 affected files produced 1,336 passes and one prompt
  growth failure (1,030 characters versus the existing 1,000-character cap).
  Removed the redundant "nothing gained casting first" claim while retaining
  the explicit post-combat cast order. All 316 affected prompt/planning checks
  then passed in 10.77 seconds, including the previously failing cap. The cap
  was not loosened. No second aggregate or full-suite run was performed.
  Lint/format passed on all 21 changed Python files; whitespace checks preserve
  `coach.py`'s existing CRLF convention. Separate ward/deadline checks: 25 passed.
- Combat integration boundary: `441418e`.
- Sources `fdcf764` (#138) and `bb0848c` (early-win #5 recurrence) then integrated
  as `2ceb040` and `0ca3da9`. All 228 combined claim, game-result, and queue
  regressions passed in 8.67 seconds; lint/format passed on five changed files.

## Explanation and early-result follow-up

#138 corrects a positively asserted ground blocker relationship when the named
attacker has flying and the alleged blocker demonstrably lacks flying/reach.
It preserves the selected action and original diagnostic reasoning; it does not
establish that choosing that removal target was strategically optimal. Unknown
grants, ambiguous identities, and conditional claims abstain. A separately
reported conditional-keyword false positive remains outside this narrow fix.

The #5 recurrence records matches observed in their room/mulligan phase even if
they end before turn one. It retains startup-history rejection, result identity,
duplicate suppression, and queue behavior. Tests use isolated state; no existing
match history was rewritten and live early-concession qualification is pending.

The tactical integration retains all existing Arena-clock clamps, cancellation,
activation intent, poison/legend projection, and current-offer expiry. It removes
planning-time burn marks: only identified confirmed damage events may fill an
omitted mark, and explicit public damage (including zero) wins.

The one commander hook conflict was resolved by retaining both the tactical
rechecks and the commander review. An additional integration regression forces
all six tactical rechecks through the same four-second deadline: backend calls
receive 4, 3, 2, and 1 seconds, and the remaining checks cannot start new calls.

Commander correction failure produces an explicit manual handoff, does not
submit the known-false decline, and does not repeatedly call the model for the
same prompt. The existing watchdog can still request Arena's default near its
deadline. The integration test uses a fake bridge: it does not establish which
choice Arena will make or prove live acceptance.

## Tactical coverage and limits

- #328: typed removal reservations include development cost and the target's
  actual ward attachments. Integration caught and fixed an unrelated Aura
  incorrectly increasing the target's ward. Both directions of attachment
  identity are covered; unrelated and unattached Auras do not add ward.
- #303: shortened card names still receive colored-mana promise checks.
  This does not establish general future land-sequencing quality.

- #322: explicit current free casts from exile enter search at their offered
  cost; unknown/X/additional costs and future permissions remain unmodeled.
- #342/#274/#279: current removal-window and named-target contradictions are
  checked. Unknown modes and protection exceptions remain conservative.
- #303/#254/#306: land entry/payment claims, counted mana, Dalkovan source-tap
  reservation, and unchanged-board optional reservations have regressions.
  General future colored-mana planning remains partial.
- #311/#308: object-bound blink and public trigger/response consequences get
  bounded rechecks. They are not a complete strategic sequencing solver.
- #227: planned or submitted targeting is no longer treated as dealt damage.
  Historical cases without actual damage evidence remain unresolved.
- #84/#262: Headliner uses the current count after its sacrifice, and surviving
  damage targets cannot be described as already dead. Unknown types and
  intervening effects are not silently assumed away. Alternative match outcomes
  and live qualification remain unproven.
- #343: the exact commander recipient's graveyard/exile zone is distinguished
  from surviving token copies. Legitimate recovery-based declines remain legal.
- #344: the current selection prompt grounds the opposing recipient; selection
  optimality remains unverified.

## Combat continuation

Integrated sources `5e0504f`, `2912879`, and `2cf2ddf` add blocker lifelink to
survival and lethal estimates, active color protection with bounded grant
lifetimes, and infect damage-step counters. The poison optimization was already
present and was not picked twice. The merge preserves counter metadata through
memoized combat, extra combats, own/opponent turns, and planeswalker attacks.
Existing observed counter effects are not subtracted twice. The continuation
also carries ordinary marked damage between modeled extra combats, and values
returned enchantment engines separately from lost creature bodies with known
ownership and visible interference. Original source packets remain missing for
some historical claims; this does not prove their alternative winning lines.

The infect fixture is synthetic, not a captured-match replay. Historical full
packets remain missing for parts of #273/#278/#284. Remaining search limits
include broader retained-engine interactions, #280 sacrifice-victim/chained-death
and Tergrid continuation, unrepresented grant durations, activated proliferate,
conditional counter/replacement effects, and generic end-step death triggers.

## Greaves and shorthand scope

The Greaves repair preserves Arena-offered targets, reviews dismissive passes
when a current free equip can grant haste, and allows a grounded planned haste
grant without incorrectly calling it innate haste. Existing precise claim
predicates and bounded decision deadlines remain intact. It does not equip every
creature simultaneously, invent green mana from colorless, force every equip,
or prove that the earlier false correction caused the subsequent pass.

The alias repair resolves dependency shorthand only when current known card
identities yield one match. Ambiguous names and ordinary prose remain unchanged.
It addresses the reproduced #240/#287 source gap, not all historical strategy
quality or the missing #272 target packet.

Other open qualifications remain: idle poison-search timing; broader poison,
mill and replacement simulation; Constructed sideboarding;
live Traditional Limited submission; draft ranking quality (#294); absent #272
target evidence; native temporary-ability metadata (#288); and the combat limits
above. No historical backlog-wide closure is claimed.

Parser diagnostic capture source `091bd189` remains excluded because it records
full content without an explicit capture control and per-file byte cap. A bounded,
privacy-aware implementation must be reviewed before integration.

## Rollout coordination

See `LOCAL_UPDATE_COORDINATION.md`. A prepared continuous activation script is
kept locally at `/tmp/coach-backlog3-transaction.py`; it has not been executed and
is pinned to the earlier prepared batch. Do not use it to claim installation of
this newer stage. Update the target and verify it before any resumed rollout.

All changes are local commits in `codex/session-burndown`. Nothing was pushed,
and no live settings, history, account, deck, or queue target was changed here.
