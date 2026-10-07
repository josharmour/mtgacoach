"""Shared pytest configuration.

This module is imported by pytest before any test module, which makes it
the one reliable place to redirect arenamcp's file logging BEFORE
``arenamcp.standalone`` (and friends) call ``configure_logging()`` at
import time.

Without this, every pytest run appends test-fixture noise (fake bridge
request types, sub-second verification timeouts, scripted planner
failures) to the LIVE ~/.arenamcp/standalone.log — which has previously
been misdiagnosed as real autopilot failures.
"""

import atexit
import contextlib
import importlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# Redirect logging to a temporary log file for pytest runs
os.environ.setdefault(
    "ARENAMCP_LOG_FILE",
    os.path.join(tempfile.gettempdir(), "arenamcp-pytest.log"),
)

# Force Qt offscreen platform plugin for headless test environments
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_MAC_DISABLE_FOREGROUND_APPLICATION_TRANSFORM", "1")


# ---------------------------------------------------------------------------
# Keep every test out of the user's real ~/.arenamcp
# ---------------------------------------------------------------------------
# These modules bind a path under ~/.arenamcp when they are imported. Without
# a redirect the suite wrote into the live directory: StandaloneCoach tests
# saved ``packet_*_m1.json`` match packets (they count toward
# match_packets.MAX_PACKETS, whose rotation deletes the oldest REAL packets,
# which are field evidence), MainWindow/sideboard tests rewrote
# settings.json, and ability-synthesizer tests filled cache/abilities.
#
# Two layers:
# * When this conftest is imported, every path is pointed at one session
#   sandbox and never put back. Modules imported later that copy LOG_DIR
#   (StandaloneCoach and its mixins) inherit it, and a background thread that
#   outlives its test still lands in the sandbox. Per-test patching alone
#   was not enough: one full run wrote a real cache/abilities entry between
#   two tests, after the previous test's monkeypatch had been undone.
# * _sandbox_arenamcp_home gives each test a fresh sandbox, so tests do not
#   see each other's packets, settings or caches.
#
# Each entry is (module, attribute, path inside the sandbox; "" is the sandbox
# root, which stands in for ~/.arenamcp).
_HOME_PATHS = (
    ("arenamcp.logging_config", "LOG_DIR", ""),
    ("arenamcp.match_packets", "PACKETS_DIR", "match_packets"),
    ("arenamcp.match_history", "HISTORY_DIR", "match_history"),
    ("arenamcp.match_history", "HISTORY_FILE", "match_history/history.json"),
    ("arenamcp.match_review", "REVIEW_DIR", "match_reviews"),
    ("arenamcp.match_review", "CALIBRATION_LOG", "win_prob_calibration.jsonl"),
    ("arenamcp.gamestate", "MATCH_STATE_PATH", "last_match.json"),
    ("arenamcp.gamestate_persistence", "MATCH_STATE_PATH", "last_match.json"),
    ("arenamcp.standalone_startup", "ENGINE_RESUME_PATH", "engine_resume.json"),
    ("arenamcp.settings", "SETTINGS_DIR", ""),
    ("arenamcp.settings", "SETTINGS_FILE", "settings.json"),
    ("arenamcp.subscription", "_SETTINGS_DIR", ""),
    ("arenamcp.subscription", "_SUB_CACHE_FILE", "subscription_cache.json"),
    ("arenamcp.desktop.runtime", "_SETTINGS_DIR", ""),
    ("arenamcp.desktop.runtime", "_SETTINGS_FILE", "settings.json"),
    ("arenamcp.rules_db", "DB_PATH", "cache/rules.db"),
    ("arenamcp.ability_synthesizer", "AbilitySynthesizer._CACHE_DIR", "cache/abilities"),
)
# Copies of LOG_DIR made at import time. These modules are heavy, so they are
# not imported here: they inherit the session redirect when something imports
# them, and each test re-points them once they are loaded.
_LOG_DIR_COPIES = (
    ("arenamcp.standalone", "LOG_DIR", ""),
    ("arenamcp.standalone", "WATCHDOG_SCREENSHOT_DIR", "watchdog_screenshots"),
    ("arenamcp.standalone_diagnostics", "LOG_DIR", ""),
    ("arenamcp.standalone_postmatch", "LOG_DIR", ""),
    ("arenamcp.standalone_autopilot_capture", "LOG_DIR", ""),
)


def _redirect_arenamcp_home(mp: pytest.MonkeyPatch, sandbox: Path) -> None:
    """Point every _HOME_PATHS entry, and every loaded LOG_DIR copy, into *sandbox*.

    A module that fails to import is skipped: no code from it can run, so it
    cannot write anywhere, and the tests that need it report the error.
    """
    targets = []
    for name, attr, rel in _HOME_PATHS:
        with contextlib.suppress(Exception):
            targets.append((importlib.import_module(name), attr, rel))
    targets += [(sys.modules[name], attr, rel) for name, attr, rel in _LOG_DIR_COPIES if name in sys.modules]
    for module, dotted, rel in targets:
        owner, _, attr = dotted.rpartition(".")
        mp.setattr(getattr(module, owner) if owner else module, attr, sandbox / rel if rel else sandbox)


_SESSION_HOME = Path(tempfile.mkdtemp(prefix="arenamcp-pytest-home-"))
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
# Deliberately never undone: daemon threads can outlive the session too.
_redirect_arenamcp_home(pytest.MonkeyPatch(), _SESSION_HOME)


@pytest.fixture(autouse=True)
def _sandbox_arenamcp_home(monkeypatch, tmp_path_factory):
    """Give this test its own stand-in for ~/.arenamcp.

    The sandbox is its own temp directory, not the test's ``tmp_path``, so
    tests that inspect their tmp_path see only what they wrote. A test's own
    monkeypatch of the same attribute runs after this and still wins.

    Read-through download caches (scryfall, mtgjson, 17lands, synergy,
    set primers, mtggoldfish) stay on the real paths: the suite only reads
    them, and an empty cache would turn those reads into network fetches.
    """
    sandbox = tmp_path_factory.mktemp("arenamcp_home")
    _redirect_arenamcp_home(monkeypatch, sandbox)

    # Singletons that may hold a path, or the user's real settings, from
    # before this test: drop them so this test builds fresh ones in the sandbox.
    settings = sys.modules.get("arenamcp.settings")
    if settings is not None:
        monkeypatch.setattr(settings, "_settings", None)
    match_history = sys.modules.get("arenamcp.match_history")
    if match_history is not None:
        monkeypatch.setattr(match_history, "_history", None)
    # A packet left recording by an earlier test would be salvage-saved by
    # this test's start_match_packet(), and a finalized "m1" would stop this
    # test from recording at all.
    match_packets = sys.modules.get("arenamcp.match_packets")
    if match_packets is not None:
        monkeypatch.setattr(match_packets, "_current_packet", None)
        monkeypatch.setattr(match_packets, "_finalized_match_ids", set())

    # MainWindow starts a UI stall watchdog that dumps to ~/.mtgacoach/anr_dumps
    # (computed in __init__, so there is no module constant to patch). Any test
    # that holds the Qt main thread for 1.5 s while a window is open writes one.
    ui_watchdog = sys.modules.get("arenamcp.desktop.ui_watchdog")
    if ui_watchdog is not None:
        original_init = ui_watchdog.UiAnrWatchdog.__init__

        def _sandboxed_init(self, *args, **kwargs):
            if len(args) < 4 and kwargs.get("dump_dir") is None:
                kwargs["dump_dir"] = sandbox / "anr_dumps"
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(ui_watchdog.UiAnrWatchdog, "__init__", _sandboxed_init)

    return sandbox


@pytest.fixture(scope="session")
def qapp_args():
    """Arguments passed to QApplication when created for testing."""
    return ["-platform", "offscreen"]


@pytest.fixture(scope="session")
def qapp(qapp_args):
    """Session-scoped QApplication instance configured for headless testing.

    Ensures a single QApplication instance exists for tests importing PySide6 widgets.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 is not installed")

    app = QApplication.instance()
    if app is None:
        app = QApplication(qapp_args)
    yield app
