# Decision & Verification Log

Standing log of architectural decisions, their rationale, the evidence behind
them, and falsifiable claims other agents can re-verify. Companion to
[PLATFORM_PARITY.md](PLATFORM_PARITY.md) (the gap tables live there; this doc
records *why* and *how we know*). Newest entries first.

---

## 2026-10-04 — Price commander recovery before accepting equivalent blocks (v3.2.1)

- Close the recorded autoplay blocker-selection acceptance issue from v3.2.0.
  The real GLM replay still proposes token 1042; adding correct facts and more
  reasoning did not reliably change that choice. The final planner now validates
  the proposed resource trade with a bounded Oracle-derived continuation.
  This is a deterministic correction, explicitly marked `planner_combat_recovery`
  so it cannot be mistaken for a successful model decision in training records.
- Compile supported self-entry effects (fixed creature tokens, guarded
  nonlegendary self-copies and fixed draws) from Oracle text, without named-card
  policies. Cache these mechanics and include their resource arithmetic in the
  initial deck analysis. The block solver prices recovery alongside immediate
  combat; material is discounted to 75% and the full observed recast cost is
  charged. This is a heuristic continuation, not a complete Magic simulator.
- Compute payment from every surviving source after the next ordinary untap.
  Count fixed multimana and per-type yields accurately, choose only one output
  per source, check colored pips, and exclude all combat deaths. Ignore costly,
  restricted or unsupported mana abilities rather than credit unproven mana.
  Current pumped stats do not become printed stats of fresh copies.
- Before accepting a losing single block, compare substitutions with the same
  blocked attackers, damage through, enemy deaths and number of own deaths.
  Require known empty hand, exact commander identity, current GRE legality,
  known printed stats/tax, payable recovery and a meaningful resource advantage.
  Counters, attachments, missing facts, stack actions and recognized interfering
  effects disable correction. Unknown mechanics and competing cards in hand
  remain decisions for the tactical planner. No additional model call is made.
- Update both blocker identity representations, reasoning and narration together.
  A selected recovery line carries through the immediately following verified
  command-zone prompt, scoped to this match, turn and commander, including its
  new graveyard instance. Existing request freshness checks remain in force.
- Recorded board: original 1034 blocks Samurai 1046; Bird 1057 blocks Atraxa 849.
  Both old Hobbit tokens survive. Eight lands plus two producers yielding two
  colorless each provide 12 mana, paying the nine-mana recast including GG and
  tax. Resolving the entry leaves five Hobbits; once all are ready they yield
  25C plus the eight lands. New creatures cannot tap immediately.
- Verification: read-only production-backend replay selects those exact final
  assignments; no game inputs submitted. Regression coverage includes other
  entry commanders, ordinary nontoken substitutes, ongoing engines, high/unknown
  tax, missing colors, dead mana sources, investments, legality, training origin,
  narration and command-zone continuation. A new live match remains untested.
- Final checks: `ruff check src tests`, `ruff format --check src tests` and the
  full suite passed (2,519 passed, 7 skipped). One preceding full run stalled
  late and was stopped; the isolated late tests and clean complete rerun passed.

---

## 2026-10-04 — Learn conditional deck policies from Oracle text (v3.2.0)

- The strategy saved in `bug_20261004_103352` omitted The Notary Hobbits,
  despite it being the designated commander, and claimed an Emrakul cast
  trigger from putting it onto the battlefield with Tooth and Nail. At
  10:33:42 the planner sacrificed token 1042 instead of original 1034 against
  Samurai 1046. The user requested a general deck-learning fix rather than
  a hardcoded commander/token substitution.
- Analyze the complete starting deck, costs, types, related faces and explicitly
  designated commander(s) in one background task. Store a structured playbook:
  primary/backup plans, key-card roles, Oracle-backed mechanisms, conditional
  exceptions to normal heuristics, phase priorities and recovery policies.
  Each exception links to mechanisms and states when it applies and when its
  costs/risks invalidate it. Commander policies compare preserving versus
  spending/recovering from a shared resource state, including both resulting
  boards, timing, lost investments and actual affordability after tax. No
  card-name-specific action override is added.
- Validate referenced cards, commander coverage, source-rule handles, both
  resource comparisons and rule links. Attach original Oracle rules to each
  mechanism in code. First reason about the deck in free-form notes; a second
  setup request checks those notes against the sources and compiles the JSON.
  Compilation permits one structural repair, then reports failure rather than
  storing error prose. Source validation proves provenance, not strategic
  correctness. Keep inference failures visible in diagnostics.
  Keep output focused on game-changing interactions instead of repeating every
  card description. The old `use_thinking` argument was unsupported by the
  backend and discarded the intended output budget through a TypeError fallback.
- Start analysis as soon as full deck inventory is available, including during
  the opening hand. Use a dedicated backend and enable reasoning for discovery,
  with a 120-second per-request ceiling. Compilation and tactical requests use
  their usual reasoning setting; per-request overrides never change the backend
  default. The bounded output allowance includes hidden reasoning; incomplete
  final JSON is rejected. Tactical request timeouts stay unchanged.
- Tactical decisions reuse the playbook and receive relevant conditions near
  the live choice. They do not rerun deck analysis or repeat the entire playbook
  alongside those same rules. Combat comparisons explicitly enumerate surviving
  creatures in each branch; losing a substitute in one branch must not also
  remove it from the commander-dies branch. These are resource facts, not an
  action override. The changing game plan keeps
  active mechanisms, resource priorities and assumptions, and refreshes when
  command-zone availability or tax changes. Learned sequencing/zone rules can
  reach the model instead of being preempted by generic land-first/command-zone
  shortcuts; legality, request freshness and submission checks still apply.
- Key analysis to starting deck counts and designated commander IDs. Changing
  decks invalidates late workers; reloads preserve validated same-deck playbooks
  and rebuild older prose-only summaries. Spoken summaries never overwrite
  internal strategy. Bug reports retain the playbook and analysis status.
- Narration now uses “Attacking with…”, “Blocking with…” and “Casting…”. Preserve
  my/mine possessives, grouped combat targets and intent-versus-resolution
  distinctions. Remove recognized model-supplied “I'm…” action prefixes as well
  as the formatter's automatic prefix.


- Verification: `ruff check src tests`, `ruff format --check src tests`, and the
  full pytest suite passed (2,468 passed, 7 skipped). Refresh five stale test
  expectations/fixtures for existing idle-bridge consumption, target-source
  validation, desktop reload wiring and passive speech filtering.
- **Acceptance issue at v3.2.0 (resolved for this replay in v3.2.1 above):** the sanitized pre-block board is retained in
  `tests/fixtures/commander_block_20261004_103338.json`. The final read-only
  GLM-5.3-Flash evaluation produced a valid full-deck playbook (74 unique card
  references), but the tactical replay still chose token 1042 against Samurai
  1046 instead of commander 1034. No game actions were submitted. This is NOT
  a verified fix for the reported commander block. The model's interpretations
  and resource priorities remain unreliable despite explicit Oracle references
  and both resource branches; passing structural tests is not evidence of good
  strategic play. Some additional evaluations also encountered backend timeouts.
  Auto-queue still needs a complete live post-match verification as recorded
  under v3.1.4. Keep these acceptance issues open.

## 2026-10-04 — Verify result dismissal, retry queueing, and group combat speech (v3.1.4)

- The 10:14 match-end log shows a `DEFEAT` click delivered at 10:14:47,
  followed by three `Unexpected navigation button label` failures. A delivered
  click did not prove the overlay closed, and the rejected label was not logged.
  The user confirmed the result animation has no button and accepts one click.
- Separate the observed `result_title` from proposed button labels. A verified
  Victory/Defeat/Draw title identifies a click-anywhere overlay, even if the model
  calls the old battlefield `match` or invents a Continue label. Click its center,
  wait for the animation, and report closure only after a fresh observation of
  the next screen with the result absent. Existing input and new-match guards apply.
- Supply the validator's allowed actions/screens/labels to the model. Retain and
  log rejected proposals, then include the specific rejection in the next request
  so retries can correct it instead of repeating an unexplained invalid choice.
- Disabling navigation retains an already trusted unfinished match completion.
  Re-enabling retries that navigation, with a new cancellation generation and a
  fresh observation. Startup still cannot initiate a game; a newly detected match
  consumes the old completion and prevents stale navigation input.
- The next live sequence at 10:27:55 classified the post-result loading screen
  as `queue` before any queue-start click. That imposed a 30-second wait. Generic
  loading now polls again after three seconds and cannot establish a new match;
  only a submitted queue start or confidently observed matchmaking gets the
  longer wait and visual handoff. The 10:28:18 toggle did successfully resume
  the already finished match; the subsequent Play click was withheld because
  the screen changed. A complete automatic requeue is not yet verified live.
- The user's three-attacker example repeated "I'm attacking" once per creature.
  Group validated attackers/blockers by recipient and describe each declaration
  as one sentence. Strip execution IDs from speech while retaining them in the
  action, preserve token/copy distinctions, and use my/mine for local-player
  possessives. Do not rewrite card titles such as Your Temple Is Under Attack.
- Validation: 254 queue, runtime, native-input, narration, combat-identity and
  version tests passed, including result-title dismissal without a button,
  confirmation after the click, proposal repair, toggle retry, loading versus
  matchmaking, grouped speech, and new-match/cancellation guards. Ruff and format
  checks pass.

## 2026-10-04 — Dismiss result overlays before requeueing (v3.1.3)

- Logs show the navigator repeatedly classified the ended battlefield as
  `match/wait` and exhausted its three-error budget. At 09:49:35 it recognized
  `results/wait`, label `DEFEAT`, confidence 0.9, but had no dismissal action:
  the schema only allowed Continue/Done-style buttons, not the result overlay.
- Add a narrowly scoped `dismiss_result` action for a visible, confidently
  identified Victory/Defeat/Draw overlay. Normalize `results/wait` with a
  verified result title to this action. The overlay accepts a center click
  when the model supplies no point; all other navigation targets still need
  explicit coordinates. The authoritative match-completion gate, fresh frame
  comparison, new-match recheck, cancellation, foreground and input-ownership
  checks remain in place.
- Treat the old battlefield during the end animation as a bounded wait rather
  than a failed navigation action. Allow 12 observations before pausing; never
  infer a new match from that board before this cycle has reached matchmaking.
  Valid waits reset the consecutive-error counter. Log actual navigation
  exceptions at INFO so future reports retain why a step failed.
- Validation: 149 queue, runtime, native-input and release tests passed. These
  include all three result titles through dismissal, Recently Played, queue,
  and new-match handoff, plus stale-screen/cancellation and unverified-overlay
  cases. Verification was offline; no live clicks or queue entries were sent.

## 2026-10-04 — Compare commander recovery with losing a token copy

- Report `bug_20261004_094314`, match `7843532a-4907-4829-90f9-adfb9862ef50`:
  at 09:42:53 autoplay traded Hobbit token 481 for Gnome 495 despite commander
  472 also being a legal blocker. Its stated plan preserved mana engines but
  omitted the original's recast trigger. Arena's designation reported tax 2;
  the next commander cast would cost seven mana and create two new copies.
- The server omitted the existing commander-cast map from its response. Preserve
  it, and synchronize the local player's count from Arena's commander designation
  `CostIncrease`, which survives instance-ID changes and mid-game reconnects.
  Do not use the opponent's count, even when both commanders share a card ID.
- Both coach and action-planner combat context now identify the exact owned
  commander and its legal token alternatives, surface printed cost plus tax
  (or explicitly unknown tax), and compare deaths under visible combat. Prefer
  commander recovery for otherwise equivalent blocks when an affordable recast
  rebuilds more value. Account for surviving mana sources, colored requirements,
  other planned spells, and summoning sickness. The approximate material solver
  still does not simulate future recasts; this adds strategic grounding rather
  than blindly replacing every token block with the commander.
- Offline regression coverage replays the identities and rules, verifies exact
  original-ID binding, rejects ineligible/stolen/token commanders, and covers
  high/unknown tax, non-dying blockers, annotation replay, and server preservation.
  No live match was driven or restarted during these checks.
- Validation: the commander/combat/server group passed 89 tests; the typed
  decision/combat/release group passed 135 tests. These targeted runs do not
  replace the earlier full-suite baseline, which had unrelated failures.

## 2026-10-04 — Preserve the commander-return policy in typed decisions

- Watched match `ee1f33ed-6b2b-4808-b553-86c09d896001`, report
  `bug_20261004_093656`: at 09:35:52 the model declined "Return The Notary
  Hobbits to the command zone?", incorrectly claiming that would prevent
  Portal to Phyrexia from reanimating it. The opponent subsequently took the
  commander and received its two token copies. Arena's request was prompt
  144, ZoneTransfer, recipient 856, gameStateId 320, msgId 442.
- The log parser recognized the commander prompt, but the typed option
  planner bypassed the existing deterministic return policy in the legacy
  action planner. Apply that policy before asking the model, only when the
  parser's validated commander context matches the typed request IDs and
  any supplied recipient IDs. Submission still re-polls the live request.
- Regression coverage supplies the observed incorrect model decline and
  verifies that no model call occurs, acceptance is bound to the original
  request, and a changed request receives no answer. Unrecognized, stale,
  unknown-ID and different-recipient choices retain their normal planning.
- Other observations from this match remain unresolved: Settle the Wilds
  consumed a Treasure before the available Forest land drop (09:32:18), and
  Malevolent Rumble's typed SelectN options lost their card identities
  (09:34:18; the match packet records `Option 256/257/258`, all grpId 0).
  The attack-confirmation fix succeeded live for Kogla at 09:38:38. The match
  ended in a loss at 09:40:27; this does not isolate any single cause of loss.

## 2026-10-04 — Confirm attacks by recipient identity across native object copies

- Report `bug_20261004_092322`: Innkeeper (instance 451) was offered as a
  legal attacker. Both 09:22:56 and 09:23:02 attempts updated its selection,
  passed the fresh-request acknowledgment check, then failed confirmation
  with "Attack declaration changed before confirmation."
- Confirmation compared `SelectedDamageRecipient` and the legal recipient
  by managed object handle. Different objects representing the same player
  or planeswalker therefore failed despite matching combat identities. Use
  the populated recipient kind and ID; retain exact-handle equality for
  shared `$ref` nodes. Unreadable identities still fail closed.
- The regression reproduces update → fresh acknowledgment → confirmation
  with a copied opponent recipient; it failed before this fix. Coverage
  also retains rejection of changed requests, different recipients, extra
  attackers, and equal numeric IDs belonging to different recipient kinds.
  This verifies the adapter path without driving a live match; the report
  did not capture the underlying native recipient handles.

## 2026-10-04 — Keep ETB searches fresh and preserve every selected card ID

- Worldwagon report `bug_20261004_005836`: accepting its optional ETB opened
  Search, but the next trigger in the same batch reused the consumed Accept
  prompt. A generic button submission then failed against Search and marked
  the new search as requiring manual input. A handled autopilot trigger now
  ends the batch so the loop re-polls. Connected bridge transitions own the
  window; an idle/error poll cannot fall through to stale legacy planning.
  Optional buttons are checked again before submission, and fresh typed
  choices receive the matching request context instead of old prompt fields.
- Vorinclex at 01:01:01 and 01:01:06: the model chose Forest instances
  `[480, 475]`, but Player.log recorded outgoing Search responses with
  `[480, 0]` and explicit `FailureReason_InvalidOptionSelection` rejections.
  The earlier 00:35 capture likewise sent `[671, 0]` for `[671, 692]`.
  These are bridge serialization failures, independent of the strategic choice.
  The native converter used the array class where IL2CPP expects its element
  class, writing 32-bit values at pointer-sized offsets. Both array reading
  and writing now use element size. For an already loaded older bridge, the
  Python adapter constructs exact-length uint arrays through managed scalar
  Add calls, covering searches, modal choices, ordering and other uint lists.
  The universal probe was rebuilt and staged atomically; the running Arena
  process is not replaced. Reloading the engine activates the uint workaround,
  while Arena's next launch loads the native fix for all primitive arrays.
  A returned method call alone is still not proof Arena accepted the choice.
- Regression coverage includes Optional → idle → Search, fresh two-Forest
  selection after a pre-cast snapshot, stale Accept against Search, and
  trigger-batch re-polling. Native acceptance still requires a live observation;
  no user match is driven by tests.

## 2026-09-29 — Require a usable body before temporary animation or crew

- Report `bug_20260929_211332`: Firdoch Core tapped to help cast Badgermole
  Cub, then the planner spent four mana animating it as a supposed "free
  attacker." Animation did not untap Core; Woodfall Primus had also just
  entered without haste. Arena's later attack menu offered only the zero-power
  Grazer. The wasted activation preceded the correct decision not to attack.
- Report `bug_20260929_212214`: the planner crewed a newly entered Lumbering
  Worldwagon with the newly entered Thorn Mammoth, claiming both could attack
  and that crewing would fetch a land. Crew does not grant haste or retrigger
  entering; Worldwagon's land trigger requires entering or an actual attack.
  The later legal attacker menu contained only Innkeeper and two Hobbit tokens.
- Shared typed/legacy preflight now withholds simple, unambiguous animation or
  crew when the body remains tapped, is redundant, or just entered on our turn
  without visible haste and has no visible other payoff. Known crew/tap triggers,
  untap effects, sacrifices, creature counts, power-based mana, and responses to
  relevant targeted spells remain planner decisions. An opponent's turn can
  justify crewing a new Vehicle to block. Unknown entry turns do not prove
  summoning sickness; ambiguous abilities and added effects are not filtered.
- Activation options retain Arena's payment solution and show cost, source tap
  state, entry turn, and current types. The board labels new Vehicles' attack
  restriction before they are crewed. Current types determine creature status
  and combat summaries; stale power/toughness after animation expires no longer
  makes a noncreature look like an active attacker. These checks are conservative
  visible-state heuristics, not a complete rules engine or a combat guarantee.

## 2026-09-29 — Value large nonlethal hits against a single expendable blocker

- Report `bug_20260929_210547`: the planner deliberately submitted no blocks
  against a 10/9 to preserve mana producers for Tooth and Nail. Arena offered
  Shang-Chi and two Notary Hobbit tokens as blockers. Life fell from 24 to 14;
  this was a strategic choice, not a missing blocker or speech/execution mismatch.
- The reconstructed solver also chose no blocks: its inverse-life penalty
  valued that ten-point hit at only 3.57 material points, versus 9 for Shang-Chi
  and 10 for each scaling-mana Hobbit. Normalize life to one material point per
  life at 20 remaining life, with increasing cost as the life total shrinks.
  The same board now sacrifices the support creature and preserves both tokens.
  Small-hit engine-preservation cases continue to pass; abundant life, trample,
  and Arena's per-blocker legality can still justify a different decision.
- Combat prompts now show life after the recommended blocks, explicitly weigh
  nonlethal damage and the next attack, and surface mana spending restrictions.
  Shang-Chi's creature-ability-only mana cannot be counted toward Tooth and Nail.
  These remain approximate visible-board valuations, not a guarantee of the
  best line against hidden cards or all continuous effects.

## 2026-09-29 — Resolve search identities and narrate submitted moves

- Report `bug_20260929_203249`: Tooth and Nail's library search reached the
  model as anonymous instance labels such as `Option 537`, with a default
  one-card limit. The model chose one anonymous option and claimed it was
  the strongest creature; the next hand contained Shang-Chi. This is not
  evidence that it compared Shang-Chi's text with the offered Eldrazi.
- Both bridge implementations now expose SearchRequest's Min/Max and resolve
  the offered instance IDs to card group IDs. Native reflection reads only
  those offered instances, checks request identity, and rejects truncated
  candidate lists. The typed chooser receives names and rules text, preserves
  the entire valid selection, and never falls back to the first tutor candidate
  after an invalid model answer. Missing identities require manual selection.
  A deliberate legal empty selection can still finish a search.
- Report `bug_20260929_201852`: the model chose Done to preserve its mana
  creatures. The combat approximation incorrectly read Selfless Savior's
  granted indestructibility as its own keyword. Keyword recognition now reads
  printed keyword lines; equal-score attacks prefer actual damage, then fewer
  attackers. The reconstructed Halfling/Savior board now selects Halfling.
- Report `bug_20260929_202111`: the model announced Birds and Vaultborn Tyrant
  before discovering the attack window had closed; that attempt did not submit.
  An explicit empty protobuf stat wrapper now means zero, while an absent stat
  remains unknown. Zero-power attackers with no visible payoff are omitted;
  mandatory attacks, attack triggers, pump possibilities, and toughness-based
  damage are exempt. The solver remains an approximation of visible combat.
- Autoplay speech now follows successful submission and uses the actual move
  returned by handlers after refinements. Stale, closed, or failed submissions
  do not announce an executed move. Background strategy stays in its dedicated
  UI panel. Typed speech omits the internal ActionsAvailable request prefix;
  stale casting-mode answers cannot fall through against the next request.
- Synthetic regressions cover combat outcomes, submitted attacker identities,
  speech timing, search bounds, card metadata, invalid/empty selections, and
  stale requests. Native behavior still needs a coach reload and live check;
  no ongoing game was restarted. Windows bridge source was updated for parity;
  no .NET SDK is available on this Mac to build that plugin.

## 2026-09-29 — Close prompt, combat, Limited, and card-coverage gaps

- Casting-time decisions now use the actual modal/numeric options, including
  localized ability text, rather than guessing a generic "Cast normally" action.
  Casting and optional choices are bound to the current request identity. A
  closed window is not retried against the next prompt; an unverified submission
  stops for manual input. Optional decisions include source and mechanic context.
- Two-sided fight/bite spells are mixed effects: choosing your own damage source
  is legal, not automatically a harmful-target mistake. Per-slot legality still
  applies. Updated the older fight-polarity regression to reflect this distinction.
- A bounded joint combat search scores player damage, known planeswalker loyalty,
  shared blockers, lost material, and surviving defenders against a counterattack.
  It supports split attacks and preserves recipient intent through Done-only
  confirmation. Unknown loyalty never establishes a kill. This remains a visible-
  board approximation, not a complete rules engine or proof against hidden tricks.
- Limited suggestions receive conservative, directed rules-text enabler/payoff
  links, actual copy counts, and curve/body requirements. Unsupported named synergy
  claims fail validation. Token-producing spells count as bodies; conditional
  token payoffs are not treated as unconditional early creatures. Mana validation
  includes required colorless sources. Unmodeled interactions remain model judgments.
- Native deck-editor observation reads the active limited model with read-only
  reflection, excludes constructed/sideboarding contexts, checks deck identity,
  and rejects truncated lists. Counts include unsaved edits. Spoken remaining cuts
  and additions reconcile against that snapshot, debounce edits, and reuse the
  same proposal instead of changing the target build after every click. Other
  platforms retain explicitly labeled logged-deck/pool advice. Nothing edits or
  saves the user's deck automatically.
- `python -m arenamcp.card_audit` checks all installed records in batches. On this
  installation: 27,071 records = 27,044 card records + 23 effect/copy markers + four
  wildcard placeholders; zero unresolved card records, missing printed costs, or
  missing text for cards with ability IDs. Unknown Shores is a real name, not an
  unresolved-card sentinel. Coverage is data availability, not verification of
  every interaction. Earlier same-day feed refresh remains in place.
- Regression tests exercise these paths; running-client metadata confirms the
  native editor class exists. Active editor behavior still requires a coach reload
  and live check. Do not restart an ongoing game just to validate the changes.

## 2026-09-29 — Preserve attackers and explicitly choose combat recipients

- Report `bug_20260929_191114`: the bridge offered Heartwood Crafter, Pia,
  and Beast, but bridge enrichment failed to restore attacker context after a
  log-window clear. The rules menu became Done-only and confirmed no attack.
  Current bridge candidates now repopulate attacker names, IDs, and raw legal
  recipients. Missing candidate resolution must not finalize an empty attack.
- The later report `bug_20260929_191654` follows an explicit model choice to
  hold back, not the same lost-context path. The user's additional observation
  exposed a separate execution defect: the native adapter always picked the
  first damage recipient, ignoring player-versus-planeswalker intent.
- Structured attacks now carry per-creature recipient names, including exact
  planeswalker identities. The planner sees legal recipients, names its targets
  aloud, and the native adapter validates and honors them independent of list
  order. Split attacks are supported. Ambiguous, vanished, or partially resolved
  selections must not silently attack the player or confirm an empty declaration.
  An incomplete or failed final confirmation is not reported as success.
- The existing combat solver evaluates player attacks only. Its material/life
  override must not overwrite an explicit planeswalker attack. This does not
  establish strategic optimality for planeswalker or split attacks. Native-Mac
  bridge regressions cover reversed recipient order, changed selections, split
  targets, stale identities, and failed finalization; live reload is still needed.

## 2026-09-29 — Speak counted forty-card builds and named cuts

- Completed draft courses expose their authoritative CardPool at DeckSelect.
  Recover that full pool rather than depending on every live pick or a retained
  match snapshot. Repeated event metadata must not erase the completed pool.
- Build advice validates available copies, exactly forty cards, a supported land
  count, and a reason for every excluded nonbasic card. Ordinary basics are free
  additions. Rules-aware model advice falls back to a counted curve/color build.
- Speak named cuts and land allocations once per pool, retain the detailed keep
  list in the compact UI, and discard advice if a match starts while computing it.
  This is a proposed build from the drafted pool, not a verified view of the live
  deck editor. No card is moved in the editor automatically.

---

## 2026-09-29 — Draft advice follows live packs, actual picks, and current card rules

- Live logs at 18:54–18:59 showed every new FRA draft pack immediately cleared
  by a retained match snapshot. Reading draft state must not infer match lifecycle
  from an old snapshot; new match events still deactivate the draft. Startup now
  replays the current draft segment, including its event and earlier picks, rather
  than only the last pack. Duplicate card copies count; duplicate pick events do not.
- Spoken draft recommendations now use the unified card database and a bounded
  rules-based model request. The prompt includes the actual counted pool, curve,
  colors, prior provisional theme, printed rules, and linked spells. PickTwo choices
  are considered together. Invalid IDs, unsupported synergy references, stale
  windows, and timeouts fall back to deterministic card-text recommendations.
  Detailed explanations, plan, remaining needs, and alternatives remain visible;
  speech does not block polling for the next pack.
- MTGJSON's index fast path previously bypassed its daily freshness check, leaving
  July data active. Refresh now precedes index loading and replaces the cache only
  after successful download and validation; offline startup retains stale data.
  Scryfall bulk names resolve locally even without Arena IDs. MTGA's numeric colors,
  encoded printed costs, creature stats, and linked-face IDs are now decoded locally.
- Refreshed MTGJSON to `5.3.0+20260929` and Scryfall to its September 29 bulk snapshot.
  Audited all 447 installed FRA entries: no missing names, types, or printed costs;
  42 entries expose linked faces. Vanilla creatures and tokens may have no rules text.
- Reports `bug_20260929_183114` and `bug_20260929_183205` showed Artifist Acumen's
  unconditional draw on an empty board, then a tapped land leaving insufficient mana
  for the available creatures. Those choices are not intrinsically illegal, but their
  rationale was discarded. Typed action explanations now reach the normal UI and
  logs, and are attached only when they match the option actually submitted.
- Live verification after the 19:00 restart: P3P2 and P3P3 produced named-card
  interaction explanations and a continuing deck plan. This verifies generation;
  it does not establish that every strategic recommendation is correct.

---

## 2026-09-29 — An installed native Mac bridge must report connection failures

- Report `bug_20260929_182449` executed zero autopilot actions: the native
  MTGA process was running without `libmtgacoach_probe.dylib` loaded, while
  the coach listened on port 44222. The library was staged, but the old
  BepInEx-only capability check incorrectly logged this as designed log-only
  coaching. Autopilot then offered Linux/Proton injection instructions.
- Keep log-only startup informational when no native bridge is installed.
  When the native library is installed or the selected device is Android,
  an absent connection gets actionable recovery guidance for that device.
  Reconnect failures and manual-required advice use the same guidance;
  snapshots without bridge metadata fall back to the live connection flag.
- Native Mac recovery requires quitting MTGA after the match and reopening
  the coach, which launches the game with the library injected. Steam launches
  can instead use the existing `mac_bridge_launch_option()` setting. Running
  matches are left alone; these changes do not inject into an existing process.
- Regressions cover installed versus absent Mac libraries, reconnect timeout,
  missing snapshot metadata, CrossOver, Proton, Windows, and Android guidance.

---

## 2026-09-28 — Combat material includes bodies and ongoing resource abilities

- Report `bug_20260928_224954`: all three Notary Hobbits and Badgermole Cub
  blocked and died killing a 5/5 Wary Zone Guard. The model explicitly chose
  that trade; the bridge executed it correctly. Replaying the combat at 21 life
  reproduces the same recommendation from the old solver: attacker P+T=10,
  sacrificed blockers P+T=10, so preventing five damage broke the apparent tie.
- P+T alone ignored both losing four bodies and dismantling the mana engine.
  Material now includes a per-body value and bounded premiums for recognized
  ongoing mana production/scaling, card draw, token production, and recursion.
  These are approximate strategic values, not mana-pool or castability estimates.
  Token copies retain the value of their abilities; names are not special-cased.
  Repeated oracle renderings do not multiply value, and spent ETBs do not earn
  repeatable-resource premiums. The bounded fallback also compares its chump
  plan against taking the damage, rather than always blocking.
- Prompts no longer describe the solver's heuristic scores as an exhaustive
  strategic truth. They explicitly compare safe damage against losing resources
  needed for next-turn recovery. Lethal damage still takes precedence.
- Regressions in `test_combat_resource_value.py` cover the observed four-for-one,
  generic engines, expendable versus mana-producing tokens, symmetric opponent
  valuation, life-critical blocks, bounded search, and prompt context.

---

## 2026-09-28 — Complete block assignments and progress-aware equipment reuse

- Report `bug_20260928_223703` stalled with `The Notary Hobbits -> ""`.
  A `Block with:` menu entry identifies only an eligible blocker, not its
  attacker. Do not convert a bare pick into a fabricated incomplete assignment;
  preserve explicit blocker/attacker mappings and validate both names. Ask once
  for a repaired structured answer, then stand down rather than auto-confirm
  no blocks if repair fails.
- Report `bug_20260928_223837` shows Lightning Greaves hidden by a one-equip
  per-turn cap after its wearer tapped and new creatures entered. Equipment may
  be activated once per distinct friendly-creature identity/tapped/attacking
  state. Attachment-only changes do not reset the guard, and returning to an
  already-used state remains blocked. This permits useful haste reuse without
  restoring the free-equip shuffle loop. Other activation caps are unchanged.
- Preserve equip text on older permanents in planner context; both planning
  prompts weigh useful haste against protection rather than blindly moving
  equipment to any untapped creature. Regressions cover block repair, malformed
  choices, equipment progress, attachment-only changes, and repeated states.

---

## 2026-09-28 — Unknown targeting effects are not beneficial effects

- Report `bug_20260928_222542`: the planner selected opposing lands 548 and 744
  for Ulamog's cast trigger. The controller-aware safety override missed
  "exile two target permanents", classified it as beneficial, and substituted
  friendly creatures 545 and 859. The wire response selected Llanowar Elves 545;
  the subsequent exile annotation confirms the loss.
- Share a conservative, quantity-aware target classifier between planning and
  single-target auto-selection. Only recognized, unmixed effects get a polarity;
  unknown effects do not justify replacing enemy picks with friendly ones.
  Without a valid model choice or safe fallback, decline rather than pick the
  first legal permanent.
- Validate every slot's remaining count using only explicitly chosen targets.
  A single slot requiring two targets must retain both; neither executor may
  invent a first-legal replacement. The native Mac adapter sends all choices for
  that slot and finalizes only after updated GRE acknowledgment, never merely
  because a timeout elapsed. A failed target submission pauses for manual review.
- Regression coverage: `test_counted_target_selection.py`,
  `test_mac_bridge_adapter.py`, and `test_typed_decision_path.py`. Native Mac
  protocol changes still require live verification after restarting the coach.

---

## 2026-09-28 — Engine payment and selection constraints are authoritative

- Current bridge auto-pay solutions determine castability, not a reconstructed
  mana pool or printed cost. Typed prompts use the current decision's menu,
  suppress contradictory mana estimates, and reassess after each action.
  Mana/activated ability text remains visible on older permanents.
- Search and SelectN decisions carry their own legal choices. Private library
  updates can be absent from Player.log (observed search request 128 after board
  update 127), so these choices do not wait for an impossible log watermark.
  ActionsAvailable still waits for the board; every submission rechecks the
  current request identity after planning.
- Native Mac effect-cost selections are exposed through the typed SelectN path,
  not mana auto-pay. Submit to `PayCostsRequest.EffectCost.CostSelection` so
  Arena's existing child callbacks build the correct EffectCost response.
  Preserve engine weights and count/weight bounds through planning, request
  identity, and submission. Crew 4 can use one contribution-4 creature or
  contributions 3+1; it does not require four creatures.
- Evidence: reports `bug_20260928_215031`, `bug_20260928_220132`, and
  `bug_20260928_220255`; local decompiled `PayCostsRequest`, `EffectCostRequest`,
  and `WeightedSelectionState`; regressions in `test_coach_engine_castability.py`,
  `test_autopilot_log_catch_up.py`, and `test_weighted_decisions.py`.
  Adapter behavior is unit-tested; live in-game verification is still required.

---

## 2026-09-23 — Native-macOS GRE bridge via the IL2CPP C API (logs stay the eyes)

### The decision

- **Autopilot on native Mac submits through the game's own GRE request objects**,
  like the Windows BepInEx plugin, instead of screenshots and synthetic clicks.
  No Wine/CrossOver. Vision autoplay remains only as the fallback when the bridge
  is not connected.
- **Observation stays log-first.** Player.log remains the primary source of what
  is going on; the bridge is the hands. The Mac adapter deliberately answers
  `get_game_state` as unsupported so the coach keeps its log-derived board
  (a connected bridge's `get_game_state` otherwise *replaces* battlefield, hand
  and graveyards in `server._get_bridge_overlay`).
- **Architecture:** a small injected arm64 library (`spikes/mac-il2cpp/probe.cpp`)
  exposes generic main-thread reflection (`reflect_batch`: pending, get, call,
  new, set, expect, expect_pending) over the existing bridge socket. The
  BepInEx plugin's command handlers are re-implemented in Python
  (`arenamcp/mac_bridge_adapter.py`) on top of it, returning the plugin's
  response shapes, so `GREBridge` and `AutopilotEngine` run unchanged. The
  Windows plugin is not replaced: Windows and Linux (Proton) run the Mono build,
  where BepInEx stays the right tool.
- Supersedes the 2026-07-16 "no native-macOS bridge" line and its rejected
  alternatives below (BepInEx 6/Il2CppInterop, direct memory access).

### Why (rationale chain)

1. The 2026-09-23 native-Mac vision autopilot match made two inputs (Keep, one
   Pass) and zero card plays; seconds per vision call, focus dependence and
   pause-forever handling made it unusable (standalone.log from 21:41).
2. The Mac client exports the IL2CPP runtime API, so an injected library can
   find classes and call methods by name. That needs no Cpp2IL/Il2CppInterop,
   no .NET hosting and no Rosetta, and it does not depend on the metadata format.
3. BepInEx's IL2CPP macOS build is x64-only (Rosetta, which Apple retires for
   general apps after macOS 27), and its metadata support lags Unity upgrades
   (v39 landed March 2026, #1284; v107 unsupported, #1395).
4. MTGA-specific logic in Python can be fixed and re-tested during a live match
   without restarting the game; the injected library stays generic and small.

### Verified facts (2026-09-23, MTGA 2026.63.0 Steam, Unity 6000.3.14f1, macOS 26.6.2 arm64)

| Claim | How verified |
|---|---|
| App not hardened-runtime signed | `codesign -dv` → `flags=0x0(none)`, no entitlements |
| Metadata v39, standard format | `global-metadata.dat` magic `0xFAB11BAF`, version 39 |
| IL2CPP C API exported | 241 `il2cpp_*` exports in the arm64 slice of `GameAssembly.dylib` |
| Main-thread access without code patching | Swapping `PAPA.Update`'s `MethodInfo->methodPointer` (name at slot 3) runs our job queue every frame; ~120 ticks/s observed |
| GRE submission works | Bot Match: `ChooseStartingPlayer`, `KeepHand`, `SubmitAction(Play #160)`; server replied `ObjectIdChanged 160→279`, `ZoneTransfer` category `PlayLand` (`spikes/mac-il2cpp/RESULTS.md`) |
| Reflection layer | 10-op batch 6 ms; 200-batch handle stress: worst batch 0.6 ms |
| Player.log records outgoing client responses | `ClientMessageType_*` blocks with `gameStateId`/`respId`/`actionType`/`instanceId`; the coach does not consume them yet |
| Opponent hand is never sent in ranked | Brawl_Ladder log: only the local hand's identities. Bot matches differ: the bot runs inside the client, so its hand appears in the log |

### Corrections / lessons (accuracy log)

1. **GC handles are pointer-sized** in this IL2CPP (since 2021.2). Storing them
   as `uint32_t` crashed MTGA on the first handle free (2026-09-23 23:50,
   `MTGA-2026-09-23-235020.ips`, fault in `il2cpp_gchandle_free` at a truncated
   address). Fixed; the library now self-tests a handle round trip at startup
   and disables handles instead of crashing.
2. **SIP strips `DYLD_*`** when launching through protected binaries
   (`/usr/bin/nohup`, `/usr/bin/env`): the first live launch ran without the
   library. Exec MTGA directly.
3. Protobuf `RepeatedField<T>` has both `Add(T)` and `Add(IEnumerable<T>)`;
   overload resolution for object arguments must check real assignability.
4. Never overwrite a dylib that a running process has mapped; install by rename.

### Falsifiable claims worth re-checking

- After each MTGA update: codesign flags still `0x0`; `global-metadata.dat`
  version; the probe log shows `reflection ready: ... handles=1` and
  `hooked PAPA.Update ... name_slot=3`.
- `tests/test_mac_bridge_adapter.py` pins the adapter's plugin-compatible shapes
  and op sequences; the member names it uses were checked against the
  2026-08-26 decompile (`re-output/`).

---

## 2026-07-22 — Sub-Second Inference, Dual Blackwell Telemetry & Ground-Truth Decision Pipeline

### The decision

- **Disable Chain-of-Thought thinking (`think: false`) by default for real-time advice**:
  Local Ollama endpoint (`10.0.0.100:11434`) running `gemma4:12b` adds ~6.36s latency per turn when CoT reasoning is enabled. Passing `extra_body: {think: false}` drops advice latency to **~820ms**.
- **Extract DeepSeek-V4-Flash reasoning fields on vLLM**:
  DeepSeek-V4-Flash running on dual Blackwell GPUs (`10.0.0.10:8002`) outputs generated advice in `choices[0].message.reasoning` / `reasoning_content` (leaving `content` empty). Adding a fallback `if not content and reasoning: content = reasoning` in `proxy.py` unlocks **129ms inference latency**.
- **Mulligan & Pre-Game Strategy Timing**:
  Pre-game deck analysis (99-card Brawl strategies) must trigger immediately upon match load (`ConnectResp` / Mulligan / Turn 0) in `standalone.py` instead of waiting for Turn 1, eliminating GPU contention during critical early turns.
- **Strict Attacker State & Mana Tag Lifecycle Rules**:
  - `gamestate.py` must clear stale `is_attacking = False` flags across turns and combat steps so non-attacking creatures (e.g. Esper Sentinel) are not misidentified as active attackers.
  - `_handle_select_n_req` in `gamestate.py` resolves library card `grp_id`s directly against `card_db`, providing full candidate names (e.g. Enlightened Tutor choices) to the LLM.
  - `coach.py` advice matcher regex must strip all mana requirement tags (`r'\s*\[[^\]]+\]'`), and fallback selection must prefer **`Pass priority`** over blind-casting unrelated instant spells when a recommended spell is unplayable due to missing mana (`[NEED:B]`).

### Why (rationale chain)

1. **Dual Blackwell GPU Power & Thermal Footprint**: Real-time telemetry during live coaching bursts on 2× NVIDIA Blackwell PRO GPUs shows GPU 1 peaking at 300W (84°C) and GPU 0 at 281W (78°C) under high token throughput. Sub-second execution keeps GPU thermal saturation low and prevents inference queueing.
2. **vLLM Reasoning Output Structure**: DeepSeek-V4-Flash on vLLM places model output in `message.reasoning` instead of `message.content`. Without explicit reasoning fallback parsing, responses were treated as empty strings (`0 chars`), causing silent coaching failures.
3. **Matcher Regex Tag Bug**: In `coach.py`, `NEED:\d+` failed to strip lettered mana tags like `[NEED:B]` or `[NEED:1+B]`, causing valid card name matches to fail and forcing `max(_candidates, key=_score_action)` to blind-pick unrelated `[OK]` instant spells (e.g. `Planar Incision`).

### Verified telemetry & test facts

| Metric / Claim | Observation / Verification |
|---|---|
| DeepSeek-V4-Flash Inference Latency | **129 ms** on dual Blackwell GPUs (`10.0.0.10:8002`) with reasoning fallback enabled |
| Ollama Gemma-4-12B Latency | **820 ms** with `think: false` (was 6.36s with CoT thinking enabled) |
| GPU Power Draw Peak | **300W** (GPU 1) / **281W** (GPU 0) recorded during live inference bursts (Grafana dashboard) |
| GPU Operating Temperature Peak | **84°C** (GPU 1) / **78°C** (GPU 0) under load |
| Regression Test Suite | **34 passed in 30.20s** (`test_coach_advice_matching.py`, `test_decisions.py`, `test_gamestate_gre_normalization.py`, `test_block_advice_specificity.py`) |

---

## 2026-07-16 — "Logs are the eyes and ears; the bridge is the hands"

### The decision

- **All observation (coaching intelligence) must be fully derivable from
  `Player.log`** with Detailed Logs enabled. The coach must never *require*
  the GRE bridge. This makes the coach tier identical on Windows, Linux, macOS.
- **The GRE bridge exists for action submission (autopilot) and enrichment
  only** (proactive decision polling, card screen positions, replays). It runs
  against the Windows Mono build: natively on Windows, Proton on Linux,
  Wine/CrossOver on macOS. There is deliberately **no native-macOS bridge**.
- Codified in CLAUDE.md's "2026-07-16 Working Model", superseding the
  2026-03-28 bridge-authoritative model.

### Why (rationale chain)

1. **The bridge never built state anyway.** Code sweep established that
   `Player.log` is the only state-*building* pipeline; the bridge only overlays
   `_bridge_*` fields onto log-built snapshots
   (`gre_bridge.enrich_snapshot_from_pending_response`, gre_bridge.py:1563).
   Legal actions *including autotap solutions* parse from the log
   (gamestate.py:3451-3506, 3632-3669). So "log-first" is a recognition of
   reality, not a rewrite.
2. **Every decision consumer already had a log fallback** via
   `decision_arbiter.arbitrate` — degradation was engineered in from the start.
3. **The intelligence ceiling is prompt + model, not data source.** The
   bridge's only informational edge is latency (4 Hz proactive poll vs
   reactive log diff; log flush can lag minutes at match end) and request-type
   labels. For voice coaching, a human is the executor, so ~1 s reactive
   latency is fine (same freshness Untapped's overlay runs on).
4. **Cross-platform parity for free.** Everything the Mac-viable competitor
   apps do (Untapped, 17Lands tools) is log parsing + separate windows. Our
   coach tier becomes platform-uniform the moment it's log-only.
5. **Autopilot's bar is "finishes arbitrary games unattended"** (the
   bathroom-break / rank-grind use case). Only bridge submission meets that bar
   — clicks/vision wedge on interactive requests (modal chains, search,
   selectN, scry piles, X-cost) exactly when nobody is at the keyboard.

### Verified platform facts (re-verifiable, with method)

| Claim | How verified (2026-07-16, dev Mac) |
|---|---|
| Mac Steam MTGA is native IL2CPP, not Mono | `file MTGA.app/Contents/MacOS/MTGA` → universal x86_64+arm64 Mach-O; `il2cpp_data/` + `GameAssembly.dylib` present; **no `Managed/` dir anywhere in the depot** |
| Mac client not hardened-runtime signed | `codesign -dv MTGA.app` → `flags=0x0(none)` — injection mechanically possible; the wall is the missing CLR, not macOS security |
| BepInEx 5 cannot load there; BepInEx 6 BE *does* list `Unity.IL2CPP-macos-x64` | bepinex.dev docs + builds.bepinex.dev. **Correction to an earlier analysis** which claimed no macOS IL2CPP toolchain exists. Still not a product path: pre-release, x64-only (Rosetta), full plugin rewrite against Il2CppInterop |
| Linux support = Windows build under Proton | No Linux Steam depot; repo's own paths go through `compatdata/2141910/pfx/drive_c/...` (watcher.py:177); `WINEDLLOVERRIDES="winhttp=n,b"` checked by platform_integration.py:132 |
| Mac writes `Player.log` at `~/Library/Logs/Wizards Of The Coast/MTGA/Player.log` | Observed on dev Mac. GRE content requires the in-game **Detailed Logs (Plugin Support)** toggle (was disabled → zero GRE/deck lines in the log) |
| Bridge transport is TCP loopback `127.0.0.1:44222`, not a named pipe | gre_bridge.py:178. CLAUDE.md's pipe description was stale; `PIPE_NAME` survives as a legacy constant (line 34). Python side is fully portable |
| Untapped.gg Companion is a pure log tailer, no injection | Inspected its Electron `app.asar` on this machine: watch path `Library/Logs/Wizards Of The Coast/MTGA/Player`; parse tokens `GreToClientEvent`(14), `GameStateMessage`(22), `ZoneType_Library`(27), plus decklist tokens `mainDeck`(512), `CourseDeck`, `EventSetDeckV3`, `Event_GetCoursesV2` |
| Untapped's "library view" is decklist − seen, not hidden info | Their own docs: log "will never contain data that your game does not know about" |
| MTGA+ (Enhancement Suite) = BepInEx 5, Windows + Linux-via-Proton, no Mac | Its README; Linux instructions say install *Windows x64* BepInEx under Proton |
| Active 17Lands draft tool is `unrealities/MTGA_Draft_17Lands` | Original `bstaple1/` archived Aug 2025; fork pushed 2026-07-16, 138★ vs 130★ |
| Full decklist arrives in-match via GRE `ConnectResp.deckMessage.deckCards` | Decompiled protobuf `re-output/GreProtobuf/.../DeckMessage.cs`: repeated uint grpIds + `sideboardCards` + `commanderCards`; already captured at gamestate.py:3209 |
| Core test suite passes on macOS | 418 passed / 0 failed (2026-07-16), excluding 3 website test modules that need `fastapi`. First-ever Mac run |

### Corrections to earlier in-session claims (accuracy log)

1. "No macOS IL2CPP modding toolchain exists" → **false**; BepInEx 6 BE ships
   a macOS x64 IL2CPP build (conclusion unchanged: not viable as product path).
2. "Decklist/course messages are not parsed on any platform" (platform audit)
   → **half-false**; business-log events (`CourseDeck` etc.) are indeed
   unparsed, but the decklist was already captured from GRE `ConnectResp`.
   The real gap was prompt injection gating (fixed, see below).
3. "Timer state is bridge-only" (first dependency sweep) → **false**; the log
   carries `GREMessageType_TimerStateMessage` (gamestate.py:3793). Bridge is
   merely fresher.

### Rejected alternatives (and why)

- **Native-macOS bridge (BepInEx 6 / Il2CppInterop)**: pre-release toolchain,
  x64-under-Rosetta only, full plugin rewrite against per-release AOT interop
  shims. Research project, not a port. Revisit only if Wine mode proves
  unviable.
- **GRE protocol proxy (MITM) for submission**: passive observation gains
  nothing over the log (same data). Submission = forging client messages =
  protocol-level botting — fragile across server changes, squarely
  ban-detectable, unlike the bridge which drives the real client's own request
  objects (`BaseUserRequest.Submit()`).
- **Direct memory access on the Mac client**: mechanically possible (no
  hardened runtime) but calling AOT'd submit functions is the same research
  project as the IL2CPP bridge.
- **Click/vision autopilot as a parity path**: planning already works from log
  state, and a Quartz `CGEvent` backend + VLM targeting (the Claude
  Desktop/Codex model: Accessibility + Screen Recording TCC permissions) could
  click the common flow — but seconds-per-VLM-call vs the rope timer, and it
  wedges on interactive dialogs. Acceptable someday as an *enhancement*, never
  as the unattended-grinding path.

### What landed (commits on master, 2026-07-16)

- `c64996d` — macOS crash fixes: `import keyboard` hard-aborts the interpreter
  on darwin (its backend calls `abort()` pre-except during import without
  root/Accessibility) → never imported on darwin; `WATCHDOG_SCREENSHOT_DIR`
  mkdir now `parents=True` (fresh-machine import crash).
- `47926ea` — library intelligence always-on: every advice path (including
  desktop chat via pipe_adapter) now injects a compact deck-minus-seen library
  summary with per-card draw odds; tutor-in-hand upgrades to the detailed
  mana-value breakdown. Tests: `tests/test_library_summary.py`
  (note: `tests/` is gitignored by policy; new tests are `git add -f`'d).
- CLAUDE.md doctrine rewrite + docs/PLATFORM_PARITY.md — **untracked by
  design**: `.gitignore` excludes `docs/`, `tests/`, `tools/`, `CLAUDE.md` as
  "dev-only (not for public repo)". They live on the shared repo volume only.

### macOS dev environment (recipe — the session venv is ephemeral)

- This Mac has no Python ≥3.10 (system 3.9.6; no brew/pyenv). The repo `.venv`
  is a **Linux** venv (shared volume with the WSL machine) — exec format error.
- Recreate a Mac venv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (→
  `~/.local/bin/uv`), then `uv venv --python 3.12 <dir>` and
  `uv pip install --python <dir>/bin/python -e /Volumes/repos/mtgacoach`.
  Run tests with that interpreter from the repo root.
- Repo-local git identity was set to match history
  (`josharmour <1240306+josharmour@users.noreply.github.com>`).

### Roadmap agreed with Josh

1. **Phase 1 — Mac coach/draft parity (log tier)**: darwin MTGA/log discovery
   (`platform_integration.py:227` is the seam), voice fallbacks
   (`say`/`afplay`), detailed-logs onboarding check, then the **guidance
   overlay** ("pane of glass": render what the coach wants you to see —
   draft-pick highlights, deck-build badges — click-through, no bridge). The
   unrealities fork review (VALUE score + reason strings, dynamic columns,
   Mini Mode, Monte Carlo deck optimizer) is the UX blueprint; its macOS
   paths are directly reusable.
2. **Phase 2 — Mac packaging** (`.app`, Gatekeeper story).
3. **Phase 3 — autopilot on Mac via Wine/CrossOver bottle** (same recipe as
   Linux/Proton; extend `repair_engine` to detect/manage the bottle).

### 2026-07-16 (later) — Phase-1 swarm landed (commits 3aeec25..19e4c04, pushed)

Six parallel agents with disjoint file ownership executed most of Phase 1 in
one pass; see PLATFORM_PARITY.md §4.1 for the per-item status. Decisions made
during integration:

- **GitHub auth from the Mac**: Josh logged into GitHub in Chrome and approved
  adding this Mac's SSH key ("M5 Pro (mtgacoach dev)") via the browser; remote
  switched to SSH. Pushes from this machine now work.
- **Platform tags**: darwin installs are `darwin-steam` / `darwin-epic`
  (native, IL2CPP — bridge impossible) vs `darwin-crossover` (Windows build in
  a bottle — bridge-capable). Consumers should treat `startswith("darwin")`
  as macOS and check for wine/crossover substrings for bridge capability.
- **Window locator lesson**: exact-title/owner match MUST beat substring match
  — during live testing the 17Lands draft tool's own window ("MTGA_Draft_Tool")
  sat in front of the real "MTGA" window and hijacked a naive filter.
- **Voice key separation**: Kokoro voice ids don't map to macOS `say` voices;
  darwin uses a separate opt-in `macos_voice`/`say_voice` settings key.
- **Detailed Logs remediation** (future repair action): detect via the log
  banner; remediate via `defaults write com.wizards.mtga UseVerboseLogs -int 1`
  ONLY while MTGA is closed (cfprefsd caches plists — never edit the file
  directly). Source: `re-output/Core/MDNPlayerPrefs.cs:1942`.
- **Draft guidance is deliberately unwired**: the engine (`draft_guidance.py`)
  ships tested but not integrated; wiring points are server.py:1799/1907
  (evaluate_pack call sites), standalone.py:2241 (voice line), and a per-pair
  stats fetch in draftstats.py (`/api/card_data?colors=XY` — the legacy
  `/card_ratings/data` route silently ignores filters).

### 2026-07-16 (evening) — first Mac run: three product decisions

Josh's first real launch of the desktop app on the Mac exposed three issues,
each now fixed (commits 84d80c5, bc48d00, aae11fe — pushed):

1. **Provisioning must not demand the bridge where it can't exist.**
   `is_fully_provisioned` required BepInEx unconditionally → native-Mac users
   were forced into "install everything." New `bridge_applicable` property:
   native darwin (no MTGA.exe) skips the bridge gate; bottles/Windows keep it.
2. **First run works with NO license key — free 7-day trial.** Client:
   `subscription.ensure_license_key()` (anonymous sha256 machine hash) is
   invoked from the repair license check; website: `POST /api/trial` mints a
   LiteLLM key (`duration: "7d"`, budget = 25% of patron, one trial per
   machine forever, `trials` table in the subscriber DB). Expired trial →
   Patreon messaging, not a key prompt. **Deploy pending**: copy
   website/{app,db,patreon}.py to the NAS build context
   (`/volume1/docker/appdata/mtgacoach/`) and rebuild the `mtgacoach`
   container per CLAUDE.md; `init_db()` auto-creates the trials table; no new
   env vars needed. Until deployed, clients treat the endpoint's 404 as
   "offline" and fall back to manual key entry.
3. **macOS click-through requires NSWindow.ignoresMouseEvents.** Qt's
   `WA_TransparentForMouseEvents` does NOT stop macOS delivering clicks to
   the window — the invisible overlay ate every click over the game area.
   `window_tracking.apply_system_click_through` (pyobjc) is the darwin
   analogue of win32 `WS_EX_TRANSPARENT`, guarded to the cocoa QPA because a
   fake offscreen winId would segfault under pyobjc. Lesson for checkers:
   "Qt click-through works on macOS" is only true *within* Qt, not across
   apps.

Also: GitHub pushes from the Mac now work (SSH key "M5 Pro (mtgacoach dev)"
added with Josh's browser approval). Dev launch artifacts: `/Applications/
MTGA Coach.app` bundle → venv at `~/Library/Application Support/mtgacoach/
venv` (editable install; recreate with uv if broken).

### 2026-07-16 (night) — trial endpoint deployed; infrastructure map corrected

- **The live stack is on plex (10.0.0.100), not the NAS.** Cloudflare routes
  mtgacoach.com and the LiteLLM gateway (port 8444) to plex; the NAS
  `mtgacoach` container is a stale mirror that still answers internally.
  Proven by request-log absence: external probes never appeared in the NAS
  container's logs. CLAUDE.md deploy runbook corrected. Deploy method on
  plex: repo mount → build context (passwordless sudo) → `docker cp` into
  the running container → restart → rebuild image tag for future
  recreations.
- **Trial endpoint verified end-to-end in production**: 422 on malformed
  machine_id; real mint 200 `{key: sk-…, expires_at: +7d, status: created}`;
  repeat call returns `existing` with the same key. One synthetic smoke-test
  trial row exists (machine_id `cafe…0001`, budget-capped, harmless).

### 2026-07-16 (late night) — the evening's real lesson: silent fallback masked a dead LLM

Josh reported chatty "still not working" after restarting; assistant wrongly
blamed a stale process, then stale bytecode. The log had the truth: the
license key was EMPTY all evening — 435 silent 401s — and the
illegal-advice replacement path swapped every "Error getting advice: 401"
for a plausible legal action. **The LLM never spoke once**; the night's
"advice quality" reports were the deterministic fallback's output. Chain of
causes, all self-inflicted: the provisioning fix let the coach start
keyless (correct), the trial endpoint wasn't deployed yet (couldn't mint),
and the fallback masked the outage (the actual bug). Fixes: error-shaped
advice now bypasses the matcher and names the problem (b5dffa6); this Mac
self-provisioned the first real production trial key (verified 200).
**Checker guidance**: when advice reads like bare legal-action strings
("Cast X [OK]"), grep the log for `[PROXY]` errors before theorizing about
prompts — and never accept "stale code" as an explanation without evidence
(process start time, pyc headers) — it was checked here and was FALSE.

### Falsifiable claims worth re-checking over time

1. BepInEx 6 macOS IL2CPP support status (could mature → revisit native
   bridge). Check builds.bepinex.dev.
2. MTGA Mac build stays IL2CPP (a Mono or arm64-modding shift changes
   everything). Re-run the §Verified table's inspection commands after big
   patches.
3. Player.log detailed-log content shape (WotC has changed logging before;
   trackers broke). The eval harness README's "re-run when prompt structure
   changes" rule applies.
4. `ConnectResp.deckMessage` field names across MTGA updates
   (re-decompile `DeckMessage.cs` if deck capture goes quiet).
5. Wheel/ALSA math and 17Lands API routes borrowed from the unrealities fork
   (they noted the old `/card_ratings/data` route silently ignores filters —
   use `/api/card_data`).
