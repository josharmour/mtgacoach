"""ArenaMCP diagnostic checker.

Runs a series of checks to verify the installation is healthy and
reports issues with actionable fix instructions.

Usage:
    python -m arenamcp.diagnose
    python -m arenamcp.standalone --diagnose
"""

import importlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ── Formatting helpers ───────────────────────────────────────────────

_W = 70  # output width

PASS = "[PASS]"
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "[INFO]"
SKIP = "[SKIP]"


def _header(title: str) -> None:
    print(f"\n{'─' * _W}")
    print(f"  {title}")
    print(f"{'─' * _W}")


def _check(label: str, status: str, detail: str = "") -> None:
    tag = status
    msg = f"  {tag:6s} {label}"
    if detail:
        msg += f"\n         {detail}"
    print(msg)


def _fix(text: str) -> None:
    """Print an indented fix instruction."""
    for line in text.strip().splitlines():
        print(f"         > {line}")


# ── Individual checks ────────────────────────────────────────────────

def check_python() -> bool:
    """Check Python version (3.10+) and architecture."""
    ver = sys.version_info
    bits = struct.calcsize("P") * 8
    version_str = f"{ver.major}.{ver.minor}.{ver.micro} ({bits}-bit)"
    ok = ver >= (3, 10)
    _check(f"Python {version_str}", PASS if ok else FAIL)
    if not ok:
        _fix("Python 3.10+ required. Download from https://python.org")
    return ok


def check_venv() -> bool:
    """Check if running inside a virtual environment."""
    in_venv = sys.prefix != sys.base_prefix
    _check("Virtual environment", PASS if in_venv else WARN,
           sys.prefix if in_venv else "Running with system Python")
    if not in_venv:
        _fix("Recommended: run install.bat or create a venv manually")
    return True  # not fatal


def check_settings_dir() -> bool:
    """Check ~/.arenamcp directory is accessible."""
    arenamcp_dir = Path.home() / ".arenamcp"
    try:
        arenamcp_dir.mkdir(parents=True, exist_ok=True)
        # Test write
        test_file = arenamcp_dir / ".diag_test"
        test_file.write_text("ok")
        test_file.unlink()
        _check(f"Config dir: {arenamcp_dir}", PASS)
        return True
    except Exception as e:
        _check(f"Config dir: {arenamcp_dir}", FAIL, str(e))
        _fix(f"Ensure {arenamcp_dir} exists and is writable")
        return False


def check_settings_json() -> bool:
    """Check settings.json is valid."""
    settings_file = Path.home() / ".arenamcp" / "settings.json"
    if not settings_file.exists():
        _check("settings.json", INFO, "Not created yet (will use defaults)")
        return True
    try:
        data = json.loads(settings_file.read_text())
        backend = data.get("backend", "auto")
        _check("settings.json", PASS, f"backend={backend}")
        return True
    except json.JSONDecodeError as e:
        _check("settings.json", FAIL, f"Invalid JSON: {e}")
        _fix(f"Delete or fix {settings_file}")
        return False


def check_mtga_log() -> bool:
    """Check MTGA Player.log exists and is readable."""
    # Check env override first
    custom = os.environ.get("MTGA_LOG_PATH")
    if custom:
        log_path = Path(custom)
    else:
        # Standard Windows path
        local_low = os.environ.get("LOCALAPPDATA", "")
        if local_low:
            # LOCALAPPDATA is AppData/Local, we need AppData/LocalLow
            log_path = Path(local_low).parent / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"
        else:
            log_path = Path.home() / "AppData" / "LocalLow" / "Wizards Of The Coast" / "MTGA" / "Player.log"

        # WSL: also check /mnt/c/Users/<user>/AppData/LocalLow/...
        if not log_path.exists() and platform.system() == "Linux" and Path("/mnt/c").exists():
            import glob
            wsl_candidates = glob.glob(
                "/mnt/c/Users/*/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
            )
            if wsl_candidates:
                log_path = Path(wsl_candidates[0])

    if log_path.exists():
        size_mb = log_path.stat().st_size / (1024 * 1024)
        _check(f"MTGA Player.log ({size_mb:.1f} MB)", PASS, str(log_path))
        return True
    else:
        _check("MTGA Player.log", WARN, f"Not found: {log_path}")
        _fix("Launch MTGA at least once to create the log file")
        _fix("Or set MTGA_LOG_PATH env var if log is in a custom location")
        return False


def check_core_deps() -> list[str]:
    """Check core Python dependencies."""
    core_deps = [
        ("mcp", "mcp", "MCP server framework"),
        ("watchdog", "watchdog", "File system monitoring"),
        ("requests", "requests", "HTTP client"),
        ("textual", "textual", "Terminal UI"),
    ]
    if os.name == "nt":
        core_deps.append(("keyboard", "keyboard", "Hotkey support (Windows)"))

    missing = []
    for import_name, pip_name, desc in core_deps:
        try:
            mod = importlib.import_module(import_name)
            ver = getattr(mod, "__version__", getattr(mod, "VERSION", "?"))
            _check(f"{desc} ({pip_name} {ver})", PASS)
        except ImportError:
            _check(f"{desc} ({pip_name})", FAIL, "Not installed")
            missing.append(pip_name)

    if missing:
        _fix(f"pip install {' '.join(missing)}")
    return missing


def check_voice_deps() -> list[str]:
    """Check voice I/O dependencies (optional)."""
    missing = []

    # sounddevice
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        output_devs = [d for d in devices if d.get("max_output_channels", 0) > 0]
        input_devs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        _check(f"sounddevice ({len(output_devs)} output, {len(input_devs)} input devices)", PASS)
        if not output_devs:
            _check("Audio output device", WARN, "No output devices found")
            _fix("Check your audio drivers and default playback device")
    except ImportError:
        _check("sounddevice", SKIP, "Not installed (voice disabled)")
        missing.append("sounddevice")
    except Exception as e:
        _check("sounddevice", FAIL, str(e))
        _fix("Check PortAudio installation: pip install sounddevice")
        missing.append("sounddevice")

    # kokoro TTS
    try:
        import kokoro
        _check(f"kokoro TTS", PASS)
    except ImportError:
        _check("kokoro TTS", SKIP, "Not installed (TTS disabled)")
        missing.append("kokoro")

    # Kokoro model files
    model_dir = Path.home() / ".cache" / "kokoro"
    model_file = model_dir / "kokoro-v1.0.onnx"
    voice_file = model_dir / "voices-v1.0.bin"
    if model_file.exists() and voice_file.exists():
        model_mb = model_file.stat().st_size / (1024 * 1024)
        _check(f"Kokoro model files ({model_mb:.0f} MB)", PASS, str(model_dir))
    elif "kokoro" not in missing:
        _check("Kokoro model files", WARN, f"Not found in {model_dir}")
        _fix("Models download automatically on first TTS use (~300 MB)")

    # faster-whisper STT
    try:
        import faster_whisper
        _check("faster-whisper STT", PASS)
    except ImportError:
        _check("faster-whisper STT", SKIP, "Not installed (voice input disabled)")
        missing.append("faster-whisper")

    if missing:
        _fix(f"For voice: pip install {' '.join(missing)}")
    return missing


def check_backends() -> dict[str, bool]:
    """Check LLM backend availability."""
    results = {}

    try:
        from arenamcp.backend_detect import validate_backend
    except ImportError:
        _check("Backend detection", FAIL, "Cannot import backend_detect module")
        return results

    backends = ["ollama", "claude-code", "gemini-cli", "codex-cli", "proxy", "api"]

    for name in backends:
        try:
            ok, msg = validate_backend(name)
            results[name] = ok
            if ok:
                _check(f"{name}", PASS, msg if msg != "OK" else "")
            else:
                # Not-configured backends are expected, not failures
                if "not configured" in msg.lower() or "not on path" in msg.lower() or "not installed" in msg.lower():
                    _check(f"{name}", SKIP, msg)
                else:
                    _check(f"{name}", WARN, msg)
        except Exception as e:
            results[name] = False
            _check(f"{name}", FAIL, str(e))

    if not any(results.values()):
        _fix("No working backend found! Install at least one:")
        _fix("  Ollama (local):  https://ollama.com  then: ollama pull llama3.2")
        _fix("  Claude Code:     npm install -g @anthropic-ai/claude-code")
        _fix("  Gemini CLI:      npm install -g @anthropic-ai/gemini-cli")

    return results


def check_ollama_models() -> bool:
    """Check Ollama has models available."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = data.get("models", [])
            if models:
                names = [m.get("name", "?") for m in models[:5]]
                extra = f" +{len(models) - 5} more" if len(models) > 5 else ""
                _check(f"Ollama models ({len(models)})", PASS,
                       ", ".join(names) + extra)
                return True
            else:
                _check("Ollama models", WARN, "Server running but no models pulled")
                _fix("Run: ollama pull llama3.2")
                return False
    except Exception:
        # Ollama not running is fine — check_backends already reported it
        return False


def check_log_file() -> bool:
    """Check the ArenaMCP log file for recent errors."""
    log_file = Path.home() / ".arenamcp" / "standalone.log"
    if not log_file.exists():
        _check("ArenaMCP log", INFO, "No log yet (first run?)")
        return True

    size_mb = log_file.stat().st_size / (1024 * 1024)
    _check(f"ArenaMCP log ({size_mb:.1f} MB)", PASS, str(log_file))

    # Scan last 50 lines for errors
    try:
        with open(log_file, "rb") as f:
            # Read last 20KB
            f.seek(0, 2)
            end = f.tell()
            start = max(0, end - 20480)
            f.seek(start)
            tail = f.read().decode("utf-8", errors="replace")

        lines = tail.splitlines()[-50:]
        errors = [l for l in lines if "| ERROR " in l or "| CRITICAL " in l]
        if errors:
            _check(f"Recent errors ({len(errors)} in last 50 lines)", WARN)
            for e in errors[-3:]:
                # Truncate long lines
                short = e[:120] + "..." if len(e) > 120 else e
                print(f"         {short}")
        else:
            _check("Recent errors", PASS, "None in last 50 lines")
    except Exception as e:
        _check("Log scan", WARN, str(e))

    return True


def check_network() -> bool:
    """Quick network connectivity check."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://api.scryfall.com/sets",
            headers={
                "User-Agent": "ArenaMCP-Diagnostics/1.0",
                "Accept": "application/json",
            },
        )
        urllib.request.urlopen(req, timeout=5)
        _check("Network (Scryfall API)", PASS)
        return True
    except Exception as e:
        _check("Network", WARN, f"Cannot reach Scryfall: {e}")
        _fix("Card data lookups require internet. Check firewall/proxy settings.")
        return False


def check_disk_space() -> bool:
    """Check available disk space."""
    try:
        home = Path.home()
        if os.name == "nt":
            import ctypes
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                str(home.drive + "\\"), None, None, ctypes.pointer(free_bytes)
            )
            free_gb = free_bytes.value / (1024 ** 3)
        else:
            st = os.statvfs(str(home))
            free_gb = (st.f_bavail * st.f_frsize) / (1024 ** 3)

        if free_gb < 1:
            _check(f"Disk space ({free_gb:.1f} GB free)", FAIL)
            _fix("Need at least 1 GB free for models and cache")
            return False
        elif free_gb < 5:
            _check(f"Disk space ({free_gb:.1f} GB free)", WARN, "Low — voice models need ~300 MB")
            return True
        else:
            _check(f"Disk space ({free_gb:.1f} GB free)", PASS)
            return True
    except Exception:
        _check("Disk space", SKIP, "Could not determine")
        return True


# ── Main runner ──────────────────────────────────────────────────────

def run_diagnostics() -> int:
    """Run all diagnostic checks. Returns 0 if healthy, 1 if issues found."""
    print(f"{'═' * _W}")
    print(f"  ArenaMCP Diagnostics")
    print(f"  {platform.system()} {platform.release()} | Python {sys.version.split()[0]}")
    print(f"{'═' * _W}")

    issues = 0

    # System
    _header("System")
    if not check_python():
        issues += 1
    check_venv()
    check_disk_space()

    # Paths & Config
    _header("Configuration")
    if not check_settings_dir():
        issues += 1
    if not check_settings_json():
        issues += 1
    check_mtga_log()

    # Dependencies
    _header("Core Dependencies")
    missing_core = check_core_deps()
    if missing_core:
        issues += 1

    _header("Voice Dependencies (optional)")
    check_voice_deps()

    # Backends
    _header("LLM Backends")
    backend_results = check_backends()
    if backend_results and not any(backend_results.values()):
        issues += 1
    check_ollama_models()

    # Network & Logs
    _header("Network & Logs")
    check_network()
    check_log_file()

    # Summary
    print(f"\n{'═' * _W}")
    if issues == 0:
        print("  RESULT: All checks passed! ArenaMCP should work correctly.")
    else:
        print(f"  RESULT: {issues} issue(s) found. See [FAIL] items above.")
    print(f"{'═' * _W}")
    print()

    return 0 if issues == 0 else 1


if __name__ == "__main__":
    sys.exit(run_diagnostics())
