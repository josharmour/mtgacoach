# GEMINI.md

## Project Summary

`mtgacoach` (`arenamcp`) is an MTGA coaching project with:
- **Python Core (`src/arenamcp/`)**: Headless coaching engine driving the pipeline.
- **Desktop UI (`src/arenamcp/desktop/`)**: PySide6-based frontend providing the dashboard, coaching log, repair tools, and transparent, click-through HUD overlay.
- **BepInEx Plugin (`bepinex-plugin/MtgaCoachBridge/`)**: C# plugin injected into MTGA for direct GRE state access, action submission, and automation.
- **Proxy Server (`proxy-server/`)**: FastAPI gateway at `api.mtgacoach.com` routing subscriber LLM requests, tracking usage, and providing an eval dashboard.
- **LLM Evaluation Harness (`tools/eval/`)**: Harness for scoring local model quality (mulligans, turn-actions, replays) using real play prompts.

## Core Direction & Architecture

- **GRE Bridge is Primary**: Real-time game state tracking and action submissions run through the direct Unity GRE named pipe (`\\.\pipe\mtgacoach_bridge_v2`). Python acts as the named pipe server; BepInEx is the client.
- **Log Parsing as Fallback**: `Player.log` is parsed via a log watcher only as a fallback and for out-of-match/metadata diagnostics.
- **File System Separation**: Installed files live read-only under `Program Files\mtgacoach`, while mutable runtime files (venv, settings, logs) belong under `%LOCALAPPDATA%\mtgacoach` and `%USERPROFILE%\.arenamcp`.
- **Local LLM Default**: Default local backend is **vLLM** (`http://localhost:8000/v1`) running `google/gemma-4-E2B-it` (aliased as `gemma4:e2b`), with fallback to Ollama (`localhost:11434`) or LM Studio.

## LLM Evaluation Harness (`tools/eval/`)

- Runs idempotent evaluation pipelines using captured prompts (`prompts.jsonl`).
- **Replay Scorer (`tools/eval/replay/`)**: Replays prompts through backends (`run.py`) and scores them against judge models on **contested decisions** (decisions where the player had a choice, to prevent easy moves from inflating match rate).
- **Mulligan Scorer**: Uses balanced accuracy (`balanced_higher_wr_rate`) to avoid keep-rate bias. Skips buckets within `MARGIN_THRESHOLD=0.05`.
- **Turn-action Scorer**: Uses Mean Jaccard (set overlap) as the headline metric.
- Supports WSL paths to allow running WSL evaluators against Windows MTGA paths.

## Installer & Repair UX

- Prefer a **small installer** built by `installer/build-installer.ps1` that packages the application and downloads/creates the venv post-install.
- The repair UI must offer explicit user-visible actions for:
  - `Create venv`
  - `Setup environment`
  - `Install BepInEx`
  - `Install Plugin`
  - Bridge repair / refresh actions

## Release Rules

When revving a release (currently version `2.3.4`):
1. Bump version in:
   - `pyproject.toml`
   - `src/arenamcp/__init__.py`
   - `installer/mtgacoach.iss`
2. Commit changes.
3. Tag `vX.Y.Z`.
4. Build the installer (`build-installer.ps1`) and verify the output at `dist/installer/mtgacoach-Setup.exe`.
5. Push master and tag.
6. Create/update the GitHub release and upload `mtgacoach-Setup.exe`.

## Common Commands

### Setup & Run
```powershell
# Editable dev install (Windows)
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m pip install -e Z:\ArenaMCP[desktop]

# Run PySide6 app from source
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m arenamcp.desktop

# Launch app using wrapper script
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\pythonw.exe Z:\ArenaMCP\scripts\launch_desktop.py
```

### Build & Test
```powershell
# Run regression tests
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m pytest Z:\ArenaMCP\tests -q

# Build BepInEx plugin DLL (C#)
cd bepinex-plugin/MtgaCoachBridge && dotnet build -c Release

# Build Windows installer from WSL
p=$(wslpath -w /home/joshu/repos/ArenaMCP/installer) && powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "\$p='${p}'; Set-Location -LiteralPath \$p; .\build-installer.ps1"
```

### Verification & Eval
```powershell
# Verify local vLLM endpoint
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe tools/verify_vllm.py

# Run replay evaluation harness
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe tools/eval/replay/run.py
```
## Repository Directory & File Map

### Top-Level Layout
* **`src/`**: The core Python codebase (`arenamcp`). This is the heart of the backend coaching loop, logs, game-state logic, Named Pipe GRE bridge server, and PySide6 desktop UI.
* **`bepinex-plugin/`**: The C# Unity plugin injected into Magic: The Gathering Arena. It intercepts internal game states and acts as a Named Pipe client connecting directly to Python.
* **`proxy-server/`**: A FastAPI application running on `api.mtgacoach.com` (under Synology/Docker) that validates licenses, routes LLM requests to Azure OpenAI (gpt-5.4), and serves the admin evaluation dashboard.
* **`tools/`**: Diagnostic scripts, vLLM verification tools, and the comprehensive evaluation harness (`tools/eval/`) for measuring LLM output quality.
* **`installer/`**: Inno Setup script (`mtgacoach.iss`) and build automation scripts (`build-installer.ps1`) to generate the lightweight Windows setup executable.
* **`dist/`** *(Generated)*: Where release builds of the app and final installer packages are staged.
* **`venv/`** / **`.venv/`** *(Gitignored)*: Local Python virtual environments used during WSL/Windows development.
* **`.tools/`** / **`.vscode/`** *(Gitignored)*: IDE configuration and local development helper scripts.

### Python Coaching Engine ([src/arenamcp/](file:///Z:/ArenaMCP/src/arenamcp))

#### Communication & Protocols
* [pipe_adapter.py](file:///Z:/ArenaMCP/src/arenamcp/pipe_adapter.py) — Establishes the JSON-over-stdio protocol used by the headless coaching subprocess to communicate back to the PySide6 desktop frontend.
* [gre_bridge.py](file:///Z:/ArenaMCP/src/arenamcp/gre_bridge.py) — Runs a local Named Pipe server (`\\.\pipe\mtgacoach_bridge_v2`) to exchange game snapshots and execute automation actions with the BepInEx Unity plugin.
* [gre_serializer.py](file:///Z:/ArenaMCP/src/arenamcp/gre_serializer.py) — Protobuf JSON translator for GRE events.
* [gre_action_matcher.py](file:///Z:/ArenaMCP/src/arenamcp/gre_action_matcher.py) — Maps high-level decisions from the action planner to low-level GRE action objects.
* [parser.py](file:///Z:/ArenaMCP/src/arenamcp/parser.py) & [watcher.py](file:///Z:/ArenaMCP/src/arenamcp/watcher.py) — File watcher and parser for `Player.log` acting as a fallback data source.

#### State & Decision Engine
* [gamestate.py](file:///Z:/ArenaMCP/src/arenamcp/gamestate.py) — Main game state management. Processes direct GRE bridge protobuf messages and log parsing.
* [coach.py](file:///Z:/ArenaMCP/src/arenamcp/coach.py) — Prompt building, LLM querying, advice post-processing, and fallback deterministic scoring logic.
* [action_planner.py](file:///Z:/ArenaMCP/src/arenamcp/action_planner.py) — Autonomous gameplay planning using LLM-generated actions.
* [autopilot.py](file:///Z:/ArenaMCP/src/arenamcp/autopilot.py) — Core executor for autonomous play, utilizing GRE bridge API and OS level inputs.
* [combat_solver.py](file:///Z:/ArenaMCP/src/arenamcp/combat_solver.py) — Solves complex combat math for planning.
* [standalone.py](file:///Z:/ArenaMCP/src/arenamcp/standalone.py) — Entry point for headless coaching loop, managing trigger states.

#### Database & External APIs
* [scryfall.py](file:///Z:/ArenaMCP/src/arenamcp/scryfall.py) — Scryfall REST API client with local disk cache.
* [draftstats.py](file:///Z:/ArenaMCP/src/arenamcp/draftstats.py) & [draft_eval.py](file:///Z:/ArenaMCP/src/arenamcp/draft_eval.py) — Queries and processes 17lands data to evaluate draft options.
* [mtgadb.py](file:///Z:/ArenaMCP/src/arenamcp/mtgadb.py) & [mtgjson.py](file:///Z:/ArenaMCP/src/arenamcp/mtgjson.py) — Integrates local card lookup SQLite databases.

#### Interaction & Desktop Integration
* [input_controller.py](file:///Z:/ArenaMCP/src/arenamcp/input_controller.py) — Coordinates direct OS clicks and key input automation.
* [screen_mapper.py](file:///Z:/ArenaMCP/src/arenamcp/screen_mapper.py) — Calculates positions on screen for card/button interactions.
* [vision_mapper.py](file:///Z:/ArenaMCP/src/arenamcp/vision_mapper.py) — VLM-based pixel-level fallback coordinate resolver.
* [tts.py](file:///Z:/ArenaMCP/src/arenamcp/tts.py) & [transcription.py](file:///Z:/ArenaMCP/src/arenamcp/transcription.py) — Text-to-Speech synthesis and transcription integration.

### Desktop UI Subsystem ([src/arenamcp/desktop/](file:///Z:/ArenaMCP/src/arenamcp/desktop))
* [app.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/app.py) — PySide6 application runtime initialization.
* [main_window.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/main_window.py) — Main workspace window and tab shell.
* [coach_tab.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/coach_tab.py) — Central control panel for voice settings, engine logs, and session controls.
* [repair_tab.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/repair_tab.py) — Self-healing diagnostic interface (venv setup, environment settings, BepInEx installation, and bridge validation).
* [hud.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/hud.py) & [card_overlay.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/card_overlay.py) — Transparent click-through overlay window displaying draft reviews directly on top of MTGA.
* [coach_process.py](file:///Z:/ArenaMCP/src/arenamcp/desktop/coach_process.py) — Manages standard subprocess pipes to start/stop the standalone backend process.

### Unity C# Plugin ([bepinex-plugin/MtgaCoachBridge/](file:///Z:/ArenaMCP/bepinex-plugin/MtgaCoachBridge))
* [Plugin.cs](file:///Z:/ArenaMCP/bepinex-plugin/MtgaCoachBridge/Plugin.cs) — Injected DLL logic. Injects into MTGA Unity lifecycle, handles connection to local Named Pipe (`\\.\pipe\mtgacoach_bridge_v2`), and executes marshaled main-thread queries/actions.
* [BotBattleBridge.cs](file:///Z:/ArenaMCP/bepinex-plugin/MtgaCoachBridge/BotBattleBridge.cs) — Intercepts and logs bot match statistics and triggers.

### Proxy Server Backend ([proxy-server/](file:///Z:/ArenaMCP/proxy-server))
* [app.py](file:///Z:/ArenaMCP/proxy-server/app.py) — FastAPI server routing LLM requests, validating client keys, and exposing admin portals.
* [providers.py](file:///Z:/ArenaMCP/proxy-server/providers.py) — Provider wrappers with priority-based route fallbacks and backoff logic.
* [db.py](file:///Z:/ArenaMCP/proxy-server/db.py) — SQLite wrapper tracking client license tokens and server metrics.
* [templates/admin.html](file:///Z:/ArenaMCP/proxy-server/templates/admin.html) — Cloud admin evaluation dashboard, displaying real-time latencies, charts, and metrics tables.

### Evaluation Harness ([tools/eval/](file:///Z:/ArenaMCP/tools/eval))
* [run.py](file:///Z:/ArenaMCP/tools/eval/run.py) & [judge.py](file:///Z:/ArenaMCP/tools/eval/judge.py) — Replays prompt sets through custom backends and uses online judges (GPT-5.4) to grade model output.
* [report.py](file:///Z:/ArenaMCP/tools/eval/report.py) — Generates accuracy tables and markdown report summaries.
* [replay/run.py](file:///Z:/ArenaMCP/tools/eval/replay/run.py) & [replay/score.py](file:///Z:/ArenaMCP/tools/eval/replay/score.py) — Runs and scores model outputs on real-game replay prompts, focusing on contested decisions to filter out easy play bias.
* [seventeenlands/score_mulligan.py](file:///Z:/ArenaMCP/tools/eval/seventeenlands/score_mulligan.py) & [seventeenlands/score_turn_actions.py](file:///Z:/ArenaMCP/tools/eval/seventeenlands/score_turn_actions.py) — Scores decisions against 17lands data using balanced accuracy and Jaccard metrics.

## Hygiene

Do not commit:
- `.venv/` or `venv/`
- `.tools/`
- `bin/` or `obj/`
- Scratch files (e.g. `.ab` files, `last_match_event.txt`, debug logs)
- Generated `dist/` or `build/` files

`tests/` may be ignored in git by default; use `git add -f tests/...` if you need to add new tests to git.
