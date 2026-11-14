"""The autopilot guards' modes: 'off', 'shadow' or 'on' (``line_guard`` and its callers).

A guard compares the model's decision with the line search and, when its mode is 'on', replaces
or adjusts it; in 'shadow' it only logs what it would have done. Each guard's mode comes from its
environment variable (a dev ``.env`` or the shell; it wins), then its ``guard_<kind>`` setting in
settings.json (Tools > Autopilot Guards in the desktop app), then ``GUARD_DEFAULTS``.

Kept free of heavy imports: the desktop app reads the labels and defaults to build its menu.
"""

from __future__ import annotations

import os

GUARD_MODES = ("off", "shadow", "on")
GUARD_ENV = {
    "line": "ARENAMCP_LINE_GUARD",
    "mode": "ARENAMCP_MODE_GUARD",
    "line_gap": "ARENAMCP_LINE_GAP",
    "attack_line": "ARENAMCP_ATTACK_LINE_GUARD",
    "attack_push": "ARENAMCP_ATTACK_PUSH",
}
GUARD_SETTINGS = {kind: f"guard_{kind}" for kind in GUARD_ENV}
# Defaults chosen by the user on 2026-10-20 from the 2026-10-19 shadow logs: the line guard logged
# 17 would-replace verdicts, 8 of them a game plan that died against a searched line that survived
# or won; the line gap only adds a high-effort second look; the attack push answers the losing
# empty declarations of c0af235f T12 (#178) and match 2149ea96 (#189). The mode guard (one shadow
# verdict ever) and the attack-line guard (none) stay in shadow until their logs make a case.
GUARD_DEFAULTS = {
    "line": "on",
    "mode": "shadow",
    "line_gap": "on",
    "attack_line": "shadow",
    "attack_push": "on",
}
# (menu title, tooltip) for the desktop app's Tools > Autopilot Guards.
GUARD_LABELS = {
    "line": (
        "Line Guard",
        "Replaces a pick (or the game plan's step for this turn) that the line search finds clearly "
        "worse than its best line, e.g. a plan that dies on T15 against a line that survives.",
    ),
    "line_gap": (
        "Line Gap Second Look",
        "A cast the line search rates far below its best line waits for a high-effort answer; with "
        "none, the best line's play is made.",
    ),
    "attack_push": (
        "Attack Push",
        "When holding back is dead and attacking survives (or clearly ranks higher), declares the "
        "searched attack instead of an empty or weaker declaration.",
    ),
    "attack_line": (
        "Attack-Line Guard",
        "Holds back declared attackers when attacking with them dies sooner than the best line that "
        "keeps them home.",
    ),
    "mode": (
        "Mode Guard",
        "Replaces a modal spell's chosen mode ('choose one') when another mode survives longer in "
        "the line search.",
    ),
}
MODE_LABELS = {
    "on": "On",
    "shadow": "Shadow (log only)",
    "off": "Off",
}


def _normalised(value: object) -> str:
    return str(value or "").strip().lower()


def setting_mode(kind: str, settings: object = None) -> str:
    """The ``guard_<kind>`` setting if it is a valid mode, else the default (no env lookup)."""
    if kind not in GUARD_SETTINGS:
        return "shadow"
    try:
        if settings is None:
            from arenamcp.settings import get_settings

            settings = get_settings()
        value = _normalised(settings.get(GUARD_SETTINGS[kind]))
    except Exception:  # a settings problem never breaks a decision
        value = ""
    return value if value in GUARD_MODES else GUARD_DEFAULTS[kind]


def guard_source(kind: str) -> str:
    """Where ``guard_mode(kind)`` comes from: 'env', 'setting' or 'default'."""
    env = GUARD_ENV.get(kind)
    if env is None:
        return "default"
    if _normalised(os.environ.get(env)) in GUARD_MODES:
        return "env"
    try:
        from arenamcp.settings import get_settings

        if _normalised(get_settings().get(GUARD_SETTINGS[kind])) in GUARD_MODES:
            return "setting"
    except Exception:
        pass
    return "default"


def guard_mode(kind: str) -> str:
    """'off', 'shadow' or 'on' for a guard: its env var, else its setting, else ``GUARD_DEFAULTS``.

    A value that is none of the modes falls through to the next source; an unknown kind is
    'shadow'.
    """
    env = GUARD_ENV.get(kind)
    if env is None:
        return "shadow"
    value = _normalised(os.environ.get(env))
    if value in GUARD_MODES:
        return value
    return setting_mode(kind)


def describe_guard_modes() -> str:
    """'line=on (default), mode=shadow (setting), ...' for the engine's log."""
    return ", ".join(f"{kind}={guard_mode(kind)} ({guard_source(kind)})" for kind in GUARD_ENV)
