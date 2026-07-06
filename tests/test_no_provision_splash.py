"""v2.7.2: an installed app (running inside PySide6) must never enter the
provisioning splash — it would run setup_wizard.py, which isn't shipped in
the wheel ('Setup failed' screenshot, v2.7.1)."""
import sys
import pytest

pytest.importorskip("PySide6")


def test_installed_app_skips_provisioning_splash(monkeypatch):
    from arenamcp.desktop import app
    import arenamcp.desktop.runtime as rt

    # Trip-wire: reaching the splash would touch the wizard command.
    monkeypatch.setattr(
        rt, "get_setup_wizard_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("splash provisioned")),
    )
    assert "PySide6" in sys.modules  # true whenever the Qt app is running
    assert app._run_first_run_setup(object()) is True
