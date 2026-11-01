# mtgacoach — AI-Powered MTGA Coaching & Overlay

Real-time AI coaching for Magic: The Gathering Arena. MTGA Coach tracks your live games and provides strategic advice, draft guidance, spoken coaching or a two-person commentary booth, and an in-game HUD overlay.

---

## Features

- **Real-Time AI Coaching**: Tracks your board, hand, opponent cards, and legal moves to deliver turn-by-turn strategic advice.
- **In-Game HUD Overlay**: Transparent, click-through overlay sits directly on top of MTGA showing coach advice, draft ratings, and target highlights.
- **Draft Assistance**: Live 17Lands-powered card ratings, composite tier badges, color-pair suggestions, and clearer explanations of alternative picks.
- **Speech & Voices**: Choose Coach advice or two ArenaOnAir announcers, using Kokoro or an available Chatterbox runtime. Preview supported voices, choose a commentary style, and adjust speech speed (default **1.5×**).
- **Autoplay & Recent Match Replay**: Native Mac bridge automation can replay Arena's far-left Recent Match entry using its actual deck and game type. Fresh connection/state checks, request identity, and duplicate-queue guards protect reconnect and result transitions.
- **Grounded Gameplay Planning**: More precise handling of legal choices, commander restrictions and tax, legend-rule replacements, combat protection, mana reservations, optional costs, and action/explanation consistency.
- **Online AI Backend**: Hosted coaching uses the active backend via **`current`** through `api.mtgacoach.com`. Get your Patreon key at [mtgacoach.com/subscribe](https://mtgacoach.com/subscribe); the same key also works with [ArenaOnAir](https://arenaonair.com). Hosted clients discover `current` through authenticated `/v1/models`; LiteLLM selects the active backend. The gateway exposes only `current`; saved legacy aliases are migrated to it.

### What's new in 3.8.0

ArenaOnAir now shares the normal speech controls, with two distinct announcers and
Classic radio booth, Esports hype desk, Test-match understatement, or Pro Tour
analysts (technical) styles. Recent completed plays can receive retrospective
analysis even after the game advances; existing user-facing coaching explanations
can contribute without an additional model request. Opening commentary retries
interrupted delivery within its limits, and urgent gameplay retains priority.

This release also consolidates the October 31–November 1 fixes for selection and
scry transitions, stalled requests, reconnects, match results, combat forecasts,
commander decisions, equipment mana sequences, and draft explanation accuracy.
See [the 3.8.0 release notes](docs/releases/v3.8.0.md) for scope and remaining limits.

---

## Installation

### Standard Windows Installer
Download and run **`mtgacoach-Setup.exe`** from the [Latest Release](https://github.com/josharmour/mtgacoach/releases/latest).
The installer includes a self-contained Python runtime, so no separate virtual
environment or Python installation is required.

---

### Command-Line Install (`uv` / `pip`)

Install the Python package (`arenamcp`) from source on Windows, macOS, or Linux using [`uv`](https://docs.astral.sh/uv/) or `pip`:

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

Alternatively, use the Python wheel (`arenamcp-X.Y.Z-py3-none-any.whl`) attached to a [completed release](https://github.com/josharmour/mtgacoach/releases/latest): `uv tool install <wheel-url>`. Check the release assets and notes for the available installer and platform qualifications.

---

## First Run & Setup

1. Launch `mtgacoach-desktop`.
2. Open the **Repair** tab — it automatically verifies your MTGA installation, Python runtime, license key, and game logging setup.
3. Ensure **Detailed Logs (Plugin Support)** is enabled in MTGA's Account Options menu.

On native macOS Steam installs the coach works from `Player.log` alone. Autoplay
acts only through the experimental native macOS bridge library (no Wine or BepInEx):
open MTGA through the coach, or set MTGA's Steam launch option to load the library.
Autoplay waits for a fresh bridge handshake and a complete hand refresh before
planning or acting. Missing hand data is shown as incomplete, not an empty or
one-card hand. Log-only manual coaching remains available with Autoplay off.

The native bridge must be updated together with Coach: `mac-il2cpp-0.3.2` adds
process identity to the handshake. An older library is reported as needing an
update and cannot drive automation. If Steam relaunches Arena without the bridge,
finish any active match manually, then close Arena and reopen it through Coach.
Coach does not reinject into a running game or change Steam launch settings.
Readiness pauses recover after a valid connection/state refresh; an explicit Stop
or manual pause is retained. Commands with an uncertain result are never replayed
onto a reconnected process.

## Speech setup

Open **Speech & voices** to turn speech on, choose **Coach advice** or **Announcers**,
and select **Kokoro** or **Chatterbox**. Use **Server Chatterbox** when an available
server should render the audio; local Chatterbox requires its supported runtime.
Use the voice preview and readiness status to check your chosen path. Announcers
use separate play-by-play and analyst roles; the style changes the booth's delivery.
Only one speech mode owns playback at a time.

## Automation limits

Autoplay and Auto Queue are explicit opt-ins. The native Mac bridge can operate
in a locked session where its in-process game actions are available; it does not
unlock macOS or bypass foreground safeguards for desktop input. If a required
step needs the desktop, the app must report that limitation. Recent Match replay
follows the displayed entry rather than a hard-coded deck or format.

Planning and commentary are bounded approximations, not a complete Magic rules
engine or a guarantee of optimal play. Some card combinations and future action
sequences remain unsupported. Traditional Limited sideboarding has a request-bound
automatic path; constructed sideboarding remains manual. Windows installer builds
and platform-specific live behavior have separate validation status in each release.

---

## License

MIT License
