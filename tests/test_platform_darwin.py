"""macOS (darwin) MTGA discovery and log-path tests.

Everything here mocks the filesystem (fake HOME built in tmp_path, patched
Path.home / sys.platform), so the suite passes identically on Windows,
Linux, and macOS. Covers:

- _find_mtga_darwin(): native Steam, Epic, CrossOver-bottle detection,
  priority order, and Player.log resolution for each flavor
- find_mtga() dispatching to the darwin backend
- watcher._default_log_path() darwin branch (native + bottle fallback)
- mtgadb.find_mtga_database() darwin layouts (MTGA_Data and the
  com.wizards.mtga direct-install layout without an MTGA_Data level)
"""

import sys
from pathlib import Path

import pytest

from arenamcp import platform_integration as pi


# ── helpers ──────────────────────────────────────────────────────────

NATIVE_LOG_SUFFIX = Path("Library/Logs/Wizards Of The Coast/MTGA/Player.log")


def _fake_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _make_native_log(home: Path) -> Path:
    log = home / NATIVE_LOG_SUFFIX
    log.parent.mkdir(parents=True)
    log.write_text("[UnityCrossThreadLogger]\n")
    return log


def _make_steam_install(home: Path) -> Path:
    steam_dir = home / "Library/Application Support/Steam/steamapps/common/MTGA"
    (steam_dir / "MTGA.app").mkdir(parents=True)
    (steam_dir / "MTGA_Data").mkdir()
    return steam_dir


def _make_bottle(home: Path, bottle_name: str = "MTGA", user: str = "crossover") -> Path:
    bottle = (
        home / "Library/Application Support/CrossOver/Bottles" / bottle_name
    )
    game_dir = bottle / "drive_c/Program Files/Wizards of the Coast/MTGA"
    game_dir.mkdir(parents=True)
    (game_dir / "MTGA.exe").write_bytes(b"MZ")
    log_dir = (
        bottle / "drive_c/users" / user
        / "AppData/LocalLow/Wizards Of The Coast/MTGA"
    )
    log_dir.mkdir(parents=True)
    (log_dir / "Player.log").write_text("[UnityCrossThreadLogger]\n")
    return bottle


# ── _find_mtga_darwin: native Steam ──────────────────────────────────

def test_darwin_steam_detected(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    steam_dir = _make_steam_install(home)
    log = _make_native_log(home)

    install = pi._find_mtga_darwin()

    assert install is not None
    assert install.platform == "darwin-steam"
    assert install.install_dir == steam_dir
    assert install.player_log == log
    assert install.steam_root == home / "Library/Application Support/Steam"


def test_darwin_steam_without_log_dir(monkeypatch, tmp_path):
    """MTGA installed but never launched → player_log is None."""
    home = _fake_home(monkeypatch, tmp_path)
    _make_steam_install(home)

    install = pi._find_mtga_darwin()

    assert install is not None
    assert install.platform == "darwin-steam"
    assert install.player_log is None


def test_darwin_steam_requires_app_bundle(monkeypatch, tmp_path):
    """A bare MTGA folder without MTGA.app is not an install."""
    home = _fake_home(monkeypatch, tmp_path)
    steam_dir = home / "Library/Application Support/Steam/steamapps/common/MTGA"
    steam_dir.mkdir(parents=True)  # no MTGA.app inside

    assert pi._find_mtga_darwin() is None


def test_darwin_nothing_installed(monkeypatch, tmp_path):
    _fake_home(monkeypatch, tmp_path)
    assert pi._find_mtga_darwin() is None


# ── _find_mtga_darwin: Epic ──────────────────────────────────────────

def test_darwin_epic_detected(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    log = _make_native_log(home)
    epic_dir = tmp_path / "Shared/Epic Games/MagicTheGathering"
    (epic_dir / "MTGA.app").mkdir(parents=True)
    monkeypatch.setattr(pi, "_DARWIN_EPIC_CANDIDATES", [epic_dir])

    install = pi._find_mtga_darwin()

    assert install is not None
    assert install.platform == "darwin-epic"
    assert install.install_dir == epic_dir
    assert install.player_log == log
    assert install.steam_root is None


# ── _find_mtga_darwin: CrossOver bottle ──────────────────────────────

def test_darwin_crossover_bottle_detected(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    bottle = _make_bottle(home, user="crossover")

    install = pi._find_mtga_darwin()

    assert install is not None
    assert install.platform == "darwin-crossover"
    assert install.install_dir == (
        bottle / "drive_c/Program Files/Wizards of the Coast/MTGA"
    )
    assert install.player_log == (
        bottle / "drive_c/users/crossover"
        / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
    )


def test_darwin_crossover_odd_user_dir_globbed(monkeypatch, tmp_path):
    """Bottle user dir naming isn't assumed — Public is skipped, real user found."""
    home = _fake_home(monkeypatch, tmp_path)
    bottle = _make_bottle(home, user="josh")
    # A Public dir that must be ignored even though it sorts first.
    (bottle / "drive_c/users/Public").mkdir()

    install = pi._find_mtga_darwin()

    assert install is not None
    assert install.player_log is not None
    assert "josh" in str(install.player_log)


def test_darwin_crossover_bottle_without_mtga(monkeypatch, tmp_path):
    """Bottles exist but none contain MTGA.exe → no install."""
    home = _fake_home(monkeypatch, tmp_path)
    (home / "Library/Application Support/CrossOver/Bottles/Empty/drive_c").mkdir(
        parents=True
    )
    assert pi._find_mtga_darwin() is None


def test_darwin_native_steam_preferred_over_bottle(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    _make_bottle(home)
    steam_dir = _make_steam_install(home)

    install = pi._find_mtga_darwin()

    assert install is not None
    assert install.platform == "darwin-steam"
    assert install.install_dir == steam_dir


# ── find_mtga() dispatch ─────────────────────────────────────────────

def test_find_mtga_dispatches_to_darwin(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    _make_steam_install(home)
    _make_native_log(home)
    monkeypatch.setattr(pi, "current_platform", lambda: "darwin")
    # Deterministic settings: no mtga_install_dir override saved.
    import arenamcp.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_settings", lambda: {})

    install = pi.find_mtga()

    assert install is not None
    assert install.platform == "darwin-steam"


def test_darwin_platform_never_matches_linux_or_windows_gates(
    monkeypatch, tmp_path
):
    """repair_engine gates on .startswith('linux') / == 'windows'; every
    darwin flavor must stay clear of both."""
    home = _fake_home(monkeypatch, tmp_path)
    _make_bottle(home)

    install = pi._find_mtga_darwin()

    assert install is not None
    assert not install.platform.startswith("linux")
    assert install.platform != "windows"
    assert install.platform.startswith("darwin")


# ── watcher._default_log_path darwin branch ──────────────────────────

def test_default_log_path_darwin_native(monkeypatch, tmp_path):
    from arenamcp import watcher

    home = _fake_home(monkeypatch, tmp_path)
    log = _make_native_log(home)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert watcher._default_log_path() == str(log)


def test_default_log_path_darwin_bottle_fallback(monkeypatch, tmp_path):
    """No native log → the CrossOver bottle's Player.log wins."""
    from arenamcp import watcher

    home = _fake_home(monkeypatch, tmp_path)
    bottle = _make_bottle(home)
    monkeypatch.setattr(sys, "platform", "darwin")

    result = Path(watcher._default_log_path())
    expected = (
        bottle / "drive_c/users/crossover"
        / "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
    )
    assert result == expected


def test_default_log_path_darwin_nothing_exists(monkeypatch, tmp_path):
    """Fresh Mac: still returns the native default path (mirrors Linux)."""
    from arenamcp import watcher

    home = _fake_home(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")

    assert watcher._default_log_path() == str(home / NATIVE_LOG_SUFFIX)


# ── mtgadb darwin card-database discovery ────────────────────────────

def _patch_mtgadb_paths(monkeypatch, paths):
    from arenamcp import mtgadb

    monkeypatch.setattr(mtgadb, "MTGA_PATHS", paths)
    import arenamcp.settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_settings", lambda: {})
    return mtgadb


def test_mtgadb_darwin_steam_layout(monkeypatch, tmp_path):
    """Steam Mac: Raw_CardDatabase under <common>/MTGA/MTGA_Data/Downloads/Raw."""
    steam_dir = tmp_path / "Steam/steamapps/common/MTGA"
    raw = steam_dir / "MTGA_Data/Downloads/Raw"
    raw.mkdir(parents=True)
    db = raw / "Raw_CardDatabase_abc123.mtga"
    db.write_bytes(b"SQLite format 3\x00")

    mtgadb = _patch_mtgadb_paths(monkeypatch, [steam_dir])
    assert mtgadb.find_mtga_database() == db


def test_mtgadb_darwin_direct_install_layout(monkeypatch, tmp_path):
    """Direct install: com.wizards.mtga holds Downloads/Raw with no
    MTGA_Data level (17Lands convention)."""
    data_dir = tmp_path / "com.wizards.mtga"
    raw = data_dir / "Downloads/Raw"
    raw.mkdir(parents=True)
    db = raw / "Raw_CardDatabase_def456.mtga"
    db.write_bytes(b"SQLite format 3\x00")

    mtgadb = _patch_mtgadb_paths(monkeypatch, [data_dir])
    assert mtgadb.find_mtga_database() == db


def test_mtgadb_crossover_paths_globbed(monkeypatch, tmp_path):
    from arenamcp import mtgadb

    home = _fake_home(monkeypatch, tmp_path)
    bottle_game_dir = _make_bottle(home) / (
        "drive_c/Program Files/Wizards of the Coast/MTGA"
    )
    paths = mtgadb._darwin_crossover_paths()
    assert bottle_game_dir in paths
