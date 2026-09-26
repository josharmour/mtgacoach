"""The background win-plan worker talks to the online gateway.

2026-09-24: it built a bare ProxyBackend(model=..., enable_thinking=True) —
no URL or license key — which falls back to localhost:8000; every win-in-2/3
request failed with "Connection error" and flipped backend health to
degraded at the start of each of the player's turns.
"""

import arenamcp.coach as coach_module
import arenamcp.standalone_hotkeys as hotkeys_module
from arenamcp.standalone import StandaloneCoach


class _Backend:
    enable_thinking = False


def test_worker_uses_the_configured_backend_factory(monkeypatch):
    made = []

    def fake_create_backend(mode, model=None, progress_callback=None):
        made.append((mode, model))
        return _Backend()

    monkeypatch.setattr(coach_module, "create_backend", fake_create_backend)
    monkeypatch.setattr(hotkeys_module.time, "sleep", lambda s: None)

    used = []

    class _Coach:
        def get_win_plan(self, game_state, turns, library_summary, backend=None):
            used.append(backend)
            return ""

    coach = StandaloneCoach.__new__(StandaloneCoach)
    coach._thinking_model = "glm-5.3-flash"
    coach._backend_name = "online"
    coach._coach = _Coach()
    coach._compute_library_summary = lambda state: ""
    coach._win_plan_worker({"turn": {"turn_number": 5}})

    assert made == [("online", "glm-5.3-flash")]
    assert used and all(isinstance(b, _Backend) and b.enable_thinking for b in used)
