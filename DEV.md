# Develop MTGA Coach

## Run from a checkout

Use Python 3.10 or newer. In a fresh checkout, the following creates an editable development environment, including the dependencies for the website tests. Run the commands from the repository root.

```sh
uv venv --python 3.11
uv pip install --python .venv -e ".[dev,full]" -r website/requirements.txt
uv run --no-sync mtgacoach-desktop
```

On the maintainer’s Mac, keep the source checkout on the network share and Python and its dependencies on the local disk. Python edits take effect on the next launch. Rebuild the C# plugin when you change its source.

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

## Find the code

| Feature | Entry point |
| --- | --- |
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
