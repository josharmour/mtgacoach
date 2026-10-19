"""Load the mtgacoach.com website (website/app.py) in tests without leaking modules.

The site imports its flat modules by bare name (``db``, ``patreon``, ``state``,
``routers.*``, ...) from website/. Each test needs fresh copies bound to its own
temp working directory (db.py opens ./data/mtgacoach.db), and none of them may
stay in ``sys.modules`` or ``sys.path`` afterwards, or a later test imports a
stale ``db`` pointing at a deleted temp dir. Everything here goes through
``monkeypatch`` so pytest undoes it at teardown.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

WEBSITE_DIR = Path(__file__).resolve().parents[1] / "website"

WEBSITE_MODULES = (
    "db",
    "abuse_protection",
    "providers",
    "patreon",
    "state",
    "arenaonair",
    "match_bundles",
    "routers",
    "routers.admin",
    "routers.billing",
    "routers.proxy",
)

_CONFIG = """
server:
  host: "127.0.0.1"
  port: 8443
providers: []
default_model: "glm-5.3-flash"
admin:
  username: "admin"
  password: "test-admin"
database:
  path: "./data/mtgacoach.db"
"""


def isolate_website_modules(monkeypatch) -> None:
    """Put website/ first on sys.path and drop cached website modules, both undone at teardown."""
    monkeypatch.syspath_prepend(str(WEBSITE_DIR))
    for name in WEBSITE_MODULES:
        # setitem records the original entry (or its absence) for teardown;
        # delitem then clears it so the next import loads a fresh module.
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)


def load_website_module(monkeypatch, name: str, filename: str):
    """Exec website/<filename> as module ``name`` and register it for this test only."""
    spec = importlib.util.spec_from_file_location(name, WEBSITE_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def load_website_app(tmp_path: Path, monkeypatch, module_name: str = "website_app_test"):
    """Load website/app.py against tmp_path; returns (app module, db, patreon)."""
    (tmp_path / "static").mkdir(exist_ok=True)
    (tmp_path / "templates").mkdir(exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_CONFIG.lstrip(), encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("LITELLM_MASTER_KEY", "sk-master-test")
    isolate_website_modules(monkeypatch)
    module = load_website_module(monkeypatch, module_name, "app.py")
    return module, sys.modules["db"], sys.modules["patreon"]
