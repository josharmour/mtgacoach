# Release Workflow

`v2.0.0` is the start of the PySide desktop app release line.

## Development

Use the repo-backed launcher described in [DEV.md](/home/joshu/repos/ArenaMCP/DEV.md):

- Runtime/interpreter: `%LOCALAPPDATA%\mtgacoach\venv`
- Source of truth: repo `src\`
- Desktop shortcut target: `pythonw.exe Z:\ArenaMCP\scripts\launch_desktop.py`

This path is for source iteration only.

## Customer Releases

Customer installs should use the packaged PySide installer, not the repo launcher.

Build it on Windows with:

```powershell
cd Z:\ArenaMCP\installer
.\build-installer.ps1
```

That script now:

1. Stages a PySide desktop release under `dist\desktop-release\app`
2. Builds a bundled runtime under `dist\desktop-release\app\runtime`
3. Packages the staged app into `dist\installer\mtgacoach-Setup.exe`

Installed launch target:

- `{app}\runtime\Scripts\pythonw.exe "{app}\scripts\launch_installed.py"`

The installed app runs from `C:\Program Files\mtgacoach` and does not depend on a repo checkout.

## Migration From WinUI

The installer keeps the existing Inno `AppId`, so a `v2.0.0` install upgrades the prior WinUI install in place.

Migration behavior:

- old WinUI shortcut target is replaced by the PySide launcher
- old `{app}\launcher` files are deleted during install
- `%LOCALAPPDATA%\mtgacoach` is preserved
- `~/.arenamcp` settings/logs are preserved

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
