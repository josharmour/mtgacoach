"""Cross-platform MTGA discovery and integration (Windows / Linux / macOS).

One seam for everything OS-specific about finding and instrumenting MTGA:
where the game is installed, where Player.log lives, and (Linux) whether
the Steam launch options let BepInEx inject under Proton. The macOS
backend covers the native Steam/Epic builds (log tier — coach/draft) and
CrossOver bottles running the Windows Mono build (the only route to the
BepInEx bridge on a Mac; see docs/PLATFORM_PARITY.md §B1).

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

# macOS: the native (IL2CPP) client writes its Player.log here — verified on
# real hardware 2026-07-16 (dir name is "Wizards Of The Coast", capital O/T;
# APFS is case-insensitive by default but don't rely on that).
_PLAYER_LOG_DARWIN_SUFFIX = (
    Path("Library") / "Logs" / "Wizards Of The Coast" / "MTGA" / "Player.log"
)


@dataclass
class MtgaInstall:
    """A located MTGA installation.

    ``platform`` values:
      - ``"windows"``
      - ``"linux-steam"`` / ``"linux-steam-flatpak"`` (Windows build under Proton)
      - ``"darwin-steam"`` / ``"darwin-epic"`` (native IL2CPP Mac build —
        log tier only, no Mono → no BepInEx bridge)
      - ``"darwin-crossover"`` (Windows Mono build in a CrossOver bottle —
        the bridge CAN work here, mirroring the Linux/Proton recipe)

    Callers that gate on ``.startswith("linux")`` / ``== "windows"`` are
    unaffected by the darwin values by construction.
    """

    install_dir: Path            # game files (MTGA.exe / MTGA.app, MTGA_Data, BepInEx)
    player_log: Optional[Path]   # Player.log (None when undetectable)
    platform: str                # see class docstring
    steam_root: Optional[Path] = None  # Linux/macOS Steam: the Steam root that owns it


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
# macOS
# ---------------------------------------------------------------------------

# Epic Games Launcher on macOS defaults game installs to /Users/Shared/Epic
# Games; the per-user location shows up when users relocate the library.
# Module-level so tests (and exotic setups) can extend it.
_DARWIN_EPIC_CANDIDATES = [
    Path("/Users/Shared/Epic Games/MagicTheGathering"),
]


def _darwin_native_log(home: Path) -> Optional[Path]:
    """Native-client Player.log path, or None when MTGA never ran."""
    log = home / _PLAYER_LOG_DARWIN_SUFFIX
    return log if log.parent.is_dir() else None


def _find_mtga_darwin_crossover(home: Path) -> Optional[MtgaInstall]:
    """Windows MTGA build inside a CrossOver bottle (bridge-capable)."""
    bottles_root = home / "Library" / "Application Support" / "CrossOver" / "Bottles"
    if not bottles_root.is_dir():
        return None
    win_dirs = (
        Path("Program Files") / "Wizards of the Coast" / "MTGA",
        Path("Program Files (x86)") / "Wizards of the Coast" / "MTGA",
        Path("Program Files (x86)") / "Steam" / "steamapps" / "common" / "MTGA",
    )
    try:
        bottles = sorted(p for p in bottles_root.iterdir() if p.is_dir())
    except OSError:
        return None
    for bottle in bottles:
        for win_dir in win_dirs:
            game_dir = bottle / "drive_c" / win_dir
            if not (game_dir / "MTGA.exe").is_file():
                continue
            # The bottle's Windows user dir is usually "crossover" but the
            # naming isn't guaranteed — glob instead of assuming.
            player_log: Optional[Path] = None
            users_dir = bottle / "drive_c" / "users"
            for user_dir in sorted(users_dir.glob("*")):
                if user_dir.name.lower() == "public" or not user_dir.is_dir():
                    continue
                candidate = user_dir / _PLAYER_LOG_WIN_SUFFIX
                if candidate.parent.is_dir():
                    player_log = candidate
                    break
            return MtgaInstall(
                install_dir=game_dir,
                player_log=player_log,
                platform="darwin-crossover",
            )
    return None


def _find_mtga_darwin() -> Optional[MtgaInstall]:
    """Locate MTGA on macOS.

    Priority: native Steam bundle, then Epic, then a CrossOver bottle
    holding the Windows build. The native client is IL2CPP (no BepInEx),
    so it serves the log tier only; a bottle install is bridge-capable.
    """
    home = Path.home()

    # (a) Native Steam: ~/Library/Application Support/Steam/steamapps/common/MTGA
    # holds MTGA.app *and* MTGA_Data (card DB) side by side — verified on
    # real hardware 2026-07-16.
    steam_root = home / "Library" / "Application Support" / "Steam"
    steam_dir = steam_root / "steamapps" / "common" / "MTGA"
    if (steam_dir / "MTGA.app").is_dir():
        return MtgaInstall(
            install_dir=steam_dir,
            player_log=_darwin_native_log(home),
            platform="darwin-steam",
            steam_root=steam_root,
        )

    # (b) Epic Games (native build, same MTGA.app layout).
    for epic_dir in _DARWIN_EPIC_CANDIDATES:
        if (epic_dir / "MTGA.app").is_dir():
            return MtgaInstall(
                install_dir=epic_dir,
                player_log=_darwin_native_log(home),
                platform="darwin-epic",
            )

    # (c) CrossOver bottle running the Windows Mono build.
    return _find_mtga_darwin_crossover(home)


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
                else _find_mtga_darwin() if current_platform() == "darwin"
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
    if plat == "darwin":
        return _find_mtga_darwin()
    return None
