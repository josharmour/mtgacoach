# Install MTGA Coach

## Download and sign in

1. Download Coach from the [download page](https://mtgacoach.com/#get-started): **Windows x64 Setup**, **Apple Silicon Mac package**, or **Linux x64 AppImage**. These downloads follow tested master builds. To follow promoted releases instead, choose a [stable download](https://github.com/josharmour/mtgacoach/releases/tag/coach-stable-feed). Intel Mac is not supported by the managed 4.0 packages.
2. In Arena, enable **Options → Account → Detailed Logs (Plugin Support)**, then restart Arena.
3. Open the newly installed **MTGA Coach** and choose **Tools → Setup & Repair**. For a seven-day trial, verify your email with the code Coach sends. Subscribers can enter their existing key there.
4. Keep Coach beside Arena. Start with manual coaching, or enable autoplay once the bridge is connected. [Read the new screen and choose your voices](docs/using-coach.md).

The managed app includes Python, the ArenaOnAir commentary core, and platform bridge assets. Coaching uses the hosted service at `https://api.mtgacoach.com/v1`. You do not need to install Python, Ollama, Torch, or Chatterbox model weights for the standard app. Hosted Chatterbox uses your Coach sign-in; select it in **Speech & voices** and use **Preview** to check the voice or pair.

## Platform setup

### Windows x64

Run the Setup EXE, then open the Coach shortcut with the blue dragon icon. Coach can deploy its bundled BepInEx loader and bridge plugin while Arena is closed. It starts Arena when the game is closed; the Wizards launcher may need time to patch the game first. If setup reports a missing or outdated bridge, finish your match, close Arena, and run **Tools → Setup & Repair → Check & Repair** before reopening Arena.

### Apple Silicon Mac

Run the Mac package. New installs go into your account’s Applications folder (`~/Applications`); launch that copy. Existing shared copies in `/Applications` can ask for macOS authorization when replacing the app. See the [Mac first-open instructions](README.md#if-macos-says-the-package-was-not-opened) if Gatekeeper blocks the unsigned package.

The managed app includes the native Arena bridge. With Arena closed, Coach stages the bundled library and launches the detected native Arena app with the bridge, starting Steam first. You do not need to compile the bridge for a managed installation.

Coach leaves an already-running Arena session open. If Arena was started without the bridge, finish your match, close Arena, and reopen it through Coach to enable autoplay. A bridge update likewise takes effect on a new Arena launch; restarting Coach alone cannot replace a bridge loaded into a running game. Manual coaching can still use the game log.

### Linux x64 / Proton

Make the AppImage executable and open it. In **Tools → Setup & Repair**, check Arena’s location, logging, the bundled BepInEx loader, and the bridge plugin. Close Arena before installing or replacing bridge files.

For Steam/Proton, Arena’s **Properties → Launch Options** must include:

```sh
WINEDLLOVERRIDES="winhttp=n,b" %command%
```

This lets the BepInEx loader run inside the Windows game under Proton. Start Arena after repair and check that the bridge connects before enabling autoplay.

## Updates and older installations

Master and stable both use the **4.0.x** series. Stable promotion retains the version and commit of the qualified master build; the channels differ in when new builds are offered. Existing managed 1.0.x master and 3.8.x stable clients can update to the newer series normally.

Coach checks for updates at launch. Compatible updates can apply between matches after post-match work finishes. For a runtime or bridge update, finish your match and use **Update** in the fixed footer. Close Arena when bridge deployment requires it. [Update details](RELEASE.md#what-happens-after-installation).

Older Inno, pip, or Git installations need a one-time managed install: finish your match, close the old Coach, install the chosen package, and use its new shortcut. Settings, trial credentials, and history stay in their existing per-user locations. A source checkout continues to follow Git rather than the managed updater.

For help with installation, access, or updates, email [support@mtgacoach.com](mailto:support@mtgacoach.com). Include your platform and the version shown in Coach’s title bar.

- [Using Coach during a match](docs/using-coach.md)
- [Update troubleshooting and release process](RELEASE.md)
- [Developer installation and local Python launchers](DEV.md)
