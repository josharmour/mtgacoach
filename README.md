# mtgacoach — AI-Powered MTGA Coaching & Overlay

Real-time AI coaching for Magic: The Gathering Arena. MTGA Coach tracks your live games and provides real-time strategic advice, draft guidance, voice coaching, and an in-game HUD overlay directly on top of MTGA.

---

## Features

- **Real-Time AI Coaching**: Tracks your board, hand, opponent cards, and legal moves to deliver turn-by-turn strategic advice.
- **In-Game HUD Overlay**: Transparent, click-through overlay sits directly on top of MTGA showing coach advice, draft ratings, and target highlights.
- **Draft Assistance**: Live 17Lands-powered card ratings, composite tier badges, and optimal color-pair recommendations during drafts.
- **Voice Coaching**: Spoken advice with neural Text-to-Speech (TTS) and hands-free Push-to-Talk voice questions.
- **Flexible AI Backends**: Runs online via `api.mtgacoach.com` or 100% locally and offline using Ollama or LM Studio.

---

## Installation

### Standard Windows Installer
Download and run **`mtgacoach-Setup.exe`** from the [Latest Release](https://github.com/josharmour/mtgacoach/releases/latest).

---

### Command-Line Install (`uv` / `pip`)

Install on Windows, macOS, or Linux using [`uv`](https://docs.astral.sh/uv/):

```bash
# 1. Install uv (if not already installed):
#    Windows (PowerShell): powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#    macOS / Linux:       curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Install mtgacoach:
uv tool install mtgacoach

# 3. Launch the app:
mtgacoach-desktop
```

---

## First Run & Setup

1. Launch `mtgacoach-desktop`.
2. Open the **Repair** tab — it automatically verifies your MTGA installation, Python runtime, license key, and game logging setup.
3. Ensure **Detailed Logs (Plugin Support)** is enabled in MTGA's Account Options menu.

---

## License

MIT License
