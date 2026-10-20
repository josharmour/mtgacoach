"""Autopilot guard modes: recommended defaults, a setting per guard and the Tools menu (2026-10-20).

The guards shipped in shadow. From the 2026-10-19 shadow logs the user chose defaults: the line
guard (17 would-replace verdicts, 8 of them a dying game plan against a surviving or lethal line),
the line gap and the attack push on; the mode and attack-line guards stay in shadow. A mode comes
from the guard's environment variable, else its ``guard_<kind>`` setting (Tools > Autopilot
Guards), else ``GUARD_DEFAULTS``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arenamcp import guard_settings as gs
from arenamcp import line_guard
from arenamcp import settings as settings_mod
from arenamcp.pipe_adapter import PipeAdapter

ENV_VARS = tuple(gs.GUARD_ENV.values())


@pytest.fixture
def fresh_settings(tmp_path, monkeypatch):
    """A settings.json of this test's own, and no guard environment variables."""
    monkeypatch.setattr(settings_mod, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings_mod, "_settings", None)
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return settings_mod.get_settings()


def test_the_recommended_defaults(fresh_settings):
    assert gs.GUARD_DEFAULTS == {
        "line": "on",
        "mode": "shadow",
        "line_gap": "on",
        "attack_line": "shadow",
        "attack_push": "on",
    }
    assert {kind: gs.guard_mode(kind) for kind in gs.GUARD_ENV} == gs.GUARD_DEFAULTS
    assert (line_guard.line_gap_mode(), line_guard.attack_line_mode(), line_guard.attack_push_mode()) == (
        "on",
        "shadow",
        "on",
    )
    assert line_guard.guard_mode is gs.guard_mode  # one reader for every guard
    assert gs.describe_guard_modes() == (
        "line=on (default), mode=shadow (default), line_gap=on (default), "
        "attack_line=shadow (default), attack_push=on (default)"
    )


def test_a_setting_beats_the_default_and_the_env_beats_the_setting(fresh_settings, monkeypatch):
    fresh_settings.set("guard_line", "off")
    fresh_settings.set("guard_attack_line", "ON ")
    assert (gs.guard_mode("line"), gs.guard_source("line")) == ("off", "setting")
    assert line_guard.attack_line_mode() == "on"
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "shadow")
    assert (gs.guard_mode("line"), gs.guard_source("line")) == ("shadow", "env")
    # Not a mode: the env var is skipped, then the setting.
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "1")
    assert (gs.guard_mode("line"), gs.guard_source("line")) == ("off", "setting")
    fresh_settings.set("guard_line", "maybe")
    assert (gs.guard_mode("line"), gs.guard_source("line")) == ("on", "default")
    fresh_settings.set("guard_line", None)
    assert gs.guard_mode("line") == "on"
    assert gs.guard_mode("unknown") == "shadow" and gs.guard_source("unknown") == "default"


def test_a_saved_setting_survives_a_new_engine(fresh_settings, monkeypatch):
    fresh_settings.set("guard_attack_push", "shadow")
    monkeypatch.setattr(settings_mod, "_settings", None)  # what a restart reads back
    assert gs.guard_mode("attack_push") == "shadow"
    assert gs.setting_mode("attack_push") == "shadow" and gs.setting_mode("line") == "on"


def _adapter(settings) -> tuple[PipeAdapter, list[dict]]:
    adapter = PipeAdapter()
    events: list[dict] = []
    adapter._emit = events.append  # type: ignore[method-assign]
    adapter._coach = SimpleNamespace(settings=settings)
    return adapter, events


def test_the_pipe_command_applies_to_the_running_engine(fresh_settings, monkeypatch):
    adapter, events = _adapter(fresh_settings)
    adapter._dispatch({"cmd": "set_guard_mode", "guard": "attack_line", "mode": "on"})
    assert fresh_settings.get("guard_attack_line") == "on" and line_guard.attack_line_mode() == "on"
    adapter._dispatch({"cmd": "set_guard_mode", "guard": "attack_line", "mode": "default"})
    assert fresh_settings.get("guard_attack_line") is None and line_guard.attack_line_mode() == "shadow"
    adapter._dispatch({"cmd": "set_guard_mode", "guard": "nonsense", "mode": "on"})
    adapter._dispatch({"cmd": "set_guard_mode", "guard": "line", "mode": "sometimes"})
    assert fresh_settings.get("guard_line") is None
    logs = [e["message"] for e in events if e.get("type") == "log"]
    assert logs == ["Autopilot guard attack_line: on", "Autopilot guard attack_line: default (shadow)"]
    monkeypatch.setenv("ARENAMCP_LINE_GUARD", "shadow")
    adapter._dispatch({"cmd": "set_guard_mode", "guard": "line", "mode": "off"})
    assert events[-1]["message"] == "Autopilot guard line: off (but ARENAMCP_LINE_GUARD=shadow overrides it)"


def _guard_actions(window):
    from PySide6.QtGui import QAction

    return {
        str(action.data()): action
        for action in window.findChildren(QAction)
        if "|" in str(action.data() or "")
    }


def test_the_tools_menu_sets_each_guard(qapp, fresh_settings, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QMenu

    from arenamcp.desktop.main_window import MainWindow

    window = MainWindow()
    try:
        sent: list[dict] = []
        monkeypatch.setattr(window._session._process, "send_payload", sent.append)
        menus = {menu.title() for menu in window.findChildren(QMenu)}
        assert "Autopilot Guards" in menus
        assert {title for title, _tip in gs.GUARD_LABELS.values()} <= menus
        actions = _guard_actions(window)
        assert len(actions) == len(gs.GUARD_ENV) * len(gs.GUARD_MODES)
        # Nothing saved: the recommended mode of each guard is checked and labelled.
        for kind, mode in gs.GUARD_DEFAULTS.items():
            assert actions[f"{kind}|{mode}"].isChecked()
            assert actions[f"{kind}|{mode}"].text().endswith("(recommended)")
        actions["line|off"].trigger()
        assert actions["line|off"].isChecked() and not actions["line|on"].isChecked()
        assert fresh_settings.get("guard_line") == "off"
        assert sent[-1] == {"cmd": "set_guard_mode", "guard": "line", "mode": "off"}
        actions["attack_line|on"].trigger()
        assert fresh_settings.get("guard_attack_line") == "on"
        window._restore_recommended_guards()
        assert all(actions[f"{kind}|{mode}"].isChecked() for kind, mode in gs.GUARD_DEFAULTS.items())
        assert fresh_settings.get("guard_line") is None and fresh_settings.get("guard_attack_line") is None
        assert {"cmd": "set_guard_mode", "guard": "line", "mode": "default"} in sent
    finally:
        window.close()


def test_a_saved_choice_is_checked_when_the_app_starts(qapp, fresh_settings):
    pytest.importorskip("PySide6")
    from arenamcp.desktop.main_window import MainWindow

    fresh_settings.set("guard_attack_push", "off")
    window = MainWindow()
    try:
        actions = _guard_actions(window)
        assert actions["attack_push|off"].isChecked() and not actions["attack_push|on"].isChecked()
    finally:
        window.close()


def test_a_bug_report_names_each_guards_mode_and_its_source(fresh_settings, monkeypatch):
    from arenamcp.standalone_diagnostics import _DiagnosticsMixin

    class Engine(_DiagnosticsMixin):
        autopilot = None

    fresh_settings.set("guard_mode", "on")
    monkeypatch.setenv("ARENAMCP_ATTACK_PUSH", "off")
    guards = Engine()._collect_autopilot_info()["guards"]
    assert guards["line"] == {"mode": "on", "source": "default"}
    assert guards["mode"] == {"mode": "on", "source": "setting"}
    assert guards["attack_push"] == {"mode": "off", "source": "env"}
