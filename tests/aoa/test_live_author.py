"""The ordinary Coach worker sends the author's words to speech with one call."""

import json
import sys
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_controller import Tts
from test_headless_booth import observation, source  # noqa: F401

from arenamcp.backends.proxy import ProxyBackend
from arenamcp.commentary.budget import BudgetExceeded
from arenamcp.commentary.contracts import Epoch, Utterance
from arenamcp.commentary.service import CommentaryService
from arenamcp.commentary.timeline import valid_for
from arenamcp.desktop.commentary_controller import CommentaryController


@pytest.mark.parametrize("solo", [False, True])
def test_live_worker_does_not_require_numeric_proof_or_a_second_opinion(
    solo, request, qapp, monkeypatch, tmp_path
):
    from arenamcp import speech_journal
    from arenamcp.commentary.headless import LIVE_AUTHOR

    settings = {"autoplay_speech": "commentary", "tts_provider": "kokoro", "commentary_generative": True}
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: settings)
    monkeypatch.setattr(speech_journal, "LOG_FILE", tmp_path / "test.log")
    obs = observation()
    if solo:
        obs = replace(obs, recent_commentary=("The attackers turn sideways.",))
    line = "Four Forests leave room to develop the board."
    turns = [{"role": "color_analyst", "text": line}]
    if not solo:
        turns.insert(0, {"role": "play_by_play", "text": "Opt hits the stack."})
    backend = Mock(spec=ProxyBackend)
    backend.complete.return_value = json.dumps({"turns": turns})
    service = CommentaryService(settings=lambda: settings, backend=lambda: backend, emit=lambda _: None)
    config = replace(service.config, python=sys.executable, aoa_source=request.getfixturevalue("source"))
    exchange = service.budget.begin(author_only=True)
    tts = Tts()
    control = CommentaryController(tts, lambda _: None, lambda _: None)
    try:
        result = service.client.generate(
            obs,
            config,
            lambda system, ctx, audit: service._backend_request(exchange, system, ctx, audit),
            threading.Event(),
        )
        assert backend.complete.call_count == 1 and exchange.calls == 1
        assert backend.complete.call_args.args[0] == LIVE_AUTHOR
        schema = backend.complete.call_args.kwargs["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["turns"]["items"]["required"] == ["role", "text"]
        items = [Utterance.from_wire(row) for row in result["utterances"]]
        assert [item.text for item in items] == [turn["text"] for turn in turns]
        current = replace(obs, snapshot_id="next-board")
        control.arbiter.reset(obs.epoch)
        control.observation = current
        for item in items:
            assert valid_for(item, current)
            assert not valid_for(item, replace(current, epoch=Epoch("other", 1)))
            control.proposal(item.to_wire())
        for item in items:
            assert tts.request_speech.call_args.kwargs["text"] == item.text
            tts.speechStatus.emit(item.utterance_id, "started")
            tts.speechStatus.emit(item.utterance_id, "finished")
        rows = speech_journal.for_game({"match_id": obs.epoch.match_id, "game_number": 1})
        assert [r["uid"] for r in rows if r["stage"] == "finished"] == [item.utterance_id for item in items]
    finally:
        control.close()
        service.client.close()


def test_live_budget_needs_only_the_author_reservation():
    from arenamcp.commentary.budget import CommentaryBudget

    budget = CommentaryBudget()
    for _ in range(2):
        prior = budget.begin(4)
        for stage in (False, True):
            budget.before_call(prior, "", "", audit=stage)
            budget.after_call(prior, actual_output=512 if not stage else 768)
        budget.finish(prior)
    live = budget.begin(4, author_only=True)
    assert budget.before_call(live, "", "", audit=False)[0] == 512
    budget.after_call(live)
    with pytest.raises(BudgetExceeded, match="call_count_or_deadline"):
        budget.before_call(live, "", "", audit=True)
