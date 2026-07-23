from __future__ import annotations

from typing import Any

from arenamcp import standalone


class _DraftHudUI:
    def __init__(self) -> None:
        self.draft_states: list[dict[str, Any]] = []
        self.game_states: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.statuses: list[tuple[str, str]] = []

    def emit_draft_state(self, snapshot: dict[str, Any]) -> None:
        self.draft_states.append(snapshot)

    def emit_game_state(self, snapshot: dict[str, Any]) -> None:
        self.game_states.append(snapshot)

    def log(self, message: str) -> None:
        self.logs.append(message)

    def status(self, key: str, value: str) -> None:
        self.statuses.append((key, value))


class _DraftMcp:
    def __init__(self, draft_pack: dict[str, Any]) -> None:
        self._draft_pack = draft_pack

    def poll_log(self) -> None:
        return None

    def get_draft_pack(self) -> dict[str, Any]:
        return self._draft_pack

    def evaluate_draft_pack(self) -> dict[str, Any]:
        return {
            "is_active": True,
            "spoken_advice": "Take Alpha",
            "picked_count": 0,
            "evaluations": [
                {
                    "name": "Alpha",
                    "score": 8.5,
                    "gih_wr": 0.6,
                    "all_reasons": ["best rate"],
                }
            ],
        }


def _make_coach(ui: _DraftHudUI, mcp: _DraftMcp) -> standalone.StandaloneCoach:
    coach = standalone.StandaloneCoach.__new__(standalone.StandaloneCoach)
    coach.ui = ui
    coach._mcp = mcp
    coach._voice_output = None
    coach._running = True
    coach.draft_mode = False
    coach.set_code = None
    coach.spoken: list[str] = []
    coach.speak_advice = coach.spoken.append
    return coach


def test_emit_pipe_snapshots_forwards_inactive_draft_state() -> None:
    ui = _DraftHudUI()
    coach = _make_coach(ui, _DraftMcp({"is_active": False}))

    coach._emit_pipe_snapshots(draft_state={"is_active": False, "message": "no draft"})

    assert ui.draft_states == [{"is_active": False, "message": "no draft"}]
    assert ui.game_states == []


def test_coaching_loop_emits_draft_state_during_active_draft(monkeypatch) -> None:
    draft_pack = {
        "is_active": True,
        "is_sealed": False,
        "set_code": "TDM",
        "pack_number": 1,
        "pick_number": 1,
        "cards": [{"name": "Alpha", "gih_wr": 0.6, "alsa": 3.2}],
    }
    ui = _DraftHudUI()
    coach = _make_coach(ui, _DraftMcp(draft_pack))

    def fake_sleep(_seconds: float) -> None:
        coach._running = False

    monkeypatch.setattr(standalone.time, "sleep", fake_sleep)

    coach._coaching_loop()

    assert ui.draft_states == [draft_pack]
    assert coach.spoken == ["Take Alpha"]
