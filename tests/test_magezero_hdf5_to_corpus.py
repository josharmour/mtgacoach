"""Unit tests for magezero_hdf5_to_corpus.py (WP-3 step #3 converter).

The converter turns MageZero generation-5+ full-state self-play HDF5 game
shards into dsv4-shaped distillation instances.  These tests exercise the pure
functions (feature-table parsing, state decoding, instance construction) with
synthetic numpy arrays and a tiny FeatureTable, so they run in CI without h5py.

Run with:
    python3 -m pytest tests/test_magezero_hdf5_to_corpus.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "tools" / "training"))

import numpy as np
import pytest

from magezero_hdf5_to_corpus import (
    action_tokens,
    decision_to_instance,
    decode_state,
    load_feature_table,
    mastery_tokens,
    parse_game_name,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FT_CONTENT = """\
0: [501884/counter target noncreature spell unless its controller pays {2}.#1]
3: [1240652/stack ability (ward {1})#1]
41: [680382/isController#1], [1712873/TAPPED#1]
107: [1244356/ward {2}#2], [1210509/counter target noncreature spell unless its controller pays {2}#1], [1587553/{U}#2], [1915165/PlayerA#1]
137: [1628807/{this} gets +1/+1 until end of turn#1], [786238/put an oil counter on {this}#1]
300: [4123/Power@17#1], [7777/Damage@2#1], [8888/LifeTotal@19#1]
900: [99/Cast Negate#1], [100/Play Island#1]
"""


@pytest.fixture(scope="module")
def table() -> dict[int, list[str]]:
    with pytest.MonkeyPatch.context() as mp:
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write(FT_CONTENT)
            path = f.name
        table = load_feature_table(path)
    return table


# ---------------------------------------------------------------------------
# FeatureTable parsing
# ---------------------------------------------------------------------------


def test_load_feature_table_basic(table):
    # Standard `[id/text#count]` entry keeps only the readable text.
    assert "counter target noncreature spell unless its controller pays {2}." in table[0]
    # Multi-feature line: every feature decoded independently.
    assert "ward {2}" in table[107]
    assert "{U}" in table[107]
    assert "PlayerA" in table[107]


def test_load_feature_table_no_slash_entry(table):
    # `[text#count]` form (no leading hashed id) must also decode.
    assert "put an oil counter on {this}" in table[137]


def test_load_feature_table_mastery_vocab(table):
    # Counters / P-T / damage / life are present (the replay gap).
    assert "Power@17" in table[300]
    assert "Damage@2" in table[300]
    assert "LifeTotal@19" in table[300]


# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------


def test_parse_game_name():
    assert parse_game_name("session148_UWTempo_vs_GBLegends.hdf5") == (
        148, "UWTempo", "GBLegends",
    )
    sid, deck, opp = parse_game_name("unknown.txt")
    assert sid is None and opp == ""


# ---------------------------------------------------------------------------
# State decoding + token classification
# ---------------------------------------------------------------------------


def test_decode_state_and_tokens(table):
    c = decode_state([300, 300, 900, 0, 137], table)
    # life / counters / p-t / damage all recovered
    assert c["Power@17"] == 2
    assert c["Damage@2"] == 2
    assert c["LifeTotal@19"] == 2
    # action tokens from Cast/Play; non-action tokens excluded
    acts = action_tokens(c)
    assert "Cast Negate" in acts
    assert "Play Island" in acts
    assert "Power@17" not in acts  # mastery signals are not actions
    master = mastery_tokens(c)
    assert any("Power@17" in t for t in master)
    assert any("oil counter" in t for t in master)


# ---------------------------------------------------------------------------
# decision_to_instance -> dsv4-shaped record
# ---------------------------------------------------------------------------


def _synthetic_game():
    # one decision point: 3 state features at indices [300, 900, 107]
    indices = np.array([300, 900, 107], dtype=np.int32)
    offsets = np.array([0, 3], dtype=np.int64)  # N=1, nnz=3
    A = 6
    # row[k, :A] = policy (all zero -> argmax=0); tail = [result, score, isP, aType]
    row = np.zeros((1, A + 4), dtype=np.float32)
    row[0, A + 0] = 0.32  # result_label
    row[0, A + 1] = 0.30  # state_score
    row[0, A + 2] = 1.0   # isPlayer = A (0.5 threshold)
    row[0, A + 3] = 0     # actionType
    return {"offsets": offsets, "indices": indices, "row": row, "N": 1, "A": A}


def test_decision_to_instance_shape(table):
    g = _synthetic_game()
    rec = decision_to_instance(g, 0, table, "SYSTEM-PROMPT", 5, "UWTempo", "GBLegends", 148)
    assert set(rec) == {"system", "user", "response", "meta"}
    assert rec["system"] == "SYSTEM-PROMPT"
    # full-state mastery is surfaced in the user block
    assert "Power@17" in rec["user"]
    assert "MASTERY" in rec["user"]
    # response must be valid JSON with a pick
    plan = json.loads(rec["response"])
    assert plan["actions"][0]["pick"] == 1
    # meta provenance
    m = rec["meta"]
    assert m["source"] == "magezero_hdf5"
    assert m["game_id"] == "session148_UWTempo_vs_GBLegends"
    assert m["gen"] == 5 and m["deck"] == "UWTempo" and m["opponent"] == "GBLegends"
    assert m["is_player"] == "A"
    assert m["gen5_full_state"] is True
    assert m["decision_index"] == 0
    assert m["num_state_features"] == 3
    assert m["sha"]


def test_decision_to_instance_chosen_action(table):
    g = _synthetic_game()
    rec = decision_to_instance(g, 0, table, "SYS", 5, "UWTempo", "GBLegends", 148)
    # Action tokens are "Cast Negate" then "Play Island" (by occurrence order)
    assert rec["meta"]["chosen_action"] == "Cast Negate"
    assert rec["meta"]["action_type"] == 0


def test_decision_to_instance_empty_slot_skipped(table):
    # decision slot with offsets equal -> returns None (no state)
    g = {
        "offsets": np.array([0, 0], dtype=np.int64),
        "indices": np.array([], dtype=np.int32),
        "row": np.zeros((1, 10), dtype=np.float32),
        "N": 1,
        "A": 6,
    }
    assert decision_to_instance(g, 0, table, "SYS", 5, "UWTempo", "GBLegends", 148) is None
