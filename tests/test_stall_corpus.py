"""Phase D: the stall corpus replays clean in CI (fable item 5).

Curated fixtures under tests/fixtures/stalls/ encode the 2026-06-09 live
failures as data. Replaying them asserts the typed pipeline's mechanics:
answers validate against the option set, the deterministic fallback never
picks an unpayable cast, and submission dispatch always builds a real
bridge call.
"""

import json
from pathlib import Path

import pytest

from arenamcp.decisions import decision_from_dict, decision_to_dict
from arenamcp.stall_corpus import load_fixture, record_stall
from tools.eval.replay_stalls import replay_fixture

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "stalls"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


@pytest.mark.parametrize("path", FIXTURES, ids=[p.stem for p in FIXTURES])
def test_curated_fixture_replays_clean(path):
    result = replay_fixture(json.loads(path.read_text(encoding="utf-8")))
    assert result["ok"], f"{path.name}: {result['errors']}"
    assert result["dispatch"], "replay must produce a bridge call"


def test_momentum_breaker_fixture_never_picks_unpayable():
    data = json.loads(
        (FIXTURE_DIR / "momentum_breaker_unpayable.json").read_text(encoding="utf-8")
    )
    result = replay_fixture(data)
    # The deterministic pick must be the payable cast — never idx:0.
    assert result["picked"] == ["idx:1"]


def test_record_stall_roundtrip(tmp_path):
    data = json.loads(
        (FIXTURE_DIR / "nurturing_presence_select_targets.json").read_text(
            encoding="utf-8"
        )
    )
    decision = decision_from_dict(data["pending_decision"])
    path = record_stall(
        decision, ["tgt:233"], "exhausted", {"turn": 5}, corpus_dir=tmp_path
    )
    assert path is not None and path.exists()
    fixture = load_fixture(path)
    assert fixture["outcome"] == "exhausted"
    rebuilt = decision_from_dict(fixture["pending_decision"])
    assert decision_to_dict(rebuilt) == decision_to_dict(decision)
    # And the recorded fixture itself replays clean.
    assert replay_fixture(fixture)["ok"]
