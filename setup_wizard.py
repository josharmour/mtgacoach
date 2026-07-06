#!/usr/bin/env python3
"""
mtgacoach Interactive Setup Wizard

Guides users through environment setup: venv creation, dependency installation,
LLM mode selection (online via mtgacoach.com or local via Ollama/LM Studio),
model selection, language configuration, and settings persistence.

Runs with system Python (no venv needed). Uses only stdlib modules.
"""

import argparse

# Modes invoked by automation (launch.bat, setup splash, Repair) — these
# must never prompt or block on stdin.
_AUTOMATION_FLAGS = {"--setup-environment", "--create-venv"}
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Prevent UnicodeEncodeError on Windows consoles using cp1252/cp437/etc.
# Replace unencodable characters with '?' instead of crashing.
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass

# -- Constants ----------------------------------------------------------------

# Source is fetched over HTTPS (GitHub API + release/branch ZIP archives)
# rather than via git, so a git install is not required for setup or updates.
GITHUB_OWNER_REPO = "josharmour/mtgacoach"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_OWNER_REPO}/releases/latest"
GITHUB_API_TAGS = f"https://api.github.com/repos/{GITHUB_OWNER_REPO}/tags"
GITHUB_ARCHIVE = f"https://github.com/{GITHUB_OWNER_REPO}/archive"
SOURCE_ZIP_URL = f"{GITHUB_ARCHIVE}/refs/heads/master.zip"
USER_AGENT = "mtgacoach-setup"
IS_WIN = sys.platform == "win32"
SETTINGS_DIR = Path.home() / ".arenamcp"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
RUNTIME_ENV_VAR = "MTGACOACH_RUNTIME_ROOT"

# ROOT is resolved at runtime -- see _resolve_root().
# These are set by _init_paths() after ROOT is known.
ROOT: Path = Path(".")
RUNTIME_ROOT: Path = Path(".")
VENV_DIR: Path = Path(".")
PIP_PATH: Path = Path(".")
PYTHON_PATH: Path = Path(".")
ENV_FILE: Path = Path(".")


def _is_repo_dir(p: Path) -> bool:
    """Return True if *p* looks like the mtgacoach repo root."""
    return (p / "pyproject.toml").exists() and (p / "src" / "arenamcp").is_dir()


def _running_as_exe() -> bool:
    """True when bundled by PyInstaller."""
    return getattr(sys, "frozen", False)


def _find_system_python() -> str:
    """Return the path to a real Python interpreter.

    When running as a PyInstaller exe, sys.executable is the .exe itself,
    which cannot be used to create venvs or run ``-m pip``.  This function
    finds the actual system Python.
    """
    if not _running_as_exe():
        return sys.executable

    # Try common names in order of preference
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            # Verify it's a real interpreter, not us
            try:
                result = subprocess.run(
                    [found, "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode == 0 and "Python" in result.stdout:
                    return found
            except Exception:
                continue
    # Last resort
    return "python"


def _powershell_single_quote(value: str) -> str:
    """Escape a string for a PowerShell single-quoted literal."""
    return value.replace("'", "''")


def _create_windows_shortcut(
    shortcut_path: Path,
    *,
    target_path: Path,
    arguments: str = "",
    working_directory: Path | None = None,
    icon_path: Path | None = None,
    description: str = "",
) -> None:
    """Create a .lnk shortcut using the built-in WScript.Shell COM object."""
    script_lines = [
        "$shell = New-Object -ComObject WScript.Shell",
        f"$shortcut = $shell.CreateShortcut('{_powershell_single_quote(str(shortcut_path))}')",
        f"$shortcut.TargetPath = '{_powershell_single_quote(str(target_path))}'",
    ]
    if arguments:
        script_lines.append(
            f"$shortcut.Arguments = '{_powershell_single_quote(arguments)}'"
        )
    if working_directory is not None:
        script_lines.append(
            f"$shortcut.WorkingDirectory = '{_powershell_single_quote(str(working_directory))}'"
        )
    if icon_path is not None:
        script_lines.append(
            f"$shortcut.IconLocation = '{_powershell_single_quote(str(icon_path))}'"
        )
    if description:
        script_lines.append(
            f"$shortcut.Description = '{_powershell_single_quote(description)}'"
        )
    script_lines.append("$shortcut.Save()")

    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "; ".join(script_lines),
        ],
        check=True,
    )


def _http_get_json(url: str, timeout: int = 10) -> object:
    """GET *url* and decode the JSON body (GitHub API)."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_latest_remote_version() -> str:
    """Return the latest release/tag version (no leading ``v``), or ``""``."""
    # Prefer the published "latest release".
    try:
        data = _http_get_json(GITHUB_API_LATEST)
        if isinstance(data, dict):
            tag = str(data.get("tag_name", "")).strip()
            if tag:
                return tag.lstrip("v")
    except Exception:
        pass
    # Fall back to the tags list (repos that tag without formal releases).
    try:
        tags = _http_get_json(GITHUB_API_TAGS)
        best: tuple[int, ...] = (0,)
        best_str = ""
        for entry in tags if isinstance(tags, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            try:
                version_tuple = tuple(int(x) for x in name.lstrip("v").split("."))
            except (ValueError, TypeError):
                continue
            if version_tuple > best:
                best = version_tuple
                best_str = name.lstrip("v")
        return best_str
    except Exception:
        return ""


def _merge_tree(source: Path, dest: Path) -> None:
    """Copy *source* into *dest*, overwriting files but never touching ``.git``."""
    for child in source.iterdir():
        if child.name == ".git":
            continue
        target = dest / child.name
        if child.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _merge_tree(child, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _download_zip_into(urls: list[str], dest_root: Path, timeout: int = 120) -> bool:
    """Download the first reachable ZIP and merge it into *dest_root*."""
    data = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            break
        except Exception:
            continue
    if data is None:
        return False
    tmp_dir = Path(tempfile.mkdtemp(prefix="mtgacoach-dl-"))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(tmp_dir)
        # GitHub archives extract to a single ``<repo>-<ref>/`` directory.
        roots = [p for p in tmp_dir.iterdir() if p.is_dir()]
        if len(roots) != 1:
            return False
        _merge_tree(roots[0], dest_root)
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _download_release_zip(version: str, dest_root: Path) -> bool:
    """Download the release tag ZIP for *version* and merge into *dest_root*."""
    return _download_zip_into(
        [
            f"{GITHUB_ARCHIVE}/refs/tags/v{version}.zip",
            f"{GITHUB_ARCHIVE}/refs/tags/{version}.zip",
        ],
        dest_root,
    )


def _resolve_root() -> Path:
    """Figure out where the mtgacoach repo lives.

    Priority:
    1. If the script/exe sits inside the repo, use that.
    2. If an existing install is recorded in settings, reuse it.
    3. Ask the user to point at an existing copy, or download the source
       ZIP over HTTPS (no git required).
    """
    # When running as PyInstaller exe, __file__ is inside a temp dir.
    # Use the directory containing the exe instead.
    if _running_as_exe():
        exe_dir = Path(sys.executable).resolve().parent
    else:
        exe_dir = Path(__file__).resolve().parent

    # 1. Already inside the repo?
    if _is_repo_dir(exe_dir):
        return exe_dir

    # 2. Previous install recorded in settings?
    settings = load_settings()
    saved = settings.get("install_dir")
    if saved:
        saved_path = Path(saved)
        if _is_repo_dir(saved_path):
            return saved_path

    # 3. Interactive -- ask the user. NEVER in automation modes: the
    # parent (launch.bat / setup splash / Repair) holds our stdin pipe and
    # prompt_choice would block or spin-flood forever (audit #16).
    if _AUTOMATION_FLAGS & set(sys.argv[1:]):
        print("    ERROR: mtgacoach files not found and this is an automated")
        print("    run — cannot prompt. Reinstall the app.")
        sys.exit(2)
    print()
    print("    The setup wizard could not find the mtgacoach files.")
    print()
    print("    mtgacoach is normally installed via the Windows installer,")
    print("    which unpacks all of the files for you. If you already have a")
    print("    copy, point me at it; otherwise I can download the source.")
    print()
    print("    [1] I already have a copy -- let me type the path")
    print("    [2] Download the source into a folder I choose")
    print()
    choice = prompt_choice(["Existing copy", "Download source"])

    if choice == 1:
        while True:
            raw = prompt_input("Path to mtgacoach folder")
            p = Path(raw).expanduser().resolve()
            if _is_repo_dir(p):
                return p
            print(f"    '{p}' doesn't look like the mtgacoach repo (no pyproject.toml + src/arenamcp).")
            if not prompt_yn("Try another path?", default=True):
                sys.exit(1)
    else:
        default_parent = Path.home()
        parent = Path(prompt_input("Parent folder for download", str(default_parent))).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        dest = parent / "mtgacoach"
        if dest.exists() and _is_repo_dir(dest):
            print(f"    Found existing copy at {dest}")
            return dest
        dest.mkdir(parents=True, exist_ok=True)
        print(f"    Downloading the latest source into {dest} ...")
        downloaded = False
        version = _fetch_latest_remote_version()
        if version:
            downloaded = _download_release_zip(version, dest)
        if not downloaded:
            # Fall back to the default branch archive.
            downloaded = _download_zip_into([SOURCE_ZIP_URL], dest)
        if not downloaded or not _is_repo_dir(dest):
            print()
            print("    ERROR: could not download the source automatically.")
            print("    Download it manually from:")
            print(f"      {SOURCE_ZIP_URL}")
            print("    extract it, then re-run this wizard from that folder.")
            sys.exit(1)
        print(f"    Source ready at {dest}")
        return dest


def _init_paths(root: Path) -> None:
    """Set the module-level path constants from the resolved ROOT."""
    global ROOT, RUNTIME_ROOT, VENV_DIR, PIP_PATH, PYTHON_PATH, ENV_FILE
    ROOT = root
    runtime_raw = os.environ.get(RUNTIME_ENV_VAR, "").strip()
    if runtime_raw:
        RUNTIME_ROOT = Path(os.path.expandvars(os.path.expanduser(runtime_raw)))
    elif IS_WIN:
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        RUNTIME_ROOT = Path(local_appdata) / "mtgacoach" if local_appdata else ROOT
    else:
        RUNTIME_ROOT = ROOT

    VENV_DIR = RUNTIME_ROOT / "venv"
    PIP_PATH = VENV_DIR / ("Scripts" if IS_WIN else "bin") / ("pip.exe" if IS_WIN else "pip")
    PYTHON_PATH = VENV_DIR / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")
    ENV_FILE = RUNTIME_ROOT / ".env"
MTGA_LOG_DEFAULT = (
    Path(os.environ.get("APPDATA", "")) / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
    if IS_WIN else Path.home() / ".wine" / "MTGA" / "Player.log"  # unlikely but placeholder
)

VLLM_DEFAULT_URL = "http://localhost:8000/v1"
OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"
LMSTUDIO_DEFAULT_URL = "http://localhost:1234/v1"
ONLINE_API_URL = "https://api.mtgacoach.com/v1"
SUBSCRIBE_URL = "https://mtgacoach.com/subscribe"

# Supported languages for TTS (Kokoro) and STT (Whisper)
LANGUAGES = [
    ("en", "English"),
    ("de", "German / Deutsch"),
    ("es", "Spanish / Espanol"),
    ("fr", "French / Francais"),
    ("it", "Italian / Italiano"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("pt", "Portuguese / Portugues"),
    ("zh", "Chinese"),
    ("hi", "Hindi"),
    ("nl", "Dutch / Nederlands (STT only, TTS falls back to English)"),
]


# -- Helpers ------------------------------------------------------------------

def print_header(step_num: int, title: str) -> None:
    """Print a colored section header."""
    bar = "-" * (50 - len(title) - 1)
    print(f"\n[{step_num}] {title.upper()} {bar}")


def prompt_choice(options: list[str], prompt_text: str = "Choice") -> int:
    """Show a numbered menu and return the 1-based selection."""
    while True:
        try:
            raw = input(f"\n    {prompt_text} [{'/'.join(str(i+1) for i in range(len(options)))}]: ").strip()
            idx = int(raw)
            if 1 <= idx <= len(options):
                return idx
        except (ValueError, EOFError):
            pass
        print(f"    Please enter a number between 1 and {len(options)}.")


def prompt_input(label: str, default: str = "") -> str:
    """Prompt for text input with an optional default."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"    {label}{suffix}: ").strip()
    except EOFError:
        raw = ""
    return raw or default


def prompt_yn(label: str, default: bool = False) -> bool:
    """Yes/No prompt."""
    hint = "[y/N]" if not default else "[Y/n]"
    try:
        raw = input(f"    {label} {hint}: ").strip().lower()
    except EOFError:
        raw = ""
    if not raw:
        return default
    return raw.startswith("y")


def ok(msg: str) -> None:
    print(f"    [OK] {msg}")


def fail(msg: str) -> None:
    print(f"    [!!] {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def run_pip(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run pip inside the venv."""
    cmd = [str(PIP_PATH)] + args
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    return subprocess.run(cmd, cwd=str(ROOT))


def check_url(url: str, timeout: int = 3) -> bool:
    """Return True if a GET to url succeeds."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:
        return False


def fetch_models_from_url(base_url: str, api_key: str = "", timeout: int = 5) -> list[str]:
    """Fetch model IDs from an OpenAI-compatible /models endpoint."""
    try:
        url = f"{base_url.rstrip('/')}/models"
        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
            return [m.get("id", "?") for m in body.get("data", [])]
    except Exception:
        return []


def fetch_ollama_models() -> list[str]:
    """List locally available Ollama models via CLI."""
    try:
        result = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=10,
        )
        lines = [l for l in result.stdout.strip().splitlines() if l and not l.startswith("NAME")]
        return [line.split()[0] for line in lines]
    except Exception:
        return []


def read_env(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict, preserving order via dict."""
    data: dict[str, str] = {}
    if not path.exists():
        return data
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip()
    return data


def write_env(path: Path, data: dict[str, str]) -> None:
    """Write a dict as a .env file with a header comment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# mtgacoach Configuration",
        "# Generated by setup_wizard.py",
        "",
    ]
    for key, value in data.items():
        lines.append(f"{key}={value}")
    lines.append("")  # trailing newline
    with open(path, "w") as f:
        f.write("\n".join(lines))


def load_settings() -> dict:
    """Load existing settings.json or return empty dict."""
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_settings(data: dict) -> None:
    """Write settings dict to ~/.arenamcp/settings.json."""
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# -- Detection ----------------------------------------------------------------

def detect_backends() -> dict[str, dict]:
    """Auto-detect which LLM modes are available.

    Returns dict with two keys:
      "online" -> {available: bool, details: str}
      "local"  -> {available: bool, details: str, models: list, provider: str}
    """
    modes: dict[str, dict] = {}

    # 1. Online (mtgacoach.com)
    settings = load_settings()
    has_key = bool(settings.get("license_key"))
    online_reachable = check_url(ONLINE_API_URL.rstrip("/") + "/models")
    modes["online"] = {
        "available": has_key or online_reachable,
        "details": "license key saved" if has_key else ("reachable" if online_reachable else "no license key"),
    }

    # 2. Local (vLLM / Ollama / LM Studio)
    vllm_running = check_url(f"{VLLM_DEFAULT_URL}/models")
    ollama_bin = shutil.which("ollama")
    ollama_running = check_url(f"{OLLAMA_DEFAULT_URL}/models")
    lmstudio_running = check_url(f"{LMSTUDIO_DEFAULT_URL}/models")
    vllm_models: list[str] = []
    ollama_models: list[str] = []
    lmstudio_models: list[str] = []
    provider = ""

    if vllm_running:
        vllm_models = fetch_models_from_url(VLLM_DEFAULT_URL)
        provider = "vllm"

    if ollama_bin or ollama_running:
        ollama_models = fetch_ollama_models()
        if not ollama_models and ollama_running:
            ollama_models = fetch_models_from_url(OLLAMA_DEFAULT_URL)
        if not provider:
            provider = "ollama"

    if lmstudio_running:
        lmstudio_models = fetch_models_from_url(LMSTUDIO_DEFAULT_URL)
        if not provider:
            provider = "lm-studio"

    all_local_models = vllm_models + ollama_models + lmstudio_models
    local_available = bool(vllm_running or ollama_bin or ollama_running or lmstudio_running)

    detail_parts = []
    if vllm_running:
        detail_parts.append(f"vLLM ({len(vllm_models)} model{'s' if len(vllm_models) != 1 else ''})")
    if ollama_running or ollama_bin:
        if ollama_models:
            detail_parts.append(f"Ollama ({len(ollama_models)} models)")
        elif ollama_bin:
            detail_parts.append("Ollama installed but no models")
        else:
            detail_parts.append("Ollama not running")
    if lmstudio_running:
        detail_parts.append(f"LM Studio ({len(lmstudio_models)} models)")
    detail = " + ".join(detail_parts) if detail_parts else "not detected"

    modes["local"] = {
        "available": local_available,
        "details": detail,
        "models": all_local_models,
        "vllm_models": vllm_models,
        "ollama_models": ollama_models,
        "lmstudio_models": lmstudio_models,
        "provider": provider,
        "vllm_running": vllm_running,
        "ollama_running": ollama_running,
        "lmstudio_running": lmstudio_running,
    }

    return modes


# -- Steps --------------------------------------------------------------------

def step_check_python() -> bool:
    """Step 1: Verify Python version."""
    print_header(1, "Check Python")

    if _running_as_exe():
        # We can't trust sys.version_info (it's the bundled Python).
        # Find and verify the system Python instead.
        python = _find_system_python()
        try:
            result = subprocess.run(
                [python, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
                capture_output=True, text=True, timeout=5,
            )
            ver_str = result.stdout.strip()
            parts = tuple(int(x) for x in ver_str.split("."))
            if parts < (3, 10):
                fail(f"Python {ver_str} -- version 3.10+ required")
                info("Please install Python 3.10+ from https://python.org")
                return False
            ok(f"Python {ver_str} (system: {python})")
        except Exception as exc:
            fail(f"Could not find a system Python: {exc}")
            info("Please install Python 3.10+ from https://python.org")
            info("Make sure 'python' is on your PATH.")
            return False
    else:
        v = sys.version_info
        if v < (3, 10):
            fail(f"Python {v.major}.{v.minor}.{v.micro} -- version 3.10+ required")
            info("Please install Python 3.10+ from https://python.org")
            return False
        ok(f"Python {v.major}.{v.minor}.{v.micro}")

    return True


def step_virtual_environment() -> bool:
    """Step 2: Create or reuse venv, upgrade pip."""
    print_header(2, "Virtual Environment")

    venv_alive = False
    if VENV_DIR.exists() and PIP_PATH.exists():
        # Repair-audit #4: existence is not health. A venv whose base
        # interpreter was uninstalled/upgraded fails every pip run forever;
        # probe it and REBUILD instead of dooming every retry.
        try:
            probe = subprocess.run(
                [str(PYTHON_PATH), "-c", "import pip"],
                capture_output=True, text=True, timeout=20,
            )
            venv_alive = probe.returncode == 0
        except Exception:
            venv_alive = False
        if not venv_alive:
            info("venv/ exists but is broken (its Python cannot run) — rebuilding")
            shutil.rmtree(VENV_DIR, ignore_errors=True)

    if venv_alive:
        ok("venv/ exists and responds")
    else:
        python = _find_system_python()
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        info(f"Creating venv (using {python})...")
        result = subprocess.run(
            [python, "-m", "venv", str(VENV_DIR)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            fail("Failed to create virtual environment")
            if result.stderr:
                info(result.stderr.strip())
            return False
        ok("venv/ created")

    # Activate internally for subprocess calls
    if IS_WIN:
        scripts = str(VENV_DIR / "Scripts")
    else:
        scripts = str(VENV_DIR / "bin")
    os.environ["VIRTUAL_ENV"] = str(VENV_DIR)
    os.environ["PATH"] = scripts + os.pathsep + os.environ.get("PATH", "")

    info("Upgrading pip...")
    result = run_pip(["install", "--upgrade", "pip"], capture=True)
    if result.returncode == 0:
        ok("pip upgraded")
    else:
        # Non-fatal -- pip may already be current
        info("pip upgrade skipped (may already be up to date)")

    return True


def step_update_code() -> bool:
    """Step 3: Download the latest release before installing dependencies.

    Uses the GitHub releases API + release ZIP archive over HTTPS, so no git
    install is required. Any local ``.git`` directory is preserved.
    """
    print_header(3, "Update Code")

    # Read local version from pyproject.toml (no package import needed)
    pyproject = ROOT / "pyproject.toml"
    if not pyproject.exists():
        info("No pyproject.toml found -- skipping auto-update.")
        return True

    local_ver = "0.0.0"
    for line in pyproject.read_text().splitlines():
        line = line.strip()
        if line.startswith("version"):
            # version = "x.y.z"
            local_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

    info("Checking for updates...")
    remote_ver = _fetch_latest_remote_version()
    if not remote_ver:
        info("Could not reach GitHub -- skipping update (will install from local code).")
        return True

    try:
        local_tuple = tuple(int(x) for x in local_ver.split("."))
        remote_tuple = tuple(int(x) for x in remote_ver.split("."))
    except (ValueError, TypeError):
        info("Could not compare versions -- skipping update.")
        return True

    if remote_tuple > local_tuple:
        info(f"Update available: v{local_ver} -> v{remote_ver}")
        if prompt_yn("Download latest code before installing?", default=True):
            info("Downloading release archive from GitHub...")
            if _download_release_zip(remote_ver, ROOT):
                ok(f"Updated to v{remote_ver}")
            else:
                fail("Download failed -- installing from current code")
        else:
            info("Skipping update.")
    else:
        ok(f"Already up to date (v{local_ver})")

    return True


def step_install_dependencies() -> bool:
    """Step 4: Install packages from pyproject.toml and extras."""
    print_header(4, "Install Dependencies")

    info("Installing core + voice + LLM packages...")
    # Editable install when we have project source at ROOT (git clone *or*
    # installer-unpacked source); no .git directory required.
    editable = (ROOT / "pyproject.toml").exists()
    full_install = ["install", "-e", ".[full]"] if editable else ["install", ".[full]"]
    base_install = ["install", "-e", "."] if editable else ["install", "."]

    result = run_pip(full_install)
    if result.returncode != 0:
        fail("Some packages from pyproject.toml failed")
        info("Trying base install only...")
        run_pip(base_install)

    # Install extras from requirements.txt not covered by pyproject.toml
    extras = [
        "openai", "websocket-client", "scipy", "Pillow",
        "networkx", "beautifulsoup4", "pyedhrec", "lxml",
        "pyautogui", "pydirectinput-rgx",
    ]
    info("Installing additional packages...")
    result = run_pip(["install"] + extras)
    if result.returncode != 0:
        fail("Some additional packages failed (non-fatal)")
    else:
        ok("All packages installed")

    return True


def run_create_venv_only() -> int:
    if not step_check_python():
        return 1
    if not step_virtual_environment():
        return 1

    settings = load_settings()
    settings["install_dir"] = str(ROOT)
    settings["runtime_root"] = str(RUNTIME_ROOT)
    save_settings(settings)

    print()
    print("    " + "=" * 44)
    print("    Venv ready.")
    print(f"      Runtime: {RUNTIME_ROOT}")
    print("    " + "=" * 44)
    print()
    return 0


def run_setup_environment_only() -> int:
    if not step_check_python():
        return 1
    if not step_virtual_environment():
        return 1
    if not step_install_dependencies():
        return 1

    settings = load_settings()
    settings["install_dir"] = str(ROOT)
    settings["runtime_root"] = str(RUNTIME_ROOT)
    save_settings(settings)

    print()
    print("    " + "=" * 44)
    print("    Environment setup complete.")
    print(f"      Runtime: {RUNTIME_ROOT}")
    print("    " + "=" * 44)
    print()
    return 0


def step_detect_and_choose_backend(settings: dict) -> tuple[str, str]:
    """Step 5: Choose mode (online or local). Returns (mode, model)."""
    print_header(5, "LLM Backend")

    info("Scanning for available backends...\n")
    modes = detect_backends()

    online_info = modes["online"]
    local_info = modes["local"]

    online_tag = f" [{online_info['details']}]" if online_info["available"] else ""
    local_tag = f" [{local_info['details']}]" if local_info["available"] else ""

    print(f"    [1] Online (mtgacoach.com subscription){online_tag}")
    print(f"        Cloud-hosted models, no GPU needed")
    print(f"    [2] Local (Ollama / LM Studio){local_tag}")
    print(f"        Run models on your own hardware")

    print()
    choice = prompt_choice(["Online (mtgacoach.com)", "Local (Ollama / LM Studio)"], "Select mode")

    model = ""

    # -- Mode 1: Online --
    if choice == 1:
        mode = "online"
        info("")
        info("An mtgacoach.com subscription is required for online mode.")
        info(f"Subscribe at: {SUBSCRIBE_URL}")
        print()

        existing_key = settings.get("license_key", "")
        license_key = prompt_input("License key", existing_key)
        settings["license_key"] = license_key

        if license_key:
            info("Testing connection to mtgacoach.com...")
            try:
                req = urllib.request.Request(
                    ONLINE_API_URL.rstrip("/") + "/models",
                    method="GET",
                )
                req.add_header("Authorization", f"Bearer {license_key}")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    body = json.loads(resp.read().decode())
                    available_models = [m.get("id", "?") for m in body.get("data", [])]
                ok("Connected to mtgacoach.com")
                if available_models:
                    ok(f"{len(available_models)} model(s) available:")
                    for i, m in enumerate(available_models[:10], 1):
                        info(f"  [{i}] {m}")
                    print()
                    raw = prompt_input("Model", available_models[0])
                    try:
                        idx = int(raw)
                        if 1 <= idx <= len(available_models):
                            model = available_models[idx - 1]
                        else:
                            model = raw
                    except ValueError:
                        model = raw
            except Exception:
                fail("Could not connect to mtgacoach.com (check your license key)")
                model = prompt_input("Model name (enter manually)", "")
        else:
            fail("No license key entered")
            info(f"Get one at: {SUBSCRIBE_URL}")

    # -- Mode 2: Local --
    else:
        mode = "local"

        vllm_running = local_info.get("vllm_running", False)
        ollama_running = local_info.get("ollama_running", False)
        lmstudio_running = local_info.get("lmstudio_running", False)
        ollama_bin = shutil.which("ollama")
        vllm_models = local_info.get("vllm_models", [])
        ollama_models = local_info.get("ollama_models", [])
        lmstudio_models = local_info.get("lmstudio_models", [])

        # Build a menu of every provider that's actually running. vLLM wins ties.
        candidates: list[tuple[str, str, str, list[str], str]] = []  # (label, provider, url, models, api_key)
        if vllm_running:
            candidates.append((f"vLLM ({len(vllm_models)} models)", "vllm",
                               VLLM_DEFAULT_URL, vllm_models, "vllm"))
        if ollama_running or ollama_bin:
            candidates.append((f"Ollama ({len(ollama_models)} models)", "ollama",
                               OLLAMA_DEFAULT_URL, ollama_models, "ollama"))
        if lmstudio_running:
            candidates.append((f"LM Studio ({len(lmstudio_models)} models)", "lm-studio",
                               LMSTUDIO_DEFAULT_URL, lmstudio_models, "lm-studio"))

        if len(candidates) > 1:
            print()
            for i, (label, *_rest) in enumerate(candidates, 1):
                print(f"    [{i}] {label}")
            print()
            sub = prompt_choice([c[0] for c in candidates], "Select provider")
            _label, provider, local_url, available_models, local_api_key = candidates[sub - 1]
            ok(f"{provider} selected")
        elif len(candidates) == 1:
            _label, provider, local_url, available_models, local_api_key = candidates[0]
            ok(f"{provider} detected")
        else:
            fail("No local LLM server detected")
            info("Start vLLM (recommended) or install Ollama from https://ollama.ai")
            info("  or LM Studio from https://lmstudio.ai")
            if not prompt_yn("Continue anyway?"):
                return "local", model
            provider = "vllm"
            local_url = VLLM_DEFAULT_URL
            local_api_key = "vllm"
            available_models = []

        if available_models:
            ok(f"{len(available_models)} model(s) found:")
            for i, m in enumerate(available_models[:10], 1):
                info(f"  [{i}] {m}")
            print()
            info("Enter the number of a model above, or type a model name.")
            raw = prompt_input("Model", available_models[0])
            try:
                idx = int(raw)
                if 1 <= idx <= len(available_models):
                    model = available_models[idx - 1]
                else:
                    model = raw
            except ValueError:
                model = raw
        elif provider == "ollama":
            fail("No models pulled yet")
            info("Browse available models at: https://ollama.com/library")
            model_name = prompt_input("Model to pull (e.g. llama3.2, mistral, phi3)", "llama3.2")
            if prompt_yn(f"Pull {model_name} now?", default=True):
                info(f"Pulling {model_name} -- this may take a while...")
                pull = subprocess.run(["ollama", "pull", model_name])
                if pull.returncode == 0:
                    ok(f"{model_name} ready")
                    model = model_name
                else:
                    fail(f"Pull failed -- retry manually: ollama pull {model_name}")
            if not model:
                model = model_name
        else:
            info("No models found -- load a model in LM Studio first.")
            model = prompt_input("Model name", "")

        settings["local_url"] = local_url
        settings["local_model"] = model
        settings["local_api_key"] = local_api_key

    return mode, model


def step_language(settings: dict) -> str:
    """Step 6: Choose spoken language for TTS and STT."""
    print_header(6, "Language")

    info("Choose the language for voice output (TTS) and input (STT).\n")
    for i, (code, name) in enumerate(LANGUAGES, 1):
        current = " (current)" if code == settings.get("language", "en") else ""
        print(f"    [{i:2d}] {code:5s}  {name}{current}")

    print()
    choice = prompt_choice([name for _, name in LANGUAGES], "Language")
    lang_code = LANGUAGES[choice - 1][0]
    lang_name = LANGUAGES[choice - 1][1]
    ok(f"Language: {lang_name} ({lang_code})")
    return lang_code


def step_voice_mode(settings: dict) -> str:
    """Step 7: Choose voice input mode."""
    print_header(7, "Voice Input")

    info("How do you want to talk to the coach?\n")
    print(textwrap.dedent("""\
        [1] Push-to-Talk (recommended)
            Hold F4 to speak, release to send

        [2] Voice Activation
            Auto-detects when you start talking

        [3] Disabled
            No voice input (keyboard only)
    """))

    choice = prompt_choice(["Push-to-Talk", "Voice Activation", "Disabled"])
    modes = ["ptt", "vox", "none"]
    return modes[choice - 1]


def step_verify(settings: dict) -> None:
    """Step 8: Quick connectivity and path checks."""
    print_header(8, "Verify")

    mode = settings.get("mode", "local")

    # Test backend
    if mode == "online":
        info("Testing connection to mtgacoach.com...")
        license_key = settings.get("license_key", "")
        try:
            req = urllib.request.Request(
                ONLINE_API_URL.rstrip("/") + "/models",
                method="GET",
            )
            if license_key:
                req.add_header("Authorization", f"Bearer {license_key}")
            with urllib.request.urlopen(req, timeout=5):
                ok(f"mtgacoach.com API responding")
        except Exception:
            fail("Could not reach mtgacoach.com (check your license key and internet)")
    elif mode == "local":
        local_url = settings.get("local_url", VLLM_DEFAULT_URL)
        base = local_url.replace("/v1", "")
        info("Testing local LLM connection...")
        if check_url(f"{local_url}/models"):
            ok(f"Local LLM API responding ({local_url})")
        elif "11434" in local_url and check_url(f"{base}/api/tags"):
            # Ollama-specific fallback: endpoint up but /v1/models flaky.
            ok(f"Local LLM responding ({base})")
        else:
            fail(f"Local LLM not responding at {local_url} (is your vLLM/Ollama/LM Studio server running?)")

    # Test MTGA log path
    info("Checking MTGA log path...")
    if MTGA_LOG_DEFAULT.exists():
        ok(f"Player.log found at {MTGA_LOG_DEFAULT}")
    else:
        fail(f"Player.log not found at {MTGA_LOG_DEFAULT}")
        info("This is normal if MTGA hasn't been run yet.")
        info("The coach will find it automatically when MTGA starts.")

    # Success banner
    print()
    print("    " + "=" * 44)
    print("    Setup complete! Configuration saved to:")
    print(f"      {SETTINGS_FILE}")
    print(f"      Runtime root: {RUNTIME_ROOT}")
    print()
    print("    Launch mtgacoach with:")
    print("      launch.vbs  (double-click / shortcut)")
    print("      launch.bat  (from a console)")
    if mode == "local":
        info("")
        info(f"  Or: python -m arenamcp.standalone --mode local --model {settings.get('local_model', 'llama3.2')}")
    print("    " + "=" * 44)
    print()


def step_desktop_shortcut() -> None:
    """Step 9: Optionally create a desktop shortcut."""
    print_header(9, "Desktop Shortcut")

    if not IS_WIN:
        info("Desktop shortcut creation is only supported on Windows.")
        return

    if not prompt_yn("Create a desktop shortcut?", default=True):
        info("Skipping shortcut.")
        return

    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        fail("Desktop folder not found -- skipping.")
        return

    shortcut_path = desktop / "mtgacoach Launcher.lnk"
    launcher_script = ROOT / "launch.vbs"
    if not launcher_script.exists():
        fail(f"Launcher entrypoint not found: {launcher_script}")
        return

    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    wscript_path = system_root / "System32" / "wscript.exe"
    icon_path = ROOT / "mtga_coach.ico"
    if not icon_path.exists():
        fallback_icon = ROOT / "icon.ico"
        icon_path = fallback_icon if fallback_icon.exists() else None

    try:
        _create_windows_shortcut(
            shortcut_path,
            target_path=wscript_path,
            arguments=f'"{launcher_script}"',
            working_directory=ROOT,
            icon_path=icon_path,
            description="Launch mtgacoach",
        )
        ok(f"Shortcut created: {shortcut_path}")
    except Exception as exc:
        fail(f"Failed to create shortcut: {exc}")


# -- Main ---------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--create-venv", action="store_true")
    parser.add_argument("--setup-environment", action="store_true")
    args, _unknown = parser.parse_known_args()

    print()
    print("=" * 52)
    print("  mtgacoach Setup Wizard")
    print("=" * 52)

    # Step 0: Resolve where the repo lives (handles exe-from-Downloads, etc.)
    root = _resolve_root()
    _init_paths(root)
    ok(f"Repo: {ROOT}")
    ok(f"Runtime root: {RUNTIME_ROOT}")

    # Persist install dir so re-runs find it automatically
    settings = load_settings()
    settings["install_dir"] = str(ROOT)
    settings["runtime_root"] = str(RUNTIME_ROOT)
    save_settings(settings)

    if args.create_venv:
        return run_create_venv_only()

    if args.setup_environment:
        return run_setup_environment_only()

    # -- Existing repo detected -> offer quick update --
    has_venv = VENV_DIR.exists() and PIP_PATH.exists()
    has_settings = bool(settings.get("mode"))

    if has_venv or has_settings:
        print()
        print("    Existing installation detected.")
        if has_venv:
            print(f"      venv:    {VENV_DIR}")
        if has_settings:
            print(f"      mode:    {settings.get('mode')}/{settings.get('local_model', settings.get('model', 'default'))}")
        print()
        print("    [1] Quick update (pull code + reinstall deps)")
        print("    [2] Full setup  (reconfigure everything)")
        print()
        mode = prompt_choice(["Quick update", "Full setup"])

        if mode == 1:
            # Quick update: python check -> venv -> pull -> deps -> done
            if not step_check_python():
                return 1
            if not step_virtual_environment():
                return 1
            if not step_update_code():
                return 1
            if not step_install_dependencies():
                return 1
            print()
            print("    " + "=" * 44)
            print("    Update complete!")
            print("    Close this window and use the mtgacoach")
            print("    app to launch the coach.")
            print("    " + "=" * 44)
            print()
            return 0

    # -- Full setup (environment bootstrap only) --
    # Backend/model/voice/language config is handled in the GUI.

    # Step 1: Python
    if not step_check_python():
        return 1

    # Step 2: Venv
    if not step_virtual_environment():
        return 1

    # Step 3: Update code from GitHub (before installing deps)
    if not step_update_code():
        return 1

    # Step 4: Dependencies
    if not step_install_dependencies():
        return 1

    # Save paths to settings
    save_settings(settings)

    print()
    print("    " + "=" * 44)
    print("    Setup complete!")
    print(f"      Runtime: {RUNTIME_ROOT}")
    print()
    print("    Close this window and use the mtgacoach")
    print("    app to configure and launch the coach.")
    print("    " + "=" * 44)
    print()

    return 0


def _pause() -> None:
    """Wait for Enter so the console window doesn't vanish."""
    print()
    try:
        input("    Press Enter to exit...")
    except EOFError:
        pass


if __name__ == "__main__":
    _interactive = not (_AUTOMATION_FLAGS & set(sys.argv[1:]))
    try:
        code = main()
        if _interactive:
            _pause()
        sys.exit(code)
    except KeyboardInterrupt:
        print("\n\n    Setup cancelled.")
        if _interactive:
            _pause()
        sys.exit(1)
    except Exception as exc:
        print(f"\n    FATAL ERROR: {exc}")
        import traceback
        traceback.print_exc()
        if _interactive:
            _pause()
        sys.exit(1)
