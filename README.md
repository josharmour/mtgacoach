# mtgacoach — AI-Powered MTGA Coaching & Overlay

Real-time AI coaching for Magic: The Gathering Arena. MTGA Coach tracks your live games and provides real-time strategic advice, draft guidance, voice coaching, and an in-game HUD overlay directly on top of MTGA.

---

## Features

- **Real-Time AI Coaching**: Tracks your board, hand, opponent cards, and legal moves to deliver turn-by-turn strategic advice.
- **In-Game HUD Overlay**: Transparent, click-through overlay sits directly on top of MTGA showing coach advice, draft ratings, and target highlights.
- **Draft Assistance**: Live 17Lands-powered card ratings, composite tier badges, and optimal color-pair recommendations during drafts.
- **Voice Coaching**: Spoken advice with neural Text-to-Speech (TTS) and hands-free Push-to-Talk voice questions.
- **Online AI Backend**: All coaching runs through the `api.mtgacoach.com` gateway — a license key from [mtgacoach.com](https://mtgacoach.com) is required.

---

## Installation

### Standard Windows Installer
Download and run **`mtgacoach-Setup.exe`** from the [Latest Release](https://github.com/josharmour/mtgacoach/releases/latest).

---

### Command-Line Install (`uv` / `pip`)

The package (`arenamcp`) is not published on PyPI — install from source on Windows, macOS, or Linux using [`uv`](https://docs.astral.sh/uv/) or `pip`:

```bash
# 1. Install uv (if not already installed):
#    Windows (PowerShell): powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
#    macOS / Linux:       curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. Clone and install mtgacoach from source:
git clone https://github.com/josharmour/mtgacoach.git
cd mtgacoach
uv venv
uv pip install -e ".[full]"
# (or with plain pip: python -m pip install -e ".[full]")

# 3. Launch the app:
uv run mtgacoach-desktop
```

Alternatively, each [release](https://github.com/josharmour/mtgacoach/releases/latest) attaches a Python wheel (`arenamcp-X.Y.Z-py3-none-any.whl`) that can be installed directly with `uv tool install <wheel-url>`.

---

## First Run & Setup

1. Launch `mtgacoach-desktop`.
2. Open the **Repair** tab — it automatically verifies your MTGA installation, Python runtime, license key, and game logging setup.
3. Ensure **Detailed Logs (Plugin Support)** is enabled in MTGA's Account Options menu.

---

## License

MIT License
