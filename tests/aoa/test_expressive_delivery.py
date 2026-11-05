import time
from dataclasses import replace

import pytest

from arenamcp.commentary.contracts import Epoch, Evidence, Observation, SpeechCategory, Utterance
from arenamcp.commentary.delivery import speech_delivery


def utterance(**kwargs):
    return Utterance(
        "u", "The match is over!", SpeechCategory.COMMENTARY, Epoch("m", 1), time.monotonic() + 20, **kwargs
    )


def test_native_delivery_survives_wire_and_preserves_calm():
    item = utterance(excitement="calm", salience=3)
    assert speech_delivery(Utterance.from_wire(item.to_wire()), None) == {"excitement": "calm", "energy": 0.3}


def test_big_moment_uses_expressive_model_and_respects_persona():
    event = Evidence("e", "game_end", 1, "s")
    observation = Observation(Epoch("m", 1), 1, "s", 1, {}, events=(event,))
    item = utterance(evidence_ids=("e",))
    assert speech_delivery(item, observation, "esports") == {"excitement": "electric", "energy": 0.8}
    assert speech_delivery(item, observation, "pro_tour") == {"excitement": "normal", "energy": 0.5}
    observation = replace(observation, events=(replace(event, kind="land_drop"),))
    assert speech_delivery(item, observation)["excitement"] == "calm"


@pytest.mark.parametrize("kwargs", [{"excitement": "invalid"}, {"salience": 4}, {"salience": True}])
def test_invalid_delivery_rejected(kwargs):
    with pytest.raises(ValueError):
        utterance(**kwargs)
