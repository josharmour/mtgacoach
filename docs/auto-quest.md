# Quest-driven Grind

Implementation files:

- `src/arenamcp/auto_quest.py`: construction, ownership, and planning.
- `src/arenamcp/quest_card_metadata.py`: native metadata and Standard legality.
- `src/arenamcp/quest_deck_operation.py` and `quest_deck_persistence.py`: durable
  operation state, exact clone identity, server save evidence, and readback.
- `src/arenamcp/standalone_auto_quest.py`, `standalone_auto_queue.py`, and
  `auto_queue.py`: explicit idle start, session gates, continuation, and queue confirmation.
- `src/arenamcp/mac_bridge_adapter.py` and `spikes/mac-il2cpp/probe.cpp`:
  reflection routing, clearing empty piles, and native explicit-ID queue submission.
- Tests: `test_auto_quest.py`, `test_grind_idle.py`, `test_quest_card_metadata.py`,
  `test_quest_deck_flow.py`, `test_quest_deck_persistence.py`,
  `test_quest_queue_bridge.py`, and `test_mac_bridge_deck_ops.py` under `tests/`.

Enable **Grind**, choose its duration, enable **Autoplay**, and open Arena Home.
Grind can start from verified idle Home or continue after a match observed during
this session. It never enables Autoplay. Cancellation, dry-run, AFK, session
replacement, and the deadline stop further actions, including after slow reads.
An active draft driver keeps ownership of navigation. Restarting requires an
explicit new Grind session; the deck-operation journal does not authorize play.

Grind prioritizes detected quests, then remaining daily/weekly win XP. Unknown
win counters do not prevent completing known quests, but pause the XP phase.
It stops when goals are complete, the duration expires, or three completed matches
show no quest/reward progress. Ordinary Auto Queue replaces Grind.

Grind narrates quest objectives, owned-card deck preparation, deck selection,
queue entry, refreshed quest progress, and daily/weekly win reward targets through
the selected coach voice. It also speaks completion and pause/stop reasons.
Unchanged polling results are not repeated. Speech is serialized on a background
worker using desktop playback acknowledgments, so consecutive Grind updates do
not replace each other or block gameplay. Voice mute is respected; muted or failed
speech still leaves the status visible. Progress updates depend on refreshed Arena
quest/reward responses, normally on returning Home after a match.

## Automatic owned-card construction

When no observed owned deck covers a casting-color daily quest, Grind attempts a
new **Standard / Play** deck. For an OR-color objective it chooses one supported
color. The current builder requires exactly 36 owned, on-color spells and 24
ordinary matching basic lands. It favors cheaper spells, caps copies at four per
name across printings, and excludes costs it cannot safely interpret/pay (including
hybrid, Phyrexian, X, snow, colorless requirements, and spells over five mana).
Multi-face cards are excluded. This is a simple quest deck, not an optimized ladder
list. Other construction objectives/formats remain unsupported; existing observed
decks can still cover creature/attack/land quests and free Brawl queues.

Legality comes from fresh server `GetFormats` data in Player.log plus native card
metadata: legal sets and allowed titles, minus banned/suspended titles, using title
identity across printings as the client does. Unsupported rules or data older than
24 hours cannot authorize construction. Colors use the native numeric enum, and
mana uses the actual printed cost rather than a fabricated zero mana value.
Only verified ordinary Plains/Island/Swamp/Mountain/Forest are unlimited basics.
Native printings require at least one owned copy to unlock that artwork; one
copy supports all 24 lands. An absent collection count is allowed only when a
provider explicitly verifies a globally free printing. Missing data or fewer than 36 eligible spells
pauses without crafting, buying, rerolling, or spending entry currency.

A readable existing **Standard** deck is required because the bridge provides
clone-and-edit, not an independent empty-deck constructor. Grind:

1. Journals a unique operation before submitting one clone; originals are preserved.
2. Identifies exactly one newly observed ID, excluding every pre-existing ID.
3. Replaces main and clears sideboard, command zone, and companion piles.
4. Waits for a matching `DeckUpsertDeckV3` request/server response and fresh bridge
   readback with the exact intended contents and format. A local cache update or
   bridge `ok` alone is insufficient.
5. Rechecks ownership/legality and sends the exact deck ID to free Standard Play.
   Generated eligibility is separate from historically observed queue pairs.
6. Confirms the submitted deck ID, name, and event against fresh `EventSetDeckV3`
   observations; an unknown queue result is not automatically resubmitted.

Delayed visibility and acknowledgements are polled. The durable journal at
`~/.arenamcp/quest-deck-operation.json` prevents duplicate clone/update requests
across cancellation, timeouts, and explicit restarts. An uncertain write, missing
acknowledgement, changed identity, rotated log, or superseded operation pauses.
Inspect the generated deck and the journal before manually clearing an unresolved
operation; deleting the journal discards duplicate-prevention evidence. The code
does not delete or overwrite user decks to recover from a failed operation.
A completed operation can be archived when a different quest requires a new deck.

## Bridge support and deployment

BepInEx with `reflect-1` routes collection/create/update/readback through the shared
reflection adapter and uses its existing explicit-ID `queue_match` command.
Native macOS uses the same client `HomePageContentController.JoinMatchMaking`
method through a narrow adapter restricted to an explicit cached Standard deck and
free Play. It checks Home, blocking screens, and deck identity before invocation.
Fresh native `DeckViewInfo.GetValidationForFormat("Standard")` results must also
report `IsValid=true`, including again in the submission batch. Invalid, unowned,
unpublished, emergency-banned or unknown validation results block submission.
An outgoing deck-selection request is not proof of server acceptance or a match.
This bypasses the old requirement that a newly created deck already have a
Recently Played tile. Other native queue types retain their existing navigation.

**Native macOS requires rebuilding and restarting with the updated probe.** The
new `retain_value` reflection flag keeps the actual boxed `Summary.DeckId` Guid
available to the subsequent call. Older probes discard that value and fail closed
before matchmaking; there is no guessed/default deck fallback. Development
validation builds to a temporary directory only, without staging a library into a
running Arena installation. Missing client classes/methods or bridge support pause
with diagnostics. Fake-bridge tests and compilation are not live-account proof of
server acceptance; no live deck writes or gameplay are part of automated tests.

## What the data proves

Verified 2026-10-26 using local Arena logs (no gameplay executed):

- `QuestGetQuests`: `quests`, `questId`, `goal`, `locKey`, `endingProgress`,
  `startingProgress`, `isNewQuest`. Missing progress is zero for a complete server quest record that omits both
  progress fields (including after the transient new-quest flag disappears). An empty array is known empty; absent/malformed data is unknown.
- `Raw_ClientLocalization*.mtga`: SQLite `Loc(Key, enUS)` supplies the objective
  at `locKey + "_Text"`. Missing localization pauses objective selection.
- `PeriodicRewardsGetStatus`: daily/weekly sequence IDs and UTC reset timestamps.
  Missing counters remain unknown, even if Arena may omit a zero value.
- Deck library, collection, and card metadata come through existing providers.
  Incomplete cards, unowned cards, and stale course snapshots are excluded.

The controller reads a bounded 8 MB quest-log tail at ten-second intervals while
awaiting idle or post-match data. Save verification scans at most 4 MB after its
journaled checkpoint and rejects log replacement or truncation. Log timestamps must be within 30 minutes, not in the
future, and after the observed match ended. Clock/timezone changes can cause a
pause. It stops at the deadline, completed objectives, or three consecutive
matches with unchanged observed quest/reward state. Unknown/no eligible state
pauses further queueing. It does not interrupt an active match at the deadline.

A recent Home quest response may predate enabling idle Grind; enabling Grind
does not require Arena to send the same response again. The native Mac probe
must support field assignment (`Client_DeckSummary.Name` is a field, not a
property) and retained value-type references. Rebuild/stage the probe and restart
Arena after native changes; restart Coach after Python changes. The native test
harness exercises the actual batch engine without connecting to Arena.

The 2026-10-28 Mac crash exposed an incorrect reference-field ABI assumption in
both the probe and its mock. In installed GameAssembly UUID
`2E91FA68-AFD7-3E68-A9D7-2E9257F219D3` (arm64), `il2cpp_field_set_value`
branches from `0xa192cc` to `0xa85ab4`, which passes `deref=false` to
`SetValueRaw` at `0xa852e0`. Reference fields therefore take the object pointer
directly; value fields take a pointer to raw data. Passing the address of the
reference slot stores a dangling stack pointer. The corrected harness mirrors
this distinction, including null references. Clone submission also checks the
detached summary's name before invoking `CreateDeck`.

A subsequent live update exposed a second ABI issue: `Client_DeckCard` is a
value type, and its constructor requires an unboxed receiver. Invoking it on
the box overwrites the managed header with card ID/quantity. The probe now
unboxes receivers for instance methods declared on value types and roots newly
allocated boxes through subsequent REF calls. Inherited reference-type methods
keep their boxed receiver. The harness constructs a readonly card struct, passes
it to a collection, and checks allocation-time header identity; it also covers
static/inherited receiver handling. Neither native mock tests nor fake bridge
tests alone establish live persistence or matchmaking success.

Proven clone failures before `CreateDeck` submission can retry only in a new
explicit Grind session, preserving the rejected journal as a separate file.
The exact legacy missing-`set_Name` failure is also recognized as pre-submission
and recovered this way. Unknown outcomes still reconcile without cloning again.

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
