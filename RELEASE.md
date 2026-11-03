# Installing, updating, and releasing MTGA Coach

## Which download should I use?

| What you want | Where to get it | What you receive |
| --- | --- | --- |
| Play with the latest tested Coach code, including trial users and the maintainer | [Trial downloads](https://mtgacoach.com/#get-started) | The tested **master** channel checks for newer builds on launch. |
| Follow named releases such as 3.8.3 | [Stable downloads](https://github.com/josharmour/mtgacoach/releases/tag/coach-stable-feed) | The **stable** channel updates when a newer official release is qualified and packaged. |
| Edit and run local source | [Development setup](DEV.md) | Your Git checkout follows the revisions you choose locally. |

### First installation or migration

1. Finish the current match and close the old Coach. Choose a channel above.
2. On **Windows x64**, run that channel's Setup EXE. On **Mac**, choose the package for Apple Silicon or Intel. On **Linux x64**, download the AppImage, make it executable, and run it.
3. Start Coach from the new installation. The title bar identifies the channel, platform, package build, and Git commit. Existing local settings, trial credentials, and match history use the same per-user locations.

An older Inno, pip, or source installation needs this one-time migration to receive managed updates. Use the new shortcut on subsequent launches. The initial Windows and Mac distributions are unsigned.

## What happens after installation?

Coach checks its channel in the background each time it starts. Velopack stages a delta when available and automatically uses a full package when needed. Users continue playing with the version that started the match.

- **Compatible update.** After a completed match, Coach waits for the existing safe-restart checks, including post-match work. It then closes, applies the staged package, and reopens. A compatible update already staged at the next launch can apply then.
- **Runtime or Arena bridge change.** Coach offers **Restart to install Coach…** under **Help**. Finish your match, then approve the restart. Windows bridge deployment waits for Arena to be closed.
- **Offline or failed release.** The installed build can still open. A failed CI or native-package check leaves the previous published update available.

## Shipping changes from master

Submit changes through a pull request and wait for **pytest**, **ruff**, and **Hosted trials and Patreon keys** to pass before merging. The push to master runs the checks on the merged commit. A successful run starts **Managed desktop releases**, which builds and launches the packaged app for Windows x64, Linux x64, and both Mac architectures. Once every build passes, CI uploads the packages and then the update indexes. Master users pick up the build on launch. You do not need to create a version tag for this channel.

To see what is actually live, open the [master feed](https://github.com/josharmour/mtgacoach/releases/tag/coach-master-feed). The release body lists the published commit and package version. Compare the commit with the one in the Coach title bar. A green test run by itself means packaging can start.

## Cutting a stable GitHub release

Use the draft-first path when you want the installers attached before users see the release. The commands below use **v3.8.4 as an example**. Choose the next unused version.

1. Update `__version__` in `src/arenamcp/__init__.py` and write `docs/releases/v3.8.4.md`. Merge the release preparation after PR checks pass. Wait for the push tests on the exact master commit to pass.
2. Tag that tested commit and create a draft. Replace `TESTED_SHA` with its full commit ID.

   ```sh
   git tag -a v3.8.4 TESTED_SHA -m "MTGA Coach 3.8.4"
   git push origin refs/tags/v3.8.4
   gh release create v3.8.4 --verify-tag --draft --title "MTGA Coach 3.8.4" --notes-file docs/releases/v3.8.4.md
   ```

3. Build the stable packages through the **Managed desktop releases** workflow. Open the run URL printed by the command and wait for **publish** to pass.

   ```sh
   gh workflow run managed-desktop.yml --ref master -f lane=stable -f version=v3.8.4
   ```

4. Check the [stable feed](https://github.com/josharmour/mtgacoach/releases/tag/coach-stable-feed) for the expected version, commit, and all four platform downloads. Confirm the ordinary release has the Windows installer and Python wheel, then publish it.

   ```sh
   gh release view v3.8.4 --json tagName,assets
   gh release edit v3.8.4 --draft=false --latest
   ```

Publishing a stable GitHub release also invokes the managed workflow. It recognizes an already-published version and commit. The separate PyPI workflow publishes the Python package. The old Inno installer workflow is available through manual dispatch for recovery.

## Diagnosing an update

Check the installed channel and commit first. Then check the corresponding feed, its most recent **Managed desktop releases** run, and **Help** for an update awaiting approval. The host writes update errors under the OS local application-data directory in `mtgacoach/updates/<channel>/update.log`.

## Bug Reports

The desktop app now exposes a `Debug Report` action.

Behavior:

1. Saves a local JSON report
2. Tries GitHub issue creation via `MTGACOACH_GITHUB_TOKEN` or `GITHUB_TOKEN`
3. Falls back to `gh issue create` if GitHub CLI is available
4. Falls back again to opening a prefilled GitHub issue in the browser

Saved report path:

- `%USERPROFILE%\.arenamcp\bug_reports\bug_*.json`

Support policy:

- Do not ship your personal GitHub token inside the customer installer.
- One-click automatic issue creation is safe on your own support machines where `MTGACOACH_GITHUB_TOKEN` or `gh auth login` is configured.
- Customer installs should use the browser fallback unless you later add your own relay service for anonymous submissions.

The full local report stays on disk so you can request it from the reporter if the GitHub issue body is not enough.
