# MTGA Coach

MTGA Coach is a desktop companion for **Magic: The Gathering Arena**. It reads Arena’s game log, suggests plays when you have priority, and can speak its advice while you play. It also helps with draft picks and prepares a playbook for your deck before a match.

## Install Coach

**New trial users should use the [desktop download page](https://mtgacoach.com/#get-started).** These downloads follow tested builds from `master`. Choose Windows x64 Setup, the Apple Silicon Mac package, or the Linux x64 AppImage. On Linux, make the AppImage executable before opening it.

If you prefer **stable**, install the package for your platform from the stable feed. The Windows installer attached to release 3.8.3 also follows stable. The initial Windows and Mac packages are unsigned. [Stable downloads](https://github.com/josharmour/mtgacoach/releases/tag/coach-stable-feed) · [Latest official release](https://github.com/josharmour/mtgacoach/releases/latest)

### If macOS says the package was “Not Opened”

The initial 3.8.3 Mac installers are unsigned and unnotarized. Gatekeeper can block the first open with a message that Apple could not verify the package. To verify your download, run `shasum -a 256` on the downloaded `.pkg` and compare it with the SHA-256 digest for that exact asset on the GitHub release. If the digests match and you trust the package, click **Done**, open **System Settings → Privacy & Security**, scroll to **Security**, and choose **Open Anyway** for that package. [Tested-master release assets](https://github.com/josharmour/mtgacoach/releases/tag/coach-master-feed) · [Apple’s instructions](https://support.apple.com/en-us/102445)

### First run

1. In Arena, open **Options → Account**, turn on **Detailed Logs (Plugin Support)**, and restart Arena.
2. Open the newly installed **MTGA Coach** app. Choose **Tools → Setup & Repair**. This checks the Arena installation, logging, and license. Use **Check & Repair** to run the checks again.
3. For a seven-day trial, enter your email and the verification code sent to it. If you already have a subscriber key, enter it in Setup & Repair. [Subscriber key and account access](https://mtgacoach.com/subscribe)

Coach uses the hosted model `current` at `https://api.mtgacoach.com/v1`. Trial and subscriber installations need no Ollama server, separate Python installation, or environment file.

### How updates work

Coach checks its channel at launch and downloads an update in the background. A compatible update can apply after the match and its post-match work finish, or on the next launch. For a runtime or Arena bridge change, click **Update** beside **Restart Coach** after your match. macOS may ask for permission to replace the app. The title bar shows the channel, package build, and Git commit.

If you used the older Windows installer, pip, or a Git checkout, close the old copy and install the managed app once. Start it from the new shortcut. Coach uses your existing per-user settings and trial credentials. [Installation, update, and release guide](RELEASE.md) · [3.8.3 release notes](docs/releases/v3.8.3.md)

## Coaching, speech, and autoplay

- **Live coaching** recommends plays using the board, your hand, and the choices Arena offers. A cached deck playbook gives Coach a starting point. During a match, it revises the plan as threats change while tactical advice can proceed.
- **Draft assistance** uses 17Lands ratings and color-pair information to explain picks and alternatives.
- **Speech & voices** lets you choose Coach advice or two ArenaOnAir announcers. Preview voices and select Kokoro or an available Chatterbox runtime in the desktop app.
- **Autoplay and Auto Queue** are opt-ins. Manual coaching reads Arena’s log. Autoplay also needs a working platform bridge. Windows and Linux/Proton use BepInEx. Native Mac autoplay uses an experimental bridge that you build separately, then load when starting Arena. [Platform setup](https://mtgacoach.com/#get-started) · [Mac bridge build script](spikes/mac-il2cpp/build.sh)

Coach can miss card interactions or suggest a suboptimal line. You can keep autoplay off and use spoken advice while making the decisions yourself.

## Run from source

The Python package includes the desktop app and an MCP server that exposes Arena game state to MCP clients. For development, use Python 3.10 or newer. The following example creates a Python 3.11 environment in a fresh checkout using `uv`.

```sh
git clone https://github.com/josharmour/mtgacoach.git
cd mtgacoach
uv venv --python 3.11
uv pip install --python .venv -e ".[full]"
uv run --no-sync mtgacoach-desktop
```

See the [development workflow](DEV.md) for tests and the [feature documentation index](docs/README.md) for subsystem guides and active work. A source checkout follows the Git revision you select. Python package users can also install the `arenamcp` wheel attached to a numbered release and launch `mtgacoach-desktop`. For desktop updates through Velopack, use a managed download above.

## License

The `arenamcp` package declares the MIT license.
