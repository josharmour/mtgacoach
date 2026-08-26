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

## Sister Repository: MageZero RL Training (`~/repos/magezero`)
`mtgacoach` is paired with **`magezero`**, an AlphaZero-style MCTS reinforcement learning engine running inside an instrumented XMage simulator on `blackwell` (`10.0.0.10`).
- **Role Split**: `mtgacoach` handles live MTG Arena coaching, GRE state extraction, and HUD overlay. `magezero` trains deck-specific policy/value neural networks via distributed self-play.
- **Canonical RL Docs**: For RL architecture, hardware topology (R9700 vs RTX 6000s), live training status, and strategy, refer directly to `~/repos/magezero/AGENTS.md` and `~/repos/magezero/docs/PLAN_OF_RECORD.md`.
- **Telemetry Command**: Inspect the live training run on blackwell via:
  ```bash
  ssh joshu@10.0.0.10 'python3 -c "import json, pathlib; runs=sorted(pathlib.Path(\"/home/joshu/repos/magezero/runs\").glob(\"*/run.json\")); d=json.loads(runs[-1].read_text()); print(f\"Run {runs[-1].parent.name} | Gen {d.get(\"current_gen\")} | Stage: {d.get(\"stage\")}\")"'
  ```

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

## Local venvs (macOS dev machine)
- `.venv_mac_311/` — Python 3.11, the working macOS venv (pip, pytest, editable arenamcp). Use this on mac.
- `.venv/` — Linux/WSL-era venv; pip shebang broken on macOS (points at old ArenaMCP path).
- Deleted 2026-07-22: `.venv_mac/`, `test_env/` (Python 3.9 — below requires-python >=3.10).

## Tooling (added 2026-07-22)
- **ruff** (lint + format) and **mypy** (advisory) — configs in `pyproject.toml`.
- **pre-commit** — `.pre-commit-config.yaml`; `pre-commit install` to enable.
- **CI** — `.github/workflows/tests.yml` runs ruff + pytest on push/PR (installer.yml unchanged).
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
