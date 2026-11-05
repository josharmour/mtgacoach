# ArenaMCP — Agent Guidance

## Project
Real-time desktop coaching for MTG Arena. Coach reads Arena’s game log; autoplay also uses the platform bridge.

Python package `arenamcp` under `src/arenamcp/`, desktop PySide6 GUI at `src/arenamcp/desktop/`, BepInEx C# plugin at `bepinex-plugin/MtgaCoachBridge/`.

## Key Architecture
- **`launcher.py`** — internal TUI runtime launcher (subprocess-based, restarts on exit code 42)
- **`windows_integration.py`** — Windows install/repair helpers (stdlib-only)
- **`src/arenamcp/desktop/app.py`** — PySide6 desktop app entrypoint (main entry for users)
- **`src/arenamcp/server.py`** — FastMCP server bridging MTGA logs to LLM
- **`src/arenamcp/coach.py`** — Coach engine using the hosted online backend
- **`src/arenamcp/standalone.py`** — TUI/"standalone" client (voice + MCP client)
- **`src/arenamcp/autopilot.py`** — AI autoplay engine
- **`tests/`** — pytest regression suite

## Developer Commands
```bash
# Install (editable, with all extras)
python -m pip install -e ".[dev,full]" -r website/requirements.txt

# Run tests (isolated log file via ARENAMCP_LOG_FILE env var)
pytest tests -q

# Run diagnostics
python -m arenamcp.diagnose

# Launch desktop app (Linux dev)
PYTHONPATH=src python -m arenamcp.desktop

# Launch standalone TUI
python -m arenamcp.standalone --backend online
```

## Local venvs (macOS dev machine)
- `~/Library/Application Support/MTGA Coach Dev/venv/` — local Python 3.11 and dependencies for macOS development. Source remains on `/Volumes/repos/mtgacoach-dev`; use `PYTHONPATH=src` when testing another worktree.
- `.venv_mac_311/` — previous network-share environment, retained for recovery; use the local environment above for Dev.
- `.venv/` — Linux/WSL-era venv; pip shebang broken on macOS (points at old ArenaMCP path).

## Tooling
- **ruff** (lint + format) and **mypy** (advisory) — configs in `pyproject.toml`.
- **pre-commit** — `.pre-commit-config.yaml`; `pre-commit install` to enable.
- **CI** — `.github/workflows/tests.yml` runs ruff + pytest on push/PR.
- Version single-sourced in `src/arenamcp/__init__.py` (`__version__`); pyproject reads it via hatch dynamic versioning.

## BepInEx C# Plugin (Linux dev)
- **Do NOT** use Wine's `csc` — too old for C# 9.0.
- Use `dotnet` (`.NET 10 SDK` installed).
- Override `MtgaDir` for Flatpak Steam:
  ```bash
  cd bepinex-plugin/MtgaCoachBridge
  dotnet build -p:MtgaDir="/home/joshu/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/MTGA"
  ```
- Deploy: copy `bin/Debug/net472/MtgaCoachBridge.dll` to MTGA's `BepInEx/plugins/`.

## Testing Quirks
- `conftest.py` redirects `ARENAMCP_LOG_FILE` to `/tmp/arenamcp-pytest.log` so test noise doesn't pollute `~/.arenamcp/standalone.log`.
- Run the focused regression for the feature, then use the lint and test commands in [DEV.md](DEV.md).
- GitHub runs lint, hosted trial checks, and pytest. Successful master tests qualify a build for managed packaging.

## Packaging
- `hatchling` builds the `arenamcp` Python package. The GUI entrypoint is `mtgacoach-desktop`.
- Velopack distributes the managed desktop for Windows, Linux, and Mac. See [RELEASE.md](RELEASE.md) for both channels and the GitHub workflow.
- The old Inno workflow is manual recovery for earlier installations.

## Log Locations (Windows)
| Log | Path |
|-----|------|
| Desktop UI | `%LOCALAPPDATA%\mtgacoach\desktop.log` |
| Launcher | `%LOCALAPPDATA%\mtgacoach\desktop-launch.log` |
| Coach runtime | `%USERPROFILE%\.arenamcp\standalone.log` |
| Bug reports | `%USERPROFILE%\.arenamcp\bug_reports\bug_*.json` |
| BepInEx | `<MTGA>\BepInEx\LogOutput.log` |

## Development Flow
- Edits under `src/arenamcp/` take effect on next launch (no C# rebuild needed).
- Use [DEV.md](DEV.md) for source development and [INSTALL.md](INSTALL.md) for managed desktop installs.
- The coaching engine runs as a subprocess over a JSON pipe protocol (separate from the UI process).
