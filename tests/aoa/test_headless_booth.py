"""Exercise the independent AOA source through Coach's headless facade.

AOA_SOURCE may point at an installed checkout. Tests never start its app.
"""

import json
import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from arenamcp.commentary.client import CommentaryClient
from arenamcp.commentary.config import PERSONAS, CommentaryConfig
from arenamcp.commentary.contracts import Evidence
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import HeadlessBooth


@pytest.fixture
def source(monkeypatch):
    root = Path(os.environ.get("AOA_SOURCE", Path(__file__).resolve().parents[3] / "arenaonair"))
    if not (root / "src/arenaonair/llm_booth.py").is_file():
        pytest.skip("Set AOA_SOURCE to run independent booth contract tests")
    monkeypatch.syspath_prepend(str(root / "src"))
    return str(root)


def observation():
    state = {
        "match_id": "replay",
        "game_number": 1,
        "local_seat_id": 1,
        "turn_info": {"turn_number": 2, "active_player": 1, "phase": "main"},
        "zones": {"stack": [{"instance_id": 10, "name": "Opt", "grp_id": 1}]},
        "players": [{"seat_id": 1, "life_total": 20}, {"seat_id": 2, "life_total": 20}],
    }
    obs = ObservationAdapter().project(state)
    evidence = Evidence(
        "cast-1",
        "action_observed",
        obs.source_sequence,
        obs.snapshot_id,
        payload={"name": "Opt", "grp_id": 1, "instance_id": 10},
        seat=1,
        received_at=obs.arrived_at,
    )
    return replace(obs, events=(evidence,))


def response(context, audit, supported=True, count=2):
    if audit:
        return {
            "checks": [
                {"index": i, "supported": supported, "unsupported_claim": "" if supported else "unsupported"}
                for i in range(len(context["lines"]))
            ]
        }
    return {
        "turns": [
            {"role": "play_by_play", "text": "Opt goes onto the stack.", "refs": context["new_events"]},
            {
                "role": "color_analyst",
                "text": "We have a cast, with its outcome still unknown.",
                "refs": context["new_events"],
            },
        ][:count]
    }


@pytest.mark.parametrize("persona", PERSONAS)
def test_native_personas_audit_anchor_and_heard_ledger(source, persona):
    requests = []

    def rpc(system, context, audit):
        requests.append(audit)
        if not audit:
            from arenaonair.personas import get

            assert context["style"] == get(persona).style
        assert len(system.encode()) + len(json.dumps(context).encode()) + 256 <= 6000
        return response(context, audit)

    booth = HeadlessBooth(
        CommentaryConfig(mode="commentary", generative=True, persona=persona), rpc, time.monotonic
    )
    lines, status = booth.generate(observation())
    assert status["state"] == "ready" and requests == [False, True]
    assert len(lines) == 2 and lines[1].anchor_uid == lines[0].uid
    assert not booth.booth.heard
    booth.delivered(lines[0].uid, "finished")
    assert len(booth.booth.heard) == 1
    booth.delivered(lines[1].uid, "superseded")
    assert len(booth.booth.heard) == 1
    assert not booth.queue.items


def test_audit_failure_and_invalid_claim_never_trigger_extra_model_calls(source):
    for invalid in (False, True):
        calls = []

        def rpc(system, context, audit, calls=calls, invalid=invalid):
            calls.append(audit)
            if invalid and not audit:
                return {
                    "turns": [
                        {"role": "play_by_play", "text": "Opt resolves.", "refs": context["new_events"]}
                    ]
                }
            return response(context, audit, supported=False)

        booth = HeadlessBooth(CommentaryConfig(mode="commentary", generative=True), rpc, time.monotonic)
        lines, status = booth.generate(observation())
        assert not lines
        assert calls == ([False] if invalid else [False, True])
        assert not booth.booth.heard


def test_real_worker_audited_protocol_and_cancellation(source):
    client = CommentaryClient()
    config = CommentaryConfig(mode="commentary", generative=True, python=sys.executable, aoa_source=source)
    calls = []
    try:

        def rpc(system, context, audit):
            calls.append(audit)
            return response(context, audit)

        result = client.generate(observation(), config, rpc, threading.Event())
        assert len(result["utterances"]) == 2 and calls == [False, True]
        first, second = result["utterances"]
        assert second["anchor_id"] == first["utterance_id"]
        client.delivered(first["utterance_id"], "finished")
        client.delivered(second["utterance_id"], "muted")
    finally:
        client.close()


def test_local_voice_render_uses_packaged_assets_without_loading_torch(source):
    import base64
    import io
    import wave

    import numpy as np

    from arenamcp.chatterbox_remote import SpeechCancelled
    from arenamcp.commentary.local_voice_worker import render, validate_model
    from arenamcp.local_chatterbox import LocalChatterboxClient

    class Engine:
        def prepare_audio(self, item):
            assert item.voice == "mira" and item.rate == 1.2
            return [(np.zeros(240, dtype=np.float32), 24000)]

    raw = base64.b64decode(render(Engine(), {"text": "Test", "voice": "mira", "speed": 1.2}))
    with wave.open(io.BytesIO(raw)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
    with pytest.raises(ValueError):
        validate_model("/tmp", "main")
    client = LocalChatterboxClient()
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(SpeechCancelled):
        client._wait(cancelled, 0.1)
    assert not client.failed


def test_worker_public_strategic_tail_survives_unrelated_private_draw(source):
    import copy

    from test_opening_delivery import finish

    from arenamcp.commentary.contracts import Utterance
    from arenamcp.commentary.timeline import valid_for
    from arenamcp.desktop.speech_arbiter import SpeechArbiter

    obs = observation()
    public = copy.deepcopy(obs.public_state)
    public["zones"]["battlefield"] = [
        dict(
            instance_id=20,
            grp_id=120,
            name="Emberheart Challenger",
            type_line="Creature",
            oracle_text="Haste\nProwess",
            power=2,
            toughness=2,
            controller_seat_id=1,
        )
    ]
    obs = replace(obs, public_state=public)
    client = CommentaryClient()
    config = CommentaryConfig(mode="commentary", generative=True, python=sys.executable, aoa_source=source)

    def rpc(system, context, audit):
        if audit:
            return {
                "checks": [
                    {"index": 0, "supported": True, "unsupported_claim": "", "scope": "historical"},
                    {"index": 1, "supported": True, "unsupported_claim": "", "scope": "position"},
                ]
            }
        assert "state:board" in context["facts"] and "card:120" in context["facts"]
        return {
            "turns": [
                {"role": "play_by_play", "text": "Opt goes onto the stack.", "refs": context["new_events"]},
                {
                    "role": "color_analyst",
                    "text": "Emberheart Challenger benefits from noncreature spells before combat; that timing can turn cards into attacking pressure.",
                    "refs": [context["new_events"][0], "state:board", "card:120"],
                },
            ]
        }

    try:
        result = client.generate(obs, config, rpc, threading.Event())
        lines = [Utterance.from_wire(u) for u in result["utterances"]]
        assert len(lines) == 2 and lines[1].position_scope == "public"
        current = [obs]
        played = []
        a = SpeechArbiter(
            start=played.append,
            stop=lambda: None,
            status=lambda *a: None,
            valid=lambda u: valid_for(u, current[0]),
        )
        a.reset(obs.epoch)
        for u in lines:
            a.submit(u)
        current[0] = replace(
            obs, snapshot_id="draw", private_state={"local_seat": 1, "hand": [{"name": "New private card"}]}
        )
        finish(a)
        finish(a)
        assert len(played) == 2 and played[-1].role == "color_analyst"
    finally:
        client.close()
