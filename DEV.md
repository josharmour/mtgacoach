# Development Workflow

Use exactly one desktop app during development: the PySide6 app in this repo.

## One-Time Setup

Run this command on Windows:

```powershell
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m pip install -e Z:\ArenaMCP[desktop]
```

`pip install -e` is important: the desktop app runs directly from the repo source tree.
`[desktop]` now includes the PySide app plus Kokoro desktop-worker dependencies.

This repo already contains a Linux `.venv` for WSL work. Do not try to use `Z:\ArenaMCP\.venv` as the Windows launch environment.

## Launch

Use exactly one launch target:

```powershell
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\pythonw.exe Z:\ArenaMCP\scripts\launch_desktop.py
```

Recommended shortcut target:

- `C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\pythonw.exe`

Recommended shortcut arguments:

- `Z:\ArenaMCP\scripts\launch_desktop.py`

Recommended shortcut "Start in":

- `Z:\ArenaMCP`

## What Updates On Next Launch

- Any edit under `src\arenamcp\` takes effect on the next app launch.
- Any edit under `src\arenamcp\desktop\` takes effect on the next app launch.
- No C# rebuild is part of the normal dev loop anymore.
- Launch does not depend on the package being importable from an editable install; the launcher script adds `src\` directly.

If you change Python dependencies or entrypoints, rerun:

```powershell
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m pip install -e Z:\ArenaMCP[desktop]
```

## Do Not Use During Development

- `C:\Users\Public\Desktop\mtgacoach.lnk`
- `C:\Program Files\mtgacoach\...`
- `installer\MtgaCoachLauncher\bin\...`
- `mtgacoach-dev.lnk`

Those are no longer part of the source-iteration workflow.

## Useful Commands

Launch from a terminal without the GUI script wrapper:

```powershell
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m arenamcp.desktop
```

Run tests:

```powershell
C:\Users\joshu\AppData\Local\mtgacoach\venv\Scripts\python.exe -m pytest Z:\ArenaMCP\tests -q
```

## Logs

- Desktop UI log: `%LOCALAPPDATA%\mtgacoach\desktop.log`
- Desktop launcher log: `%LOCALAPPDATA%\mtgacoach\desktop-launch.log`
- Coach runtime log: `%USERPROFILE%\.arenamcp\standalone.log`
- Saved bug reports: `%USERPROFILE%\.arenamcp\bug_reports\bug_*.json`
- BepInEx log: `<MTGA>\BepInEx\LogOutput.log`

## Notes

- The PySide6 app is the full desktop app now: coach UI plus repair/install flows.
- The coaching engine still runs as a subprocess internally over the existing JSON pipe protocol. That separation is intentional and keeps the UI responsive while preserving the current backend seam.
- Customer releases are now expected to come from the packaged PySide installer built by `installer\build-installer.ps1`, not the old WinUI launcher.
