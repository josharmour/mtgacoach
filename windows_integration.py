"""Windows launcher/install helpers for mtgacoach.

This module is intentionally stdlib-only so it can be reused by the GUI
launcher, setup flows, and future installer entry points before the full
Python environment is initialized.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable, Optional
import zipfile

try:
    import winreg  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - non-Windows
    winreg = None


REPO_DIR = Path(__file__).resolve().parent
SETTINGS_DIR = Path.home() / ".arenamcp"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
RUNTIME_ENV_VAR = "MTGACOACH_RUNTIME_ROOT"
PLUGIN_BUILD_PATH = (
    REPO_DIR
    / "bepinex-plugin"
    / "MtgaCoachBridge"
    / "bin"
    / "Release"
    / "net472"
    / "MtgaCoachBridge.dll"
)


def get_runtime_root() -> Path:
    override = os.environ.get(RUNTIME_ENV_VAR, "").strip()
    if override:
        try:
            return Path(os.path.expandvars(override)).expanduser()
        except Exception:
            pass

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            return Path(local_appdata) / "mtgacoach"

    return REPO_DIR


def _default_mtga_dir() -> Path:
    program_files = Path(
        os.environ.get("ProgramFiles", r"C:\Program Files")
    )
    return program_files / "Wizards of the Coast" / "MTGA"


def _default_player_log_path() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        base = Path(local_appdata).parent
    else:
        base = Path.home() / "AppData"
    return (
        base
        / "LocalLow"
        / "Wizards Of The Coast"
        / "MTGA"
        / "Player.log"
    )


def _default_bepinex_log_path(mtga_dir: Optional[Path]) -> Optional[Path]:
    if not mtga_dir:
        return None
    return mtga_dir / "BepInEx" / "LogOutput.log"


def _load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_settings(data: dict) -> None:
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_saved_mtga_dir() -> Optional[Path]:
    raw = _load_settings().get("mtga_install_dir")
    if not raw:
        return None
    try:
        return Path(raw)
    except Exception:
        return None


def set_saved_mtga_dir(path: Path) -> None:
    data = _load_settings()
    data["mtga_install_dir"] = str(path)
    _save_settings(data)


def _looks_like_mtga_dir(path: Path) -> bool:
    return (
        (path / "MTGA.exe").exists()
        or (path / "MTGA_Data" / "Managed" / "Assembly-CSharp.dll").exists()
    )


def _normalize_mtga_dir(path: Path) -> Path:
    if path.is_file():
        return path.parent
    if path.name.lower() == "mtga_data":
        return path.parent
    return path


def _clean_registry_path(raw: str) -> Optional[Path]:
    raw = raw.strip().strip('"')
    if not raw:
        return None
    raw = raw.split(",")[0].strip().strip('"')
    raw = raw.split(" /")[0].strip()
    candidate = Path(raw)
    if candidate.name.lower() == "mtga.exe":
        return candidate.parent
    return candidate


def _registry_install_candidates() -> Iterable[tuple[Path, str]]:
    if os.name != "nt" or winreg is None:
        return []

    results: list[tuple[Path, str]] = []
    uninstall_roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    needles = ("mtga", "magic: the gathering arena", "magic the gathering arena")

    for hive, subkey in uninstall_roots:
        try:
            with winreg.OpenKey(hive, subkey) as root:
                count = winreg.QueryInfoKey(root)[0]
                for idx in range(count):
                    try:
                        child_name = winreg.EnumKey(root, idx)
                        with winreg.OpenKey(root, child_name) as child:
                            display_name = ""
                            for name in ("DisplayName", "QuietDisplayName"):
                                try:
                                    display_name = str(winreg.QueryValueEx(child, name)[0] or "")
                                    if display_name:
                                        break
                                except OSError:
                                    continue
                            if not any(needle in display_name.lower() for needle in needles):
                                continue

                            raw_candidates = []
                            for value_name in ("InstallLocation", "DisplayIcon", "InstallSource"):
                                try:
                                    raw_value = str(winreg.QueryValueEx(child, value_name)[0] or "").strip()
                                except OSError:
                                    continue
                                if raw_value:
                                    raw_candidates.append(raw_value)

                            for raw_value in raw_candidates:
                                path = _clean_registry_path(raw_value)
                                if path is None:
                                    continue
                                path = _normalize_mtga_dir(path)
                                if _looks_like_mtga_dir(path):
                                    results.append((path, f"registry:{display_name}"))
                    except OSError:
                        continue
        except OSError:
            continue

    return results


def find_mtga_install_dir() -> tuple[Optional[Path], str]:
    candidates: list[tuple[Optional[Path], str]] = []

    saved = get_saved_mtga_dir()
    if saved:
        candidates.append((saved, "settings"))

    env_dir = os.environ.get("MTGA_DIR", "").strip()
    if env_dir:
        candidates.append((Path(env_dir), "env:MTGA_DIR"))

    candidates.extend(_registry_install_candidates())

    common_roots = [
        _default_mtga_dir(),
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Wizards of the Coast"
        / "MTGA",
    ]
    for path in common_roots:
        candidates.append((path, "common"))

    seen: set[str] = set()
    for raw_path, source in candidates:
        if raw_path is None:
            continue
        try:
            candidate = _normalize_mtga_dir(raw_path.expanduser())
        except Exception:
            continue
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_mtga_dir(candidate):
            return candidate, source

    return None, "not_found"


def is_mtga_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq MTGA.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    output = f"{result.stdout}\n{result.stderr}"
    return "MTGA.exe" in output


def _find_bepinex_bundle() -> Optional[Path]:
    search_roots = [
        REPO_DIR / "third_party",
        REPO_DIR / "assets",
        REPO_DIR,
    ]

    for root in search_roots:
        if not root.exists():
            continue
        # Prefer an extracted bundle directory if present.
        for dirname in ("BepInEx", "bepinex", "BepInExBundle"):
            candidate_dir = root / dirname
            if candidate_dir.is_dir():
                return candidate_dir
        for pattern in ("*BepInEx*.zip", "*bepinex*.zip"):
            matches = sorted(root.glob(pattern))
            if matches:
                return matches[0]
    return None


def _venv_python_candidates() -> list[Path]:
    candidates: list[Path] = []
    runtime_root = get_runtime_root()
    search_roots = [runtime_root]
    if runtime_root != REPO_DIR:
        search_roots.append(REPO_DIR)

    for root in search_roots:
        for env_dir_name in ("venv", ".venv"):
            base = root / env_dir_name / "Scripts"
            candidates.extend([
                base / "python.exe",
                base / "pythonw.exe",
            ])
    return candidates


def find_python_executable_details() -> tuple[Optional[Path], str]:
    runtime_root = get_runtime_root()
    runtime_scripts = runtime_root / "venv" / "Scripts"
    app_venv_scripts = REPO_DIR / "venv" / "Scripts"
    app_dotvenv_scripts = REPO_DIR / ".venv" / "Scripts"

    candidate_sources = [
        (runtime_scripts / "python.exe", "runtime_venv"),
        (runtime_scripts / "pythonw.exe", "runtime_venv"),
        (app_venv_scripts / "python.exe", "app_venv"),
        (app_venv_scripts / "pythonw.exe", "app_venv"),
        (app_dotvenv_scripts / "python.exe", "app_venv"),
        (app_dotvenv_scripts / "pythonw.exe", "app_venv"),
    ]
    for candidate, source in candidate_sources:
        if candidate.exists():
            return candidate, source

    exe = Path(sys.executable)
    if exe.exists() and exe.name.lower().startswith("python"):
        return exe, "current_process"

    for name in ("python.exe", "pythonw.exe", "python"):
        found = shutil.which(name)
        if found:
            return Path(found), f"path:{name}"

    return None, "missing"


def find_python_executable() -> Optional[Path]:
    return find_python_executable_details()[0]


@dataclass
class RuntimeState:
    repo_dir: Path
    runtime_root: Path
    runtime_venv_dir: Path
    runtime_venv_exists: bool
    python_exe: Optional[Path]
    python_source: str
    mtga_dir: Optional[Path]
    mtga_dir_source: str
    mtga_running: bool
    player_log: Path
    bepinex_log: Optional[Path]
    bepinex_dir: Optional[Path]
    bepinex_installed: bool
    plugin_install_path: Optional[Path]
    plugin_installed: bool
    plugin_build_path: Path
    plugin_built: bool
    bepinex_bundle: Optional[Path]
    issues: list[str] = field(default_factory=list)


def detect_runtime_state() -> RuntimeState:
    runtime_root = get_runtime_root()
    runtime_venv_dir = runtime_root / "venv"
    python_exe, python_source = find_python_executable_details()
    mtga_dir, source = find_mtga_install_dir()
    bepinex_dir = mtga_dir / "BepInEx" if mtga_dir else None
    bepinex_installed = bool(
        bepinex_dir and (bepinex_dir / "core" / "BepInEx.dll").exists()
    )
    plugin_install_path = (
        bepinex_dir / "plugins" / "MtgaCoachBridge.dll"
        if bepinex_dir
        else None
    )
    plugin_installed = bool(plugin_install_path and plugin_install_path.exists())

    state = RuntimeState(
        repo_dir=REPO_DIR,
        runtime_root=runtime_root,
        runtime_venv_dir=runtime_venv_dir,
        runtime_venv_exists=any(
            candidate.exists()
            for candidate in (
                runtime_venv_dir / "Scripts" / "pythonw.exe",
                runtime_venv_dir / "Scripts" / "python.exe",
            )
        ),
        python_exe=python_exe,
        python_source=python_source,
        mtga_dir=mtga_dir,
        mtga_dir_source=source,
        mtga_running=is_mtga_running(),
        player_log=_default_player_log_path(),
        bepinex_log=_default_bepinex_log_path(mtga_dir),
        bepinex_dir=bepinex_dir,
        bepinex_installed=bepinex_installed,
        plugin_install_path=plugin_install_path,
        plugin_installed=plugin_installed,
        plugin_build_path=PLUGIN_BUILD_PATH,
        plugin_built=PLUGIN_BUILD_PATH.exists(),
        bepinex_bundle=_find_bepinex_bundle(),
    )

    if state.python_exe is None:
        state.issues.append("Python runtime not found")
    elif not state.runtime_venv_exists:
        if state.python_source == "app_venv":
            state.issues.append(
                "Installed LocalAppData runtime not provisioned yet; "
                f"launcher is falling back to the repo venv ({state.python_exe})"
            )
        elif state.python_source == "current_process" or state.python_source.startswith("path:"):
            state.issues.append(
                "Installed LocalAppData runtime not provisioned yet; "
                f"launcher is falling back to {state.python_exe}"
            )
        else:
            state.issues.append(
                f"Python environment not installed yet ({state.runtime_venv_dir})"
            )
    if state.mtga_dir is None:
        state.issues.append("MTGA install path not detected")
    if state.mtga_dir and not state.bepinex_installed:
        state.issues.append("BepInEx is not installed into MTGA")
    if state.mtga_dir and state.bepinex_installed and not state.plugin_installed:
        state.issues.append("MtgaCoachBridge.dll is not installed into BepInEx/plugins")
    if state.mtga_dir and not state.plugin_built:
        state.issues.append("Plugin DLL has not been built yet")
    return state


def _copy_directory_contents(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def install_bepinex(mtga_dir: Path) -> Path:
    mtga_dir = _normalize_mtga_dir(mtga_dir)
    if not _looks_like_mtga_dir(mtga_dir):
        raise FileNotFoundError(f"MTGA install not found: {mtga_dir}")
    if is_mtga_running():
        raise RuntimeError("Close MTGA before installing or repairing BepInEx")

    bundle = _find_bepinex_bundle()
    if bundle is None:
        raise FileNotFoundError(
            "No BepInEx bundle found in assets/, third_party/, or the repo root"
        )

    if bundle.is_dir():
        _copy_directory_contents(bundle, mtga_dir)
    else:
        with zipfile.ZipFile(bundle) as zf:
            zf.extractall(mtga_dir)

    return mtga_dir / "BepInEx"


def install_plugin(mtga_dir: Path) -> Path:
    mtga_dir = _normalize_mtga_dir(mtga_dir)
    if not _looks_like_mtga_dir(mtga_dir):
        raise FileNotFoundError(f"MTGA install not found: {mtga_dir}")
    if is_mtga_running():
        raise RuntimeError("Close MTGA before installing or updating the bridge plugin")
    if not PLUGIN_BUILD_PATH.exists():
        raise FileNotFoundError(
            f"Built plugin DLL not found: {PLUGIN_BUILD_PATH}"
        )

    plugin_dir = mtga_dir / "BepInEx" / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    target = plugin_dir / PLUGIN_BUILD_PATH.name
    shutil.copy2(PLUGIN_BUILD_PATH, target)
    return target


def repair_bridge_stack(mtga_dir: Path) -> list[Path]:
    changed: list[Path] = []
    state = detect_runtime_state()
    if not state.bepinex_installed:
        changed.append(install_bepinex(mtga_dir))
    changed.append(install_plugin(mtga_dir))
    return changed


def launch_mode(
    *,
    autopilot: bool = False,
    dry_run: bool = False,
    afk: bool = False,
) -> subprocess.Popen:
    python_exe = find_python_executable()
    if python_exe is None:
        raise RuntimeError("Python executable not found")

    cmd = [str(python_exe), str(REPO_DIR / "launcher.py")]
    if autopilot:
        cmd.append("--autopilot")
    if dry_run:
        cmd.append("--dry-run")
    if afk:
        cmd.append("--afk")

    kwargs = {
        "cwd": str(REPO_DIR),
        "env": os.environ.copy(),
    }
    kwargs["env"].setdefault(RUNTIME_ENV_VAR, str(get_runtime_root()))
    src_dir = str(REPO_DIR / "src")
    existing_pythonpath = kwargs["env"].get("PYTHONPATH", "")
    kwargs["env"]["PYTHONPATH"] = (
        src_dir if not existing_pythonpath else src_dir + os.pathsep + existing_pythonpath
    )
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    return subprocess.Popen(cmd, **kwargs)


def run_setup_wizard() -> subprocess.Popen:
    python_exe = find_python_executable()
    if python_exe is None:
        raise RuntimeError("Python executable not found")

    kwargs = {
        "cwd": str(REPO_DIR),
        "env": os.environ.copy(),
    }
    kwargs["env"].setdefault(RUNTIME_ENV_VAR, str(get_runtime_root()))
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    return subprocess.Popen(
        [str(python_exe), str(REPO_DIR / "setup_wizard.py")],
        **kwargs,
    )


def open_path(path: Path) -> None:
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
        return
    raise RuntimeError("open_path is only implemented for Windows")


def find_zombie_processes() -> list[dict[str, str]]:
    """Find orphaned mtgacoach Python processes (excludes the current process).

    Identifies matches by command-line keywords, the stale lock-file PID, and
    executable paths under known mtgacoach venv directories.
    """
    if os.name != "nt":
        return []

    my_pid = os.getpid()

    # Read lock-file PID if available.
    lock_pid: Optional[int] = None
    lock_file = SETTINGS_DIR / "launcher.lock"
    try:
        payload = json.loads(lock_file.read_text(encoding="utf-8") or "{}")
        lock_pid = int(payload["pid"])
    except Exception:
        pass

    # Known venv prefixes (lowered for comparison).
    venv_prefixes: list[str] = []
    for root in {get_runtime_root(), REPO_DIR}:
        for name in ("venv", ".venv"):
            venv_prefixes.append(str(root / name).lower())

    # Enumerate python/pythonw processes via wmic.
    try:
        result = subprocess.run(
            [
                "wmic", "process", "where", "name like 'python%'",
                "get", "ProcessId,CommandLine,ExecutablePath",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []

    # Parse key=value list format (records separated by blank lines).
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            current[key.strip()] = value.strip()
    if current:
        records.append(current)

    cmd_markers = ("arenamcp", "launcher.py", "mtgacoach", "launcher_gui")
    zombies: list[dict[str, str]] = []

    for rec in records:
        try:
            pid = int(rec.get("ProcessId", "0"))
        except ValueError:
            continue
        if pid == 0 or pid == my_pid:
            continue

        cmdline = rec.get("CommandLine", "")
        exe_path = rec.get("ExecutablePath", "")
        cmdline_lower = cmdline.lower()
        exe_lower = exe_path.lower()

        reasons: list[str] = []
        if any(m in cmdline_lower for m in cmd_markers):
            reasons.append("command line match")
        if lock_pid is not None and pid == lock_pid:
            reasons.append("lock file owner")
        if any(exe_lower.startswith(p) for p in venv_prefixes):
            reasons.append("mtgacoach venv")

        if reasons:
            zombies.append({
                "pid": str(pid),
                "cmdline": cmdline or "(unavailable)",
                "reason": ", ".join(reasons),
            })

    return zombies


def kill_zombie_processes() -> tuple[list[dict[str, str]], bool]:
    """Kill orphaned mtgacoach Python processes and clean up the stale lock file.

    Returns ``(killed_list, lock_file_cleaned)``.
    """
    zombies = find_zombie_processes()

    for z in zombies:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", z["pid"]],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            z["status"] = "killed"
        except Exception as exc:
            z["status"] = f"failed: {exc}"

    lock_cleaned = False
    lock_file = SETTINGS_DIR / "launcher.lock"
    try:
        if lock_file.exists():
            lock_file.unlink()
            lock_cleaned = True
    except Exception:
        pass

    return zombies, lock_cleaned


def tail_text(path: Optional[Path], *, max_bytes: int = 8192) -> str:
    if path is None or not path.exists():
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
        return data[-max_bytes:]
    except Exception as exc:
        return f"Failed to read {path}: {exc}"
