# mtgacoach — AI-Powered MTGA Coaching

Real-time AI coaching for Magic: The Gathering Arena. Watches your live games and delivers spoken strategic advice through a native Windows desktop app.

## Features

- **Native Windows app** — WinUI 3 desktop GUI with dark theme, no console window
- **Real-time coaching** — AI sees your board, hand, life totals, and legal actions
- **Voice output** — Kokoro neural TTS with 7 voice options and speed control
- **Voice input** — Push-to-talk to ask questions mid-game
- **Win plan detection** — Background analysis finds lethal lines and alerts you
- **Draft helper** — 17lands stats + composite scoring for draft picks
- **Autopilot** — AI plays for you with bridge-verified action execution
- **Replay recording** — Automatic match recording for debugging and post-game analysis
- **Post-match analysis** — Detailed review after each game
- **Local model support** — Run with Ollama or LM Studio for free, offline play

## Quick Start (Windows)

### Install

1. Install [Python 3.10+](https://python.org) (check "Add Python to PATH")
2. Download [**mtgacoach-Setup.exe**](https://github.com/josharmour/mtgacoach/releases/latest/download/mtgacoach-Setup.exe) from the latest release
3. Run the installer — it installs to `Program Files\mtgacoach`
4. Launch from the desktop shortcut or Start Menu
5. First launch: click "Provision Runtime" in the Repair tab to create the Python environment

The app auto-starts the coach when launched and connects to MTGA automatically.

### BepInEx Bridge (Required for Full Features)

The GRE bridge plugin enables direct game state access and autopilot. The Repair tab can install it for you:

1. Open the **Repair** tab
2. Click **Install BepInEx** (if not already installed)
3. Click **Install Plugin** to deploy MtgaCoachBridge.dll
4. Restart MTGA

## AI Backend

| Mode | Setup | Cost |
|------|-------|------|
| **Online** | Subscribe at [mtgacoach.com](https://mtgacoach.com/subscribe), enter key in settings | Subscription |
| **Local** | Install [Ollama](https://ollama.com) | Free |

Switch between modes using the **Online/Local** button in the app.

## App Controls

| Button | Key | Action |
|--------|-----|--------|
| Online/Local | | Switch AI backend |
| Model | | Cycle available models |
| Style | F2 | Toggle advice frequency (Concise/Verbose) |
| Screen | F3 | Analyze current screenshot via VLM |
| Mute | F5 | Toggle voice output |
| Voice | F6 | Cycle TTS voice (Adam, Michael, Heart, Bella, Nicole, Sarah, Sky) |
| Debug | F7 | Save bug report (JSON with replay, autopilot, bridge state) |
| Speed | F8 | Cycle TTS speed (1.0x / 1.2x / 1.4x) |
| Restart | | Restart the coaching engine |
| **Autopilot** | | |
| AP:OFF/ON | F12 | Toggle autopilot |
| AP Cancel | F1 | Cancel current autopilot plan |
| AP Abort | F4 | Abort autopilot execution immediately |
| AFK | F9 | Toggle AFK mode (auto-pass everything) |
| Land Only | F10 | Toggle land-only mode (auto-play lands) |
| Win Plan | | Read current win probability |

## Chat Commands

Type in the chat box at the bottom of the Coach tab:

| Command | Action |
|---------|--------|
| Any text | Ask the coach a question about the current game |

## Troubleshooting

- **Wrong player / advice is backwards** — Click Restart to re-detect seat
- **No voice output** — TTS models download automatically on first speak (~340MB). Wait for download.
- **Ollama connection refused** — Make sure Ollama is running: `ollama serve`
- **BepInEx / bridge plugin missing** — Open the Repair tab and use Install BepInEx + Install Plugin
- **Run diagnostics** — `python -m arenamcp.diagnose`
- **Copy debug logs** — Press F7 or click "Debug" to save a bug report with full game state, replay data, and autopilot diagnostics

## Development

For developers working from the repo:

```bash
# Install Python dependencies
python -m pip install -e .[dev,full]

# Run tests
pytest tests -q

# Build the WinUI debug exe
cd installer/MtgaCoachLauncher && dotnet build -c Debug -p:Platform=x64

# Run the debug exe (uses repo source directly)
installer/MtgaCoachLauncher/bin/x64/Debug/net8.0-windows10.0.19041.0/win-x64/MtgaCoachLauncher.exe

# Build the installer
cd installer/MtgaCoachLauncher && dotnet publish -c Release -p:Platform=x64 --self-contained
iscc installer/mtgacoach.iss
```

## License

MIT
