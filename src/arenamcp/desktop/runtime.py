from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore[assignment]


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
    launcher_dir = path / "installer" / "MtgaCoachLauncher"
    bridge_dir = path / "bepinex-plugin" / "MtgaCoachBridge"
    if pyproject.exists() and src_dir.is_dir():
        return True
    if src_dir.is_dir() and (launcher_dir.is_dir() or bridge_dir.is_dir()):
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
    app_runtime = app_root / "runtime" / "Scripts" / "python.exe"

    candidates: list[tuple[Path, str]] = [
        (app_runtime, "app_runtime"),
        (runtime_root / "venv" / "Scripts" / "python.exe", "runtime_venv"),
    ]

    current = _normalize_current_python(Path(sys.executable))
    if current is not None:
        candidates.append((current, "current"))

    candidates.append((app_root / ".venv" / "Scripts" / "python.exe", "app_venv"))

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

    for candidate in _COMMON_MTGA_PATHS:
        if candidate.is_dir():
            return (str(candidate), "common_path")

    return (None, "not_found")


def is_mtga_running() -> bool:
    if sys.platform != "win32":
        return False

    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MTGA.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        return "MTGA.exe" in result.stdout
    except Exception:
        return False


def _safe_mtime(path: Optional[Path]) -> float:
    if path is None or not path.exists():
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _restart_mtga_required(
    *,
    player_log: Path,
    bepinex_dir: Optional[Path],
    plugin_install_path: Optional[Path],
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
    return max(install_times, default=0.0) > player_log_mtime


def detect_runtime_state() -> RuntimeState:
    app_root = Path(get_app_root())
    repo_checkout = (app_root / ".git").exists()
    runtime_root = Path(get_runtime_root())
    app_runtime_dir = app_root / "runtime"
    runtime_venv_dir = runtime_root / "venv"
    runtime_dir = app_runtime_dir if (app_runtime_dir / "Scripts" / "python.exe").exists() else runtime_venv_dir
    runtime_venv_exists = (runtime_dir / "Scripts" / "python.exe").exists()
    python_exe, python_source = find_python_executable()
    python_ready, python_ready_detail = _check_python_runtime(python_exe)
    mtga_dir, mtga_dir_source = find_mtga_install_dir()
    mtga_exe_path = None
    mtga_running = is_mtga_running()

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

    plugin_build = (
        app_root
        / "bepinex-plugin"
        / "MtgaCoachBridge"
        / "bin"
        / "Release"
        / "net472"
        / "MtgaCoachBridge.dll"
    )
    plugin_built = plugin_build.exists()

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

    return str(target_dir)


def install_plugin(mtga_dir: str) -> str:
    if is_mtga_running():
        raise RuntimeError("Close MTGA before installing the plugin")

    app_root = Path(get_app_root())
    source_dll = (
        app_root
        / "bepinex-plugin"
        / "MtgaCoachBridge"
        / "bin"
        / "Release"
        / "net472"
        / "MtgaCoachBridge.dll"
    )
    if not source_dll.exists():
        raise FileNotFoundError(f"Plugin DLL not found at {source_dll}")

    plugins_dir = Path(mtga_dir) / "BepInEx" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest_dll = plugins_dir / "MtgaCoachBridge.dll"
    shutil.copy2(source_dll, dest_dll)
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
    if sys.platform != "win32":
        raise RuntimeError("Closing MTGA is only supported on Windows")
    if not is_mtga_running():
        return False

    result = subprocess.run(
        ["taskkill", "/IM", "MTGA.exe", "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
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
