#!/usr/bin/env python3
"""
ArenaMCP Interactive Setup Wizard

Guides users through environment setup: venv creation, dependency installation,
LLM mode selection (online via mtgacoach.com or local via Ollama/LM Studio),
model selection, language configuration, and settings persistence.

Runs with system Python (no venv needed). Uses only stdlib modules.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.error
import urllib.request
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

GITHUB_REPO = "https://github.com/josharmour/mtgacoach.git"
IS_WIN = sys.platform == "win32"
SETTINGS_DIR = Path.home() / ".arenamcp"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# ROOT is resolved at runtime -- see _resolve_root().
# These are set by _init_paths() after ROOT is known.
ROOT: Path = Path(".")
VENV_DIR: Path = Path(".")
PIP_PATH: Path = Path(".")
PYTHON_PATH: Path = Path(".")
ENV_FILE: Path = Path(".")


def _is_repo_dir(p: Path) -> bool:
    """Return True if *p* looks like the ArenaMCP repo root."""
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


def _resolve_root() -> Path:
    """Figure out where the ArenaMCP repo lives.

    Priority:
    1. If the script/exe sits inside the repo, use that.
    2. If an existing clone is recorded in settings, reuse it.
    3. Ask the user to point at an existing clone or pick a directory
       for a fresh ``git clone``.
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

    # 3. Interactive -- ask the user
    print()
    print("    The setup wizard needs to know where ArenaMCP is (or should be).")
    print()
    print("    [1] I already have a git clone -- let me type the path")
    print("    [2] Clone fresh into a folder I choose")
    print()
    choice = prompt_choice(["Existing clone", "Clone fresh"])

    if choice == 1:
        while True:
            raw = prompt_input("Path to ArenaMCP folder")
            p = Path(raw).expanduser().resolve()
            if _is_repo_dir(p):
                return p
            print(f"    '{p}' doesn't look like the ArenaMCP repo (no pyproject.toml + src/arenamcp).")
            if not prompt_yn("Try another path?", default=True):
                sys.exit(1)
    else:
        default_parent = Path.home()
        parent = Path(prompt_input("Parent folder for clone", str(default_parent))).expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        dest = parent / "ArenaMCP"
        if dest.exists() and _is_repo_dir(dest):
            print(f"    Found existing clone at {dest}")
            return dest
        if not shutil.which("git"):
            print()
            print("    ERROR: git is not installed (or not on your PATH).")
            print()
            if IS_WIN:
                print("    Install it from https://git-scm.com/download/win")
                print("    or run:  winget install Git.Git")
            else:
                print("    Install it with your package manager, e.g.:")
                print("      sudo apt install git   # Debian/Ubuntu")
                print("      brew install git        # macOS")
            print()
            print("    After installing, restart this wizard.")
            sys.exit(1)
        print(f"    Cloning {GITHUB_REPO} into {dest} ...")
        result = subprocess.run(
            ["git", "clone", GITHUB_REPO, str(dest)],
            timeout=120,
        )
        if result.returncode != 0:
            print("    ERROR: git clone failed.")
            sys.exit(1)
        return dest


def _init_paths(root: Path) -> None:
    """Set the module-level path constants from the resolved ROOT."""
    global ROOT, VENV_DIR, PIP_PATH, PYTHON_PATH, ENV_FILE
    ROOT = root
    VENV_DIR = ROOT / "venv"
    PIP_PATH = VENV_DIR / ("Scripts" if IS_WIN else "bin") / ("pip.exe" if IS_WIN else "pip")
    PYTHON_PATH = VENV_DIR / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")
    ENV_FILE = ROOT / ".env"
MTGA_LOG_DEFAULT = (
    Path(os.environ.get("APPDATA", "")) / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
    if IS_WIN else Path.home() / ".wine" / "MTGA" / "Player.log"  # unlikely but placeholder
)

OLLAMA_DEFAULT_URL = "http://localhost:11434/v1"
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
    lines = [
        "# ArenaMCP Configuration",
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

    # 2. Local (Ollama / LM Studio)
    ollama_bin = shutil.which("ollama")
    ollama_running = check_url("http://localhost:11434/v1/models")
    lmstudio_running = check_url("http://localhost:1234/v1/models")
    ollama_models: list[str] = []
    lmstudio_models: list[str] = []
    provider = ""

    if ollama_bin or ollama_running:
        ollama_models = fetch_ollama_models()
        if not ollama_models and ollama_running:
            ollama_models = fetch_models_from_url(OLLAMA_DEFAULT_URL)
        provider = "ollama"

    if lmstudio_running:
        lmstudio_models = fetch_models_from_url("http://localhost:1234/v1")
        if not provider:
            provider = "lm-studio"

    all_local_models = ollama_models + lmstudio_models
    local_available = bool(ollama_bin or ollama_running or lmstudio_running)

    if ollama_running and lmstudio_running:
        detail = f"Ollama ({len(ollama_models)} models) + LM Studio ({len(lmstudio_models)} models)"
    elif ollama_running or ollama_bin:
        detail = f"Ollama: {len(ollama_models)} model(s)" if ollama_models else ("Ollama installed but no models" if ollama_bin else "Ollama not running")
    elif lmstudio_running:
        detail = f"LM Studio: {len(lmstudio_models)} model(s)"
    else:
        detail = "not detected"

    modes["local"] = {
        "available": local_available,
        "details": detail,
        "models": all_local_models,
        "ollama_models": ollama_models,
        "lmstudio_models": lmstudio_models,
        "provider": provider,
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

    if VENV_DIR.exists() and PIP_PATH.exists():
        ok("venv/ exists")
    else:
        python = _find_system_python()
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
    """Step 3: Pull latest code from git before installing dependencies."""
    print_header(3, "Update Code")

    git_bin = shutil.which("git")
    if not git_bin:
        info("git not found on PATH -- skipping auto-update.")
        return True

    # Check if we're inside a git repo
    check = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True, text=True, cwd=str(ROOT),
    )
    if check.returncode != 0:
        info("Not a git repository -- skipping auto-update.")
        return True

    # Show current version
    info("Checking for updates...")
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "origin"],
        capture_output=True, text=True, timeout=10, cwd=str(ROOT),
    )
    if result.returncode != 0:
        info("Could not reach remote -- skipping update (will install from local code).")
        return True

    # Parse remote tags to find latest version
    best = (0, 0, 0)
    best_str = ""
    for line in result.stdout.splitlines():
        parts = line.split("refs/tags/")
        if len(parts) != 2:
            continue
        tag = parts[1].strip()
        if tag.endswith("^{}"):
            continue
        ver_str = tag.lstrip("v")
        try:
            ver_tuple = tuple(int(x) for x in ver_str.split("."))
            if ver_tuple > best:
                best = ver_tuple
                best_str = ver_str
        except (ValueError, TypeError):
            continue

    # Read local version from pyproject.toml (no package import needed)
    local_ver = "0.0.0"
    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        for line in pyproject.read_text().splitlines():
            line = line.strip()
            if line.startswith("version"):
                # version = "x.y.z"
                local_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

    local_tuple = tuple(int(x) for x in local_ver.split("."))

    if best > local_tuple and best_str:
        info(f"Update available: v{local_ver} -> v{best_str}")
        if prompt_yn("Pull latest code before installing?", default=True):
            info("Running git pull --ff-only ...")
            pull = subprocess.run(
                ["git", "pull", "--ff-only", "origin", "master"],
                capture_output=True, text=True, timeout=60, cwd=str(ROOT),
            )
            if pull.returncode == 0:
                ok(f"Updated to latest code")
                # Show summary line
                summary = pull.stdout.strip().splitlines()[-1] if pull.stdout.strip() else ""
                if summary:
                    info(summary)
            else:
                stderr = pull.stderr.strip()
                fail("git pull failed -- installing from current code")
                if "not possible to fast-forward" in stderr or "divergent" in stderr:
                    info("Local branch has diverged. Run 'git pull' manually to resolve.")
                elif "uncommitted changes" in stderr or "dirty" in stderr:
                    info("You have uncommitted local changes. Commit or stash them first.")
                else:
                    info(stderr[:200] if stderr else "Unknown error")
        else:
            info("Skipping update.")
    elif best_str:
        ok(f"Already up to date (v{local_ver})")
    else:
        info("No remote tags found -- skipping version check.")

    return True


def step_install_dependencies() -> bool:
    """Step 4: Install packages from pyproject.toml and extras."""
    print_header(4, "Install Dependencies")

    info("Installing core + voice + LLM packages...")
    result = run_pip(["install", "-e", ".[full]"])
    if result.returncode != 0:
        fail("Some packages from pyproject.toml failed")
        info("Trying base install only...")
        run_pip(["install", "-e", "."])

    # Install extras from requirements.txt not covered by pyproject.toml
    extras = [
        "textual", "openai", "websocket-client", "scipy", "Pillow",
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

        ollama_running = local_info.get("ollama_running", False)
        lmstudio_running = local_info.get("lmstudio_running", False)
        ollama_bin = shutil.which("ollama")
        ollama_models = local_info.get("ollama_models", [])
        lmstudio_models = local_info.get("lmstudio_models", [])

        # Determine which local provider to use
        if ollama_running and lmstudio_running:
            print()
            print(f"    [1] Ollama ({len(ollama_models)} models)")
            print(f"    [2] LM Studio ({len(lmstudio_models)} models)")
            print()
            sub = prompt_choice(["Ollama", "LM Studio"], "Select provider")
            if sub == 1:
                provider = "ollama"
                local_url = OLLAMA_DEFAULT_URL
                local_api_key = "ollama"
                available_models = ollama_models
            else:
                provider = "lm-studio"
                local_url = "http://localhost:1234/v1"
                local_api_key = "lm-studio"
                available_models = lmstudio_models
        elif lmstudio_running:
            provider = "lm-studio"
            local_url = "http://localhost:1234/v1"
            local_api_key = "lm-studio"
            available_models = lmstudio_models
            ok("LM Studio detected")
        elif ollama_bin or ollama_running:
            provider = "ollama"
            local_url = OLLAMA_DEFAULT_URL
            local_api_key = "ollama"
            available_models = ollama_models
            if ollama_running:
                ok("Ollama detected")
            else:
                info("Ollama installed but not running")
        else:
            fail("No local LLM server detected")
            info("Install Ollama from https://ollama.ai")
            info("  or LM Studio from https://lmstudio.ai")
            if not prompt_yn("Continue anyway?"):
                return "local", model
            provider = "ollama"
            local_url = OLLAMA_DEFAULT_URL
            local_api_key = "ollama"
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
        local_url = settings.get("local_url", OLLAMA_DEFAULT_URL)
        base = local_url.replace("/v1", "")
        info("Testing local LLM connection...")
        if check_url(f"{local_url}/models"):
            ok(f"Local LLM API responding ({local_url})")
        elif check_url(f"{base}/api/tags"):
            ok(f"Local LLM responding ({base})")
        else:
            fail("Local LLM not responding (start Ollama with: ollama serve)")

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
    print()
    print("    Run the coach with:")
    print("      coach.bat")
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

    shortcut_path = desktop / "ArenaMCP Coach.bat"
    content = f'@echo off\ncd /d "{ROOT}"\ncall coach.bat %*\n'

    try:
        with open(shortcut_path, "w") as f:
            f.write(content)
        ok(f"Shortcut created: {shortcut_path}")
    except Exception as exc:
        fail(f"Failed to create shortcut: {exc}")


# -- Main ---------------------------------------------------------------------

def main() -> int:
    print()
    print("=" * 52)
    print("  ArenaMCP Setup Wizard")
    print("=" * 52)

    # Step 0: Resolve where the repo lives (handles exe-from-Downloads, etc.)
    root = _resolve_root()
    _init_paths(root)
    ok(f"Repo: {ROOT}")

    # Persist install dir so re-runs find it automatically
    settings = load_settings()
    settings["install_dir"] = str(ROOT)
    save_settings(settings)

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
            print("    Update complete! Run the coach with:")
            print("      coach.bat")
            print("    " + "=" * 44)
            print()
            return 0

    # -- Full setup --

    # Step 1: Python
    if not step_check_python():
        return 1

    # Step 2: Venv
    if not step_virtual_environment():
        return 1

    # Step 3: Update code from git (before installing deps)
    if not step_update_code():
        return 1

    # Step 4: Dependencies
    if not step_install_dependencies():
        return 1

    # Step 5: Mode + model
    mode, model = step_detect_and_choose_backend(settings)
    settings["mode"] = mode
    if model:
        settings["model"] = model
        if mode == "local":
            settings["local_model"] = model

    # Step 6: Language
    lang = step_language(settings)
    settings["language"] = lang

    # Step 7: Voice mode
    voice_mode = step_voice_mode(settings)
    settings["voice_mode"] = voice_mode

    # Also write voice mode to .env for backward compat
    if voice_mode != "none":
        env = read_env(ENV_FILE)
        env["VOICE_MODE"] = voice_mode
        write_env(ENV_FILE, env)

    # Save everything to settings.json
    info("\nSaving configuration...")
    save_settings(settings)
    ok(f"Settings saved to {SETTINGS_FILE}")

    # Step 8: Verify
    step_verify(settings)

    # Step 9: Desktop shortcut
    step_desktop_shortcut()

    return 0


def _pause() -> None:
    """Wait for Enter so the console window doesn't vanish."""
    print()
    try:
        input("    Press Enter to exit...")
    except EOFError:
        pass


if __name__ == "__main__":
    try:
        code = main()
        _pause()
        sys.exit(code)
    except KeyboardInterrupt:
        print("\n\n    Setup cancelled.")
        _pause()
        sys.exit(1)
    except Exception as exc:
        print(f"\n    FATAL ERROR: {exc}")
        import traceback
        traceback.print_exc()
        _pause()
        sys.exit(1)
