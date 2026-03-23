# mtgacoach — AI-Powered MTGA Coaching

Real-time AI coaching for Magic: The Gathering Arena. Watches your live games and delivers spoken strategic advice.

## Features

- **Real-time coaching** — AI sees your board, hand, life totals, and legal actions
- **Voice output** — Advice spoken aloud via Kokoro TTS so you can focus on the game
- **Voice input** — Push-to-talk to ask questions mid-game
- **Win plan detection** — Background analysis finds lethal lines and alerts you
- **Draft helper** — 17lands stats + composite scoring for draft picks
- **Autopilot** — AI plays for you when you're AFK
- **Local model support** — Run with Ollama or LM Studio for free, offline play

## Quick Start (Windows)

### Option 1: Installer

Download [**ArenaMCP-Setup.exe**](https://github.com/josharmour/mtgacoach/releases/latest/download/ArenaMCP-Setup.exe) from the latest release.

### Option 2: Manual

1. Install [Python 3.10+](https://python.org) (check "Add Python to PATH")
2. Double-click **`install.bat`** and follow the setup wizard
3. Double-click **`coach.bat`** to launch

See [INSTALL.md](INSTALL.md) for details.

## AI Backend

| Mode | Setup | Cost |
|------|-------|------|
| **Online** | Subscribe at [mtgacoach.com](https://mtgacoach.com/subscribe), enter key with `/key` | Subscription |
| **Local** | Install [Ollama](https://ollama.com), configure with `/local ollama` | Free |

Switch between modes anytime in the TUI.

## TUI Commands

| Command | Action |
|---------|--------|
| `/key LICENSE_KEY` | Set subscription license key |
| `/subscribe` | Open subscription page |
| `/online` | Switch to online mode |
| `/local` | Configure local model endpoint |
| `/update` | Check for updates |
| `/bugreport` | Submit a bug report |

## Hotkeys

| Key | Action |
|-----|--------|
| F2 | Toggle coaching style (concise/detailed) |
| F3 | Analyze screenshot |
| F4 | Push-to-talk voice input |
| F5 | Mute/unmute TTS |
| F6 | Cycle TTS voice |
| F8 | Swap seat (fix wrong player) |
| F10 | Cycle TTS speed |
| F12 | Cycle model |
| Ctrl+0 | Read win plan aloud |
| Ctrl+Q | Quit |

## Troubleshooting

- **Wrong player / advice is backwards** — Press F8 to swap seat
- **No voice output** — TTS models download automatically on first launch (~340MB)
- **Ollama connection refused** — Make sure Ollama is running: `ollama serve`
- **Run diagnostics** — `python -m arenamcp.diagnose`

## License

MIT
