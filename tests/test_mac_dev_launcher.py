from __future__ import annotations

import importlib.util
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).resolve().parents[1] / "scripts/install_mac_dev_launcher.py"
    spec = importlib.util.spec_from_file_location("coach_dev_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dev_installer_refuses_to_replace_managed_app(installer, tmp_path):
    checkout = tmp_path / "checkout"
    entry = checkout / "src/arenamcp/desktop/app.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    app = tmp_path / "Managed.app"
    info = app / "Contents/Info.plist"
    info.parent.mkdir(parents=True)
    original = plistlib.dumps({"CFBundleIdentifier": "com.MTGACoachMaster.MTGACoachMaster"})
    info.write_bytes(original)
    with pytest.raises(ValueError, match="another application"):
        installer.install(checkout, Path(sys.executable), app)
    assert info.read_bytes() == original


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac source launcher qualification")
def test_native_dev_launcher_restarts_once_and_keeps_source_environment(installer, tmp_path, monkeypatch):
    checkout = tmp_path / "Source with spaces"
    entry = checkout / "src/arenamcp/desktop/app.py"
    entry.parent.mkdir(parents=True)
    entry.touch()
    script = checkout / "scripts/launch_desktop.py"
    script.parent.mkdir()
    script.write_text("""
import json, os, pathlib, sys
p = pathlib.Path("attempt.json")
count = json.loads(p.read_text())["count"] + 1 if p.exists() else 1
p.write_text(json.dumps({"count": count, "args": sys.argv[1:],
    "source": os.environ["PYTHONPATH"], "checkout": os.environ["MTGACOACH_DEV_CHECKOUT"],
    "app": os.environ["MTGACOACH_DEV_APP"],
    "pending": os.environ.get("MTGACOACH_VELOPACK_PENDING")}))
raise SystemExit(42 if count == 1 else 0)
""")
    monkeypatch.setenv("MTGACOACH_VELOPACK_PENDING", "must-not-use-managed-updater")
    app = installer.install(checkout, Path(sys.executable), tmp_path / "Dev.app")
    assert plistlib.loads((app / "Contents/Info.plist").read_bytes())["LSUIElement"] is True
    subprocess.run([app / "Contents/MacOS/CoachDev", "--candidate"], check=True, timeout=30)
    result = json.loads((checkout / "attempt.json").read_text())
    assert result == {
        "count": 2,
        "args": ["--candidate"],
        "source": str(checkout / "src"),
        "checkout": str(checkout),
        "app": str(app),
        "pending": None,
    }
