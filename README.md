# mtgacoach — AI-Powered MTGA Coaching

Real-time AI coaching for Magic: The Gathering Arena. Watches your live games and delivers spoken strategic advice through a native Windows desktop app — with an in-game overlay that draws right on top of MTGA.

## What's new in 2.1

- **In-game overlay** — Transparent, click-through overlay sits on top of MTGA with the latest coach advice, a card-count pipeline indicator, and per-card highlight rings for autopilot's suggested actions. Pick any of the four corners for the advice panel; hide the whole overlay when you want an unobstructed view.
- **Ground-truth card positions from BepInEx** — The plugin projects every visible `DuelScene_CDC` through MTGA's `Camera.MainCamera.WorldToScreenPoint`, so highlights land exactly on the actual cards instead of guessing from layout heuristics. DPI-corrected so everything sizes correctly on high-DPI displays.
- **Draft overlay with Draftsmith-style scoring** — Badges on each draft card show tier (WEAK/BRONZE/SILVER/GOLD/FIRE) and composite score. Per-color-pair scoring inspired by untapped.gg's approach: every card is scored for all 10 two-color pairs and the best-fitting pair is surfaced. Uses 17lands format color-pair win rates when available and falls back to rarity + CMC + stats when a set isn't in 17lands yet.
- **Autopilot no longer steals the mouse** (default) — When the GRE bridge can't submit an action, autopilot now emits `MANUAL REQUIRED: ...` advice instead of falling through to mouse clicks. A new **Fallback: Advice / Fallback: Mouse** toggle lets you opt back into mouse fallback if you want.
- **Better planner stability** — Temperature-0 for planning (deterministic), turn-memo that tells the LLM "you already proposed X — stay committed", and a fixed comma-in-name bug that was dropping valid "Attack with: Lluwen, Imperfect Naturalist"-style plans and falling back to the first legal action.
- **Select-N / Search-Library via bridge** — Lluwen-style ETB searches now submit via the bridge directly instead of clicking by list index (which was the cause of the classic "stuck on select-n loop").
- **Quick / Chatty advice styles** — Previously "Concise / Verbose"; the prompts now actually differ. Quick is one imperative sentence; Chatty is conversational with reasoning and tradeoffs.
- **Screenshots in bug reports** — Clicking Debug Report captures both the coach window and the MTGA window as PNGs, stores them in the local bug-reports folder, and references them in the GitHub issue body.
- **Capped bridge-fallback telemetry** — When autopilot hits a bridge-miss, the event is buffered and, at match end, up to 5 randomly-sampled events are silently auto-reported to GitHub (tagged `auto-reported`) so we accumulate coverage on what action types need bridge support.
- **Draft pack sequencing** — The draft-type detector (Premier, Quick, Traditional, PickTwo, Sealed, and the PickTwo variants) is now the single source of truth; advice for normal drafts is "Take X" rather than the old "Take X. Or Y."
- **Debug Report UX** — Click the button once; the local report is saved and the path is copied to your clipboard immediately. Then a second dialog asks whether you want to upload to GitHub with an optional description.
- **Restart button actually restarts** — Previously sent a pipe command to a dying process that wouldn't relaunch. Now properly stops + respawns the coach, non-blocking so your UI doesn't stutter.
- **No more console flashes on launch** — All bootstrap subprocess calls run with `CREATE_NO_WINDOW`.
- **Log filter** — View → Show Debug Logging now re-renders history; autopilot operational noise is demoted to debug by default, strategic `PLAN:` summaries stay visible.

## Features

- **Native Windows app** — PySide6 desktop GUI with dark theme, no console window
- **In-game overlay** — Advice panel + highlight rings drawn directly on MTGA using ground-truth Unity positions
- **Real-time coaching** — AI sees your board, hand, life totals, and legal actions
- **Voice output** — Kokoro neural TTS with voice + speed options
- **Voice input** — Push-to-talk to ask questions mid-game
- **Draft helper** — Per-color-pair scoring, tier badges, per-card overlays
- **Autopilot** — AI plays for you via the BepInEx GRE bridge with no mouse interference
- **Quick / Chatty advice styles** — short imperative or explanatory
- **Replay recording** — Automatic match recording for debugging and post-game analysis
- **Post-match analysis** — Detailed review after each game
- **Auto bug-report telemetry** — Up to 5 random bridge-fallback events per match
- **Local model support** — Run with Ollama or LM Studio for free, offline play

## Install

The app installs with [**uv**](https://docs.astral.sh/uv/) on Windows, Linux,
and macOS — one tool that manages an isolated Python environment for you.

```bash
# 1. Install uv (once):
#    Linux/macOS:  curl -LsSf https://astral.sh/uv/install.sh | sh
#    Windows:      powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. Install (or update) mtgacoach:
uv tool install --force \
  https://github.com/josharmour/mtgacoach/releases/download/v2.7.1/arenamcp-2.7.1-py3-none-any.whl
# (newer versions: grab the .whl URL from the latest release page)

# 3. Run:
mtgacoach-desktop     # the app (with the Repair tab)
mtgacoach-repair      # the same check-and-repair checklist from a terminal
```

**Windows, no terminal?** Download **mtgacoach-Setup.exe** from the
[latest release](https://github.com/josharmour/mtgacoach/releases/latest) and run it.

### First run

Open the **Repair** tab. It checks everything automatically — Python runtime,
license key, MTGA location, game logging, BepInEx, the bridge plugin, and
(Linux/Proton) your Steam launch options — and fixes what it safely can. Follow
any ⚠ rows it shows (for example, paste your license key right in the tab), then
restart MTGA once so the plugin loads.

Requires MTGA via Steam/Proton (Linux) or the official client (Windows), with
**Detailed Logs (Plugin Support)** enabled in MTGA's options. The BepInEx bridge
(for the overlay and autopilot) is installed and kept up to date by the Repair
tab — no manual steps.

## AI Backend

| Mode | Setup | Cost |
|------|-------|------|
| **Online** | Subscribe at [mtgacoach.com](https://mtgacoach.com/subscribe), enter key in settings | Subscription |
| **Local** | Install [Ollama](https://ollama.com) | Free |

Switch between modes using the **Online/Local** button in the app.

## App Controls

### Core
| Button | Key | Action |
|--------|-----|--------|
| Online/Local | | Switch AI backend |
| Model | | Cycle available models |
| Quick/Chatty | F2 | Toggle advice style (short vs conversational) |
| Screen | F3 | Analyze current screenshot via VLM |
| Mute | F5 | Toggle voice output |
| Voice | F6 | Cycle TTS voice |
| Speed | F8 | Cycle TTS speed |
| Debug Report | F7 | Save local bug report + optional GitHub upload (captures screenshots) |
| Restart | | Restart the coaching engine (non-blocking) |

### Autopilot
| Button | Key | Action |
|--------|-----|--------|
| AP:OFF/ON | F12 | Toggle autopilot |
| AP Cancel | F1 | Cancel current autopilot plan |
| AP Abort | F4 | Abort autopilot execution immediately |
| Fallback: Advice/Mouse | | When the bridge can't submit an action, either warn (default) or fall back to mouse clicks |
| Win Plan | | Read current win probability |

### Overlays
| Button | Action |
|--------|--------|
| Overlay | Show/hide the in-game overlay entirely |
| Advice Corner | Cycle the advice panel through the four corners of MTGA |
| Match Calib | Draw a diagnostic outline around every card the plugin detects (turns off click-through) |
| Cards | Toggle per-card badges on draft packs |
| Calib | Diagnose draft-pack grid alignment |

## Chat Commands

Type in the chat box at the bottom of the Coach tab:

| Command | Action |
|---------|--------|
| Any text | Ask the coach a question about the current game |
| `/analyze` | Run post-match analysis on the most recent match |
| `/deck` | Generate or recall a deck strategy brief |
| `/chance` | Estimate win probability |
| `/bugreport <notes>` | Save + upload a bug report |
| `/key <license>` | Set your mtgacoach.com license key |
| `/online`, `/local` | Switch backend |

## Troubleshooting

- **Wrong player / advice is backwards** — Click Restart to re-detect seat
- **No voice output** — TTS models download automatically on first speak (~340MB). Wait for download.
- **Ollama connection refused** — Make sure Ollama is running: `ollama serve`
- **BepInEx / bridge plugin missing** — Open the Repair tab and use Install BepInEx + Install Plugin
- **Overlay not visible** — Make sure MTGA is windowed or borderless-windowed (not exclusive fullscreen). Click "Overlay" to toggle. Click "Match Calib" to draw a diagnostic outline.
- **Overlay positions are off** — Usually a DPI/scaling change; click Restart to refresh geometry
- **Autopilot stealing the mouse** — Make sure "Fallback: Advice" is the mode (bridge-only). If "Fallback: Mouse", autopilot will click when the bridge can't submit.
- **Run diagnostics** — `python -m arenamcp.diagnose`
- **Copy debug logs** — Click "Debug Report" to save a bug report with full game state, replay data, autopilot diagnostics, and screenshots

## Development

For developers working from the repo:

```bash
# Install Python dependencies
python -m pip install -e .[dev,full]

# Run tests
pytest tests -q

# Build the BepInEx plugin
cd bepinex-plugin/MtgaCoachBridge
dotnet build -c Release

# Build the installer (Windows, requires Inno Setup)
iscc installer/mtgacoach.iss
```

## License

MIT

## Repair & recovery

- In the app: the **Repair** tab runs one "Check & Repair" pass over your
  whole setup (runtime, license, MTGA, BepInEx bridge) and fixes what it
  safely can.
- From a terminal (works even when the GUI won't start):
  `mtgacoach-repair` — same checklist, no Qt required.
  `mtgacoach-repair --set-license sk-...` — set your license key.
  `mtgacoach-repair --update` — update the app via pip and re-check.
