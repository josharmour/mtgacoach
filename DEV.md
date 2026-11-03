# Develop MTGA Coach

## Run from a checkout

Use Python 3.10 or newer. In a fresh checkout, the following creates an editable development environment, including the dependencies for the website tests. Run the commands from the repository root.

```sh
uv venv --python 3.11
uv pip install --python .venv -e ".[dev,full]" -r website/requirements.txt
uv run --no-sync mtgacoach-desktop
```

On the maintainer’s existing Mac checkout, use the working `.venv_mac_311` environment as described in [AGENTS.md](AGENTS.md). Python edits take effect on the next launch. Rebuild the C# plugin when you change its source.

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
