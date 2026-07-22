# ArenaMCP — Agent Guidance

## Project
Real-time AI coaching for MTG Arena with in-game overlay (BepInEx-driven ground-truth card positions).  
Python package `arenamcp` under `src/arenamcp/`, desktop PySide6 GUI at `src/arenamcp/desktop/`, BepInEx C# plugin at `bepinex-plugin/MtgaCoachBridge/`.

## Key Architecture
- **`launcher.py`** — internal TUI runtime launcher (subprocess-based, restarts on exit code 42)
- **`windows_integration.py`** — Windows install/repair helpers (stdlib-only)
- **`src/arenamcp/desktop/app.py`** — PySide6 desktop app entrypoint (main entry for users)
- **`src/arenamcp/server.py`** — FastMCP server bridging MTGA logs to LLM
- **`src/arenamcp/coach.py`** — Coach engine with pluggable LLM backends
- **`src/arenamcp/standalone.py`** — TUI/"standalone" client (voice + MCP client)
- **`src/arenamcp/autopilot.py`** — AI autoplay engine
- **`tests/`** — pytest test suite (600+ tests)

## Developer Commands
```bash
# Install (editable, with all extras)
pip install -e .[dev,full]

# Run tests (isolated log file via ARENAMCP_LOG_FILE env var)
pytest tests -q

# Run diagnostics
python -m arenamcp.diagnose

# Launch desktop app (Linux dev)
PYTHONPATH=src python -m arenamcp.desktop

# Launch standalone TUI
python -m arenamcp.standalone --backend online
```

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
- No linter/formatter config in `pyproject.toml` (only pytest dependency). No pre-commit, no typechecker configured.
- CI only builds Windows installer via GitHub Actions (`.github/workflows/installer.yml`).

## Packaging
- **`hatchling`** build backend, single package `src/arenamcp`
- GUI entrypoint: `mtgacoach-desktop = "arenamcp.desktop.app:main"`
- Installer: Inno Setup script at `installer/mtgacoach.iss`, built only on `windows-latest` runner (tag push `v*` or `workflow_dispatch`).

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
- `launch.bat` is the canonical Windows entrypoint for repo/manual installs.
- The coaching engine runs as a subprocess over a JSON pipe protocol (separate from the UI process).
