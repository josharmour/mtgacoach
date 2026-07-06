from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]


# Windows: suppress the console flash when we shell out from the GUI launcher.
# Without this, every subprocess.run() below pops a brief cmd window which
# makes the launcher appear to "flash" 6-8 times before the PySide window
# becomes visible.
_NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


_SETTINGS_DIR = Path.home() / ".arenamcp"
_SETTINGS_FILE = _SETTINGS_DIR / "settings.json"
_COMMON_MTGA_PATHS = [
    Path(r"C:\Program Files\Wizards of the Coast\MTGA"),
    Path(r"C:\Program Files (x86)\Wizards of the Coast\MTGA"),
    Path(r"D:\Program Files\Wizards of the Coast\MTGA"),
    Path(r"C:\Program Files\Epic Games\MagicTheGathering"),
]
_RELEASES_URL = "https://github.com/josharmour/mtgacoach/releases"
_PYTHON_DOWNLOADS_URL = "https://www.python.org/downloads/windows/"


@dataclass(slots=True)
class RuntimeState:
    repo_dir: str
    repo_checkout: bool
    runtime_root: str
    runtime_venv_dir: str
    runtime_venv_exists: bool
    python_exe: Optional[str]
    python_source: str
    python_ready: bool
    python_ready_detail: str
    mtga_dir: Optional[str]
    mtga_dir_source: str
    mtga_exe_path: Optional[str]
    mtga_running: bool
    player_log: str
    bepinex_log: Optional[str]
    bepinex_dir: Optional[str]
    bepinex_installed: bool
    plugin_install_path: Optional[str]
    plugin_installed: bool
    plugin_build_path: Optional[str]
    plugin_built: bool
    bepinex_bundle: Optional[str]
    restart_mtga_required: bool
    issues: list[str] = field(default_factory=list)

    @property
    def has_ready_python_runtime(self) -> bool:
        return (
            self.python_exe is not None
            and self.python_ready
            and (
                self.runtime_venv_exists
                or self.python_source == "app_runtime"
                or (self.repo_checkout and self.python_source in {"current", "app_venv"})
            )
        )

    @property
    def bridge_ready(self) -> bool:
        return (
            self.mtga_dir is not None
            and self.bepinex_installed
            and self.plugin_installed
        )

    @property
    def is_launchable(self) -> bool:
        return self.has_ready_python_runtime

    @property
    def is_fully_provisioned(self) -> bool:
        return self.has_ready_python_runtime and self.bridge_ready


def _is_app_root(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False

    pyproject = path / "pyproject.toml"
    src_dir = path / "src" / "arenamcp"
    bridge_dir = path / "bepinex-plugin" / "MtgaCoachBridge"
    if pyproject.exists() and src_dir.is_dir():
        return True
    if src_dir.is_dir() and bridge_dir.is_dir():
        return True
    return False


def get_app_root() -> str:
    env_val = os.environ.get("MTGACOACH_APP_ROOT", "").strip()
    if env_val:
        env_path = Path(env_val).expanduser().resolve()
        if _is_app_root(env_path):
            return str(env_path)

    start_points = []
    if getattr(sys, "frozen", False):
        start_points.append(Path(sys.executable).resolve().parent)
    else:
        start_points.append(Path(__file__).resolve().parent)
    start_points.append(Path.cwd().resolve())

    seen: set[Path] = set()
    for start in start_points:
        current = start
        while current not in seen:
            seen.add(current)
            if _is_app_root(current):
                return str(current)
            if current.parent == current:
                break
            current = current.parent

    return str(start_points[0])


def get_runtime_root() -> str:
    env_val = os.environ.get("MTGACOACH_RUNTIME_ROOT", "").strip()
    if env_val:
        return str(Path(env_val).expanduser())

    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            return str(Path(local_appdata) / "mtgacoach")

    return str(Path.home() / ".local" / "share" / "mtgacoach")


def _is_real_python(path: Path) -> bool:
    name = path.name.lower()
    return path.exists() and name.startswith("python")


def _normalize_current_python(path: Path) -> Optional[Path]:
    if not _is_real_python(path):
        return None

    if path.name.lower() == "pythonw.exe":
        sibling = path.with_name("python.exe")
        if sibling.exists():
            return sibling
    return path


def _find_python_on_path() -> tuple[Optional[str], str]:
    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["py", "-3", "-c", "import sys; print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                creationflags=_NO_WINDOW_FLAGS,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    candidate = line.strip()
                    if candidate and Path(candidate).exists():
                        return (candidate, "py_launcher")
        except Exception:
            pass

        try:
            result = subprocess.run(
                ["where", "python"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                creationflags=_NO_WINDOW_FLAGS,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    candidate = line.strip()
                    if candidate and Path(candidate).exists():
                        return (candidate, "PATH")
        except Exception:
            pass

    found = shutil.which("python")
    if found:
        return (found, "PATH")
    return (None, "not_found")


def _check_python_runtime(python_exe: Optional[str]) -> tuple[bool, str]:
    if not python_exe:
        return (False, "Python executable not found")

    try:
        result = subprocess.run(
            [python_exe, "-c", "import PySide6"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            creationflags=_NO_WINDOW_FLAGS,
        )
    except Exception as exc:
        return (False, str(exc))

    if result.returncode == 0:
        return (True, "PySide6 import ok")

    detail = (result.stderr or result.stdout).strip() or f"exit code {result.returncode}"
    return (False, detail)


def find_python_executable() -> tuple[Optional[str], str]:
    runtime_root = Path(get_runtime_root())
    app_root = Path(get_app_root())
    scripts_dir = "Scripts" if sys.platform == "win32" else "bin"
    py_exe = "python.exe" if sys.platform == "win32" else "python"
    
    app_runtime = app_root / "runtime" / scripts_dir / py_exe

    candidates: list[tuple[Path, str]] = [
        (app_runtime, "app_runtime"),
        (runtime_root / "venv" / scripts_dir / py_exe, "runtime_venv"),
    ]

    current = _normalize_current_python(Path(sys.executable))
    if current is not None:
        candidates.append((current, "current"))

    candidates.append((app_root / ".venv" / scripts_dir / py_exe, "app_venv"))

    for candidate, source in candidates:
        if candidate.exists():
            return (str(candidate), source)

    from_path, path_source = _find_python_on_path()
    if from_path:
        return (from_path, path_source)

    return (None, "not_found")


def get_saved_mtga_dir() -> Optional[str]:
    try:
        if not _SETTINGS_FILE.exists():
            return None
        with _SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        value = data.get("mtga_install_dir")
        if isinstance(value, str) and Path(value).is_dir():
            return value
    except Exception:
        pass
    return None


def set_saved_mtga_dir(path: str) -> None:
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {}
    if _SETTINGS_FILE.exists():
        try:
            with _SETTINGS_FILE.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    data["mtga_install_dir"] = path
    with _SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def _find_mtga_from_registry() -> Optional[str]:
    if sys.platform != "win32" or winreg is None:
        return None

    uninstall_keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for key_path in uninstall_keys:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
                count = winreg.QueryInfoKey(key)[0]
                for index in range(count):
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                            if not isinstance(display_name, str):
                                continue
                            lower = display_name.lower()
                            if "magic" not in lower or "gathering" not in lower:
                                continue
                            install_location, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                            if isinstance(install_location, str) and Path(install_location).is_dir():
                                return install_location
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def find_mtga_install_dir() -> tuple[Optional[str], str]:
    saved = get_saved_mtga_dir()
    if saved and Path(saved).is_dir():
        return (saved, "settings")

    env_val = os.environ.get("MTGA_DIR", "").strip()
    if env_val and Path(env_val).is_dir():
        return (env_val, "environment")

    from_registry = _find_mtga_from_registry()
    if from_registry:
        return (from_registry, "registry")

    if sys.platform != "win32":
        linux_paths = [
            Path.home() / ".steam/steam/steamapps/compatdata/2141910/pfx/drive_c/Program Files/Wizards of the Coast/MTGA",
            Path.home() / ".local/share/Steam/steamapps/compatdata/2141910/pfx/drive_c/Program Files/Wizards of the Coast/MTGA",
            Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/2141910/pfx/drive_c/Program Files/Wizards of the Coast/MTGA",
        ]
        for candidate in linux_paths:
            if candidate.is_dir():
                return (str(candidate), "proton_path")

    for candidate in _COMMON_MTGA_PATHS:
        if candidate.is_dir():
            return (str(candidate), "common_path")

    return (None, "not_found")


_running_cache_value: Optional[bool] = None
_running_cache_time: float = 0.0
_running_cache_lock = threading.Lock()


def is_mtga_running() -> bool:
    global _running_cache_value, _running_cache_time
    now = time.monotonic()
    if now - _running_cache_time < 1.0:
        return _running_cache_value if _running_cache_value is not None else False

    with _running_cache_lock:
        # Re-check inside lock
        if now - _running_cache_time < 1.0:
            return _running_cache_value if _running_cache_value is not None else False
        _running_cache_time = now

        if sys.platform != "win32":
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "MTGA.exe"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
                _running_cache_value = bool(result.stdout.strip())
            except Exception:
                _running_cache_value = False
        else:
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq MTGA.exe", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                    creationflags=_NO_WINDOW_FLAGS,
                )
                _running_cache_value = "MTGA.exe" in result.stdout
            except Exception:
                _running_cache_value = False
        return _running_cache_value


def _safe_mtime(path: Optional[Path]) -> float:
    if path is None or not path.exists():
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# Config files that require an MTGA restart to take effect, relative to the
# MTGA install dir. Tracked alongside the BepInEx/plugin binaries so that
# editing a config (not just swapping a DLL) is detected.
_RESTART_CONFIG_FILES = {
    "doorstop_config.ini": ("doorstop_config.ini",),
    "BepInEx.cfg": ("BepInEx", "config", "BepInEx.cfg"),
}


def _config_paths_for(mtga_dir: str) -> dict[str, Path]:
    base = Path(mtga_dir)
    return {name: base.joinpath(*parts) for name, parts in _RESTART_CONFIG_FILES.items()}


def get_saved_config_mtimes(mtga_dir: str) -> dict[str, float]:
    """Return the recorded baseline config mtimes for *mtga_dir*.

    The baseline is stored per MTGA directory under ``config_mtimes`` in
    settings.json. Missing/old settings files degrade gracefully to ``{}``.
    """
    try:
        if not _SETTINGS_FILE.exists():
            return {}
        with _SETTINGS_FILE.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        all_mtimes = data.get("config_mtimes")
        if isinstance(all_mtimes, dict):
            entry = all_mtimes.get(mtga_dir)
            if isinstance(entry, dict):
                return {
                    str(k): float(v)
                    for k, v in entry.items()
                    if isinstance(v, (int, float))
                }
    except Exception:
        pass
    return {}


def set_saved_config_mtimes(mtga_dir: str, mtimes: dict[str, float]) -> None:
    """Persist the baseline config mtimes for *mtga_dir* into settings.json."""
    _SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {}
    if _SETTINGS_FILE.exists():
        try:
            with _SETTINGS_FILE.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            pass
    all_mtimes = data.get("config_mtimes")
    if not isinstance(all_mtimes, dict):
        all_mtimes = {}
    all_mtimes[mtga_dir] = {str(k): float(v) for k, v in mtimes.items()}
    data["config_mtimes"] = all_mtimes
    with _SETTINGS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def _record_config_mtimes(mtga_dir: str) -> None:
    """Snapshot the current config mtimes as the baseline for *mtga_dir*.

    Called right after an install/repair so that freshly-written configs do
    not produce a spurious "restart required" signal.
    """
    mtimes: dict[str, float] = {}
    for name, path in _config_paths_for(mtga_dir).items():
        current = _safe_mtime(path)
        if current > 0:
            mtimes[name] = current
    if mtimes:
        set_saved_config_mtimes(mtga_dir, mtimes)


def _restart_mtga_required(
    *,
    player_log: Path,
    bepinex_dir: Optional[Path],
    plugin_install_path: Optional[Path],
    mtga_dir: Optional[str] = None,
) -> bool:
    if bepinex_dir is None and plugin_install_path is None:
        return False

    player_log_mtime = _safe_mtime(player_log)
    if player_log_mtime <= 0:
        return False

    install_times = [
        _safe_mtime(plugin_install_path),
        _safe_mtime(bepinex_dir / "core" / "BepInEx.dll" if bepinex_dir else None),
        _safe_mtime(bepinex_dir / "plugins" / "MtgaCoachBridge.dll" if bepinex_dir else None),
    ]

    # Config changes (BepInEx.cfg, doorstop_config.ini) also require a restart
    # to take effect, but only the binaries were tracked before. Fold the
    # configs into the same Player.log comparison: a config newer than the
    # latest Player.log write means MTGA is running with a stale config. This
    # is self-clearing (Player.log becomes newest again after the restart).
    #
    # The recorded baseline (from a prior install/repair) is used to suppress
    # configs that still match what we installed, which avoids a false signal
    # right after install when Player.log can be older than the fresh configs.
    if mtga_dir:
        saved = get_saved_config_mtimes(mtga_dir)
        for name, path in _config_paths_for(mtga_dir).items():
            current = _safe_mtime(path)
            if current <= 0:
                continue  # config not written yet (created on first MTGA run)
            baseline = saved.get(name)
            if baseline is not None and current <= baseline:
                continue  # unchanged since we recorded the baseline
            install_times.append(current)

    return max(install_times, default=0.0) > player_log_mtime


def detect_runtime_state() -> RuntimeState:
    app_root = Path(get_app_root())
    # A valid checkout is anything that ships pyproject.toml (git clone *or*
    # installer-unpacked source); we no longer require a .git directory.
    repo_checkout = (app_root / "pyproject.toml").exists()
    runtime_root = Path(get_runtime_root())
    app_runtime_dir = app_root / "runtime"
    runtime_venv_dir = runtime_root / "venv"
    
    scripts_dir = "Scripts" if sys.platform == "win32" else "bin"
    py_exe = "python.exe" if sys.platform == "win32" else "python"
    
    runtime_dir = app_runtime_dir if (app_runtime_dir / scripts_dir / py_exe).exists() else runtime_venv_dir
    runtime_venv_exists = (runtime_dir / scripts_dir / py_exe).exists()
    python_exe, python_source = find_python_executable()
    python_ready, python_ready_detail = _check_python_runtime(python_exe)
    mtga_dir, mtga_dir_source = find_mtga_install_dir()
    mtga_exe_path = None
    mtga_running = is_mtga_running()

    env_log_path = os.environ.get("MTGA_LOG_PATH")
    if env_log_path:
        player_log_path = Path(env_log_path)
    else:
        if sys.platform != "win32":
            if mtga_dir:
                prefix_dir = None
                curr = Path(mtga_dir)
                for _ in range(5):
                    if curr.name.lower() == "steamapps":
                        prefix_dir = curr / "compatdata" / "2141910" / "pfx"
                        break
                    if curr.parent == curr:
                        break
                    curr = curr.parent
                
                if not prefix_dir:
                    prefix_dir = Path(mtga_dir).parent.parent.parent.parent

                player_log_path = prefix_dir / "drive_c" / "users" / "steamuser" / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
                if not player_log_path.exists():
                    drive_c = prefix_dir / "drive_c"
                    if drive_c.exists():
                        users_dir = drive_c / "users"
                        if users_dir.exists():
                            for user_folder in users_dir.iterdir():
                                candidate = user_folder / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
                                if candidate.exists():
                                    player_log_path = candidate
                                    break
            else:
                common_prefixes = [
                    Path.home() / ".steam/steam/steamapps/compatdata/2141910/pfx",
                    Path.home() / ".local/share/Steam/steamapps/compatdata/2141910/pfx",
                    Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/2141910/pfx",
                ]
                for pfx in common_prefixes:
                    candidate = pfx / "drive_c" / "users" / "steamuser" / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
                    if candidate.exists():
                        player_log_path = candidate
                        break
                else:
                    player_log_path = Path.home() / ".steam/steam/steamapps/compatdata/2141910/pfx/drive_c/users/steamuser/AppData/LocalLow/Wizards Of The Coast" / "MTGA" / "Player.log"
        else:
            player_log_path = (
                Path.home()
                / "AppData"
                / "LocalLow"
                / "Wizards Of The Coast"
                / "MTGA"
                / "Player.log"
            )
    player_log = str(player_log_path)

    bepinex_dir: Optional[str] = None
    bepinex_installed = False
    bepinex_log: Optional[str] = None
    plugin_install_path: Optional[str] = None
    plugin_installed = False

    if mtga_dir:
        mtga_exe = Path(mtga_dir) / "MTGA.exe"
        if mtga_exe.exists():
            mtga_exe_path = str(mtga_exe)
        b_dir = Path(mtga_dir) / "BepInEx"
        core_dll = b_dir / "core" / "BepInEx.dll"
        if b_dir.is_dir() and core_dll.exists():
            bepinex_dir = str(b_dir)
            bepinex_installed = True
        bepinex_log = str(b_dir / "LogOutput.log")

        plugin_path = b_dir / "plugins" / "MtgaCoachBridge.dll"
        if plugin_path.exists():
            plugin_install_path = str(plugin_path)
            plugin_installed = True

    # A deployable DLL from ANY source (packaged resource preferred, dev
    # build tree fallback) — on end-user installs the dev tree never
    # exists, which made plugin_built permanently False and unreachable
    # every update path that gated on it (repair-audit blockers #1, #7).
    plugin_build = find_plugin_dll()
    plugin_built = plugin_build is not None

    bepinex_bundle: Optional[str] = None
    for subdir in ("third_party", "assets"):
        bundle_dir = app_root / subdir / "BepInEx"
        if bundle_dir.is_dir():
            bepinex_bundle = str(bundle_dir)
            break
        bundle_zip = app_root / subdir / "BepInEx.zip"
        if bundle_zip.exists():
            bepinex_bundle = str(bundle_zip)
            break

    restart_mtga_required = _restart_mtga_required(
        player_log=player_log_path,
        bepinex_dir=Path(bepinex_dir) if bepinex_dir else None,
        plugin_install_path=Path(plugin_install_path) if plugin_install_path else None,
        mtga_dir=mtga_dir,
    )

    issues: list[str] = []
    if python_exe is None:
        issues.append("Python 3.10+ not found")
    elif not python_ready:
        issues.append("Setup environment has not completed")
    if mtga_dir is None:
        issues.append("MTGA install not detected")
    if mtga_dir is not None and not bepinex_installed:
        issues.append("BepInEx not installed in MTGA")
    if mtga_dir is not None and bepinex_installed and not plugin_installed:
        issues.append("MtgaCoachBridge.dll not deployed to BepInEx/plugins")
    if restart_mtga_required:
        issues.append("Restart MTGA to load the updated bridge")

    return RuntimeState(
        repo_dir=str(app_root),
        repo_checkout=repo_checkout,
        runtime_root=str(runtime_root),
        runtime_venv_dir=str(runtime_dir),
        runtime_venv_exists=runtime_venv_exists,
        python_exe=python_exe,
        python_source=python_source,
        python_ready=python_ready,
        python_ready_detail=python_ready_detail,
        mtga_dir=mtga_dir,
        mtga_dir_source=mtga_dir_source,
        mtga_exe_path=mtga_exe_path,
        mtga_running=mtga_running,
        player_log=player_log,
        bepinex_log=bepinex_log,
        bepinex_dir=bepinex_dir,
        bepinex_installed=bepinex_installed,
        plugin_install_path=plugin_install_path,
        plugin_installed=plugin_installed,
        plugin_build_path=str(plugin_build) if plugin_built else None,
        plugin_built=plugin_built,
        bepinex_bundle=bepinex_bundle,
        restart_mtga_required=restart_mtga_required,
        issues=issues,
    )


def tail_text(path: Optional[str], max_bytes: int = 8192) -> str:
    if not path:
        return ""

    target = Path(path)
    if not target.exists():
        return ""

    try:
        with target.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            if length <= 0:
                return ""
            offset = max(0, length - max_bytes)
            handle.seek(offset)
            return handle.read(max_bytes).decode("utf-8", errors="replace")
    except Exception:
        return ""


def read_version() -> str:
    pyproject = Path(get_app_root()) / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            trimmed = line.strip()
            if trimmed.startswith("version = "):
                parts = trimmed.split('"')
                if len(parts) >= 2:
                    return parts[1]
    except Exception:
        pass
    return "unknown"


def _subprocess_creationflags() -> int:
    if sys.platform != "win32":
        return 0
    return getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


def get_setup_wizard_command(mode: str | None = None) -> tuple[str, list[str], dict[str, str]]:
    app_root = Path(get_app_root())
    runtime_root = get_runtime_root()
    python_exe, _ = find_python_executable()
    if python_exe is None:
        raise RuntimeError("Python executable not found")

    env = os.environ.copy()
    env["MTGACOACH_RUNTIME_ROOT"] = runtime_root
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    args = [str(app_root / "setup_wizard.py")]
    if mode == "create_venv":
        args.append("--create-venv")
    elif mode == "setup_environment":
        args.append("--setup-environment")
    return (python_exe, args, env)


def run_setup_wizard(mode: str | None = None) -> subprocess.Popen[str]:
    app_root = Path(get_app_root())
    python_exe, args, env = get_setup_wizard_command(mode)
    return subprocess.Popen(
        [python_exe] + args,
        cwd=app_root,
        env=env,
        creationflags=_subprocess_creationflags(),
        text=True,
    )


def install_bepinex(mtga_dir: str) -> str:
    if is_mtga_running():
        raise RuntimeError("Close MTGA before installing BepInEx")

    state = detect_runtime_state()
    if not state.bepinex_bundle:
        raise FileNotFoundError("No BepInEx bundle found in assets/ or third_party/")

    bundle = Path(state.bepinex_bundle)
    target_dir = Path(mtga_dir) / "BepInEx"
    if bundle.suffix.lower() == ".zip":
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(mtga_dir)
    elif bundle.is_dir():
        _copy_directory(bundle, target_dir)

    app_root = Path(get_app_root())
    bundle_parent = bundle.parent if bundle.exists() else app_root
    for filename in ("winhttp.dll", "doorstop_config.ini"):
        source = bundle_parent / filename
        if not source.exists():
            source = app_root / "assets" / filename
        if not source.exists():
            source = app_root / "third_party" / filename
        if source.exists():
            shutil.copy2(source, Path(mtga_dir) / filename)

    # Baseline the freshly-written configs so detect_runtime_state() doesn't
    # report a spurious "restart required" immediately after install.
    _record_config_mtimes(mtga_dir)

    return str(target_dir)


def find_plugin_dll() -> Optional[Path]:
    """Locate the MtgaCoachBridge.dll to deploy, wherever this app lives.

    Order: the DLL shipped inside the installed package (works for pip/uv
    installs on every OS — repair-audit blocker #1: every CI installer
    since v2.4.0 shipped WITHOUT the DLL and install_plugin dead-ended on
    FileNotFoundError with no fallback), then the dev build tree.
    """
    try:
        from importlib import resources

        packaged = resources.files("arenamcp.resources") / "MtgaCoachBridge.dll"
        if packaged.is_file():
            return Path(str(packaged))
    except Exception:
        pass
    dev_dll = (
        Path(get_app_root())
        / "bepinex-plugin"
        / "MtgaCoachBridge"
        / "bin"
        / "Release"
        / "net472"
        / "MtgaCoachBridge.dll"
    )
    if dev_dll.exists():
        return dev_dll
    return None


def install_plugin(mtga_dir: str) -> str:
    if is_mtga_running():
        raise RuntimeError("Close MTGA before installing the plugin")

    source_dll = find_plugin_dll()
    if source_dll is None:
        raise FileNotFoundError(
            "Plugin DLL not found in the installed package or the dev build "
            "tree — reinstall the app (pip install --force-reinstall arenamcp)"
        )

    plugins_dir = Path(mtga_dir) / "BepInEx" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest_dll = plugins_dir / "MtgaCoachBridge.dll"
    shutil.copy2(source_dll, dest_dll)

    # Refresh the config baseline so the install doesn't trip the restart
    # detector before MTGA has been launched against the new plugin.
    _record_config_mtimes(mtga_dir)

    return str(dest_dll)


def repair_bridge_stack(mtga_dir: str) -> list[str]:
    changed: list[str] = []

    bepinex_core = Path(mtga_dir) / "BepInEx" / "core" / "BepInEx.dll"
    if not bepinex_core.exists():
        changed.append(install_bepinex(mtga_dir))

    plugin_dll = Path(mtga_dir) / "BepInEx" / "plugins" / "MtgaCoachBridge.dll"
    if not plugin_dll.exists():
        changed.append(install_plugin(mtga_dir))

    return changed






def close_mtga() -> bool:
    if not is_mtga_running():
        return False

    if sys.platform != "win32":
        result = subprocess.run(
            ["pkill", "-f", "MTGA.exe"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0 and result.returncode != 1:
            detail = (result.stderr or result.stdout).strip() or "pkill failed"
            raise RuntimeError(detail)
        return True

    result = subprocess.run(
        ["taskkill", "/IM", "MTGA.exe", "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        creationflags=_NO_WINDOW_FLAGS,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "taskkill failed"
        raise RuntimeError(detail)
    return True


def launch_mtga(mtga_dir: str) -> str:
    executable = Path(mtga_dir) / "MTGA.exe"
    if not executable.exists():
        raise FileNotFoundError(f"MTGA.exe not found at {executable}")

    if sys.platform == "win32":
        os.startfile(str(executable))  # type: ignore[attr-defined]
    else:
        steam_bin = shutil.which("steam")
        flatpak_bin = shutil.which("flatpak")
        if steam_bin:
            subprocess.Popen([steam_bin, "steam://rungameid/2141910"])
        elif flatpak_bin:
            subprocess.Popen([flatpak_bin, "run", "com.valvesoftware.Steam", "steam://rungameid/2141910"])
        else:
            subprocess.Popen([str(executable)])
    return str(executable)


def restart_mtga(mtga_dir: str) -> str:
    close_mtga()
    return launch_mtga(mtga_dir)


def open_path(path: str) -> None:
    if not path:
        return
    target = Path(path)
    if not target.exists():
        return
    if sys.platform == "win32":
        os.startfile(str(target))  # type: ignore[attr-defined]
        return
    xdg_open = shutil.which("xdg-open")
    if xdg_open:
        subprocess.Popen([xdg_open, str(target)])
    else:
        webbrowser.open(target.as_uri())


def open_url(url: str = _RELEASES_URL) -> None:
    webbrowser.open(url)


def _copy_directory(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = dest / child.name
        if child.is_dir():
            _copy_directory(child, target)
        else:
            shutil.copy2(child, target)


_geometry_cache_value: Optional[dict[str, int]] = None
_geometry_cache_time: float = 0.0
_geometry_cache_lock = threading.Lock()


def get_linux_window_geometry(title: str = "MTGA") -> Optional[dict[str, int]]:
    global _geometry_cache_value, _geometry_cache_time
    now = time.monotonic()
    if now - _geometry_cache_time < 0.3:
        return _geometry_cache_value

    with _geometry_cache_lock:
        # Re-check inside lock
        if now - _geometry_cache_time < 0.3:
            return _geometry_cache_value
        _geometry_cache_time = now

        xwininfo = shutil.which("xwininfo")
        if not xwininfo:
            _geometry_cache_value = None
            return None
        try:
            result = subprocess.run(
                [xwininfo, "-name", title],
                capture_output=True,
                text=True,
                timeout=2,
                check=False
            )
            if result.returncode != 0:
                _geometry_cache_value = None
                return None

            geometry = {}
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("Absolute upper-left X:"):
                    geometry["left"] = int(line.split(":")[-1].strip())
                elif line.startswith("Absolute upper-left Y:"):
                    geometry["top"] = int(line.split(":")[-1].strip())
                elif line.startswith("Width:"):
                    geometry["width"] = int(line.split(":")[-1].strip())
                elif line.startswith("Height:"):
                    geometry["height"] = int(line.split(":")[-1].strip())
                elif line.startswith("Map State:"):
                    geometry["is_visible"] = "IsViewable" in line

            if "left" in geometry and "top" in geometry and "width" in geometry and "height" in geometry:
                geometry["is_minimized"] = not geometry.get("is_visible", True)
                _geometry_cache_value = geometry
                return geometry
        except Exception:
            pass
        _geometry_cache_value = None
        return None
