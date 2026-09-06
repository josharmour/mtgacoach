"""Unit tests for MageZeroStateEncoder and live index survivability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arenamcp.magezero_encoder import MageZeroStateEncoder
from arenamcp.magezero_hash import path_index


def test_hash_known_probes():
    """Verify known probes from magezero-integration-review.md."""
    assert path_index(["Player#1", "LifeTotal@10#1"]) == 147844
    assert (
        path_index(["Player#1", "Battlefield#1", "Malcolm, Alluring Scoundrel#1", "Tapped#1"])
        == 1951888
    )
    assert path_index(["Stack#1", "Cast Negate#1", "targets#1"]) == 1257923
    assert path_index(["Opponent#1", "Hand#1", "Spell Pierce#2"]) == 1254893


def test_state_encoder_output():
    state: dict[str, Any] = {
        "local_seat_id": 1,
        "turn": {
            "turn_number": 3,
            "phase": "Phase_Main1",
            "active_player": 1,
            "priority_player": 1,
        },
        "players": [
            {
                "seat_id": 1,
                "life_total": 20,
                "is_local": True,
                "lands_played": 0,
                "mana_pool": {"U": 2},
            },
            {"seat_id": 2, "life_total": 18, "is_local": False},
        ],
        "battlefield": [
            {
                "name": "Malcolm, Alluring Scoundrel",
                "controller_seat_id": 1,
                "power": 2,
                "toughness": 1,
                "is_tapped": False,
                "type_line": "Legendary Creature — Siren Pirate",
            },
            {
                "name": "Island",
                "controller_seat_id": 1,
                "type_line": "Basic Land — Island",
                "is_tapped": True,
            },
        ],
        "hand": [
            {"name": "Spell Pierce", "mana_cost": "{U}", "type_line": "Instant"},
        ],
    }

    indices = MageZeroStateEncoder.encode(state)
    assert len(indices) > 0
    assert all(0 <= idx < 2_000_000 for idx in indices)

    # Check that known probe indices are present
    assert 147844 in indices  # Player#1 LifeTotal@10#1 (since life is 20)

    # Check live indices survivability against training ignore filter
    live_path = Path(__file__).parents[1] / "tools" / "magezero" / "live_indices.json"
    if live_path.is_file():
        live_indices = set(json.loads(live_path.read_text(encoding="utf-8")))
        live_hits = [idx for idx in indices if idx in live_indices]
        # Must hit a substantial number of live indices (> 20)
        assert len(live_hits) >= 20, f"Expected >=20 live hits, got {len(live_hits)}"
