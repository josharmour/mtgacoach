"""Launch-time update check for installed copies (pip / uv tool).

Standard desktop pattern: on launch, ask GitHub Releases for the latest
version and, if newer, let the user update + restart. Never blocks startup,
never raises.

Two deliberate no-ops:
  * An **editable / source checkout** (a developer running from the repo) is
    never nagged and never auto-updated — it tracks local edits, not
    releases. This keeps the maintainer's own machine quiet.
  * Any network/parse failure is swallowed — no internet must never affect
    launching the app.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from shutil import which
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 6

_GITHUB_REPO = "josharmour/mtgacoach"
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_TAGS_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/tags"
_USER_AGENT = "mtgacoach-updater"


def _version_tuple(version_str: str) -> Tuple[int, ...]:
    import re

    nums = re.findall(r"\d+", version_str or "")
    return tuple(int(n) for n in nums[:3]) or (0,)


def is_editable_install() -> bool:
    """True when running from a source checkout (dev), not a wheel install."""
    try:
        from importlib.metadata import distribution

        durl = distribution("arenamcp").read_text("direct_url.json")
        if durl and json.loads(durl).get("dir_info", {}).get("editable"):
            return True
    except Exception:
        pass
    try:
        for parent in Path(__file__).resolve().parents:
            if (parent / "pyproject.toml").exists() and (parent / ".git").exists():
                return True
    except Exception:
        pass
    return False


def _http_get_json(url: str) -> object:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_release() -> Optional[dict]:
    try:
        data = _http_get_json(_RELEASES_LATEST_URL)
        if isinstance(data, dict) and data.get("tag_name"):
            return data
    except Exception as exc:
        logger.debug("releases/latest lookup failed: %s", exc)
    return None


def _fetch_latest_remote_version() -> str:
    rel = _latest_release()
    if rel:
        return str(rel.get("tag_name", "")).lstrip("v")
    # Fallback: highest tag (repos that tag without formal releases).
    try:
        tags = _http_get_json(_TAGS_URL)
        best, best_str = None, ""
        for entry in tags if isinstance(tags, list) else []:
            name = str((entry or {}).get("name", "")).strip()
            if not name:
                continue
            t = _version_tuple(name)
            if best is None or t > best:
                best, best_str = t, name.lstrip("v")
        return best_str
    except Exception as exc:
        logger.debug("tags lookup failed: %s", exc)
        return ""


def check_for_update() -> Tuple[bool, str, str]:
    """(update_available, local_version, remote_version).

    Returns (False, local, "") for editable installs, when the check is
    disabled (MTGACOACH_NO_UPDATE_CHECK), or on any failure.
    """
    try:
        from arenamcp import __version__ as local_version
    except Exception:
        local_version = "0"
    if is_editable_install() or os.environ.get("MTGACOACH_NO_UPDATE_CHECK"):
        return False, local_version, ""
    try:
        remote = _fetch_latest_remote_version()
        if not remote:
            return False, local_version, ""
        available = _version_tuple(remote) > _version_tuple(local_version)
        if available:
            logger.info("update available: %s -> %s", local_version, remote)
        return available, local_version, remote
    except Exception as exc:
        logger.debug("update check failed: %s", exc)
        return False, local_version, ""


def _latest_wheel_url() -> Optional[str]:
    rel = _latest_release()
    for asset in (rel or {}).get("assets", []) or []:
        if str(asset.get("name") or "").endswith(".whl"):
            return asset.get("browser_download_url")
    return None


def _find_uv() -> Optional[str]:
    found = which("uv")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / "uv"
    return str(cand) if cand.exists() else None


def apply_update() -> Tuple[bool, str]:
    """Upgrade this install in place (does NOT restart the app).

    Picks the right command for how the app was installed: PyPI-style
    upgrade for uv/pip, else a forced (re)install from the release wheel.
    Editable/dev checkouts are declined so local edits are never clobbered.
    """
    if is_editable_install():
        return (
            False,
            "This is a source checkout — update it with 'git pull' (auto-update "
            "won't overwrite local changes).",
        )
    available, local_version, remote = check_for_update()
    if not remote:
        return False, "Could not determine the latest version"
    if not available:
        return True, f"Already up to date (v{local_version})"

    wheel = _latest_wheel_url()
    uv = _find_uv()
    cmds: list[list[str]] = []
    if uv:
        cmds.append([uv, "tool", "upgrade", "arenamcp"])
        if wheel:
            cmds.append([uv, "tool", "install", "--force", wheel])
    cmds.append([sys.executable, "-m", "pip", "install", "--upgrade", "arenamcp"])
    if wheel:
        cmds.append(
            [sys.executable, "-m", "pip", "install", "--upgrade",
             "--force-reinstall", wheel]
        )

    last = ""
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if r.returncode == 0:
                return True, f"Updated to v{remote}. Restart mtgacoach to use it."
            last = (r.stderr or r.stdout or "").strip()[-300:]
        except Exception as exc:
            last = str(exc)
    return False, f"Update failed: {last or 'no working install method found'}"
