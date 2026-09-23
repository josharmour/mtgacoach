# Native Mac autoplay

Native Steam Arena now has a screenshot-and-input execution path. It does not
use CrossOver, Wine, Rosetta, or the BepInEx plugin. The Python process reads
`Player.log`, sends the Arena window screenshot and game state to an image-capable
model, and posts one mouse or keyboard action through macOS Quartz. It observes
the screen again before deciding the next input, including UI choices that do
not immediately produce a log message.

This is an experimental implementation, not yet verified through a complete
match on Mac hardware. Automated tests exercise its routing, coordinate mapping,
stop behavior, stale-screen rejection, and multi-step UI continuation. They do
not establish model accuracy or in-game compatibility. The older bridge-only
policy in the historical platform-parity notes is superseded for native Mac
execution; the existing GRE bridge remains the executor for Windows/Proton.

## Setup

1. Install the updated checkout using the Python environment that runs the app:

   ```bash
   .venv_mac_311/bin/python -m pip install -e .
   ```

   macOS installs now include `pyobjc-framework-Cocoa` and
   `pyobjc-framework-Quartz`, and `pyobjc-framework-ApplicationServices`.
   These run natively on Apple Silicon.

2. Run the native Steam version of Arena. Enable **Detailed Logs (Plugin
   Support)** in Arena's Account settings. That option enables logging; no
   plugin needs to be installed.

3. In the coach's **Tools → Autoplay Vision Model**, enter an image-capable model
   ID available through the configured endpoint. Leaving this blank uses the
   coach's model, which must itself support images. Restart the coach after
   changing the model. This uses the existing endpoint/authentication and sends
   only the captured Arena window, along with the parsed game state. A text-only
   default model cannot run this executor.

   For a direct LiteLLM connection, set `autopilot_vision_url` to its OpenAI-compatible
   base URL (for example `http://10.0.0.10:8444/v1`) and `autopilot_vision_api_key` to
   a key restricted to the required model. `autopilot_vision_model` selects the
   gateway alias, such as `glm-5.3-flash`. These overrides apply only to native
   autoplay; they do not change the voice coach's backend. Do not put a LiteLLM
   administrator key in the client settings.

4. Open **Tools → Autoplay Permissions**. macOS may prompt for **Accessibility** and **Screen
   Recording** access. Grant these to the actual Python/app used by the coach
   under **System Settings → Privacy & Security**, then restart the coach.
   Launching from a terminal may attribute permission to the terminal application.
   Permission belongs to the launching process; an SSH session does not prove
   the desktop app has permission.

5. Start a match yourself, enable **AP**, and bring Arena to the foreground.
   Autoplay waits while another application is focused. Use the AP toggle,
   **Tools → Stop Autoplay**, or the global **F11** hotkey to stop it (depending on
   keyboard settings, hold Fn to send F11).

Windowed mode is supported; fullscreen and a fixed game resolution are not
required. Other apps may remain open, but do not interact with them while
autoplay is running. Keep Arena in front, avoid covering its input targets,
and avoid resizing or hovering over cards during a model request. A resize or
changed hover layout causes the pending action to be discarded and observed
again rather than clicked at outdated coordinates.

The engine is selected automatically when running on macOS against a native
installation. The status report names it `native-mac-desktop`. A saved path
pointing at a Wine bottle must be removed or changed to the native Steam install.

## Behavior and limits

- The visual action vocabulary includes click, double-click, hover, drag, scroll,
  game keys, and numeric input. It can express casting, targeting, combat, modal
  selection, search, ordering, and X-value UI interactions. Individual cards and
  dialogs still require live coverage testing.
  The model is instructed to prefer double-clicking cards in hand to play them,
  then observe and handle targeting/payment separately. Drag remains available
  for blockers and selection piles.
- Capture is limited to the Arena window using macOS `screencapture`; normalized
  screenshot coordinates map to Quartz screen points, including Retina displays
  and negative monitor origins.
- A response is discarded if the game state, window geometry, or relevant screen
  region changes while the model is thinking. Each input checks foreground
  ownership without relying on AppKit's run-loop-cached foreground state;
  mouse input uses Accessibility hit-testing to reject covering applications
  while allowing click-through overlays. Key/mouse releases
  are sent even when a drag or press is interrupted.
- Repeated inputs without log progress are bounded. Low-confidence or malformed
  model responses pause automation and explain the problem. Toggle off/on after
  resolving it. These checks do not guarantee correct card selection.
- This plays an already-open match. Automatic queueing, deck selection, drafting,
  purchases, and conceding are outside this implementation. It waits on the
  home/queue screens. It does not claim arbitrary unattended full-game coverage.
- Latency and accuracy depend on the selected vision model. A request has a
  15-second maximum configured timeout, and old visual decisions are discarded.
  Transient request failures retry with fresh screenshots after a short delay;
  three consecutive failures pause autoplay. Turning autoplay off/on resets
  that failure state after the endpoint recovers.
- Diagnostics count **inputs sent**, not verified successful game actions.
  A later screenshot and log snapshot inform the next step; a posted event alone
  is not proof that Arena accepted it.
- Bug reports include the last model screenshot as `screenshots.autoplay`,
  together with window bounds, image size, the proposed action and last notice.
  This distinguishes model grounding errors from desktop coordinate problems.

## Development checks

```bash
python -m pytest -q tests/test_native_mac_autopilot.py tests/test_standalone_native_autopilot.py
```

Live acceptance testing must cover permission denial, windowed/fullscreen Arena,
Retina scaling, hover-expanded hands, mulligan/bottoming, cast/target/resolve,
attack/block selection, search/modal/X dialogs, focus loss, and stopping while an
image request is in flight. Start with a practice match before assessing broader
coverage. Use the desktop app's actual runtime for these checks.
