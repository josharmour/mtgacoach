"""Auto-update helpers for mtgacoach.

Uses the public GitHub releases API and release ZIP archives over urllib so
there is no dependency on a local ``git`` install. No authentication is
required for public repositories; a short timeout and a ``User-Agent`` header
are used to stay within the unauthenticated rate limit.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Timeout (seconds) for network operations
_HTTP_TIMEOUT = 5
_DOWNLOAD_TIMEOUT = 60

_GITHUB_REPO = "josharmour/mtgacoach"
_RELEASES_LATEST_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
_TAGS_URL = f"https://api.github.com/repos/{_GITHUB_REPO}/tags"
_ARCHIVE_BASE = f"https://github.com/{_GITHUB_REPO}/archive"
_USER_AGENT = "mtgacoach-updater"


def _get_repo_root() -> Path:
    """Get the install root for the arenamcp package.

    Derives the root from this file's location so it works regardless of the
    process's current working directory. This file is at
    ``src/arenamcp/updater.py`` so the root is three levels up.
    """
    return Path(__file__).resolve().parent.parent.parent


def _version_tuple(version_str: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in version_str.lstrip("v").split("."))


def _http_get_json(url: str, timeout: int) -> object:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_latest_remote_version() -> str:
    """Return the latest release/tag version (without a leading ``v``), or ``""``.

    Prefers the published "latest release"; falls back to the tags list for
    repositories that tag without cutting formal releases.
    """
    # 1. Published latest release.
    try:
        data = _http_get_json(_RELEASES_LATEST_URL, _HTTP_TIMEOUT)
        if isinstance(data, dict):
            tag = str(data.get("tag_name", "")).strip()
            if tag:
                return tag.lstrip("v")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        logger.debug("releases/latest lookup failed: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("releases/latest parse failed: %s", exc)

    # 2. Fall back to the tags list and pick the highest semantic version.
    try:
        tags = _http_get_json(_TAGS_URL, _HTTP_TIMEOUT)
        best: Optional[Tuple[int, ...]] = None
        best_str = ""
        for entry in tags if isinstance(tags, list) else []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", "")).strip()
            if not name:
                continue
            try:
                version_tuple = _version_tuple(name)
            except (ValueError, TypeError):
                continue
            if best is None or version_tuple > best:
                best = version_tuple
                best_str = name.lstrip("v")
        return best_str
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        logger.debug("tags lookup failed: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("tags parse failed: %s", exc)
    return ""


def check_for_update() -> Tuple[bool, str, str]:
    """Check whether a newer version is available on GitHub.

    Returns:
        (update_available, local_version, remote_version)

    On any failure (offline, rate-limited, etc.) returns ``(False, local, "")``.
    """
    from arenamcp import __version__ as local_version

    try:
        remote_version = _fetch_latest_remote_version()
        if not remote_version:
            return False, local_version, ""

        try:
            remote_tuple = _version_tuple(remote_version)
            local_tuple = _version_tuple(local_version)
        except (ValueError, TypeError):
            return False, local_version, ""

        update_available = remote_tuple > local_tuple
        return update_available, local_version, remote_version
    except Exception as exc:
        logger.debug("update check failed: %s", exc)
        return False, local_version, ""


def _download_bytes(urls: list[str], timeout: int) -> Optional[bytes]:
    """Try each URL in order; return the first successful body, or ``None``."""
    last_err = ""
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            last_err = str(exc)
            logger.debug("download failed for %s: %s", url, exc)
            continue
    if last_err:
        logger.debug("all download URLs failed: %s", last_err)
    return None


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


def apply_update() -> Tuple[bool, str]:
    """Download the latest release ZIP and merge it into the install root.

    Intended for installer-based installs. Git checkouts are declined (the
    caller is told to use ``git pull``) so uncommitted local edits are never
    clobbered.

    Returns:
        (success, message)
    """
    try:
        update_available, local_version, remote_version = check_for_update()
        if not remote_version:
            return False, "Could not determine the latest version"
        if not update_available:
            return True, f"Already up to date (v{local_version})"

        archive_urls = [
            f"{_ARCHIVE_BASE}/refs/tags/v{remote_version}.zip",
            f"{_ARCHIVE_BASE}/refs/tags/{remote_version}.zip",
        ]
        data = _download_bytes(archive_urls, _DOWNLOAD_TIMEOUT)
        if data is None:
            return False, "Download failed (could not reach GitHub)"

        repo_root = _get_repo_root()
        # Protect developer clones: the ZIP merge overwrites files in place and
        # would silently clobber uncommitted local edits. We can't fast-forward
        # without git, so for a git checkout we decline and defer to ``git pull``
        # (checking only for the ``.git`` dir keeps this git-dependency-free for
        # the installer path, which has no ``.git``).
        if (repo_root / ".git").is_dir():
            return (
                False,
                "Detected a git checkout; auto-update will not overwrite local "
                "changes. Update with 'git pull' instead.",
            )
        tmp_dir = Path(tempfile.mkdtemp(prefix="mtgacoach-update-"))
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(tmp_dir)
            # GitHub archives extract to a single ``<repo>-<ref>/`` directory.
            roots = [p for p in tmp_dir.iterdir() if p.is_dir()]
            if len(roots) != 1:
                return False, "Unexpected archive layout"
            _merge_tree(roots[0], repo_root)
            return True, f"Updated to v{remote_version}"
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    except zipfile.BadZipFile:
        return False, "Downloaded archive was corrupt"
    except Exception as exc:
        return False, str(exc)
