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
import errno
import importlib
import os
import shutil
import socket
import sys
import tempfile
import threading
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
    ("arenamcp.draft_event", "DRAFT_RUN_DIR", "draft_runs"),
    ("arenamcp.set_research", "RESEARCH_DIR", "cache/set_research"),
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
    ("arenamcp.opponent_tricks", "TRICK_DIR", "cache/trick_tables"),
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


def _offline_trick_service(sandbox: Path):
    """A TrickTableService that never reads a set primer, 17Lands data or the card database.

    ``GamePlanManager.observe()`` calls ``TrickTableService.shared().ensure_for_state()``:
    the real singleton builds a set's trick table from the user's primer and
    17Lands caches (downloading when they are missing) and the MTGA card
    database, on a background thread, and writes it to
    ~/.arenamcp/cache/trick_tables (a real FRA.json was written that way on
    2026-10-07 02:07). Tests that want a table register one on this service.
    """
    from arenamcp import opponent_tricks

    return opponent_tricks.TrickTableService(
        primer_fn=lambda code: None,
        ratings_fn=lambda code: [],
        color_ratings_fn=lambda code: None,
        card_lookup=lambda grp: None,
        cache_dir=sandbox / "cache" / "trick_tables",
    )


_SESSION_HOME = Path(tempfile.mkdtemp(prefix="arenamcp-pytest-home-"))
atexit.register(shutil.rmtree, _SESSION_HOME, ignore_errors=True)
# Deliberately never undone: daemon threads can outlive the session too.
_redirect_arenamcp_home(pytest.MonkeyPatch(), _SESSION_HOME)
with contextlib.suppress(Exception):
    from arenamcp import opponent_tricks as _opponent_tricks

    _opponent_tricks.TrickTableService._shared = _offline_trick_service(_SESSION_HOME)


# ---------------------------------------------------------------------------
# Never touch the live bridge ports
# ---------------------------------------------------------------------------
# A GREBridge built by get_bridge() (the keepalive thread) binds its server on
# connect(). On 2026-10-18 a hung `pytest tests` held 127.0.0.1:44222 for two
# hours and the user's real coach could not start its bridge ("[Errno 48]
# Address already in use"). Tests bind ephemeral ports, and any bind of a live
# port fails loudly instead of competing with a running coach.
_LIVE_BRIDGE_PORTS = frozenset({44222, 44223})
with contextlib.suppress(Exception):
    from arenamcp import gre_bridge as _gre_bridge

    _gre_bridge.BRIDGE_PORT = 0
    _gre_bridge.BOT_BATTLE_PORT = 0

_original_socket_bind = socket.socket.bind


def _bind_off_live_ports(self, address):
    port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
    if port in _LIVE_BRIDGE_PORTS:
        raise OSError(
            errno.EACCES,
            f"tests must not bind the live bridge port {port}; use port 0 (see tests/conftest.py)",
        )
    return _original_socket_bind(self, address)


socket.socket.bind = _bind_off_live_ports


# ---------------------------------------------------------------------------
# Never act on the real machine: repair pipeline, screen capture
# ---------------------------------------------------------------------------
# Showing the Setup & Repair tab starts RepairEngine().run() on a daemon
# thread, and its checks repair the real install (BepInEx and the plugin into
# the real MTGA folder on Windows/Proton). The desktop bug report grabbed the
# user's real Arena window. Both ran in the suite until 2026-10-18. The guards
# stay installed for the whole session (a daemon thread can outlive its test),
# and because callers swallow exceptions (the repair thread, the bug-report
# capture), the test that tripped one fails at teardown too.
_REAL_MACHINE_VIOLATIONS: list[str] = []


class RealMachineAccess(RuntimeError):
    """A test reached code that acts on the real machine instead of a stub."""


def _refuse(what: str) -> None:
    import threading

    message = (
        f"tests must not {what} (thread {threading.current_thread().name}); stub it, see tests/conftest.py"
    )
    _REAL_MACHINE_VIOLATIONS.append(message)
    raise RealMachineAccess(message)


def _blocked(what: str):
    def _guard(*_args, **_kwargs):
        _refuse(what)

    return _guard


with contextlib.suppress(Exception):
    from arenamcp import repair_engine as _repair_engine

    _real_repair_run = _repair_engine.RepairEngine.run
    _REPAIR_CHECKS = tuple(name for name in vars(_repair_engine.RepairEngine) if name.startswith("_check_"))

    def _guarded_repair_run(self, *args, **kwargs):
        # Allowed only when every check is stubbed on the instance, which is
        # how the pipeline-order tests drive run().
        if not all(name in vars(self) for name in _REPAIR_CHECKS):
            _refuse("run the real repair pipeline (RepairEngine.run)")
        return _real_repair_run(self, *args, **kwargs)

    _repair_engine.RepairEngine.run = _guarded_repair_run

with contextlib.suppress(Exception):
    from PIL import ImageGrab as _image_grab

    _image_grab.grab = _blocked("capture the real screen (PIL.ImageGrab.grab)")

with contextlib.suppress(Exception):
    from arenamcp import screen_capture as _screen_capture

    _screen_capture.capture_window_via_printwindow = _blocked("capture a real window (PrintWindow)")

with contextlib.suppress(Exception):
    from arenamcp import native_mac_input as _native_mac_input

    _native_mac_input.NativeMacInput.capture = _blocked("capture the real Arena window (screencapture)")


@pytest.fixture(autouse=True)
def _no_real_machine_access():
    """Fail the test that reached a real-machine guard, even if its caller swallowed the error."""
    _REAL_MACHINE_VIOLATIONS.clear()
    yield
    if _REAL_MACHINE_VIOLATIONS:
        found = list(dict.fromkeys(_REAL_MACHINE_VIOLATIONS))
        _REAL_MACHINE_VIOLATIONS.clear()
        pytest.fail("; ".join(found))


def pytest_collection_finish(session):
    """Move everything alive after collection out of the cyclic GC's reach.

    The imported modules and collected tests are ~800k tracked objects; one
    full collection over them took 346 ms inside a line-search unit test with a
    350 ms wall-clock deadline (2026-10-07 review), truncating its search. They
    live for the whole session, so freezing them only spares each later
    collection the walk over them.
    """
    import gc

    gc.collect()
    gc.freeze()


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
    # A packet left recording by an earlier test would be salvage-saved by
    # this test's start_match_packet(), and a finalized "m1" would stop this
    # test from recording at all.
    match_packets = sys.modules.get("arenamcp.match_packets")
    if match_packets is not None:
        monkeypatch.setattr(match_packets, "_current_packet", None)
        monkeypatch.setattr(match_packets, "_finalized_match_ids", set())
    # A fresh offline trick table service (see _offline_trick_service), and no
    # trick-risk verdict cached from another test's service.
    opponent_tricks = sys.modules.get("arenamcp.opponent_tricks")
    if opponent_tricks is not None:
        monkeypatch.setattr(opponent_tricks.TrickTableService, "_shared", _offline_trick_service(sandbox))
    game_plan = sys.modules.get("arenamcp.game_plan")
    if game_plan is not None:
        game_plan._TRICK_CACHE.clear()

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


def _reset_llm_gates() -> None:
    """Forget the proxy's process-wide breaker, priority and background-lane state.

    Only modules something already imported are touched (an unimported one
    holds no state, and importing the proxy here would pull the SDK into every
    test). Breakers are keyed by (base_url, model), so a test that trips the
    breaker for a URL another test reuses would otherwise make that test's
    calls fail fast, and one server rejection of "priority" would stop every
    later test from sending it.
    """
    health = sys.modules.get("arenamcp.backends.health")
    if health is not None:
        health.reset_circuit_breakers()
    proxy = sys.modules.get("arenamcp.backends.proxy")
    if proxy is not None:
        proxy.reset_priority_state()
        # A request thread a test left behind may still hold the old lane;
        # its release() on the new lane is a no-op.
        proxy._BACKGROUND_LANE = proxy._BackgroundLane()


@pytest.fixture(autouse=True)
def _isolate_llm_gates():
    """Circuit breakers, the priority rejection and the background lane never leak between tests."""
    _reset_llm_gates()
    yield
    _reset_llm_gates()


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


@pytest.fixture
def unlimited_search_deadline(monkeypatch):
    """The line search's wall-clock hard cap never fires; an explicit ``hard_ms`` still wins.

    Its soft limit is a deterministic work budget; only the hard cap (350 ms
    for the assessment, 200 ms for a mode comparison) is a clock, and a loaded
    test machine must not truncate the lines a test pins.
    """
    from arenamcp import line_guard, line_search

    search, compare = line_search.search_lines, line_search.compare_modes

    def unlimited_search(model, **kwargs):
        return search(model, **{"hard_ms": 1e9, **kwargs})

    def unlimited_compare(state, decision, **kwargs):
        return compare(state, decision, **{"hard_ms": 1e9, **kwargs})

    monkeypatch.setattr(line_search, "search_lines", unlimited_search)
    monkeypatch.setattr(line_search, "compare_modes", unlimited_compare)
    monkeypatch.setattr(line_guard, "compare_modes", unlimited_compare)


@pytest.fixture
def instant_combat_acks(monkeypatch):
    """Combat submissions stop waiting for the GRE to acknowledge them (autopilot_bridge)."""
    from arenamcp import autopilot_bridge

    for name in ("_BLOCK_ACK_MAX_WAIT_S", "_ATTACK_ACK_MAX_WAIT_S", "_COMBAT_ACK_SETTLE_S"):
        monkeypatch.setattr(autopilot_bridge, name, 0.0)


@pytest.fixture
def plan_threads(monkeypatch):
    """The game plan's background threads, joined (and checked finished) after the test.

    One background strategy job runs at a time, process-wide: first wait out a
    plan refresh another test may have left running.
    """
    from arenamcp.game_plan import STRATEGIC_LANE

    assert STRATEGIC_LANE.wait_idle(5)
    started: list[threading.Thread] = []
    real_thread = threading.Thread

    def make(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        started.append(thread)
        return thread

    monkeypatch.setattr("arenamcp.game_plan.threading.Thread", make)
    yield started
    for thread in started:
        thread.join(timeout=3)
        assert not thread.is_alive()
