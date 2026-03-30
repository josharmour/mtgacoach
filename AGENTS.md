# Repository Guidelines

## Project Structure & Module Organization

Core Python code lives in `src/arenamcp/`. Key runtime modules include `server.py` and `standalone.py` for the in-process MCP app, `gamestate.py` and `parser.py` for log/GRE state assembly, `gre_bridge.py` and `autopilot.py` for direct bridge execution, and `tui.py` for the current operator UI. Tests live in `tests/`. Windows launcher and install surfaces live at the repo root (`launch.bat`, `launch.vbs`, `launcher.py`, `launcher_gui.py`, `setup_wizard.py`, `windows_integration.py`) and in `installer/`. The MTGA bridge plugin is a separate .NET project in `bepinex-plugin/MtgaCoachBridge/`.

## Build, Test, and Development Commands

- `python -m pip install -e .[dev,full]`: install the Python package with test and full runtime extras.
- `pytest tests -q`: run the Python regression suite.
- `pytest tests/test_bridge_prompt_enrichment.py -q`: run a targeted bridge-related test.
- `python -m arenamcp.standalone`: start the coach directly in a dev shell.
- `python -m arenamcp.diagnose`: run local environment diagnostics.
- `cd bepinex-plugin/MtgaCoachBridge && dotnet build -c Release -p:MtgaDir="C:\Program Files\Wizards of the Coast\MTGA"`: build the BepInEx plugin DLL.

## Coding Style & Naming Conventions

Use 4-space indentation in Python and keep type hints on public functions where practical. Follow existing Python naming: `snake_case` for functions/modules, `PascalCase` for classes, and concise internal helper names prefixed with `_`. Keep JSON-like state keys stable; downstream code depends on exact names such as `pending_decision`, `decision_context`, and `local_seat_id`. In C#, follow the existing plugin style: `PascalCase` methods, private `_camelCase` fields.

## Testing Guidelines

Use `pytest` for Python changes. Add or update focused regression tests in `tests/test_*.py` whenever you touch bridge serialization, game-state normalization, autopilot planning, or launcher behavior. There is no formal coverage gate, but state-pipeline fixes should include a reproducer-oriented test.

## Commit & Pull Request Guidelines

Recent history uses short imperative subjects with prefixes like `fix:`, `feat:`, `debug:`, plus versioned release commits such as `v1.6.1: expand GRE bridge state and decision handling`. Keep commits scoped and explain the subsystem touched. PRs should include: a concise summary, validation steps (`pytest`, plugin build, launcher/manual checks as applicable), linked bug reports/issues, and screenshots for launcher/TUI changes.

## Security & Configuration Tips

Do not commit API keys, `%LOCALAPPDATA%\mtgacoach` runtime data, `.arenamcp` user settings, MTGA logs, or copied game binaries. Treat `bin/`, `obj/`, `dist/`, and `__pycache__/` as generated artifacts unless a release task explicitly requires them.
