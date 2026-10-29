import copy

import pytest

from arenamcp.commentary.qualification import evaluate


def measurements():
    return {
        "replay": "fixture",
        "strategy": True,
        "load": "controlled",
        "platform": "mock",
        "decisions": [{"id": str(i), "seconds": 1, "timeout": False, "fallback": False} for i in range(20)],
        "essential_start_seconds": [0.2],
        "preemption_seconds": [0.1],
        "stale_game_utterances": 0,
        "overlapping_utterances": 0,
        "upstream_cancellation_verified": True,
        "gateway_admission_verified": True,
    }


def test_acceptance_requires_every_gate_and_matched_replays():
    base = measurements()
    enabled = copy.deepcopy(base)
    assert evaluate(base, enabled)["passed"]
    enabled["decisions"][0]["seconds"] = 2
    enabled["decisions"][1]["seconds"] = 2
    assert not evaluate(base, enabled)["passed"]
    enabled = measurements()
    enabled["gateway_admission_verified"] = False
    assert not evaluate(base, enabled)["passed"]
    enabled["replay"] = "different"
    with pytest.raises(ValueError):
        evaluate(base, enabled)


def test_missing_measurements_cannot_pass():
    base = measurements()
    enabled = measurements()
    enabled["preemption_seconds"] = []
    with pytest.raises(ValueError):
        evaluate(base, enabled)
    enabled = measurements()
    del enabled["stale_game_utterances"]
    with pytest.raises(KeyError):
        evaluate(base, enabled)
