"""Regression tests: backend-health tags must reach the GUI, and locally
generated advice must be tagged.

Two verified product bugs this locks down:

1. ``pipe_adapter.strip_markup`` treated ``[BACKEND ERROR]`` / ``[LOCAL
   FALLBACK]`` as Rich markup and deleted them from every log/advice/status/
   error event, so the desktop UI never displayed a single health tag —
   defeating the entire point of the tagging work.
2. ``_postprocess_advice`` substituted the deterministic scorer's best legal
   action for illegal model advice ("Replaced illegal advice with legal
   action") *without* a ``[LOCAL FALLBACK]`` tag, making locally generated
   advice indistinguishable from model output.

Plus the TTS guard: a caller-prepended game-plan intro used to bury a leading
tag mid-string, where the ``startswith``-based ``strip_health_tags`` could not
remove it — so the voice read "local fallback" out loud.
"""

from __future__ import annotations

import json
from typing import Any

from arenamcp.backend_health import (
    BACKEND_ERROR_PREFIX,
    LOCAL_FALLBACK_PREFIX,
    strip_health_tags,
)
from arenamcp.coach import CoachEngine
from arenamcp.pipe_adapter import PipeAdapter, health_tag_of, strip_markup

# ── 1. strip_markup must not eat the health tags ─────────────────────


def test_strip_markup_preserves_local_fallback_tag():
    assert strip_markup(f"{LOCAL_FALLBACK_PREFIX} pass priority") == (
        f"{LOCAL_FALLBACK_PREFIX} pass priority"
    )


def test_strip_markup_preserves_backend_error_tag():
    assert strip_markup(f"{BACKEND_ERROR_PREFIX} HTTP 401 from gateway") == (
        f"{BACKEND_ERROR_PREFIX} HTTP 401 from gateway"
    )


def test_strip_markup_still_strips_rich_markup():
    assert strip_markup("[bold]Attack[/bold] now[/]") == "Attack now"
    assert strip_markup("[link=https://x.test]click[/link]") == "click"
    # Legal-action markers are still markup-shaped noise and still go.
    assert strip_markup("Cast Lightning Bolt [OK]") == "Cast Lightning Bolt"


def test_strip_markup_preserves_tag_alongside_real_markup():
    out = strip_markup(f"{LOCAL_FALLBACK_PREFIX} [bold]Cast Bolt[/bold]")
    assert out == f"{LOCAL_FALLBACK_PREFIX} Cast Bolt"


def test_health_tag_of():
    assert health_tag_of(f"{LOCAL_FALLBACK_PREFIX} pass priority") == "local_fallback"
    assert health_tag_of(f"{BACKEND_ERROR_PREFIX} auth failed") == "backend_error"
    assert health_tag_of("Attack with the 3/3.") == ""


# ── 2. Tags survive all the way into the emitted pipe events ─────────


def _drain(adapter: PipeAdapter) -> list[dict[str, Any]]:
    events = []
    while not adapter._write_queue.empty():
        events.append(json.loads(adapter._write_queue.get_nowait()))
    return events


def test_advice_event_keeps_local_fallback_tag():
    adapter = PipeAdapter()
    adapter.advice(f"{LOCAL_FALLBACK_PREFIX} Cast Lightning Bolt", "SEAT 1")
    (event,) = _drain(adapter)
    assert event["type"] == "advice"
    assert event["text"].startswith(LOCAL_FALLBACK_PREFIX)
    assert event["health_tag"] == "local_fallback"


def test_error_event_keeps_backend_error_tag():
    adapter = PipeAdapter()
    adapter.error(f"{BACKEND_ERROR_PREFIX} HTTP 401 from api.mtgacoach.com")
    (event,) = _drain(adapter)
    assert event["type"] == "error"
    assert event["message"].startswith(BACKEND_ERROR_PREFIX)
    assert event["health_tag"] == "backend_error"


def test_log_and_status_events_keep_tags():
    adapter = PipeAdapter()
    adapter.log(f"{BACKEND_ERROR_PREFIX} gateway unreachable")
    adapter.status("BACKEND", f"{LOCAL_FALLBACK_PREFIX} degraded")
    log_event, status_event = _drain(adapter)
    assert log_event["message"].startswith(BACKEND_ERROR_PREFIX)
    assert status_event["value"].startswith(LOCAL_FALLBACK_PREFIX)


# ── 3. The scorer-replacement path is tagged ─────────────────────────


def _make_coach() -> CoachEngine:
    class _Stub:
        timeout_s = 5.0

        def complete(self, *a, **k):
            return ""

    return CoachEngine(backend=_Stub())


def _make_state(legal_actions: list[str], hand: list[dict] | None = None) -> dict[str, Any]:
    return {
        "players": [
            {"seat_id": 1, "is_local": True, "life_total": 20},
            {"seat_id": 2, "is_local": False, "life_total": 20},
        ],
        "turn": {
            "active_player": 1,
            "priority_player": 1,
            "turn_number": 4,
            "phase": "Phase_Main1",
            "step": "Step_Main",
        },
        "hand": hand or [],
        "battlefield": [],
        "graveyard": [],
        "stack": [],
        "exile": [],
        "legal_actions": legal_actions,
    }


def test_illegal_advice_replacement_is_tagged_local_fallback():
    """The deterministic scorer speaking must never look like model advice."""
    coach = _make_coach()
    state = _make_state(legal_actions=["Cast Lightning Bolt [OK]", "Pass"])

    out = coach._postprocess_advice("Sacrifice the Bloodghast to your Altar.", state)

    assert out.startswith(LOCAL_FALLBACK_PREFIX), out
    assert "Lightning Bolt" in out
    # And the tag comes off cleanly for TTS.
    assert LOCAL_FALLBACK_PREFIX not in strip_health_tags(out)


def test_legal_model_advice_is_not_tagged():
    """Real model advice that survives the legality filter stays untagged."""
    coach = _make_coach()
    state = _make_state(legal_actions=["Cast Lightning Bolt [OK]", "Pass"])

    out = coach._postprocess_advice("Cast Lightning Bolt at their face.", state)

    assert LOCAL_FALLBACK_PREFIX not in out
    assert BACKEND_ERROR_PREFIX not in out


def test_no_legal_actions_fallback_keeps_single_leading_tag():
    coach = _make_coach()
    state = _make_state(legal_actions=[])

    out = coach._postprocess_advice("Sacrifice the Bloodghast to your Altar.", state)

    assert out.startswith(LOCAL_FALLBACK_PREFIX), out
    assert out.count(LOCAL_FALLBACK_PREFIX) == 1


# ── 4. A buried tag is hoisted so TTS never reads it aloud ───────────


def test_prepended_game_plan_intro_does_not_bury_the_tag():
    """coach.get_advice() prepends "<plan intro>. " to advice, which used to
    push a leading [LOCAL FALLBACK] into the middle of the string where
    strip_health_tags (startswith-based) could not remove it."""
    coach = _make_coach()
    state = _make_state(legal_actions=["Cast Lightning Bolt [OK]", "Pass"])

    buried = f"Plan: race them down. {LOCAL_FALLBACK_PREFIX} Cast Lightning Bolt."
    out = coach._postprocess_advice(buried, state)

    assert out.startswith(LOCAL_FALLBACK_PREFIX), out
    assert out.count(LOCAL_FALLBACK_PREFIX) == 1
    spoken = strip_health_tags(out)
    assert "LOCAL FALLBACK" not in spoken
    assert "local fallback" not in spoken.lower()
    assert "Plan: race them down" in spoken


def test_buried_backend_error_tag_wins_over_local_fallback():
    coach = _make_coach()
    state = _make_state(legal_actions=["Cast Lightning Bolt [OK]", "Pass"])

    buried = f"Plan: race them down. {BACKEND_ERROR_PREFIX} Cast Lightning Bolt."
    out = coach._postprocess_advice(buried, state)

    assert out.startswith(BACKEND_ERROR_PREFIX), out
    assert "LOCAL FALLBACK" not in strip_health_tags(out)
