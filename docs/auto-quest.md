# Auto-quest (initial implementation)

Click **Grind: Off**, choose how many minutes to play, enable Autoplay, and start
the first match yourself. Grind automatically prioritizes detected quests, then
pursues any remaining daily or weekly win XP. A win can advance both counters.
It stops once quests are gone, daily wins reach 10, and weekly wins reach 15,
or the session time expires. Click **Grind: On** to stop early.

There are no daily/weekly goal or end-date choices in the desktop flow. The
controller re-plans after each match and shows whether it is completing quests,
earning mastery XP, waiting for fresh data, or finished. Unknown win counters do
not prevent completing known quests, but must be refreshed before the XP phase.
Ordinary Auto-queue replaces Grind. A restart requires an explicit start; there
is no unattended daily scheduler. Read-only inspection remains available through
the `inspect_auto_quest` runtime command.

This does not buy entry, reroll quests, craft cards, buy mastery levels, or play a
match during setup. Only Play, Ladder, Play_Brawl_Historic, and Brawl_Ladder are
eligible, and only for a deck previously submitted to that queue in the current
log tail. Bot games and limited/paid events are excluded. On macOS the exact
Recently Played tile must be visible; Windows uses the existing queue bridge with
an explicit deck ID. If Arena rejects a changed deck, navigation pauses; prior
submission is evidence of eligibility, not a guarantee of current format legality.

## Deck diversity preparation

Starter decks can supply the required colors **if unlocked, owned, and legal in
the chosen queue**. The controller reads the account's actual library and
collection, not a presumed starter deck list. Cast-color quests favor the density
of matching nonland cards; creature/attack quests favor creatures, land quests
favor lands. Multicolor cards count once per objective. Multiple quests contribute
to the score. Reward-win goals currently have no win-rate prediction.

For a coverage gap, use this process before restarting the run:

1. Inspect the objective reported in the Auto-quest status. Check the Decks library
   for an owned starter or constructed deck covering those colors/card types.
2. If none exists, use the existing deck editor to build or adapt a deck from owned
   cards, with enough lands and early plays. For broad color coverage, keep decks
   spanning all five colors; creature-heavy decks also cover attack/cast quests.
3. Check legality in Arena and play that deck once in a free eligible queue. This
   records the exact deck/event pair and makes it a candidate on the next run.
4. Refresh Home and restart Auto-quest. Coverage gaps are reported rather than
   silently triggering wildcard spending.

Automatic construction/format validation of missing decks is **not implemented**.
This is a preparation process plus automatic coverage detection and selection.
Removal, color-specific win, and unfamiliar quest types pause with their actual
objective text rather than use an unreliable heuristic. A future builder should
produce owned-card proposals for the editor, validate format legality in Arena,
and only then make the resulting decks eligible.

## What the data proves

Verified 2026-10-26 using local Arena logs (no gameplay executed):

- `QuestGetQuests`: `quests`, `questId`, `goal`, `locKey`, `endingProgress`,
  `startingProgress`, `isNewQuest`. Missing progress is zero only for an explicitly
  new quest. An empty array is known empty; absent/malformed data is unknown.
- `Raw_ClientLocalization*.mtga`: SQLite `Loc(Key, enUS)` supplies the objective
  at `locKey + "_Text"`. Missing localization pauses objective selection.
- `PeriodicRewardsGetStatus`: daily/weekly sequence IDs and UTC reset timestamps.
  Missing counters remain unknown, even if Arena may omit a zero value.
- Deck library, collection, and card metadata come through existing providers.
  Incomplete cards, unowned cards, and stale course snapshots are excluded.

The controller reads a bounded 8 MB Player.log tail at ten-second intervals while
awaiting post-match data. Log timestamps must be within 30 minutes, not in the
future, and after the observed match ended. Clock/timezone changes can cause a
pause. It stops at the deadline, completed objectives, or three consecutive
matches with unchanged observed quest/reward state. Unknown/no eligible state
pauses further queueing. It does not interrupt an active match at the deadline.

Mastery level, accumulated XP, season identity/end, special-event XP, and a
monthly completion forecast remain **unknown**. No disappearing quest is counted
as earned XP. The available graph-state logs do not yet provide a verified
season-linked XP contract. A month is a chosen work window, not a mastery season.

Wizards' [Mastery System FAQ](https://mtgarena-support.wizards.com/hc/en-us/articles/360029675112-Mastery-System-FAQ)
(checked 2026-10-26) says set mastery resets with Standard set releases. It lists
500 XP per quest, 25 XP for each of the first ten daily wins, and 250 XP for each
of the first fifteen weekly wins. These explain goal options; they are not used
as a substitute for observed account XP or as a promise to finish a pass.

Runtime JSON command: `{"cmd":"set_auto_quest","enabled":true,"goal":"mastery",
"minutes":60,"deadline":1793516400}`. `deadline` is optional Unix seconds, capped
by the session duration. The default goal is `mastery`; legacy `quests`, `daily4`, `daily10`, `weekly15`
commands remain supported. Status
uses `AUTO_QUEST` for details and `GRIND` (`ON`/`OFF`) for the toggle. `enabled:false` stops quest queueing.
