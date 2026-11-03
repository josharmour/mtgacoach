# Install MTGA Coach

1. Download Coach for your platform from the [trial download page](https://mtgacoach.com/#get-started). Choose the Windows x64 installer, the Mac package for your processor, or the Linux x64 AppImage. These downloads follow tested master builds. To follow official releases instead, choose a [stable download](https://github.com/josharmour/mtgacoach/releases/tag/coach-stable-feed).
2. Enable **Options → Account → Detailed Logs (Plugin Support)** in Arena and restart Arena.
3. Open **MTGA Coach** from the new installation. Choose **Tools → Setup & Repair**. For a trial, verify your email with the code Coach sends. Subscribers can enter their existing key there.

The managed app includes Python and connects to the hosted coaching model through `https://api.mtgacoach.com/v1`. A standard installation needs no local model server or environment file. Arena’s game log supports manual coaching. Autoplay also requires the appropriate platform bridge.

- [Mac first-open instructions](README.md#if-macos-says-the-package-was-not-opened)
- [Migration from an older installation and automatic updates](RELEASE.md)
- [Developer installation from source](README.md#run-from-source)
