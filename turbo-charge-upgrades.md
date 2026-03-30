# Turbo-Charge Upgrades: mtgacoach Improvement Plan

Based on reverse-engineering MTGA's managed DLLs (Assembly-CSharp.dll, Core.dll,
Wizards.MDN.GreProtobuf.dll, SharedClientCore.dll) via ReVa/Ghidra string analysis,
cross-referenced against the current codebase architecture.

**Date:** 2026-03-26

## 2026-03-28 Architecture Update: Bridge-Authoritative Runtime

The original turbo plan correctly pushed toward richer bridge access, but the current
reverse-engineering work makes the target architecture much clearer:

- **Direct bridge state must become the in-match source of truth.**
  `Player.log` tailing should not remain the primary runtime model for live games.
- **Direct bridge interaction snapshots must become the source of truth for decisions.**
  Pending decisions should come from live `BaseUserRequest` objects, not inferred log diffs.
- **Autopilot should submit through GRE for request families, not just priority actions.**
  The bridge should cover casts, passes, casting-time choices, searches, selections,
  pay-costs branches, and later targets/blockers/grouping.
- **Logs should be demoted to fallback, bootstrapping, and diagnostics.**
  Keep them for match lifecycle events, draft/out-of-game flows, and bug reports,
  but not as the main engine for in-game synchronization.

### Critical RE Findings That Change the Design

- `BaseUserRequest.Type.ToString()` returns **enum-style request names** like
  `ActionsAvailable`, `CastingTimeOptions`, `Search`, and `Mulligan`.
- `request.GetType().Name` returns **CLR class names** like
  `ActionsAvailableRequest`, `CastingTimeOptionRequest`, `SearchRequest`, and `MulliganRequest`.
- The bridge layer must normalize these two identities. Any Python mapping that assumes
  only one naming style will drift or silently misclassify requests.
- `CastingTimeOptionRequest` is **not** a flat action list. It is a parent wrapper over
  `ChildRequests`, and each child has its own submit method.
- `PayCostsRequest` is also a parent wrapper over child requests such as
  `SelectNRequest`, `ActionsAvailableRequest`, `EffectCostRequest`, and `AutoTapActionsRequest`.
- The correct bridge abstraction is therefore a **typed interaction snapshot**, not just
  `actions[]`.

### 2026-03-28 Runtime Finding: Intermission Is Not an Actionable GRE Decision

- Live bridge logs confirmed that autopilot is already using **direct GRE injection**
  for real gameplay actions (`Submitting action [...]` entries in `BepInEx/LogOutput.log`).
- A later "stuck" bug report (`bug_20260328_211436.json`) was **not** a gameplay
  execution failure. It occurred after a win, during `IntermissionRequest`.
- The plugin surfaced `IntermissionRequest` through the pending-request path, which
  caused Python to treat it like a real `decision_required` trigger.
- That led autopilot down the wrong branch:
  `IntermissionRequest -> plan click_button -> GRE submit_pass rejected -> mouse fallback with empty button name`.
- Design implication: **not every `BaseUserRequest` should become an actionable
  autopilot decision**. We need an explicit non-actionable/transition class for bridge
  requests, starting with `Intermission*`.
- Immediate mitigation implemented in runtime code:
  - bridge polling ignores `Intermission`, `IntermissionReq`, and `IntermissionRequest`
  - autopilot no-ops if an intermission request still leaks through

### Updated End State

The intended runtime architecture should be:

1. Plugin reads live MTGA state from `GameManager.CurrentGameState`.
2. Plugin serializes live interaction state from `BaseUserRequest` trees.
3. Python builds snapshots from bridge state first.
4. Python uses logs only when the bridge is unavailable or for metadata the bridge
   does not yet expose.
5. Autopilot executes through direct GRE submission whenever the request family has
   a stable submit path.

### 2026-03-28 Scope Expansion: From Power-User Tool to Installed Windows Product

The original turbo plan assumed "better internals on top of a repo-style install."
That is no longer enough. The new target is a **GRE-first desktop product** with
an installer, launcher, repair path, and versioned bridge contract.

This expands the plan in four concrete ways:

1. **Runtime layout must support `Program Files` installs.**
   The app payload can be read-only, but mutable runtime state must live under
   `%LOCALAPPDATA%\\mtgacoach` so setup/repair can create the Python environment,
   `.env`, and future cached assets without requiring write access to the install dir.

2. **The launcher becomes part of the platform, not just a convenience wrapper.**
   It must detect:
   - runtime venv readiness
   - MTGA install path
   - BepInEx presence
   - plugin DLL presence/version
   - bridge readiness
   and provide one-click repair flows for the pieces it can remediate.

3. **The installer must bootstrap the GRE bridge stack, not just copy files.**
   A valid install means:
   - app files copied
   - desktop/start menu entries created
   - uninstall entry present
   - BepInEx bundle available for repair/install
   - `MtgaCoachBridge.dll` deployable into MTGA

4. **Prompting and autopilot must consume more raw GRE truth.**
   Now that bounded raw GRE history exists, prompt design should stop acting like
   context is scarce. We still need bounds, but the online GPT-5.4 path should use:
   - richer live request payloads
   - richer candidate metadata
   - richer recent raw GRE context
   - less lossy compaction of legal actions and decision frames

### New Concrete Workstreams

#### A. Bridge-Authoritative Interaction Snapshots

**Current repo status:** In progress.

What exists now:
- `Plugin.cs` returns additive bridge metadata on `get_pending_actions`, including
  `request_type`, `request_class`, `request_payload`, and generic `decision_context`.
- `CastingTimeOptionRequest` is flattened into bridge-facing options with direct submit
  support for several child request shapes.
- Python now preserves `_bridge_request_type`, `_bridge_request_class`,
  `_bridge_request_payload`, and bridge decision metadata for prompting/debugging.

What is still missing:
- a dedicated `get_interaction_snapshot` contract
- first-class typed serialization for all priority request families
- stable direct submit coverage for search / select / target / pay-cost trees

Expand plugin serialization so `get_pending_actions` returns additive, typed
payloads for request families beyond `ActionsAvailableRequest`.

Priority order:
- `SelectTargetsReq`
- `SearchReq`
- `DistributionReq`
- `NumericInputReq`
- `SelectNRequest` / grouping / ordering variants

Design rule:
- Python should not have to infer candidate sets when GRE already knows them.
- If a request family cannot yet be submitted through the bridge, it should still
  be serialized authoritatively for prompting/debugging.

#### B. GRE-Rich Prompting

**Current repo status:** In progress.

What exists now:
- `gamestate.py` retains bounded `raw_gre_events`.
- `coach.py` and `action_planner.py` now include bridge request identity, bridge
  request payloads, and recent raw GRE history in prompt construction.

What is still missing:
- a clear split between the richer GPT-5.4 path and lighter local-model path
- broader use of bridge-native state outside decision frames
- final prompt-budget policy for how much raw GRE context to include per backend

The prompt pipeline should branch by capability:

- **Online / GPT-5.4 path**:
  use richer `LegalGRE`, richer `decision_context`, and bounded raw GRE history.
- **Local / smaller-model path**:
  keep the existing compact path or a lighter bounded variant.

The target is not "dump the firehose." The target is:
- richer current request
- richer recent causal context
- less heuristic reconstruction

#### C. Windows Runtime / Launcher Contract

**Current repo status:** In progress.

What exists now:
- `launch.bat`, `launch.vbs`, `install.bat`, `launcher.py`, and `setup_wizard.py`
  understand `MTGACOACH_RUNTIME_ROOT` and default to `%LOCALAPPDATA%\\mtgacoach`.
- The setup/runtime split is implemented for repo-launched Windows flows.

What is still missing:
- validation of the installed `Program Files` layout end to end
- migration away from repo-root execution as the default path
- a packaged runtime that no longer depends on batch files during normal use

The launcher/setup/install path should converge on this contract:

- App root:
  - repo root during development
  - `Program Files\\mtgacoach` in installed mode
- Runtime root:
  - `%LOCALAPPDATA%\\mtgacoach`
- Batch entrypoints and GUI:
  - always set/observe the runtime root
  - always launch with `src` import support even without editable-install assumptions
- Setup wizard:
  - create venv under runtime root
  - keep mutable files out of the read-only install dir
  - use non-editable installs for packaged app mode

#### D. Repairable BepInEx Dependency

**Current repo status:** In progress.

What exists now:
- `windows_integration.py` detects MTGA, BepInEx, plugin deployment, and can install
  or repair the bridge stack when the required assets are present.
- `launcher_gui.py` exposes those checks and repair actions in the GUI.

What is still missing:
- guaranteed bundled BepInEx payload in release builds
- plugin/app compatibility enforcement
- installed-build repair validation and version mismatch handling

Treat BepInEx as a first-class dependency with repair semantics:

- detect MTGA path from registry/common locations/user override
- verify BepInEx core
- verify bridge plugin deployment
- ship a bundled BepInEx payload when licensing/distribution allows
- surface plugin build/install mismatch clearly in the launcher

This is now core product scope, not optional polish.

#### E. Native Windows Shell (WinUI 3)

**Current repo status:** Planned, but environment-blocked on the current machine.

What exists now:
- The current Windows surface is still the Python/Tk launcher (`launcher_gui.py`)
  plus the internal TUI restart wrapper (`launcher.py`).
- An Inno Setup installer scaffold already exists in `installer/mtgacoach.iss`,
  including Start Menu / desktop shortcuts that target `launch.vbs`.
- The launcher/runtime contract is already oriented around a single entry surface,
  `%LOCALAPPDATA%\\mtgacoach` mutable state, and `Program Files` app payloads.

What is still missing:
- WinUI toolchain readiness on the current Windows environment
- a scaffolded native launcher project
- native implementations of launch / repair / status surfaces
- integration between the native shell and the existing Python runtime contract

Current design direction:
- the eventual native shell should **replace** the Tk launcher, not sit beside it
- the likely packaging model is **unpackaged WinUI**, because install/update is already
  being handled by an external installer rather than MSIX
- the native shell should own:
  - launch coach / autopilot
  - runtime provisioning status
  - MTGA / BepInEx / bridge repair flows
  - logs / diagnostics entrypoints

2026-03-28 readiness note:
- Using the `winui-app` skill flow, `dotnet new list winui` currently returns
  **no installed WinUI templates** on the Windows machine. Native shell work should
  therefore stay in the plan, but scaffolding is blocked until the Windows App SDK /
  WinUI project template toolchain is installed.
- Do **not** add Windows App SDK packages to `MtgaCoachBridge.csproj`.
  The BepInEx bridge plugin remains a separate .NET Framework game plugin;
  the native launcher must live in its own desktop project.

### 2026-03-28 Status Checkpoint

- **A. Bridge-authoritative interaction snapshots:** partially implemented.
  `Plugin.cs` already returns request identity and additive bridge payloads on
  `get_pending_actions`, and Python persists `_bridge_request_type`,
  `_bridge_request_class`, and `_bridge_request_payload` for prompting and
  debugging.
- **A.1 2026-03-29 update:** the first authoritative snapshot slice is now in
  `src/arenamcp/server.py:get_game_state()`. The server overlays bridge live
  `turn`, `players`, visible zones, seat IDs, timer state, and nested `zones`
  over the published snapshot while preserving log/poller authority for
  `pending_decision`, `decision_context`, `legal_actions`, `damage_taken`,
  `deck_cards`, `action_history`, and similar metadata. This removes a major
  source of board-state drift without yet hard-cutting away the log parser.
- **B. GRE-rich prompting:** partially implemented.
  `src/arenamcp/gamestate.py` retains bounded `raw_gre_events`, and both
  `src/arenamcp/coach.py` and `src/arenamcp/action_planner.py` already inject
  bridge request identity, bridge payloads, and recent GRE context into prompts.
  Missing work is now mostly policy: backend-specific prompt budgets, less lossy
  online prompting, and broader consumption of bridge-native state outside the
  immediate decision frame.
- **C. Windows runtime / launcher contract:** in progress and materially improved.
  The repo now has `launch.vbs`, `launch.bat`, `launcher_gui.py`,
  `windows_integration.py`, and `installer/mtgacoach.iss` aligned around a single
  launcher surface and `%LOCALAPPDATA%\\mtgacoach` runtime state. As of
  2026-03-28, the internal restart wrapper is quiet by default so TUI restarts no
  longer flash the launcher banner, and the setup wizard creates a real
  `mtgacoach Launcher.lnk` pointing at `launch.vbs` instead of dropping another
  `.bat` wrapper on the desktop.
- **D. Repairable BepInEx dependency:** in progress.
  `windows_integration.py` can detect MTGA, BepInEx, plugin presence, and bundled
  payloads, while `launcher_gui.py` exposes bridge repair flows. Still missing are
  guaranteed bundled BepInEx assets in release builds, explicit bridge/app
  compatibility enforcement, and installed-build validation for repair paths.
- **E. Native Windows shell:** not started in code, but now clarified.
  The direction is a single installed launcher that eventually replaces the TUI
  with a proper native GUI. The blocker is environmental, not conceptual:
  the current Windows machine does not yet have the WinUI template/toolchain
  required to scaffold that shell.

### Phase 1 Status: COMPLETE (2026-03-26)

All Phase 1 items (1.1-1.4) and quick wins (6.1-6.3) implemented in branch
`worktree-phase1-parse-missing-gre-data`. Changes to `gamestate.py` and `server.py`:

- **25+ new annotation handlers** — TargetSpec, Modified*, Designations, PhasedIn/Out,
  ClassLevel, DungeonStatus, SuspendLike, LinkedDamage, ColorProduction, AddAbility,
  CopiedObject, BoonInfo, CrewedThisTurn, SaddledThisTurn, DamagedThisTurn, Shuffle,
  Vote, DieRoll, PredictedDirectDamage, LayeredEffect, SupplementalText, NewTurnStarted
- **16 new fields on GameObject** — modified_power/toughness/cost/colors/types/name,
  granted_abilities, removed_abilities, damaged/crewed/saddled_this_turn, is_phased_out,
  class_level, copied_from_grp_id, targeting, color_production
- **5 new game-level state fields** — designations, dungeon_status, timer_state,
  action_history, sideboard_cards
- **AutoTap mana solver data** parsed per-action with castability flags and tap sequences
- **Ability metadata** extracted (abilityGrpId, sourceId, alternativeGrpId, mana cost string)
- **Timer state** parsed from GREMessageType_TimerStateMessage (chess clock, rope)
- **Sideboard tracking** from SubmitDeckReq in BO3
- **Action history buffer** (rolling 50-entry buffer from UserActionTaken annotations)
- **Debug else clause** logs all unhandled annotation types at DEBUG level
- **Known-but-skippable** annotation types explicitly listed (no false positives in debug log)

### Phase 2 Status: COMPLETE (2026-03-26)

BepInEx plugin expanded from 3 commands to 6, with dramatically richer data.
Plugin version bumped to 0.2.0. Changes to `Plugin.cs` and `gre_bridge.py`:

- **`get_game_state` command** — Serializes full MtgGameState from GameManager.CurrentGameState:
  zones (battlefield/hand/stack/graveyard/exile/command/library) with full card instances,
  players (life/mana/status/mulligan/dungeon/designations), turn info (phase/step/active/deciding),
  combat info (attack/block mappings), timers, and pending interaction type
- **`get_timer_state` command** — Per-player chess clock data: time remaining, timer type,
  running state, warning threshold, behavior. Both game-level and player-level timers
- **`get_match_info` command** — Match metadata: game state ID, stage, GameInfo fields,
  local/opponent seat IDs and life totals
- **Enhanced `SerializeAction`** — Now includes: AssumeCanBePaidFor (ground-truth castability),
  FacetId, UniqueAbilityId, full AutoTapSolution with tap sequence (via reflection),
  Targets, Highlight, ShouldStop, IsBatchable
- **Enhanced `SerializeCard`** — Full MtgCardInstance serialization: power/toughness, loyalty,
  defense, combat state, summoning sickness, phasing, damage, class level, copy info,
  card types, subtypes, colors, counters, color production, targets, attachments,
  visibility, face-down state, crew/saddle
- **Cached GameManager lookup** — 5-second TTL cache avoids repeated FindObjectOfType
- **Python client** (`gre_bridge.py`) — Added get_game_state(), get_timer_state(),
  get_match_info() methods

**2026-03-28 note:** These Phase 2 items are now best understood as **foundation complete**,
not final architecture complete. The remaining work is to make direct bridge state and
typed bridge interactions the primary live pipeline, replacing log-first state assembly
during matches.

### Phase 3 Status: COMPLETE (2026-03-26)

Replay recording and match history system implemented. Plugin version 0.3.0.

- **Plugin commands**: `enable_replay`, `disable_replay`, `get_replay_status`, `list_replays`
  — toggles MTGA's built-in TimedReplayRecorder via PlayerPrefs, lists .rply files
- **Python bridge** (`gre_bridge.py`) — enable_replay(), disable_replay(),
  get_replay_status(), list_replays() methods
- **Match history module** (`match_history.py`) — JSON-backed persistent history:
  - `MatchRecord` dataclass: match_id, result, opponent name/rank/colors, turns,
    life totals, deck colors, replay path
  - `MatchHistory` class: add records, query win rates, matchup stats, session stats
  - `parse_replay_cosmetics()` — extract player names/ranks from .rply header
  - `parse_replay_result()` — scan replay for win/loss annotations
  - `record_from_game_end()` — create record from game end snapshot + opponent cards
  - Deduplication by match_id, 500-record cap, stored at ~/.arenamcp/match_history/

---

## Phase 0: Installation, Bootstrap, and Productization

The original turbo plan assumed a power-user workflow: unzip, install Python manually,
install BepInEx manually, copy the plugin DLL manually, and run the TUI from scripts.

That is no longer the right product model.

Now that direct GRE tracking and direct GRE injection are part of the core runtime,
**BepInEx is effectively a required dependency** for the full product. That means the
installation story has to be first-class.

### 0.1 Build a Real Windows Installer

**Current repo status:** Prototype in progress.

What exists now:
- `installer/mtgacoach.iss` exists as an Inno Setup prototype.
- Docs, launch scripts, and runtime-root logic are now aligned with a future
  installed layout.

What is still missing:
- a finished packaged installer artifact
- a native installed launcher binary / app entrypoint
- validated install / uninstall / upgrade behavior on Windows

**Goal:** install mtgacoach like a normal Windows desktop app.

Expected outcomes:
- installed under `Program Files`
- visible in **Add/Remove Programs**
- installs/uninstalls cleanly
- creates Start Menu and Desktop shortcuts
- can register logs/config/data locations cleanly
- launches reliably without depending on fragile `.bat` files or ad hoc shortcuts

**Installer responsibilities:**
- install the app binaries/runtime
- detect MTGA install path
- detect whether BepInEx is already installed
- install or upgrade BepInEx when missing/outdated
- deploy `MtgaCoachBridge.dll` into `MTGA\\BepInEx\\plugins`
- write uninstall metadata
- create shortcuts for:
  - Coach
  - Autopilot
  - Voice Advisor
  - optionally a diagnostics / repair tool

**Likely implementation options:**
- WiX Burn bootstrapper + MSI
- Inno Setup
- NSIS

The exact packaging stack matters less than the outcome: a signed, versioned Windows
installer with repair/uninstall support.

### 0.2 Automatic BepInEx Detection and Installation

**Current repo status:** In progress.

What exists now:
- MTGA detection via settings, environment, registry/uninstall keys, and common paths.
- BepInEx/plugin verification in `windows_integration.py`.
- repair/install helpers for BepInEx and `MtgaCoachBridge.dll`.

What is still missing:
- explicit version compatibility checks
- guaranteed bundled BepInEx payload in release artifacts
- polished upgrade/repair behavior across MTGA updates

**New requirement:** because bridge-first runtime depends on the plugin, BepInEx can no
longer be a manual README step.

**Installer/runtime should do:**
- locate MTGA install path via:
  - common install paths
  - registry / uninstall keys
  - existing shortcuts if needed
  - user browse fallback
- detect BepInEx presence and version
- verify key files:
  - `BepInEx\\core\\BepInEx.dll`
  - `BepInEx\\plugins\\MtgaCoachBridge.dll`
  - `BepInEx\\LogOutput.log`
- install or repair BepInEx automatically when missing
- install/update the plugin DLL automatically
- verify plugin version compatibility with Python app version

**Repair flow should support:**
- "MTGA moved"
- "BepInEx missing or corrupted"
- "Plugin DLL missing"
- "Bridge version mismatch"

### 0.3 Replace Script-Based Launch With a Proper Launcher

**Current repo status:** In progress.

What exists now:
- `launcher_gui.py` provides a Windows GUI launcher / repair surface.
- The launcher can start coach or autopilot and expose setup/repair actions.
- `launch.bat` is now the canonical Windows entrypoint for repo/manual installs.
- `launch.vbs` exists as the installed GUI-friendly shim so Windows shortcuts can launch
  the single launcher surface without a visible `cmd.exe` window.

What is still missing:
- replacing repo/batch driven launch as the primary user path
- a GUI-first app runtime instead of the current launcher -> `launcher.py` -> Textual TUI chain
- packaged entrypoints that do not assume a repo checkout

**2026-03-28 launcher findings:**
- The previous launcher surface had drifted into multiple user-facing wrappers:
  - `run.bat` (removed)
  - `coach.bat` (removed)
  - `launch.bat`
  - `autopilot.bat` (removed)
  - `gui.bat` (removed)
  - `install.bat`
- Under the new architecture, this wrapper sprawl is now considered **downstream
  obsolete complexity**.
- The obsolete wrapper scripts should be removed from the repo and from the installed
  product surface rather than maintained as compatibility layers.
- The intended Windows product path is:
  - one canonical launcher entrypoint
  - one Start Menu item
  - one desktop shortcut
  - one `Program Files` install root
  - launcher-owned navigation to coach / autopilot / repair flows
- Internal launch shims are acceptable only when they are implementation details of the
  single launcher experience, not separate user-facing entrypoints.

The current launch model depends on shell scripts, terminal setup, and user-managed
shortcuts. A proper launcher should:

- start the GUI app directly
- start background services/components in the right order
- verify bridge prerequisites before launch
- surface actionable status:
  - MTGA found / not found
  - BepInEx installed / missing
  - plugin loaded / not loaded
  - bridge connected / disconnected
- offer one-click repair actions where possible

### 0.4 Installation/Packaging Compatibility Matrix

**Current repo status:** Pending.

The target scenarios are documented, but upgrade/migration/uninstall behavior has not
been fully implemented or validated yet.

The productized installer must explicitly handle:
- fresh install on a machine with MTGA but no BepInEx
- upgrade from current script/TUI-based install
- MTGA update that replaces/modifies files
- per-user data migration from `~/.arenamcp`
- uninstall without removing user data unless requested

### 0.5 Versioning and Compatibility Contract

**Current repo status:** Pending.

There is not yet a hard compatibility check across Python app version, bridge protocol,
plugin DLL version, BepInEx version, and MTGA client build.

Once installer-managed, we need a compatibility contract across:
- Python app version
- bridge protocol version
- `MtgaCoachBridge.dll` version
- minimum supported BepInEx version
- minimum supported MTGA client build

The installer and launcher should detect incompatible combinations and either repair
them or block launch with a clear message.

---

## Phase 1: Parse Missing GRE Data (Low Effort, High Impact)

These are data fields the game engine already sends in GRE messages that we're either
ignoring or only partially parsing. No BepInEx changes needed — just expand gamestate.py.

### 1.1 Add Missing Annotation Types

**Current state:** gamestate.py handles 19 of 70+ annotation types (lines 1451-1641).
Unhandled types are silently dropped with no else clause.

**Add these annotation handlers:**

| Annotation | What We Get | Where to Use |
|---|---|---|
| `TargetSpec` | Spell/ability targets (instance IDs of targeted objects) | Coach: "Opponent targeted your [X] with removal" — currently listed as a gap in server.py |
| `PredictedDirectDamage` | GRE's own damage prediction for pending combat | Replace manual combat math in coach.py (lines 928-1006) |
| `LayeredEffect` | Active continuous effects (anthems, debuffs, static abilities) | Coach: know actual P/T vs base P/T; detect anthem sources |
| `ModifiedPower` | Real modified power value after all effects | Expose in get_game_state — currently only base stats shown |
| `ModifiedCost` | Actual mana cost after reductions/increases | "This spell costs 2 less because of Goblin Electromancer" |
| `ModifiedColor` | Modified color identity (Painter's Servant, etc.) | Correct color tracking for protection/devotion |
| `ModifiedType` | Modified type line (animated lands become creatures) | Track when a land becomes a creature for combat |
| `ModifiedName` | Modified name (Clone effects, Spy Kit) | Correct card identification |
| `DamagedThisTurn` | Which permanents took damage this turn | Coach: enrage triggers, damage-matters synergies |
| `CrewedThisTurn` | Which vehicles were crewed | Coach: "Vehicle already crewed, don't waste another crew" |
| `SaddledThisTurn` | Which mounts were saddled | Same as crew |
| `PhasedIn` / `PhasedOut` | Phasing state of permanents | Track phased-out cards (currently invisible to coach) |
| `ClassLevel` | Current level of Class enchantments | Coach: "Level up your class for the next ability" |
| `DungeonStatus` | Current room in dungeon | Coach: dungeon progress tracking |
| `SuspendLike` | Suspend/foretell exile with time counters | Coach: "Suspend card comes off in 2 turns" |
| `LinkedDamage` / `DamageSource` | Damage attribution (which source dealt what) | Post-match: "Lost 8 life to Sheoldred triggers over 4 turns" |
| `SupplementalText` | Extra context text from GRE | Pass to LLM as additional context |
| `ColorProduction` | Mana colors a permanent can produce | Perfect mana analysis without oracle text parsing |
| `AddAbility` / `RemoveAbility` | Granted/lost abilities (flying, hexproof, etc.) | Coach: track temporary keyword grants |
| `Designation` / `GainDesignation` / `LoseDesignation` | Monarch, initiative, city's blessing, day/night | Coach: "You have the monarch — protect it" |
| `CopiedObject` | Copy relationships | Coach: know what a Clone copied |
| `BoonInfo` | Boon/emblem effects | Track active emblems and boons |
| `Vote` | Voting results (Council's Dilemma) | Coach: voting strategy |
| `Shuffle` | Library shuffled events | Track tutors, fetchlands |

**Implementation:** Add elif branches in the annotation handler at gamestate.py:1451-1641.
Store new state on GameObject (modified_power, modified_types, etc.) and in game-level
dicts (designations, dungeon_status, etc.). Expose via get_game_state() in server.py.

**Files to change:**
- `src/arenamcp/gamestate.py` — annotation handler (lines 1451-1641), GameObject class (line 59), snapshot builder
- `src/arenamcp/server.py` — get_game_state() to include new fields

### 1.2 Parse AutoTap/Mana Solver Data

**Current state:** gamestate.py line 2358 checks `autoTapSolutions` as a boolean only:
```python
has_autotap = bool(req.get("autoTapActionsReq", {}).get("autoTapSolutions"))
```
The actual mana payment solutions, tap actions, and castability data are discarded.

**What to extract:**
- `AutoTapSolution` — which lands to tap for each legal play (the game already solved it)
- `ManaPaymentOptions` — alternative payment methods
- `AssumeCanBePaidFor` — ground-truth castability flag per action
- `ManaCost` array — structured cost breakdown (already partially parsed at lines 2330-2356)
- `AutoTapActions` — the specific tap sequence the game would use

**Impact:** Eliminates the `[OK]`/`[NEED:3]` mana heuristic in the system prompt. Instead
of parsing oracle text to determine castability, we use the game engine's own mana solver.
The coach gets perfect "can I cast this?" answers.

**Files to change:**
- `src/arenamcp/gamestate.py` — expand action parsing (lines 2231-2324) to extract full AutoTap data
- `src/arenamcp/server.py` — include castability and mana solution in legal_actions output
- `src/arenamcp/coach.py` — simplify/remove manual mana calculation from system prompt

### 1.3 Extract Ability Metadata from Actions

**Current state:** Legal action parsing (gamestate.py:2231-2324) only extracts actionType,
grpId, and card name. All other fields are ignored.

**What to extract:**
- `AbilityPaymentType` — distinguishes `Loyalty` (planeswalker), `TapSymbol`, `None` (spells)
- `AbilityCategory` — `Activated`, `Triggered`, `Static`, `Spell`, `AlternativeCost`
- `AbilitySubCategory` — `Cycling`, `Crew`, `Explore`, `Surveil`, `Scry`, `Investigate`, etc.
- `SourceId` — what permanent is activating the ability
- `AlternativeGrpId` — adventure/MDFC/mutate alternative

**Impact:** The coach can say "activate Jace's +1 loyalty ability" instead of "activate
ability on Jace." Draft helper can identify cycling cards structurally (not by oracle text
regex).

**Files to change:**
- `src/arenamcp/gamestate.py` — expand action parser to extract these fields
- `src/arenamcp/server.py` — include in legal_actions_raw and formatted actions

### 1.4 Parse Timer State Messages

**Current state:** gamestate.py line 2472 silently ignores `GREMessageType_TimerStateMessage`.

**What to extract:**
- Chess clock time remaining for both players
- Timeout extensions remaining (BO3)
- Rope state (how close to timing out)

**Impact:** Coach can warn "You have 30 seconds left — play quickly" or "Opponent is roping,
they may be considering a big play." Autopilot can adjust execution speed.

**Files to change:**
- `src/arenamcp/gamestate.py` — add timer state tracking
- `src/arenamcp/server.py` — expose timer info in game state

---

## Phase 2: Expand BepInEx Plugin Into the Primary Runtime (Medium Effort, Transformative)

The bridge now exposes the first wave of commands, but the remaining work is no longer
"add a few more plugin helpers." The real architectural goal is to make the bridge the
primary live runtime surface for both state and decisions.

This phase should now be read as:

- direct live state from MTGA objects
- typed live interaction snapshots from `BaseUserRequest`
- direct GRE submission across request families
- logs relegated to fallback and diagnostic roles

### 2.1 Make `get_game_state` the Authoritative In-Match State Source

**Current repo status:** Foundation complete, first overlay slice implemented, full
switchover still in progress.

What exists now:
- the plugin-side `get_game_state` command exists and can serialize live MTGA state
- bridge-first decision detection/enrichment is active in `standalone.py`
- `server.get_game_state()` now overlays bridge-authoritative live board state for:
  - `turn`
  - `players`
  - top-level visible zones
  - nested `zones`
  - `local_seat_id` / `opponent_seat_id`
  - `timer_state`

What is still missing:
- bridge state does not yet fully replace log-derived decision surfaces
- Python has not yet switched to a bridge-only in-match authority model
- logs are still required for lifecycle, recovery, and several normalized metadata fields

**2026-03-28 implementation findings:**
- The safest first integration slice is a **bridge overlay** in `server.get_game_state()`,
  not an immediate hard cutover.
- The server can opportunistically call bridge `get_game_state()` on each snapshot read,
  because bridge `connect()` is already non-blocking and cooldown-gated in `gre_bridge.py`.
- The first overlay should prefer bridge live state for:
  - `turn`
  - `players`
  - zones / visible objects
  - timer data
- The first overlay should keep log-derived fields for now when the bridge does not yet
  expose equivalent normalized data:
  - `pending_decision`
  - `decision_context`
  - `legal_actions`
  - `legal_actions_raw`
  - `deck_cards`
  - `action_history`
  - `sideboard_cards`
  - `damage_taken`

**Schema adapter gaps identified:**
- Plugin `get_game_state()` returns `zones` as structured zone objects with `cards`,
  `zone_id`, and `total_count`; the server snapshot expects flattened lists/counts such as:
  - `battlefield`
  - `my_hand`
  - `opponent_hand_count`
  - `graveyard`
  - `stack`
  - `exile`
  - `command`
  - `library_count`
- Plugin turn data uses `deciding_player`; the normalized server snapshot currently uses
  `priority_player`. The bridge adapter should map `deciding_player -> priority_player`.
- Plugin cards use `owner_id` / `controller_id`; the normalized snapshot expects
  `owner_seat_id` / `controller_seat_id`.
- Plugin cards expose `object_type`; the normalized snapshot tends to use `object_kind`.
- Plugin bridge state does not yet replace decision/action surfaces by itself; current
  `BridgeDecisionPoller` enrichment should remain the source for bridge-aware pending
  decisions during the first overlay step.

**Contract hardening note:**
- `server.get_game_state()` now returns explicit `local_seat_id` and
  `opponent_seat_id`, and also republishes the underscore-prefixed bridge fields
  used by downstream prompting (`_bridge_request_type`, `_bridge_request_class`,
  `_bridge_request_payload`, etc.).

**Current state:** the runtime now has a hybrid model:
- bridge owns live public board state while connected
- log parsing still owns normalized decisions, recovery metadata, and several
  historical/derived fields

This is materially better than the previous model because the visible board no longer
depends primarily on log reconstruction during normal connected play.

**Updated goal:** Treat bridge state as authoritative during live games. Build the
Python snapshot from the plugin first, then merge logs only for missing metadata or
events the bridge does not yet expose.

**2026-03-29 implementation update:**
- added a schema adapter in `src/arenamcp/server.py` to map bridge:
  - `deciding_player -> priority_player`
  - `owner_id -> owner_seat_id`
  - `controller_id -> controller_seat_id`
  - `object_type -> object_kind`
- added timer normalization from bridge `player_timers` into the existing
  `timer_state` schema
- added regression coverage in `tests/test_server_bridge_overlay.py`
- verified focused test suite:
  - `tests/test_server_bridge_overlay.py`
  - `tests/test_bridge_prompt_enrichment.py`
  - `tests/test_gamestate_gre_normalization.py`

**Primary live source:**
- `GameManager.CurrentGameState`
- `GameManager.WorkflowController`
- `GameManager.MatchManager`

This should be used to derive:
- zones / objects / players / turn / combat / timers
- local pending request metadata
- request-linked state like deciding player or source object

**Implementation approach:**
```csharp
case "get_game_state":
    var gameManager = FindObjectOfType<GameManager>();
    var state = gameManager?.CurrentGameState;
    // Serialize zones, objects, players, turn info
    // Return as JSON
    break;
```

**Why this matters:**
- **Eliminates log tailing as the primary in-game source**
- **No more stale data** — get state at the exact moment you ask
- **No more missing fields** — full state, not partial diffs
- **No more backfill scanning** — instant state on coach startup mid-game
- **Eliminates entire bug classes**: brace-depth parser errors, truncated JSON, race conditions between log write and our read
- **Performance**: one JSON response vs. continuously tailing a 40MB+ file

**Risk:** internal MTGA APIs may change between client updates. Use reflection as a
safety net where direct references are brittle.

**Files to change:**
- `bepinex-plugin/MtgaCoachBridge/Plugin.cs` — keep expanding `get_game_state` fidelity
- `src/arenamcp/gre_bridge.py` — add `get_game_state()` method
- `src/arenamcp/gamestate.py` — bridge-first snapshot update path
- `src/arenamcp/standalone.py` — prefer bridge state when available, fall back to log

### 2.2 Replace Action-Only Polling With a Typed `get_interaction_snapshot`

**Current repo status:** In progress.

What exists now:
- `get_pending_actions` now carries request identity in both enum-style and CLR-class form
- additive `request_payload` and `decision_context` are returned
- `CastingTimeOptionRequest` has dedicated flattening logic

What is still missing:
- a dedicated interaction snapshot API/contract
- fully typed coverage across all request families
- removal of the remaining action-centric assumptions in Python

**2026-03-28 implementation findings:**
- `get_pending_actions()` is already carrying enough additive data to act as the
  transitional transport for typed interaction work:
  - `request_type`
  - `request_class`
  - `request_payload`
  - `decision_context`
- `CastingTimeOptionRequest` is materially ahead of the other request families:
  it already has plugin-side flattening plus direct submit support for several child
  requests.
- Because the interaction path is ahead of the state path, bridge-first runtime work can
  proceed in parallel:
  1. normalize live state from bridge `get_game_state()`
  2. continue expanding typed request-family snapshots on `get_pending_actions()`
  3. add direct submit paths for the remaining request families

**Current state:** `get_pending_actions` is still conceptually action-centric. That works
for `ActionsAvailableRequest`, but it is the wrong abstraction for request trees like
`CastingTimeOptionRequest` and `PayCostsRequest`.

**Updated goal:** Expose one normalized interaction snapshot for all request families.

**Snapshot shape should include:**
- normalized request identity:
  - enum-style request type
  - CLR request class
- source metadata:
  - `sourceId`
  - prompt / label / can-cancel / allow-undo
- request-specific decision context:
  - option counts
  - selection IDs / grp IDs
  - constraints (min/max, repeatable, etc.)
- child request data when the request is a wrapper
- direct bridge-submit indices where possible

**Proposed coverage for request families:**
- `ActionsAvailableRequest` → legal actions (existing)
- `CastingTimeOptionRequest` → flattened child choices, one bridge-facing option per
  submit path when possible
- `SearchRequest` → searchable grp IDs / zones / selection limits
- `SelectNRequest` → IDs, list type, ID type, min/max, context
- `PayCostsRequest` → child request decomposition (`SelectN`, `ActionsAvailable`,
  `EffectCost`, `AutoTapActions`)
- `SelectTargetsRequest` → valid target groups / target indices / ability group
- `MulliganRequest` → mulligan counts, free mulligan info, starting hand size
- `GroupRequest` / `SelectFromGroupsRequest` / `SelectNGroupRequest` → grouping constraints
- `DistributionReq` / `NumericInputReq` / `SelectCountersReq` → min/max/targets/values

**Impact:** The coach and autopilot get structured decision context instead of inferring
it from log messages (which are often incomplete or arrive out of order).

**Files to change:**
- `bepinex-plugin/MtgaCoachBridge/Plugin.cs` — normalize request identity and serialize
  typed interaction snapshots
- `src/arenamcp/gre_bridge.py` — parse and expose normalized request snapshots
- `src/arenamcp/gamestate.py` — enrich `decision_context` from bridge snapshots
- `src/arenamcp/autopilot.py` — use typed request context for better action planning

### 2.3 Add `get_autotap_solutions` Command

**Current repo status:** Effectively complete via action serialization.

The explicit standalone command was not added, but the actionable part of this work is
already present: bridge action payloads carry AutoTap/mana solution detail for prompting
and planning.

**Current state:** The plugin serializes `action.AutoTapSolution != null` as a boolean
(Plugin.cs line 458). The actual tap sequence is discarded.

**Proposed:** Serialize the full AutoTapSolution for each action:
```csharp
if (action.AutoTapSolution != null)
{
    var tapActions = new JArray();
    foreach (var tapAction in action.AutoTapSolution.TapActions)
    {
        tapActions.Add(new JObject
        {
            ["instanceId"] = tapAction.InstanceId,
            ["manaProduced"] = tapAction.ManaProduced.ToString()
        });
    }
    obj["autoTapSolution"] = tapActions;
}
```

**Impact:** The coach knows exactly which lands to tap and can advise "Tap Island + Swamp,
keep Plains untapped for removal" — currently impossible without oracle text analysis.

**Files to change:**
- `bepinex-plugin/MtgaCoachBridge/Plugin.cs` — serialize AutoTapSolution details
- `src/arenamcp/gre_bridge.py` — parse tap solutions in action data

### 2.4 Add `get_timer_state` Command

**Current repo status:** Complete.

**Proposed:** Read timer state from MatchManager or the GRE's timer system.

```csharp
case "get_timer_state":
    // Access timer through MatchManager or TimerPackage
    // Return: player1_time, player2_time, active_timer, rope_state, timeouts_remaining
    break;
```

### 2.5 Add `get_match_info` Command

**Current repo status:** Complete.

**Proposed:** Read match metadata not available in logs:
- Match ID, game number within match (game 1/2/3)
- Format/event name
- Opponent display name
- Sideboard contents (between games)
- Previous game results in this match

### 2.6 Normalize Request Identity Across Plugin and Python

**Current repo status:** In progress.

What exists now:
- plugin responses include both `request_type` and `request_class`
- Python maps both forms into bridge decision typing and prompt labels
- bridge metadata is preserved on game-state snapshots for prompts/debugging

What is still missing:
- a single stable internal bridge request ID used end-to-end
- cleanup of remaining call sites that still assume one naming style or the other
- broader typed routing beyond the currently enriched request families

**Problem discovered in live debugging:** MTGA exposes request identity in two forms:

- enum-style via `BaseUserRequest.Type.ToString()`
- CLR class name via `request.GetType().Name`

These differ materially:

- `ActionsAvailable` vs `ActionsAvailableRequest`
- `CastingTimeOptions` vs `CastingTimeOptionRequest`
- `Search` vs `SearchRequest`

**Why this matters:** Python mappings, prompt context, trigger detection, and autopilot
routing all become brittle if they assume only one naming convention.

**Proposed fix:**
- plugin should return both forms explicitly
- Python should normalize to a stable internal bridge request ID
- prompting / planning / verification should use that normalized ID

### 2.7 Direct GRE-First Autopilot Coverage

**Current repo status:** In progress.

What exists now:
- direct GRE submit for pass
- direct GRE submit for `ActionsAvailableRequest`
- direct GRE submit for several `CastingTimeOptionRequest` child choices

What is still missing:
- direct GRE submit for search / select / target / mulligan / pay-cost request trees
- broader verification that the planner can stay on GRE injection without falling back to clicks
- completion of the request-family priority list below

**2026-03-28 implementation findings:**
- The current direct-submit path in `autopilot.py` already covers:
  - pass / resolve via `submit_pass()`
  - `ActionsAvailableRequest` matching/submission
  - several `CastingTimeOptionRequest` child submissions
- This means bridge-authoritative state and direct GRE execution can advance
  concurrently without blocking each other:
  - state authority work can happen in `server.py` + normalization helpers
  - request-family submit expansion can continue in `Plugin.cs` + `gre_bridge.py`
  - downstream planning/verification work can continue in `autopilot.py`

The direct bridge execution path should be expanded from "submit pass / cast / play"
to request-family coverage.

**Priority order:**
- `ActionsAvailableRequest`
- `CastingTimeOptionRequest` child choices
- `SearchRequest`
- `SelectNRequest`
- `PayCostsRequest` child requests
- `MulliganRequest`
- `SelectTargetsRequest`
- combat declarations / grouping / counters / numeric input

**Design rule:** If a request exposes a stable submit method in MTGA's managed code,
autopilot should prefer GRE injection over screen clicks.

### 2.8 Demote Log Tailing to Fallback and Diagnostics

**Current repo status:** In progress.

What exists now:
- bridge-first decision detection is live in `standalone.py`
- logs already serve as a diagnostic source and bug-report source

What is still missing:
- bridge-first state assembly for normal in-match operation
- ability to run full live matches without relying on log-derived state
- clear separation of "bridge unavailable fallback" from the normal path

**2026-03-28 implementation findings:**
- The runtime already has **bridge-first decision detection** in `standalone.py` via
  `BridgeDecisionPoller`.
- The next step is therefore not "invent bridge-first runtime from scratch," but:
  - keep log tailing for lifecycle / recovery / deck / history fields
  - overlay bridge live state into `server.get_game_state()`
  - then progressively retire log-derived in-match state assembly once normalized bridge
    fields have proven stable
- This staged approach reduces blast radius and allows live validation while preserving
  the existing bug-report and recovery tooling based on `Player.log`.

**2026-03-29 update:**
- the bridge live-state overlay step is now complete in `server.get_game_state()`
- remaining work is concentrated in:
  - bridge-native `pending_decision` / `decision_context` parity
  - bridge-native legal-action normalization
  - reducing or eliminating log dependence for in-match state once those surfaces
    are reliable

**Revised role for logs:**
- startup / recovery when the bridge is unavailable
- match start/end metadata not yet bridged
- draft / menus / out-of-game flows
- F7 bug reports and forensic trails

**Non-goal:** logs should no longer be required for correct in-game synchronization
once the bridge is connected and state snapshots are available.

---

## Phase 3: Replay System Integration (Medium Effort, High Value)

### 3.1 Hook TimedReplayRecorder for Auto-Save

**ReVa findings:** MTGA has a complete replay system:
- `Wotc.Mtga.TimedReplays.TimedReplayRecorder` — records games
- `Wotc.Mtga.TimedReplays.ReplayWriter` — serializes to file
- `Wotc.Mtga.TimedReplays.ReplayReader` — deserializes from file
- `Wotc.Mtga.Replays.ReplayGUI` — debug UI for browsing/launching replays
- `SaveDSReplays` property — toggle for auto-saving

**Proposed:** Add BepInEx command to enable replay recording:
```csharp
case "enable_replay_recording":
    // Find or create TimedReplayRecorder
    // Set SaveDSReplays = true
    // Configure replay folder path
    break;

case "get_replay_path":
    // Return path to last saved replay
    break;
```

**Impact on post-match analysis:** Currently, post-match sends a lossy text summary
of trigger history to the LLM. With replays, we'd have every GRE message timestamped —
the LLM gets perfect recall of every play, every decision, every mistake.

**Files to change:**
- `bepinex-plugin/MtgaCoachBridge/Plugin.cs` — add replay enable/path commands
- `src/arenamcp/gre_bridge.py` — add replay methods
- `src/arenamcp/coach.py` — load replay data for post-match analysis
- `src/arenamcp/standalone.py` — enable replay recording at match start

### 3.2 Replay-Powered Match History

Save replay files to `~/.arenamcp/replays/` with metadata (date, opponent, result,
format, deck archetype). Build a match history database that the coach can reference:

- "You're 2-5 against mono-red this session — board in more lifegain"
- "Last time you faced this deck, you lost to flyers — hold removal for their flyers"
- Win rate tracking per archetype, per format

**Files to change:**
- New: `src/arenamcp/match_history.py` — replay storage, indexing, querying
- `src/arenamcp/server.py` — add `get_match_history()` MCP tool
- `src/arenamcp/coach.py` — include match history context in prompts

---

## Phase 4: Prediction Engine (Medium-High Effort, Unique Advantage)

### 4.1 Use GRE's Built-In Prediction System

**Current repo status:** Pending.

**ReVa findings:** The GRE protocol includes:
- `ClientMessageType_PredictionReq` — request game state prediction
- `GREMessageType_PredictionResp` — predicted state response
- `EnablePredictionsFieldNumber` — toggle predictions
- `AllowPrediction` — per-action prediction flag
- `ManaSpecType_Predictive` — predictive mana analysis
- `AnnotationType_PredictedDirectDamage` — combat damage predictions
- `IsUnpredictable` — flag for non-deterministic outcomes

**Proposed:** Send prediction requests through BepInEx to ask "what happens if I cast X?"

This would give the coach the game engine's own simulation of outcomes — not LLM
hallucination, not heuristic estimation, but the actual rules engine computing what
happens. This is potentially the biggest competitive advantage possible.

**Investigation needed:**
- Can PredictionReq be sent from the client side, or is it server-only?
- What's the response format? Full game state diff or summary?
- Does it handle all game mechanics or just combat?
- Performance: how fast does it respond?

**Files to change:**
- `bepinex-plugin/MtgaCoachBridge/Plugin.cs` — send prediction requests via GreInterface
- `src/arenamcp/gre_bridge.py` — add `predict_action()` method
- `src/arenamcp/coach.py` — use predictions to validate advice before speaking

### 4.2 Use Advisability Flags

**Current repo status:** Pending.

**ReVa findings:** The GRE tags certain choices with `Advisability` and has
`ModalChoiceAdvisability_Discourage` for bad plays.

**Proposed:** Parse advisability from GRE messages and expose to the coach:
- If the game engine itself says a play is discouraged, the coach should flag it
- Useful as a sanity check on LLM-generated advice

**Files to change:**
- `src/arenamcp/gamestate.py` — parse advisability from action messages
- `src/arenamcp/coach.py` — include advisability warnings in prompts

---

## Phase 5: Social & Advanced Features (High Effort, Future Vision)

### 5.1 Discord Rich Presence Coaching Status

**ReVa findings:** MTGA has `DiscordManager` with `FakeRichPresence`, `ActivitySecrets`,
`CreateOrJoinLobby`.

**Proposed:** Show coaching status in Discord:
- "Playing Standard — mtgacoach active"
- Share coaching session link for friends to follow along
- Post-match stats to Discord webhook

### 5.2 Lobby System for Coaching Sessions

**ReVa findings:** Full lobby system: `Client_Lobby`, `Client_LobbyMessage`,
`LobbyController`, `SendCreateLobby`, etc.

**Future:** Create coaching lobbies where a human coach can supervise the AI coach,
override advice, or spectate a student's game.

### 5.3 Tournament Coaching

**ReVa findings:** `TournamentDataProvider`, `Client_TournamentState`,
`Client_TournamentPairing`, `GetTournamentStandings`.

**Future:** Adapt coaching based on tournament context:
- Conservative play when leading in standings
- Aggressive play when elimination is near
- Sideboard advice based on expected meta at the tournament stage

### 5.4 Table Draft Intelligence

**ReVa findings:** `HumanDraftPod`, `TableDraftQueueView`, `BotDraftPod`.

**Future:** In live table drafts, track signals from other drafters:
- What colors/archetypes are open based on what wheels
- Adjust pick recommendations based on pod dynamics
- "Player to your right is in red — cut the Lightning Bolt"

---

## Phase 6: Quality of Life

### 6.1 Add Debug Logging for Dropped Annotations

**Quick fix:** Add an else clause to the annotation handler (gamestate.py:1641) that
logs unhandled annotation types at DEBUG level. This helps identify new annotation types
as MTGA updates.

```python
else:
    logger.debug("Unhandled annotation type: %s (affected: %s)", ann_type, affected_ids)
```

### 6.2 Track Sideboard Between Games

**Current gap:** No sideboard contents exposed to LLM between games in a match.

**Fix:** Parse `SubmitDeckReq` messages in BO3 to capture sideboard changes.
Expose via `get_game_state()` as `sideboard_cards`.

### 6.3 Action History Buffer

**Current gap:** No history of spells cast / actions taken this turn or game.

**Fix:** Maintain a rolling buffer of the last N actions (from `UserActionTaken`
annotations and zone transfers). Expose as `recent_actions` in game state.

### 6.4 Opponent Archetype Detection

**Current repo status:** Pending.

**Current gap:** No archetype identification beyond manual color analysis.

**Fix:** After seeing N opponent cards, match against known meta decks from
`get_metagame()`. Report "Opponent is likely playing Azorius Control (78% match)."

---

## Phase 7: Desktop GUI and Operator Experience

The current TUI has been useful for development, but it is not the right primary
interface for a product that installs into Windows, owns a bridge runtime, and needs
repair/status workflows.

### 7.1 Build a Real Desktop GUI

**Current repo status:** Prototype in progress.

What exists now:
- `launcher_gui.py` provides a Windows desktop GUI for launch/setup/repair
- the GUI is useful operationally and already exposes bridge-related health checks

What is still missing:
- replacing the main coaching surface
- moving coach/autopilot controls out of the Textual TUI
- a packaged GUI-first runtime

**2026-03-28 launcher UX findings:**
- The installed product should expose **one launcher icon** with the proper app icon,
  not separate user-facing shortcuts for coach/autopilot/repair.
- Launch mode selection belongs inside the launcher surface, not in separate wrapper
  scripts or separate installed shortcuts.
- A hidden-script or native launcher shim is acceptable as an implementation detail
  if it prevents `cmd.exe` windows from flashing during normal Windows launch.

**Goal:** replace or wrap the TUI with a proper Windows desktop application that exposes
the product's main modes and runtime status clearly.

Core GUI responsibilities:
- mode selection:
  - Coach
  - Autopilot
  - Voice Advisor
  - Draft
- backend/model settings
- bridge/plugin/BepInEx health status
- MTGA detection status
- one-click start/stop/reconnect actions
- logs and diagnostics access
- bug report submission / export

### 7.2 Add Guided Setup and Repair UX

**Current repo status:** In progress.

What exists now:
- MTGA path save/browse
- BepInEx install/repair
- plugin install/update
- "Repair MTGA Bridge"
- log-tail visibility and setup wizard launch from the GUI

What is still missing:
- a more polished guided flow
- packaging-backed repair validation
- better inline explanations for failures and next actions

Because BepInEx and the GRE bridge are now operational dependencies, the GUI should
include setup/repair flows rather than expecting the user to manage files manually.

Setup/repair screens should support:
- locate MTGA
- install/repair BepInEx
- install/update plugin
- verify bridge connectivity
- explain failures with concrete actions

### 7.3 Surface Live Runtime State in the GUI

**Current repo status:** Early / partial.

What exists now:
- runtime root / Python runtime / MTGA / BepInEx / plugin / log status
- bridge readiness summary and log tails

What is still missing:
- live current request type
- current match / game state summary
- autopilot state, last executed action, and direct bridge activity

The GUI should make the bridge-first runtime visible:
- MTGA running / not running
- plugin loaded / not loaded
- bridge connected / disconnected
- current request type
- current match / game status
- current autopilot state and last executed action

### 7.4 GUI Technology Direction

**Current repo status:** Open decision, prototype uses Tkinter.

Tkinter was a pragmatic choice for the first launcher/repair surface because it is
stdlib-only and easy to bootstrap. It should not be treated as the final product UI
decision until packaging, runtime control, and coach-surface needs are compared against
PySide6/Qt or another more product-grade option.

The GUI should be chosen based on Windows installability and reliability, not terminal
reuse. Good options include:
- PySide6 / Qt for a native-feeling packaged desktop app
- a small local web UI wrapped in a desktop shell if packaging is robust enough

The important product requirement is:
- packaged cleanly
- launched without shell scripts
- repairable
- friendly to installers, shortcuts, and standard Windows UX

---

## 2026-03-28 Reprioritization

These items now supersede the older sequencing below.

| Priority | Upgrade | Effort | Impact | Status |
|---|---|---|---|---|
| 1 | Windows installer + BepInEx bootstrap/repair + proper launcher | 3-7 days | **Transformative** | In progress: launcher/repair surface and installer prototype exist; packaged installer still missing |
| 2 | Bridge-authoritative live state pipeline (`get_game_state` first, logs second) | 2-4 days | **Transformative** | In progress: plugin state command exists, Python runtime is still log-first |
| 3 | Typed `get_interaction_snapshot` / normalized request identity | 2-4 days | **Transformative** | In progress: typed payloads now ride on `get_pending_actions`; dedicated snapshot contract still missing |
| 4 | Direct GRE submission for casting-time and pay-costs request trees | 2-5 days | **Transformative** | In progress: casting-time direct submit exists; pay-costs tree submit still missing |
| 5 | Direct GRE submission for search / select / mulligan / targets | 3-5 days | High | Pending: prompt/debug serialization is partial, direct submit paths are still missing |
| 6 | Demote log tailing to fallback / diagnostics only | 1-3 days | High | In progress: bridge-first decision detection exists; full state is still log-derived |
| 7 | Desktop GUI with setup/repair/runtime status | 4-10 days | High | In progress: launcher GUI exists, main coach surface is still the TUI |
| 8 | GRE prediction engine / advisability after bridge-first runtime is stable | 3-5 days | **Transformative** | Pending |

## Historical Implementation Priority

The table below captures the original sequence prior to the 2026-03-28 bridge-first
architecture update.

| # | Upgrade | Effort | Impact | Dependencies | Status |
|---|---------|--------|--------|-------------|--------|
| 1 | 6.1 Debug log dropped annotations | 30 min | Low (diagnostic) | None | **DONE** (2026-03-26) |
| 2 | 1.1 Add missing annotations (TargetSpec, Modified*, Designations) | 1-2 days | High | None | **DONE** (2026-03-26) |
| 3 | 1.2 Parse AutoTap/mana solver data | 1 day | High | None | **DONE** (2026-03-26) |
| 4 | 1.3 Extract ability metadata | 0.5 day | Medium | None | **DONE** (2026-03-26) |
| 5 | 1.4 Parse timer state | 0.5 day | Medium | None | **DONE** (2026-03-26) |
| 6 | 6.2 Track sideboard | 0.5 day | Medium | None | **DONE** (2026-03-26) |
| 7 | 6.3 Action history buffer | 0.5 day | Medium | None | **DONE** (2026-03-26) |
| 8 | 6.4 Opponent archetype detection | 1 day | Medium | None | Pending |
| 9 | 2.3 Plugin: serialize AutoTap solutions | 1 day | High | BepInEx rebuild | **DONE** (2026-03-26) |
| 10 | 2.2 Plugin: rich interaction detail | 2 days | High | BepInEx rebuild | **DONE** (2026-03-26) |
| 11 | 2.1 Plugin: get_game_state from GameManager | 3-5 days | **Transformative** | BepInEx rebuild, reflection exploration | **DONE** (2026-03-26) |
| 12 | 2.4-2.5 Plugin: timer + match info | 1 day | Medium | BepInEx rebuild | **DONE** (2026-03-26) |
| 13 | 3.1 Hook replay recorder | 2-3 days | High | BepInEx, replay format RE | **DONE** (2026-03-26) |
| 14 | 3.2 Match history database | 2 days | High | 3.1 | **DONE** (2026-03-26) |
| 15 | 4.2 Advisability flags | 0.5 day | Medium | None (from log) | Pending |
| 16 | 4.1 GRE prediction engine | 3-5 days | **Transformative** | BepInEx, protocol RE | Pending |
| 17 | 5.1-5.4 Social/tournament/draft | Weeks | Future | All above | Future |

**Recommended order:** Items 1-8 first (pure Python, no plugin changes, immediate gains),
then 9-12 (plugin batch), then 13-16 (advanced features).

---

## Key Technical Notes

### Annotation Handling Location
- `src/arenamcp/gamestate.py` lines 1451-1641 — the annotation if/elif chain
- No else clause — unhandled types silently dropped
- Handler receives: `ann_type` (string), annotation dict, `affected_ids` list

### AutoTap Data Location
- `src/arenamcp/gamestate.py` line 2358 — boolean-only check, data discarded
- The GRE sends full `autoTapSolutions` array inside `autoTapActionsReq`
- Each solution contains tap actions with instance IDs and mana produced

### Plugin Architecture
- `Plugin.cs` accesses MTGA via `FindObjectOfType<GameManager>()` (the only relevant MonoBehaviour)
- `GameManager` → `WorkflowController` → `CurrentWorkflow` → `BaseRequest` (the pending interaction)
- `GameManager` → `MatchManager` → `GreInterface` (for direct GRE access)
- **Important:** `MatchManager` and `InteractionDirector` are NOT MonoBehaviours — cannot use `FindObjectOfType` on them. Must go through `GameManager`.
- Uses reflection for `BaseRequest`/`PendingWorkflow` properties (generic type varies per workflow)
- Main thread execution via `ConcurrentQueue<PipeCommand>` + Unity Update()
- Communication: Windows named pipe `\\.\pipe\mtgacoach_gre`, JSON newline-delimited

### GRE Protobuf Message Types
- 45+ message types defined in `Wizards.MDN.GreProtobuf.dll`
- Currently handling: `GameStateMessage`, 29 Req types, 12 Resp types
- Currently ignoring: `UIMessage`, `TimerStateMessage`, `BinaryGameState`,
  `EdictalMessage`, `PredictionResp`, `AllowForceDraw`, `IllegalRequest`

### MTGA Internal Namespaces (from ReVa)
- `Wotc.Mtga.Replays` / `Wotc.Mtga.TimedReplays` — full replay system
- `Wotc.Mtga.AutoPlay` — scripted autoplay framework
- `Core.Shared.Code.DebugTools` — massive debug toolkit (HacksPageGUI, GREWatcherGUI, etc.)
- `Core.Meta.Social.Tables` — lobby/table system
- `Wizards.Mtga.PrivateGame` — direct challenge infrastructure
- `Wizards.Arena.Gathering` — friend/social platform
- `HasbroGo.SocialManager` — Hasbro social SDK (friend/chat/challenge)
