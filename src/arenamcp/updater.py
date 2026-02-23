"""Auto-update helpers for ArenaMCP.

Uses git ls-remote + git pull so there is no GitHub API dependency and
no authentication / rate-limit concern.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

# Timeout (seconds) for git network operations
_GIT_TIMEOUT = 5


def _get_repo_root() -> Path:
    """Get the git repo root for the arenamcp package.

    Derives the repo root from this file's location so that git commands
    work regardless of the process's current working directory.
    """
    # This file is at src/arenamcp/updater.py, repo root is 3 levels up
    return Path(__file__).resolve().parent.parent.parent


def check_for_update() -> Tuple[bool, str, str]:
    """Check whether a newer version is available on the remote.

    Returns:
        (update_available, local_version, remote_version)

    On any failure (no git, offline, etc.) returns ``(False, local, "")``.
    """
    from arenamcp import __version__ as local_version

    try:
        repo_root = _get_repo_root()
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            cwd=str(repo_root),
        )
        if result.returncode != 0:
            logger.debug("git ls-remote failed: %s", result.stderr.strip())
            return False, local_version, ""

        # Parse tags – each line looks like:
        #   <sha>\trefs/tags/v0.3.0
        # We ignore ^{} dereferenced entries.
        tags: list[Tuple[int, ...]] = []
        raw_tags: dict[Tuple[int, ...], str] = {}
        for line in result.stdout.splitlines():
            parts = line.split("refs/tags/")
            if len(parts) != 2:
                continue
            tag = parts[1].strip()
            if tag.endswith("^{}"):
                continue
            version_str = tag.lstrip("v")
            try:
                version_tuple = tuple(int(x) for x in version_str.split("."))
                tags.append(version_tuple)
                raw_tags[version_tuple] = version_str
            except (ValueError, TypeError):
                continue

        if not tags:
            return False, local_version, ""

        highest = max(tags)
        remote_version = raw_tags[highest]

        local_tuple = tuple(int(x) for x in local_version.split("."))
        update_available = highest > local_tuple
        return update_available, local_version, remote_version

    except FileNotFoundError:
        logger.debug("git not found on PATH")
        return False, local_version, ""
    except subprocess.TimeoutExpired:
        logger.debug("git ls-remote timed out")
        return False, local_version, ""
    except Exception as exc:
        logger.debug("update check failed: %s", exc)
        return False, local_version, ""


def apply_update() -> Tuple[bool, str]:
    """Pull the latest code via ``git pull --ff-only``.

    Returns:
        (success, message)
    """
    try:
        repo_root = _get_repo_root()
        result = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "master"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(repo_root),
        )
        if result.returncode == 0:
            summary = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "Updated"
            return True, summary
        else:
            stderr = result.stderr.strip()
            if "not possible to fast-forward" in stderr or "divergent" in stderr:
                return False, "Local branch has diverged from origin. Please resolve manually with `git pull`."
            if "uncommitted changes" in stderr or "dirty" in stderr:
                return False, "You have uncommitted changes. Commit or stash them first."
            return False, stderr or "git pull failed"
    except FileNotFoundError:
        return False, "git not found on PATH"
    except subprocess.TimeoutExpired:
        return False, "git pull timed out"
    except Exception as exc:
        return False, str(exc)
