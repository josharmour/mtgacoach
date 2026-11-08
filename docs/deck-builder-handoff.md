# Deck suggestions integration handoff

Prepared November 4, 2026 (Pacific) for the sole release coordinator.

## Scope and baseline

- Base: `4f617b503775117409d4a4d1fa876b7ea3b18433` (coordinator-confirmed remote master).
- Branch: `codex/deck-suggestions-handoff`.
- Worktree: `/private/tmp/mtgacoach-deck-suggestions`.
- Extracted only the deck feature from the older dirty main checkout. No shared
  main/Dev source changes, resets, stashes, cleanup, app restarts or deployment.
- Rank/history implementation is already in the base. No copies of the old
  `ladder.py`, `rank_strip.py`, `performance_tab.py`, commentary or probe work
  are included. `MatchHistory` and `RankLog` are unchanged.

## UI integration order and exact overlap

Apply the approved UI owner's work first, then this optional feature.

`src/arenamcp/desktop/main_window.py` has only these changes:

1. After `self.setCentralWidget(self._stack)` in `_build_central_widget`, connect
   `self._session.adviceReceived.connect(self._on_deck_invitation)`.
2. Add `_on_deck_invitation(text, label)` before `_show_pending_release`.
   It filters the DECK BUILDING label, shows a nonmodal popup, explains the
   permanent Decks entry point, and opens the existing Decks page/builder only
   when accepted. No geometry, contact-menu or Coach-layout changes.

`tests/test_desktop_window_geometry.py` adds only
`window._on_deck_invitation = Mock()` to the bare-QMainWindow fixture before
calling `_build_central_widget`. If the UI owner's fixture has moved, keep this
mock with that fixture; the rest of its layout assertions belong to the UI owner.

DecksTab adds its own entry button and `open_builder`; it turns off auto-queue
on entry and requests collection data. No auto-resume, Arena navigation, deck
mutation or wildcard spending is performed by the feature.

## Validation on this base

91 focused tests passed with the local Dev Python executable, with the worktree
first on PYTHONPATH. Modules were explicitly verified to import from this
worktree, not the editable shared Dev checkout.

```sh
PYTHONPATH=src:/private/tmp/mtgacoach-deck-test-deps \
  '/Users/joshu/Library/Application Support/MTGA Coach Dev/venv/bin/python' -m pytest \
  tests/test_deck_recommendation.py tests/test_deck_builder.py tests/test_ladder.py \
  tests/test_performance_tab.py tests/test_decks_tab.py tests/test_desktop_deck_editor.py \
  tests/test_desktop_window_geometry.py tests/test_deck_library_backend.py \
  tests/test_mac_bridge_deck_ops.py -q
```

Ruff lint and format checks passed for all ten affected Python files, as did
`git -c core.whitespace=cr-at-eol diff --check` (existing files use CRLF).
No redundant full suite was run for this handoff. The coordinator owns combined
validation after integration.

The first dependency check found BeautifulSoup absent in the shared Dev venv.
The initial focused run had 73 passes and one missing-dependency failure. To
avoid changing shared Dev, BeautifulSoup 4.15.0 and soupsieve 4.0.1 were installed
only under `/private/tmp/mtgacoach-deck-test-deps`. Both packaging paths already
install the existing `[full]` extra, which includes the scraping dependencies;
a new managed artifact was not built or installed here.

Tests exercise the real desktop signal → popup → Decks → builder path with
mocked transport, including auto-queue-off, collection request, bridge failure,
and declining without queue/collection effects. Adapter/pipe checks are mocked,
not live Arena evidence.

## Data, backend and entitlement prerequisites

- Ranked history plus a usable `RankGetCombinedRankInfo` log snapshot is needed
  for the prompt. At least 20 results in one queue are required. It is aggregate
  player history, not per-deck, per-rank or account-partitioned performance.
- Collection and wildcard reads require a connected reflection-capable bridge
  (`reflect-1`) and Arena at home. The existing adapter can route Mac, Android
  and BepInEx runtimes; none was live-qualified for this new UI in this handoff.
- Candidate lookup uses public MTGGoldfish HTTP pages and the existing card
  database stack. No hosted LLM request, new feature-specific subscription check,
  API credential or Untapped account is used by automatic candidates. Ordinary
  Coach startup/access requirements still apply; active/expired trial behavior
  was not exercised.
- Untapped opens externally; its gated statistics may require the user's own
  access. No direct Untapped statistics integration or entitlement bypass exists.
- Earlier implementation checks fetched six complete Standard candidates and
  valid exports, and checked example rarity fallback against the local catalog.
  Those read-only source checks are historical receipts, not a fresh provider
  SLA, a real user's affordability test, or an end-to-end Arena import test.

## Recommendation and remaining qualification

**Recommend the next release, not unconditional 4.0 inclusion.** This is ready
for scoped code review and an optional controlled integration experiment. It is
not independently reviewed or qualified for a trial-user launch.

Automatic ordering is MTGGoldfish's Standard paper metagame popularity. It does
not establish that a suggested deck has higher Arena win rate than the player's
current deck. Other formats require a pasted list. Limited/Bo3/Mythic automatic
invitations are excluded. Commander/companion exports are unsupported. Arena
does the final legality and wildcard confirmation after manual clipboard import.

Before launch: test a real eligible post-match invitation and cooldown across
restart, dismissal and re-entry; check collection/rarity/wildcard accuracy on the
supported platforms; verify the supported active-trial startup/bridge path;
check the new popup against the approved responsive UI; test provider failure,
slow/disconnected collection reads, and candidate import/legality in Arena.
The collection request currently relies on the engine responding to release the
busy UI, so disconnect recovery needs explicit live scrutiny. Unknown card costs
remain excluded from affordable filtering.

No installation, live-gameplay qualification, trial deployment, independent
verification, merge, release or announcement is claimed. The coordinator owns
item selection, combined checks, controlled Dev update and release gates.
