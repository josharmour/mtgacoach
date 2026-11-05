# Develop MTGA Coach

## Run from a checkout

Use Python 3.11 for the full setup below, including generated ArenaOnAir commentary (the base package supports Python 3.10 or newer). In a fresh checkout, the following creates an editable development environment, including the dependencies for the website tests. Run the commands from the repository root.

```sh
uv venv --python 3.11
uv pip install --python .venv -e ".[dev,full]" -r website/requirements.txt
uv run --no-sync mtgacoach-desktop
```

Managed downloads bundle Python, the commentary core, and platform bridge assets. A source checkout uses the Python environment and native bridge you provision. Install the pinned [ArenaOnAir core](docs/commentary.md#runtime-requirement) in the commentary worker’s environment to enable generated analysis; selecting hosted Chatterbox does not require local Torch or speech-model weights.

On the maintainer’s Mac, keep the source checkout on the network share and Python and its dependencies on the local disk. Python edits take effect on the next launch. Rebuild the [C# plugin](bepinex-plugin/README.md) when you change its source. Native Mac source users build the [IL2CPP bridge](spikes/mac-il2cpp/build.sh) separately; the managed Mac installer bundles it. Stage bridge changes with Arena closed and launch a new Arena process to load them.

Dev windows have a **Copy commit** button in the status bar. It copies the full source commit captured at Coach startup, even if the shared checkout changes afterward. Use **Restart Coach** after an iterative source update to load and identify the new revision.

### Quick Mac iteration

Use **MTGA Coach (Dev)** for local feedback instead of waiting for a public release. Create its launcher with a source checkout and an existing working environment:

```sh
COACH_DEV_ENV="$HOME/Library/Application Support/MTGA Coach Dev/venv"
# Create/install once, or refresh dependencies when they change:
uv venv --python 3.11 "$COACH_DEV_ENV"
uv pip install --python "$COACH_DEV_ENV/bin/python" -e ".[dev,full]" -r website/requirements.txt
python3 scripts/install_mac_dev_launcher.py --checkout "$PWD" --python "$COACH_DEV_ENV/bin/python"
```

The app is installed in `~/Applications/MTGA Coach (Dev).app`. Quit the current Coach after the match, then open Dev. It reads Python directly from the selected checkout and shows `Dev (<commit>)` in its title. Relaunch after Python edits; no packaging or GitHub runner is involved. It uses the existing Coach settings and does not fetch Git changes or replace the managed application.

The maintainer's dedicated Dev checkout is `/Volumes/repos/mtgacoach-dev` (also `~/repos/mtgacoach-dev`). Its local environment is `~/Library/Application Support/MTGA Coach Dev/venv`. Before asking for a local test, put the tested source changes in that checkout and verify its commit and imports. Run checks against a different worktree with `PYTHONPATH=src "$COACH_DEV_ENV/bin/python" -m pytest ...` from that worktree. Keep native bridge/runtime qualification separate from Python iteration. The managed release remains the installation and update path for other users.

For stable local screen-capture identity, install the launcher with `--sign-identity "Apple Development: ..."` using your existing development certificate. The native launcher keeps that identity while the external Python source changes. macOS still requires a separate Screen Recording grant for Dev. Release distributions require their own distribution signing setup.

The maintainer tests changes in Dev first. Do not merge or publish follow-up changes until explicitly asked to **release to master** or **release to stable**. Master release publishes to master users; a named stable release publishes to stable users. Creating a reviewable PR and running its checks does not publish an update.

### Quick Windows iteration

Keep Python and dependencies on the Windows disk and use the shared source checkout:

```powershell
py -3.11 Z:\mtgacoach-dev\scripts\install_windows_dev_launcher.py --checkout Z:\mtgacoach-dev --create-venv --git-dir Z:\mtgacoach\.git\worktrees\mtgacoach-dev
```

This creates `%LOCALAPPDATA%\MTGACoachDev\venv` and a **MTGA Coach (Dev)** desktop shortcut. The existing master shortcut stays available. Setup installs dependencies once; normal launches read source directly without installing packages or waiting for a release. Re-run setup when dependencies change. For an existing local environment, use `--python C:\path\to\venv\Scripts\python.exe` instead of `--create-venv`.

The `--git-dir` option is only needed for a worktree created on another operating system whose `.git` file contains a path Windows cannot resolve. A regular clone needs no override or Git executable for its Dev title. Run setup in your normal desktop session with the share connected, and keep that share connected when using Dev.

**Restart Coach** reloads the shared Python source using the same local environment. Dev uses the existing Coach settings; quit the other Coach before opening it. The maintainer's Windows `Z:\mtgacoach-dev` and Mac `/Volumes/repos/mtgacoach-dev` refer to the same checkout. Launcher configuration and startup errors are in `%LOCALAPPDATA%\MTGACoachDev`.

## Find the code

| Feature | Entry point |
| --- | --- |
| Current-decision UI and forecast display | [User guide](docs/using-coach.md), [strategy view](src/arenamcp/desktop/strategy_view.py), and [presentation model](src/arenamcp/desktop/strategy_model.py) |
| Desktop UI and setup | [PySide app](src/arenamcp/desktop/app.py) and [Setup & Repair](src/arenamcp/desktop/repair_tab.py) |
| Coaching and model calls | [Coach](src/arenamcp/coach.py), [backend factory](src/arenamcp/coach_backends.py), [hosted client](src/arenamcp/backends/proxy.py) |
| Arena state, MCP tools and Autoplay | [Server](src/arenamcp/server.py), [autopilot](src/arenamcp/autopilot.py), [bridge guide](bepinex-plugin/README.md) |
| Trials and subscriptions | [Website setup](website/SETUP.md) |
| Feature evidence and open bugs | [Development docs](docs/README.md) |

The UI runs the coaching engine as a subprocess over a JSON pipe. Desktop model calls use `current` at `https://api.mtgacoach.com/v1`. To test a compatible gateway deliberately, set `ARENAMCP_GATEWAY_URL` in the process environment or use the saved `gateway_url` setting. Clear both overrides to use the public endpoint.

## Verify and ship changes

Run a focused regression test while working, then check formatting and the test suite before merging.

```sh
uv run --no-sync python -m pytest tests/test_desktop_runtime.py -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync python -m pytest tests -q
```

GitHub runs lint, hosted trial checks, and pytest. The combat timing regressions run in their own process. For customer builds, follow the [managed release workflow](RELEASE.md). A successful test run qualifies a commit for packaging; the published feed identifies the build users can install.
