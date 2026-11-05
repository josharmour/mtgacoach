"""Create a Windows Dev shortcut with local Python and a selected source checkout."""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SHORTCUT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$path = Join-Path ([Environment]::GetFolderPath('Desktop')) 'MTGA Coach (Dev).lnk'
$shortcut = $shell.CreateShortcut($path)
$shortcut.TargetPath = $env:COACH_DEV_PYTHONW
$shortcut.Arguments = '"' + $env:COACH_DEV_LAUNCHER + '"'
$shortcut.WorkingDirectory = $env:COACH_DEV_HOME
$shortcut.IconLocation = $env:COACH_DEV_ICON
$shortcut.Description = 'MTGA Coach Dev - local Python, shared source'
$shortcut.Save()
Write-Output $path
"""


def install(checkout: Path, python: Path, destination: Path, git_dir: Path | None = None):
    checkout, python, destination = checkout.absolute(), python.absolute(), destination.absolute()
    pythonw = python.with_name("pythonw.exe")
    if not (checkout / "scripts/launch_desktop.py").is_file() or not pythonw.is_file():
        raise ValueError("Choose a Coach checkout and a working local Windows Python environment.")
    if git_dir and not (git_dir / "HEAD").is_file():
        raise ValueError("The selected Git metadata directory has no HEAD file.")
    destination.mkdir(parents=True, exist_ok=True)
    repository = Path(__file__).resolve().parents[1]
    launcher = destination / "launcher.pyw"
    shutil.copyfile(repository / "installer/windows-dev/launcher.pyw", launcher)
    icon = destination / "CoachDev.ico"
    shutil.copyfile(repository / "installer/mtga_coach_dev.ico", icon)
    (destination / "launcher.json").write_text(
        json.dumps({"checkout": str(checkout), "git_dir": str(git_dir) if git_dir else ""}, indent=2),
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "COACH_DEV_PYTHONW": str(pythonw),
        "COACH_DEV_LAUNCHER": str(launcher),
        "COACH_DEV_HOME": str(destination),
        "COACH_DEV_ICON": str(icon),
    }
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    print(result.stdout.strip())
    return launcher


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--python", type=Path)
    parser.add_argument(
        "--git-dir", type=Path, help="Windows path to Git metadata for a worktree created on another OS"
    )
    parser.add_argument(
        "--create-venv",
        action="store_true",
        help="Create local Python environment and install checkout dependencies",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run this installer on Windows.")
    destination = Path(os.environ["LOCALAPPDATA"]) / "MTGACoachDev"
    python = args.python or destination / "venv/Scripts/python.exe"
    if args.create_venv:
        if args.python:
            parser.error("Use either --python or --create-venv.")
        if not python.is_file():
            subprocess.run([sys.executable, "-m", "venv", str(destination / "venv")], check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-e", str(args.checkout.absolute()) + "[dev,full]"],
            check=True,
        )
    subprocess.run([str(python), "-c", "import PySide6, openai, kokoro_onnx"], check=True)
    install(args.checkout, python, destination, args.git_dir)


if __name__ == "__main__":
    main()
