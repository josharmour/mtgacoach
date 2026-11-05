"""Render the real compact UI with a labeled test fixture for the public site.

Run with the project's development environment (PySide6 and pytest installed):
    python scripts/render_website_coach.py [output.webp]

No live game, model call, engine process or saved user setting is used.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "website/static/img/coach-4-example.webp"
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = "2"
    sys.path[:0] = [str(root / "src"), str(root)]

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from tests.test_compact_coach import make_snapshot
    from tests.test_strategy_view import forecast

    from arenamcp.desktop import theme
    from arenamcp.desktop.compact_coach import CompactCoachPanel

    app = QApplication([])
    saved = []
    with patch("arenamcp.settings.get_settings") as get_settings:
        get_settings.return_value.get.side_effect = lambda key, default=None: default
        theme.apply_theme(app, "dark")
        panel = CompactCoachPanel()
        state = make_snapshot()
        plan = forecast(state)
        plan["facts"]["role_reason"] = "Keep pressure on while preserving your next play."
        panel._on_game_state_changed(state)
        panel._on_game_plan_changed(plan)
        panel._on_status_changed("AUTOPILOT", "OFF")
        panel._on_status_changed("MODEL", "Online")
        panel.resize(420, 820)
        panel.show()

        def save():
            saved.append(panel.grab().save(str(output), "WEBP", 90))
            app.quit()

        QTimer.singleShot(900, save)
        app.exec()
    return 0 if saved == [True] else 1


if __name__ == "__main__":
    raise SystemExit(main())
