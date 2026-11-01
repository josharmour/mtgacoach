# Traditional Limited sideboarding (#318)

The native IL2CPP event driver owns `SubmitDeckRequest` between games. It reads
`get_sideboard_state`, chooses a bounded plan, and calls `submit_sideboard` once.
The ordinary gameplay planner, initial deck builder, generic cancel, and
AutoRespond paths remain excluded from this request family.

## Protocol evidence

The implementation follows the local 2026-08-26 client decompile:

- `SharedClientCore/GreClient.Rules/SubmitDeckRequest.cs`: readonly `Deck`,
  `SubmitDeck(DeckMessage)`, and the preconfigured `SubmitDeckResp` envelope.
- `SharedClientCore/GreClient.Rules/BaseUserRequest.cs`: `OriginalMessage` owns
  `GameStateId`/`MsgId`; `Submit()` spends `OnSubmit`.
- `Core/Wotc.Mtga.DuelScene.Interactions/SubmitDeckWorkflow.cs`:
  `LatestGameState.GameInfo.DeckConstraintInfo`, match-local submission,
  scene unlock, and `MatchManager.LocalPlayerInfo.SetDeckInfo`.
- `Core/Wotc.Mtga.DuelScene/DuelSceneStateManager.cs` and `DuelSceneState.cs`:
  `SetState(false)` in C# resolves to a three-argument nullable overload. The
  bridge instead constructs the exact five-field `DuelSceneState` value,
  preserving the current flags except the transition lock, and calls the
  one-argument value overload. All value construction occurs before sending.
- `Core/MatchManager.cs`: `SetDeckInfo` clears its style list. Copy `CardStyles`
  through the actual List's `ToArray()` first, rather than pass the list to itself.
- `Core/DeckBuilderModel.cs`: the Limited editor's generated sideboard includes
  its free basic-land pool. The GRE deck, not that expanded editor pool, is the
  registration authority for automatic plans.

No live protocol calls were made to develop or test this change. These members
must still exist in the running client. Missing members, truncated collections,
unknown constraints, absent delegates, or unsupported retained-value operations
stop before submission. The repository's current reflect-1 probe supports the
boxed-value retention and value constructors this path requires; old loaded
probes can fail the preflight without sending a deck.

## Plan and submission rules

- Scope is Traditional **Limited**, checked from both `SuperFormat` and the
  active editor context. Constructed sideboarding remains a manual handoff.
- The model receives exact registered card ids/counts, available rules text, and
  opponent evidence only when its current or completed match/game identity matches.
- One model call has an eight-second budget. With 15 seconds or less remaining,
  no opponent evidence, a busy/unavailable model, or an invalid answer, retain
  the current deck. Late responses are never applied to a later request.
- Accept at most five nonland swaps, with no repeated/circular ids, no invented
  cards, and exact conservation of the registered multiset and both pile sizes.
  Enforce all six GRE deck/sideboard/commander size bounds. Decks with a commander
  or companion may be retained unchanged, but automatic swaps are refused.
- `request_id` binds match id, completed-game number, GRE game-state id, message
  id, and native request handle. `source_deck` must still match exactly.
- Read the active sideboard workflow and editor. Manual main/commander/companion
  edits cause a handoff. Recheck request, delegate, identity, constraints, context,
  and editor contents in the same Unity frame as the final send.
- Clone `request.Deck`, then modify only the clone's main and sideboard lists.
  The original request deck, saved collection deck, event deck, and editor model
  are not written. Commander, companion, and unknown protobuf fields survive.
- Attempt once per completed game. A consumed delegate, concurrent caller,
  rejected request, or uncertain transport outcome cannot trigger a resend.
  A reissued request or an unchanged sideboard after 15 seconds produces a
  manual-status message. A later game has a separate identity and may submit.
- Success means **submitted, awaiting acceptance**. The driver releases the UI
  when gameplay resumes; source tests are not evidence of live acceptance.

## Verification status

Offline tests interpret actual reflection operations, including separate source,
clone and saved decks; stale/manual changes in the final frame; correct scene
state overload; style preservation; spent delegates; duplicate/concurrent calls;
rejection/lost-reply behavior; old-probe rejection; model timeout/invalid proposals;
and subsequent-game transitions. Live Traditional Limited verification remains
pending. No event was entered, no currency spent, and no running deck changed.
