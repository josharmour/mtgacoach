"""Lightweight backend detection and auto-selection using only stdlib.

Used by the TUI/standalone to detect available backends on launch,
validate subscriptions, and pick the best default backend.

Priority order for auto-selection:
  1. claude-code  (subscription CLI)
  2. gemini-cli   (subscription CLI)
  3. codex-cli    (subscription CLI)
  4. ollama       (local, always-available baseline)
"""

import json
import logging
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _which_cli(name: str) -> Optional[str]:
    """Find a CLI binary, trying Windows shim variants (.cmd, .ps1) if needed."""
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        for ext in (".cmd", ".ps1", ".bat", ".exe"):
            found = shutil.which(f"{name}{ext}")
            if found:
                return found
    return None

# Where advanced custom-endpoint config lives
CUSTOM_ENDPOINTS_FILE = Path.home() / ".arenamcp" / "endpoints.json"

# Preferred auto-select order (first available wins)
_AUTO_PRIORITY = ["claude-code", "gemini-cli", "codex-cli", "ollama"]

# Default Ollama model
DEFAULT_OLLAMA_MODEL = "llama3.2"


def detect_backends_quick() -> dict[str, bool]:
    """Check which LLM backends are available right now.

    Returns a dict of backend_name -> is_available.
    HTTP checks use a 2-second timeout so this never blocks for long.
    """
    results: dict[str, bool] = {}

    # Ollama: binary on PATH or HTTP server responding
    ollama_bin = shutil.which("ollama") is not None
    ollama_http = False
    if not ollama_bin:
        try:
            req = urllib.request.Request("http://localhost:11434/", method="GET")
            with urllib.request.urlopen(req, timeout=2):
                ollama_http = True
        except Exception:
            pass
    results["ollama"] = ollama_bin or ollama_http

    # CLI-based backends (try Windows shim variants too)
    results["claude-code"] = _which_cli("claude") is not None
    results["gemini-cli"] = _which_cli("gemini") is not None
    results["codex-cli"] = _which_cli("codex") is not None

    return results


def validate_backend(backend_name: str) -> tuple[bool, str]:
    """Validate that a backend can actually serve requests.

    Goes beyond binary detection — runs a quick health check.

    Returns:
        (is_working, error_message)  — error_message is empty on success.
    """
    backend_name = backend_name.lower()

    if backend_name == "ollama":
        return _validate_ollama()
    elif backend_name in ("claude-code", "claude"):
        return _validate_claude_code()
    elif backend_name in ("gemini-cli", "gemini"):
        return _validate_gemini_cli()
    elif backend_name in ("codex-cli", "codex"):
        return _validate_codex_cli()
    elif backend_name == "proxy":
        return _validate_proxy()
    elif backend_name == "api":
        return _validate_custom_api()
    else:
        return False, f"Unknown backend: {backend_name}"


def _validate_ollama() -> tuple[bool, str]:
    """Check Ollama is running and has at least one model."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            models = data.get("models", [])
            if models:
                return True, ""
            return False, "Ollama is running but has no models. Run: ollama pull llama3.2"
    except Exception:
        # Try binary
        if shutil.which("ollama"):
            return False, "Ollama binary found but server not running. Run: ollama serve"
        return False, "Ollama not found. Install from https://ollama.com"


def _validate_claude_code() -> tuple[bool, str]:
    """Check claude CLI is available and can respond."""
    resolved = _which_cli("claude")
    if not resolved:
        return False, "claude CLI not found on PATH"
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True, ""
        stderr = (result.stderr or "").lower()
        if "auth" in stderr or "login" in stderr or "expired" in stderr:
            return False, "Claude Code subscription expired or not logged in"
        return False, f"claude CLI error: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "claude CLI not found"
    except subprocess.TimeoutExpired:
        return False, "claude CLI timed out"
    except Exception as e:
        return False, f"claude CLI check failed: {e}"


def _validate_gemini_cli() -> tuple[bool, str]:
    """Check gemini CLI is available."""
    resolved = _which_cli("gemini")
    if not resolved:
        return False, "gemini CLI not found on PATH"
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True, ""
        return False, f"gemini CLI error: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "gemini CLI not found"
    except subprocess.TimeoutExpired:
        return False, "gemini CLI timed out"
    except Exception as e:
        return False, f"gemini CLI check failed: {e}"


def _validate_codex_cli() -> tuple[bool, str]:
    """Check codex CLI is available."""
    resolved = _which_cli("codex")
    if not resolved:
        return False, "codex CLI not found on PATH"
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return True, ""
        return False, f"codex CLI error: {result.stderr.strip()}"
    except FileNotFoundError:
        return False, "codex CLI not found"
    except subprocess.TimeoutExpired:
        return False, "codex CLI timed out"
    except Exception as e:
        return False, f"codex CLI check failed: {e}"


def _validate_proxy() -> tuple[bool, str]:
    """Check cli-api-proxy is reachable."""
    try:
        from arenamcp.settings import get_settings
        s = get_settings()
        url = s.get("proxy_url") or "http://127.0.0.1:8080/v1"
    except Exception:
        url = "http://127.0.0.1:8080/v1"

    try:
        req = urllib.request.Request(f"{url}/models", method="GET")
        with urllib.request.urlopen(req, timeout=3):
            return True, ""
    except Exception:
        return False, "cli-api-proxy not reachable (requires custom endpoint config)"


def _validate_custom_api() -> tuple[bool, str]:
    """Check custom API endpoint from endpoints.json."""
    endpoints = load_custom_endpoints()
    if not endpoints:
        return False, "No custom endpoints configured in ~/.arenamcp/endpoints.json"

    url = endpoints.get("api_url", "")
    key = endpoints.get("api_key", "")
    if not url:
        return False, "api_url not set in endpoints.json"

    try:
        headers = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(f"{url.rstrip('/')}/models", headers=headers)
        with urllib.request.urlopen(req, timeout=3):
            return True, ""
    except Exception as e:
        return False, f"Custom API endpoint unreachable: {e}"


def load_custom_endpoints() -> dict:
    """Load custom endpoint configuration from ~/.arenamcp/endpoints.json.

    This is the advanced configuration path for users who want to use
    cli-proxy-api or other OpenAI-compatible endpoints.

    Expected format:
    {
        "proxy_url": "http://127.0.0.1:8080/v1",
        "proxy_api_key": "your-key",
        "api_url": "https://api.openai.com/v1",
        "api_key": "sk-..."
    }
    """
    if not CUSTOM_ENDPOINTS_FILE.exists():
        return {}
    try:
        with open(CUSTOM_ENDPOINTS_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load {CUSTOM_ENDPOINTS_FILE}: {e}")
        return {}


def auto_select_backend() -> tuple[str, Optional[str]]:
    """Auto-select the best available backend.

    Checks subscription CLIs first, falls back to Ollama.

    Returns:
        (backend_name, model_or_none)
    """
    detected = detect_backends_quick()

    for backend in _AUTO_PRIORITY:
        if detected.get(backend):
            if backend == "ollama":
                return "ollama", DEFAULT_OLLAMA_MODEL
            return backend, None

    # Nothing found — return Ollama anyway as the baseline expectation
    return "ollama", DEFAULT_OLLAMA_MODEL


def is_query_failure_retriable(error_text: str) -> bool:
    """Check whether an error message indicates a billing/quota/auth failure.

    These errors mean the backend won't recover on retry — the user should
    switch providers or fall back to Ollama.
    """
    error_lower = error_text.lower()
    failure_indicators = [
        "insufficient",
        "billing",
        "quota",
        "rate limit",
        "rate_limit",
        "expired",
        "unauthorized",
        "403",
        "401",
        "429",
        "payment required",
        "credit",
        "subscription",
        "not logged in",
        "auth",
        "permission denied",
        "api key",
        "apikey",
        "invalid_api_key",
        "account",
    ]
    return any(indicator in error_lower for indicator in failure_indicators)
