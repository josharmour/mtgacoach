"""Cross-platform MTGA discovery and integration (Windows / Linux).

One seam for everything OS-specific about finding and instrumenting MTGA:
where the game is installed, where Player.log lives, and (Linux) whether
the Steam launch options let BepInEx inject under Proton. macOS is
structured as an add-a-backend job but intentionally unclaimed until it
can be tested on real hardware.

The desktop runtime and the repair engine consume this module instead of
hardcoding per-OS paths (repair-audit follow-up: the old Linux probe
looked for MTGA inside the Proton prefix drive_c, where Steam never
installs games — steamapps/common/MTGA is the real location).
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MTGA_STEAM_APPID = "2141910"

# Wine prefix path fragment holding MTGA's C: drive under Proton.
_PROTON_PFX = Path("steamapps") / "compatdata" / MTGA_STEAM_APPID / "pfx"
_PLAYER_LOG_WIN_SUFFIX = (
    Path("AppData") / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
)


@dataclass
class MtgaInstall:
    """A located MTGA installation."""

    install_dir: Path            # game files (MTGA.exe, MTGA_Data, BepInEx)
    player_log: Optional[Path]   # Player.log (None when undetectable)
    platform: str                # "windows" | "linux-steam" | "linux-steam-flatpak"
    steam_root: Optional[Path] = None  # Linux: the Steam root that owns it


def current_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


# ---------------------------------------------------------------------------
# Linux: Steam discovery
# ---------------------------------------------------------------------------

def _steam_roots() -> list[Path]:
    """Candidate Steam roots: native and Flatpak."""
    home = Path.home()
    candidates = [
        home / ".local/share/Steam",
        home / ".steam/steam",
        home / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
    ]
    seen: set[Path] = set()
    roots: list[Path] = []
    for c in candidates:
        try:
            real = c.resolve()
        except OSError:
            continue
        if real in seen or not (c / "steamapps").is_dir():
            continue
        seen.add(real)
        roots.append(c)
    return roots


def _steam_libraries(root: Path) -> list[Path]:
    """The root's steamapps plus every library in libraryfolders.vdf."""
    libs = [root / "steamapps"]
    vdf = root / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(errors="replace")
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            lib = Path(m.group(1)) / "steamapps"
            if lib.is_dir() and lib not in libs:
                libs.append(lib)
    except OSError:
        pass
    return libs


def _find_mtga_linux() -> Optional[MtgaInstall]:
    for root in _steam_roots():
        for lib in _steam_libraries(root):
            manifest = lib / f"appmanifest_{MTGA_STEAM_APPID}.acf"
            game_dir = lib / "common" / "MTGA"
            if not (game_dir / "MTGA.exe").is_file():
                if not manifest.is_file():
                    continue
                # Manifest names the installdir explicitly.
                try:
                    m = re.search(
                        r'"installdir"\s+"([^"]+)"', manifest.read_text(errors="replace")
                    )
                    if m:
                        game_dir = lib / "common" / m.group(1)
                except OSError:
                    pass
                if not (game_dir / "MTGA.exe").is_file():
                    continue
            # Proton prefix lives in the SAME library as the game.
            pfx = lib / "compatdata" / MTGA_STEAM_APPID / "pfx"
            player_log = (
                pfx / "drive_c" / "users" / "steamuser" / _PLAYER_LOG_WIN_SUFFIX
            )
            flatpak = ".var/app/com.valvesoftware.Steam" in str(root)
            return MtgaInstall(
                install_dir=game_dir,
                player_log=player_log if player_log.parent.is_dir() else None,
                platform="linux-steam-flatpak" if flatpak else "linux-steam",
                steam_root=root,
            )
    return None


def proton_launch_options_ok(install: MtgaInstall) -> Optional[bool]:
    """Linux: do MTGA's Steam launch options let BepInEx inject?

    BepInEx's winhttp.dll doorstop silently does not load under Proton
    without ``WINEDLLOVERRIDES="winhttp=n,b"`` in the launch options —
    the documented cause of a whole 'bridge never connects' failure
    class. Returns None when undetermined (localconfig.vdf unreadable).
    """
    if install.steam_root is None:
        return None
    userdata = install.steam_root / "userdata"
    if not userdata.is_dir():
        return None
    verdict: Optional[bool] = None
    for cfg in userdata.glob("*/config/localconfig.vdf"):
        try:
            text = cfg.read_text(errors="replace")
        except OSError:
            continue
        # LaunchOptions sit near the top of the per-app block, but the
        # appid string appears in several unrelated VDF sections (cloud,
        # tickets, playtime) and nesting depth varies by Steam version —
        # so scan a window after EVERY occurrence and judge from whichever
        # windows actually contain a LaunchOptions entry.
        saw_launch_options = False
        for m in re.finditer(re.escape(f'"{MTGA_STEAM_APPID}"'), text):
            window = text[m.end():m.end() + 4000]
            lo = re.search(r'"LaunchOptions"\s+"((?:\\.|[^"\\])*)"', window)
            if not lo:
                continue
            saw_launch_options = True
            if "winhttp=n,b" in lo.group(1).replace("\\", ""):
                return True
        if saw_launch_options:
            verdict = False
    return verdict


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _find_mtga_windows() -> Optional[MtgaInstall]:
    # Registry + well-known paths live in desktop.runtime (winreg-guarded);
    # reuse rather than duplicate.
    from arenamcp.desktop import runtime as _runtime

    mtga_dir, _source = _runtime.find_mtga_install_dir()
    if not mtga_dir:
        return None
    player_log = (
        Path.home() / _PLAYER_LOG_WIN_SUFFIX
    )
    return MtgaInstall(
        install_dir=Path(mtga_dir),
        player_log=player_log if player_log.parent.is_dir() else None,
        platform="windows",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def find_mtga() -> Optional[MtgaInstall]:
    """Locate MTGA on this machine, honoring a saved settings override."""
    # Explicit user setting wins on every platform.
    try:
        from arenamcp.settings import get_settings

        saved = get_settings().get("mtga_install_dir")
        if saved and (Path(saved) / "MTGA_Data").is_dir():
            install = MtgaInstall(
                install_dir=Path(saved),
                player_log=None,
                platform=current_platform(),
            )
            detected = (
                _find_mtga_linux() if current_platform() == "linux"
                else _find_mtga_windows() if current_platform() == "windows"
                else None
            )
            if detected and detected.install_dir == install.install_dir:
                return detected
            # Saved dir differs from (or lacks) auto-detection — keep the
            # saved dir but try to fill in the Player.log from detection.
            if detected:
                install.player_log = detected.player_log
                install.steam_root = detected.steam_root
                install.platform = detected.platform
            return install
    except Exception as e:
        logger.debug(f"settings-based MTGA lookup failed: {e}")

    plat = current_platform()
    if plat == "windows":
        return _find_mtga_windows()
    if plat == "linux":
        return _find_mtga_linux()
    return None  # darwin: backend not yet implemented (untested hardware)
