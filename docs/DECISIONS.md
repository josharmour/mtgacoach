# Decision & Verification Log

Standing log of architectural decisions, their rationale, the evidence behind
them, and falsifiable claims other agents can re-verify. Companion to
[PLATFORM_PARITY.md](PLATFORM_PARITY.md) (the gap tables live there; this doc
records *why* and *how we know*). Newest entries first.

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
