"""Proton/Linux launch + discovery helpers for MTG Arena.

MTGA on Linux runs under Steam (Flatpak or native) via Proton. This module is
stdlib-only (no third-party deps) so it can be imported from setup/repair flows
and the headless self-play orchestrator without pulling the full app stack.

Responsibilities:
- Locate the MTGA install (game dir + Proton prefix + log files).
- Detect whether the wine MTGA.exe process is running.
- Build/launch the correct Steam command (Flatpak form when available).
- Wait for the game process and, best-effort, for the BepInEx bridge plugin to
  connect back to the Python TCP server on port 44222.

Nothing here launches the game on import; ``launch_mtga()`` is only meant to be
called at real runtime, never during tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_APP_ID = "2141910"

# BepInEx log line emitted once the in-game plugin connects back to the Python
# TCP server that owns the main GRE bridge on port 44222.
_PLUGIN_CONNECTED_MARKER = "TCP client connected to Python server on port 44222"


@dataclass
class MtgaInstall:
    """Resolved on-disk locations for an MTGA install under Proton."""

    game_dir: Path
    prefix_dir: Path
    player_log: Path
    bepinex_log: Path
    app_id: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"MtgaInstall(app_id={self.app_id}, game_dir={self.game_dir}, "
            f"prefix_dir={self.prefix_dir}, player_log={self.player_log}, "
            f"bepinex_log={self.bepinex_log})"
        )


def _steam_root_candidates() -> List[Path]:
    """Steam root directories to probe, in search-priority order."""
    home = Path.home()
    return [
        # Flatpak Steam (primary on this box).
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        # Native Steam install locations.
        home / ".steam/steam",
        home / ".steam/root",
        home / ".local/share/Steam",
        home / ".steam/debian-installation",
    ]


def _parse_library_paths(steam_root: Path) -> List[Path]:
    """Parse extra Steam library paths from steamapps/libraryfolders.vdf."""
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    paths: List[Path] = []
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return paths
    # Lines look like:  "path"   "/some/library"
    for match in re.finditer(r'"path"\s*"([^"]+)"', text):
        try:
            paths.append(Path(match.group(1)))
        except (ValueError, OSError):
            continue
    return paths


def _candidate_steam_dirs() -> List[Path]:
    """All Steam library roots to scan (known roots + parsed library folders)."""
    seen: List[Path] = []

    def _add(p: Path) -> None:
        if p not in seen:
            seen.append(p)

    for root in _steam_root_candidates():
        if not root.exists():
            continue
        _add(root)
        for lib in _parse_library_paths(root):
            _add(lib)
    return seen


def find_mtga_install(app_id: str = DEFAULT_APP_ID) -> Optional[MtgaInstall]:
    """Locate the MTGA install + Proton prefix.

    Searches the Flatpak Steam path first, then native Steam roots, plus any
    extra library folders declared in ``libraryfolders.vdf``. Returns ``None``
    if ``MTGA.exe`` cannot be found.
    """
    for steam_dir in _candidate_steam_dirs():
        steamapps = steam_dir / "steamapps"
        game_dir = steamapps / "common" / "MTGA"
        if not (game_dir / "MTGA.exe").exists():
            continue

        prefix_dir = steamapps / "compatdata" / app_id / "pfx"
        player_log = (
            prefix_dir
            / "drive_c/users/steamuser/AppData/LocalLow"
            / "Wizards Of The Coast/MTGA/Player.log"
        )
        bepinex_log = game_dir / "BepInEx" / "LogOutput.log"

        return MtgaInstall(
            game_dir=game_dir,
            prefix_dir=prefix_dir,
            player_log=player_log,
            bepinex_log=bepinex_log,
            app_id=app_id,
        )
    return None


def is_mtga_running() -> bool:
    """Return True if a wine MTGA.exe process is currently running."""
    # Prefer scanning /proc directly (works without pgrep installed).
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            cmdline_path = entry / "cmdline"
            try:
                raw = cmdline_path.read_bytes()
            except OSError:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
            if "MTGA.exe" in cmdline:
                return True
        return False

    # Fallback: pgrep.
    if shutil.which("pgrep"):
        try:
            result = subprocess.run(
                ["pgrep", "-f", "MTGA.exe"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except OSError:
            return False
    return False


def is_flatpak_steam() -> bool:
    """Return True if the Flatpak Steam app appears to be installed."""
    if not shutil.which("flatpak"):
        return False
    flatpak_data = Path.home() / ".var/app/com.valvesoftware.Steam"
    if flatpak_data.exists():
        return True
    try:
        result = subprocess.run(
            ["flatpak", "info", "com.valvesoftware.Steam"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def steam_launch_command(app_id: str = DEFAULT_APP_ID) -> List[str]:
    """Build the Steam launch command for MTGA.

    Uses the Flatpak form when Flatpak Steam is present, otherwise the native
    ``steam`` CLI.
    """
    if is_flatpak_steam():
        return [
            "flatpak",
            "run",
            "com.valvesoftware.Steam",
            "-applaunch",
            app_id,
        ]
    return ["steam", "-applaunch", app_id]


def launch_mtga(app_id: str = DEFAULT_APP_ID) -> bool:
    """Spawn the Steam launch command DETACHED.

    Returns True if the command was spawned. This is only called at real
    runtime, never during tests.
    """
    cmd = steam_launch_command(app_id)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def wait_for_mtga_process(timeout: float = 90.0) -> bool:
    """Poll until the MTGA.exe process is running or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while True:
        if is_mtga_running():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def wait_for_plugin_connected(
    install: MtgaInstall,
    timeout: float = 180.0,
    since_pos: Optional[int] = None,
) -> bool:
    """Wait for the BepInEx bridge plugin to connect (best-effort).

    Tails ``install.bepinex_log`` for the connection marker. ``since_pos`` lets
    callers ignore connection lines written before launch (capture the log size
    before launching, then pass it here). Returns True if the marker is seen
    within ``timeout``.
    """
    log_path = install.bepinex_log
    start_pos = since_pos if since_pos is not None else 0
    deadline = time.monotonic() + timeout
    while True:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(start_pos)
                for line in fh:
                    if _PLUGIN_CONNECTED_MARKER in line:
                        return True
        except OSError:
            # Log may not exist yet; keep waiting.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def current_bepinex_log_pos(install: MtgaInstall) -> int:
    """Return the current byte length of the BepInEx log (0 if missing).

    Useful as the ``since_pos`` argument to :func:`wait_for_plugin_connected`
    so a fresh connection after launch can be distinguished from stale lines.
    """
    try:
        return install.bepinex_log.stat().st_size
    except OSError:
        return 0
