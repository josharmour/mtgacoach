"""Reload the coaching engine between matches when its code on disk has changed.

Dev machines run the app from an editable checkout: a fix merged to master is
on disk at once, but a running engine keeps the modules it imported. The
engine records a fingerprint of its code at start and, only between matches,
compares it with the code on disk. When they differ it asks the desktop for
the Reload engine path (``{"type": "request_engine_reload"}``): the desktop
sends ``prepare_engine_reload``, this engine checkpoints (standalone_startup.py,
including armed post-match navigation) and exits, and the new engine restores
the checkpoint and continues requeueing (standalone_auto_queue.py).

"Between matches" means: the last match this engine saw has completed (the
room's match-scope completion, not a Bo3 game end), no other match is under
way, no draft is running, the game end is recorded (packet saved, bundle and
advice score written), no concede or bug capture is in flight, and auto-queue
has not yet clicked into the next queue. One automatic reload per 3 minutes;
the rate limit rides through the checkpoint.

Gating: ``ARENAMCP_AUTO_RELOAD=0/1`` overrides. Otherwise on for a source
checkout (``arenamcp`` imported from a git working tree) or
``MTGACOACH_DEV=1``, off for packaged installs, whose code changes only
through the installer. The desktop must also say it handles the request
(``MTGACOACH_FRONTEND_FEATURES``), so a desktop started before this feature
never gets one it would ignore.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUTO_RELOAD_ENV = "ARENAMCP_AUTO_RELOAD"
FRONTEND_FEATURES_ENV = "MTGACOACH_FRONTEND_FEATURES"
RELOAD_REQUEST_FEATURE = "request_engine_reload"
RATE_LIMIT_S = 180.0
# How often the code on disk is fingerprinted again while between matches.
CHECK_INTERVAL_S = 30.0
# The desktop answers a request with prepare_engine_reload within milliseconds.
ANSWER_TIMEOUT_S = 20.0
_GIT_TIMEOUT_S = 10.0
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

# Background work that must finish before this interpreter exits (pipe mode
# ends with os._exit, which kills daemon threads mid-write).
_BUSY_THREADS = (
    ("match-bundle-", "the match bundle is still being written"),
    ("post-match-analysis", "post-match analysis is running"),
    ("auto-advice-score", "the match's advice score is still being written"),
    ("concede-countdown", "an auto-concede is in flight"),
    ("debug-report", "a bug report is being saved"),
    ("bug-report", "a bug report is being saved"),
    ("autopilot-stuck-screenshot", "an autopilot bug capture is saving its screenshot"),
)


@dataclass(frozen=True)
class CodeFingerprint:
    """What the engine's code is: ``value`` is compared, ``label`` is logged."""

    kind: str  # "git": HEAD's tree of the package; "files": (path, mtime, size) of every .py
    value: str
    label: str


def package_dir() -> Path:
    import arenamcp

    return Path(arenamcp.__file__).resolve().parent


def source_checkout(pkg: Path | None = None) -> Path | None:
    """The git working tree the package is imported from, or None (installed package)."""
    pkg = Path(pkg or package_dir())
    if {part.lower() for part in pkg.parts} & {"site-packages", "dist-packages"}:
        return None
    for folder in (pkg, *pkg.parents):
        if (folder / ".git").exists():
            return folder
    return None


def auto_reload_default(pkg: Path | None = None) -> tuple[bool, str]:
    """Whether automatic reloads are on, and why."""
    override = os.environ.get(AUTO_RELOAD_ENV, "").strip().lower()
    if override in _TRUE:
        return True, f"{AUTO_RELOAD_ENV}=1"
    if override in _FALSE:
        return False, f"{AUTO_RELOAD_ENV}=0"
    if os.environ.get("MTGACOACH_DEV", "").strip().lower() in _TRUE:
        return True, "MTGACOACH_DEV=1"
    if getattr(sys, "frozen", False):
        return False, "packaged app; its code changes only through the installer"
    checkout = source_checkout(pkg)
    if checkout is not None:
        return True, f"running from the source checkout {checkout}"
    return False, "installed package; its code changes only through the installer"


def frontend_handles_reload() -> bool:
    features = os.environ.get(FRONTEND_FEATURES_ENV, "")
    return RELOAD_REQUEST_FEATURE in {feature.strip() for feature in features.split(",")}


def _git_fingerprint(pkg: Path) -> CodeFingerprint | None:
    """HEAD's tree id for the package directory: changes only when committed engine code does."""
    extra: dict[str, Any] = {}
    if sys.platform == "win32":
        extra["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD", "HEAD:./"],
            cwd=str(pkg),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            check=False,
            **extra,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.debug("Code fingerprint: git unavailable: %s", error)
        return None
    lines = result.stdout.split()
    if result.returncode != 0 or len(lines) != 2:
        logger.debug(
            "Code fingerprint: git rev-parse failed (%s): %s", result.returncode, result.stderr.strip()
        )
        return None
    commit, tree = lines
    return CodeFingerprint("git", tree, commit[:8])


def _files_fingerprint(pkg: Path) -> CodeFingerprint | None:
    """(relative path, mtime, size) of every .py file under the package."""
    digest = hashlib.sha1()
    count = 0
    try:
        for folder, dirs, files in os.walk(pkg):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            for name in sorted(files):
                if not name.endswith(".py"):
                    continue
                path = Path(folder, name)
                stat = path.stat()
                digest.update(
                    f"{path.relative_to(pkg).as_posix()}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode()
                )
                count += 1
    except OSError as error:
        logger.debug("Code fingerprint: could not stat the package: %s", error)
        return None
    if not count:
        return None
    value = digest.hexdigest()
    return CodeFingerprint("files", value, value[:8])


def compute_fingerprint(pkg: Path | None = None, kind: str | None = None) -> CodeFingerprint | None:
    """Fingerprint the engine's code; ``kind`` repeats the kind chosen at start.

    A git checkout uses HEAD's tree of the package, so merges that touch only
    tests or docs do not reload. Otherwise (or when git fails at start) the
    files themselves. A later failure of the same kind returns None, never a
    different kind (that would read as a change).
    """
    pkg = Path(pkg or package_dir())
    if kind == "git" or (kind is None and source_checkout(pkg) is not None):
        found = _git_fingerprint(pkg)
        if found is not None or kind == "git":
            return found
    return _files_fingerprint(pkg)


def _busy_thread(threads: Iterable[threading.Thread] | None = None) -> str:
    for thread in threading.enumerate() if threads is None else threads:
        if not thread.is_alive():
            continue
        for prefix, why in _BUSY_THREADS:
            if thread.name.startswith(prefix):
                return why
    return ""


class _AutoReloadMixin:
    def _init_auto_reload(self) -> None:
        """Decide whether this engine reloads itself, and fingerprint the code it runs."""
        self._auto_reload_baseline: CodeFingerprint | None = None
        self._auto_reload_change: tuple[CodeFingerprint, CodeFingerprint] | None = None
        self._auto_reload_checked_at = time.monotonic()
        self._auto_reload_requested_at = 0.0
        self._auto_reload_waiting = ""
        enabled, why = auto_reload_default()
        if enabled and not callable(getattr(self.ui, "_emit", None)):
            enabled, why = False, "no desktop app to start the new engine"
        elif enabled and not frontend_handles_reload():
            enabled, why = (
                False,
                "this desktop app predates automatic reloads; restart the app to enable them",
            )
        baseline = compute_fingerprint() if enabled else None
        if enabled and baseline is None:
            enabled, why = False, "could not fingerprint the engine's code"
        self._auto_reload_baseline = baseline
        if baseline is not None:
            logger.info(
                "Auto-reload between matches: on (%s); engine code %s %s", why, baseline.kind, baseline.label
            )
        else:
            logger.info("Auto-reload between matches: off (%s)", why)

    # -- engine-reload checkpoint (standalone_startup.py) ---------------------

    def _auto_reload_export(self) -> dict | None:
        last = getattr(self, "_auto_reload_last_wall", 0.0)
        return {"requested_at": last} if last else None

    def _auto_reload_resume_from(self, saved: Any) -> None:
        """The previous engine's last automatic request keeps the rate limit across reloads."""
        if not isinstance(saved, dict):
            return
        try:
            self._auto_reload_last_wall = float(saved.get("requested_at") or 0.0)
        except (TypeError, ValueError):
            pass

    # -- coaching loop ---------------------------------------------------------

    def _auto_reload_between_matches(self, state: dict) -> str:
        """'' when no match is being played or about to start; otherwise why not."""
        current = state.get("match_id")
        if current:
            self._auto_reload_seen_match = current
        if not getattr(self, "_startup_finished", False):
            return "the engine is still starting"
        if getattr(self, "draft_mode", False):
            return "a draft is in progress"
        driver = getattr(self, "_draft_event_driver", None)
        run = getattr(driver, "run", None)
        if driver is not None and driver.enabled and getattr(run, "event_name", None) and not run.finished:
            return "draft autoplay owns this event"
        from arenamcp.server import get_completed_match_for_navigation

        completed = get_completed_match_for_navigation()
        ended = completed.get("match_id")
        if not ended or not completed.get("match_complete"):
            return "no match has finished"
        if ended != getattr(self, "_auto_reload_seen_match", None):
            # Bo3 between games: the match being played is not the completed one.
            return "the last match seen has not finished"
        if current and current != ended:
            return f"match {current} is under way"
        return ""

    def _auto_reload_blocker(self) -> str:
        """Work in flight that a reload would cut short; '' when the engine may go."""
        control = getattr(self, "_autopilot_control_status", None)
        if callable(control) and control() == "AP:PAUSED":
            # The new engine would come back with autoplay off, not paused.
            return "autoplay is paused"
        concede = getattr(self, "concede_in_progress", None)
        if callable(concede) and concede():
            return "an auto-concede is in flight"
        recorder = getattr(self, "_autopilot_bug_capture", None)
        if recorder is not None and getattr(recorder, "active", False):
            return "an autopilot bug capture is recording"
        try:
            from arenamcp import server

            if server.game_state.game_ended_event.is_set():
                return "the game end is not recorded yet"
        except Exception:
            pass
        if getattr(self, "_post_match_analysis_running", False):
            return "post-match analysis is running"
        busy = _busy_thread()
        if busy:
            return busy
        navigator = getattr(self, "_auto_queue_navigator", None)
        if navigator is not None and navigator.active:
            if navigator.queue_joined:
                return "auto-queue already joined the next queue"
            if navigator.step_running:
                return "an auto-queue navigation step is running"
        last = getattr(self, "_auto_reload_last_wall", 0.0)
        if last and time.time() - last < RATE_LIMIT_S:
            return f"the last automatic reload was {time.time() - last:.0f}s ago (at most one per 3 min)"
        return ""

    def _auto_reload_detect_change(
        self, baseline: CodeFingerprint
    ) -> tuple[CodeFingerprint, CodeFingerprint] | None:
        change = getattr(self, "_auto_reload_change", None)
        if change is not None:
            return change
        now = time.monotonic()
        if now - getattr(self, "_auto_reload_checked_at", 0.0) < CHECK_INTERVAL_S:
            return None
        self._auto_reload_checked_at = now
        current = compute_fingerprint(kind=baseline.kind)
        if current is None or current.value == baseline.value:
            return None
        self._auto_reload_change = (baseline, current)
        return self._auto_reload_change

    def _maybe_request_auto_reload(self, state: dict) -> bool:
        """Between matches, with the code changed and nothing in flight: ask the desktop to reload.

        Called every loop pass after auto-queue's poll; cheap until a match
        has finished. Returns True when the request was sent.
        """
        baseline = getattr(self, "_auto_reload_baseline", None)
        if (
            baseline is None
            or getattr(self, "_auto_reload_requested_at", 0.0)
            or getattr(self, "_engine_reload_preparing", False)
        ):
            return False
        if self._auto_reload_between_matches(state):
            self._auto_reload_waiting = ""
            return False
        change = self._auto_reload_detect_change(baseline)
        if change is None:
            return False
        old, new = change
        reason = f"Code changed since engine start ({old.label} -> {new.label})"
        blocker = self._auto_reload_blocker()
        if blocker:
            if blocker != getattr(self, "_auto_reload_waiting", ""):
                self._auto_reload_waiting = blocker
                logger.info("%s; the reload waits: %s", reason, blocker)
            return False
        emit = getattr(self.ui, "_emit", None)
        if not callable(emit):
            return False
        self._auto_reload_requested_at = time.monotonic()
        self._auto_reload_last_wall = time.time()
        logger.info("%s; reloading between matches", reason)
        emit({"type": "request_engine_reload", "reason": reason, "old": old.label, "new": new.label})
        return True

    def _auto_reload_holds(self, state: dict) -> bool:
        """True while a sent request waits for the desktop: no new navigation step starts.

        A request the desktop never answers turns automatic reloads off for
        this engine instead of retrying.
        """
        requested = getattr(self, "_auto_reload_requested_at", 0.0)
        if not requested:
            return False
        if getattr(self, "_engine_reload_preparing", False):
            return True
        waited = time.monotonic() - requested
        if waited > ANSWER_TIMEOUT_S:
            self._auto_reload_requested_at = 0.0
            self._auto_reload_baseline = None
            logger.warning(
                "The desktop did not reload the engine within %.0fs of the request; "
                "automatic reloads are off until the engine restarts",
                waited,
            )
            return False
        return not self._auto_reload_between_matches(state)
