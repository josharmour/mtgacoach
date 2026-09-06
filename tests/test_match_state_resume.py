from __future__ import annotations

import json
import time

from arenamcp import gamestate


def test_mark_match_ended_prevents_loading_saved_state(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "last_match.json"
    monkeypatch.setattr(gamestate, "MATCH_STATE_PATH", state_path)

    state_path.write_text(
        json.dumps(
            {
                "match_id": "match-123",
                "local_seat_id": 1,
                "log_offset": 456,
                "turn_number": 3,
                "phase": "Main1",
                "timestamp": time.time(),
                "status": "active",
            }
        ),
        encoding="utf-8",
    )

    assert gamestate.load_match_state() is not None

    gamestate.mark_match_ended()

    assert gamestate.load_match_state() is None
