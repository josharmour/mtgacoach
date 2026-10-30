"""Two roles, real catalog choices, factual style variants, and sequential delivery."""

import time
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_controller import Tts

from arenamcp.commentary.config import PERSONAS, CommentaryConfig
from arenamcp.commentary.contracts import Evidence
from arenamcp.commentary.events import ObservationAdapter
from arenamcp.commentary.headless import deterministic
from arenamcp.commentary.voices import VoicePairUnavailable, pair_options, voice_pair
from arenamcp.desktop.commentary_controller import CommentaryController

CATALOG = {"voices": ["atlas", "mira", "default"], "default": "atlas"}


def observed(kind="action_observed"):
    state = {"match_id": "duo", "game_number": 1, "turn_info": {"turn_number": 4}, "zones": {}, "players": []}
    obs = ObservationAdapter().project(state)
    event = Evidence(
        "public-cast",
        kind,
        obs.source_sequence,
        obs.snapshot_id,
        seat=1,
        received_at=time.monotonic(),
        payload={"name": "Bear", "to_zone": "battlefield"},
    )
    return state, replace(obs, events=(event,))


def test_automatic_pair_follows_server_default_without_old_troubleshooting_values():
    settings = {
        "tts_provider": "chatterbox_remote",
        "chatterbox_voice_override": "atlas",
        "chatterbox_voice": "mira",
        "commentary_chatterbox_analyst": "atlas",
    }
    assert voice_pair(settings, CATALOG) == ("server_default", "mira")
    assert voice_pair(settings, dict(CATALOG, default="mira")) == ("server_default", "atlas")
    assert voice_pair(dict(settings, commentary_chatterbox_pair="mira|atlas"), CATALOG) == ("mira", "atlas")
    assert voice_pair({"tts_provider": "chatterbox_local"}) == ("atlas", "mira")
    assert voice_pair(
        {"tts_provider": "kokoro", "voice": "af_heart", "commentary_kokoro_analyst": "af_heart"}
    ) == ("af_heart", "am_adam")


@pytest.mark.parametrize(
    "catalog",
    [None, {"voices": ["atlas"], "default": "atlas"}, {"voices": ["atlas", "mira"], "default": None}],
)
def test_unknown_or_single_server_voice_cannot_claim_a_distinct_pair(catalog):
    with pytest.raises(VoicePairUnavailable):
        voice_pair({"tts_provider": "chatterbox_remote"}, catalog)


def test_pair_selection_must_be_supported_by_current_catalog():
    with pytest.raises(VoicePairUnavailable):
        voice_pair(
            {"tts_provider": "chatterbox_remote", "commentary_chatterbox_pair": "invented|mira"}, CATALOG
        )
    assert all(
        "invented" not in label
        for _, label, _ in pair_options({"tts_provider": "chatterbox_remote"}, CATALOG)
    )


@pytest.mark.parametrize("kind", ["action_observed", "spell_resolved", "land_drop"])
def test_grounded_exchange_has_two_distinct_facts_and_style_specific_wording(kind):
    state, observation = observed(kind)
    all_texts = set()
    for style in PERSONAS:
        lines = deterministic(observation, CommentaryConfig(mode="commentary", persona=style))
        assert len(lines) == 2
        first, second = lines
        assert first.role == "play_by_play" and second.role == "color_analyst"
        assert second.anchor_id == first.utterance_id
        assert first.evidence_ids == second.evidence_ids == ("public-cast",)
        assert first.event_bound and second.event_bound
        assert "Bear" in first.text and first.text != second.text
        assert "Player 1" in second.text or "battlefield" in second.text
        all_texts.add(tuple(line.text for line in lines))
    assert len(all_texts) == 4
    assert len(deterministic(observation, CommentaryConfig(mode="commentary", speakers=1))) == 1
    unknown = replace(observation, events=(replace(observation.events[0], seat=None),))
    assert len(deterministic(unknown, CommentaryConfig(mode="commentary"))) == 1


@pytest.mark.parametrize("source", ["grounded", "generated"])
@pytest.mark.parametrize("end", ["finished", "failed", "mode", "mute", "game", "expired"])
def test_both_proposal_paths_use_distinct_voices_and_depend_on_actual_delivery(
    qapp, monkeypatch, source, end
):
    settings = {
        "tts_provider": "chatterbox_remote",
        "autoplay_speech": "commentary",
        "speech_speed": 1.5,
        "_commentary_voice_catalog": CATALOG,
    }
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: settings)
    tts, sent = Tts(), []
    c = CommentaryController(tts, sent.append, lambda _: None)
    state, obs = observed()
    c.observe(state)
    first, second = deterministic(obs, c.config)
    if source == "generated":
        first, second = replace(first, event_bound=False), replace(second, event_bound=False)
    if end == "expired":
        second = replace(second, expires_at=time.monotonic() - 1)
    c.proposal(first.to_wire())
    c.proposal(second.to_wire())
    assert tts.request_speech.call_count == 1
    assert tts.request_speech.call_args.kwargs["voice_id"] == "server_default"
    assert tts.request_speech.call_args.kwargs["speed"] == 1.5
    if end == "mode":
        c.configure(CommentaryConfig(mode="actions").to_wire())
    elif end == "mute":
        c.arbiter.set_muted(True)
    elif end == "game":
        c.observe(dict(state, game_number=2))
    else:
        tts.speechStatus.emit(first.utterance_id, "started")
        tts.speechStatus.emit(first.utterance_id, "failed" if end == "failed" else "finished")
    assert tts.request_speech.call_count == (2 if end == "finished" else 1)
    if end == "finished":
        assert tts.request_speech.call_args.kwargs["voice_id"] == "mira"
        assert tts.request_speech.call_args.kwargs["speed"] == 1.5
    c.close()


def test_pair_preview_is_ordered_preserves_mute_and_ignores_advice_voice(qapp, monkeypatch):
    settings = {
        "tts_provider": "chatterbox_remote",
        "muted": True,
        "autoplay_speech": "commentary",
        "_commentary_voice_catalog": CATALOG,
    }
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: settings)
    tts = Tts()
    c = CommentaryController(tts, Mock(), Mock())
    c.preview(dict(settings, commentary_chatterbox_pair="mira|atlas", commentary_persona="pro_tour"))
    assert tts.request_speech.call_count == 1
    first = tts.request_speech.call_args.kwargs
    assert first["voice_id"] == "mira" and "precise" in first["text"]
    tts.speechStatus.emit(first["speech_id"], "started")
    tts.speechStatus.emit(first["speech_id"], "finished")
    second = tts.request_speech.call_args.kwargs
    assert tts.request_speech.call_count == 2 and second["voice_id"] == "atlas"
    tts.speechStatus.emit(second["speech_id"], "started")
    tts.speechStatus.emit(second["speech_id"], "finished")
    assert not c.preview_settings and c.arbiter.muted and settings["muted"]
    c.close()


def test_missing_catalog_recovers_without_silently_using_one_voice(qapp, monkeypatch):
    settings = {"tts_provider": "chatterbox_remote", "autoplay_speech": "commentary"}
    monkeypatch.setattr("arenamcp.desktop.commentary_controller.get_settings", lambda: settings)
    tts, sent = Tts(), []
    c = CommentaryController(tts, sent.append, Mock())
    state, obs = observed()
    c.observe(state)
    first, second = deterministic(obs, c.config)
    c.proposal(first.to_wire())
    assert not tts.request_speech.called
    assert sent[-1]["state"] == "failed"
    settings["_commentary_voice_catalog"] = CATALOG
    # A new exchange can recover after discovery; the failed one is not replayed.
    first = replace(first, utterance_id="recovered")
    second = replace(second, utterance_id="recovered:analyst", anchor_id=first.utterance_id)
    c.proposal(first.to_wire())
    c.proposal(second.to_wire())
    tts.speechStatus.emit(first.utterance_id, "started")
    tts.speechStatus.emit(first.utterance_id, "finished")
    assert [call.kwargs["voice_id"] for call in tts.request_speech.call_args_list] == [
        "server_default",
        "mira",
    ]
    c.close()
