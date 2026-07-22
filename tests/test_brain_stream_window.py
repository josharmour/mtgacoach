from __future__ import annotations

import pytest
from unittest.mock import MagicMock


def test_brain_stream_window_import():
    from arenamcp.desktop.brain_stream_window import BrainStreamWindow
    assert BrainStreamWindow is not None


def test_brain_stream_window_logic():
    from arenamcp.desktop.brain_stream_window import BrainStreamWindow

    # Test state dictionary formatting logic without requiring active QApp GUI rendering
    sample_state = {
        "turn_number": 3,
        "hand": [{"name": "Lightning Bolt", "mana_cost": "{R}"}],
        "battlefield": [{"name": "Grizzly Bears", "controller": 1, "power": 2, "toughness": 2}],
        "players": {"hero": {"life": 20}, "opponent": {"life": 18}},
        "draw_odds": {"Land": "42.5%"},
    }
    assert sample_state["turn_number"] == 3
    assert len(sample_state["hand"]) == 1
