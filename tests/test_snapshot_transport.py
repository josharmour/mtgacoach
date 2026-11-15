"""Regression for the 118-decision November bundle that exceeded the B1 size budget."""

import copy
import gzip
import json
import random

import pytest
from tests.test_decision_benchmark import sample
from tools import decision_benchmark as benchmark

from arenamcp import match_bundle as bundles
from arenamcp.decision_snapshot import game_state_from_snapshot
from arenamcp.snapshot_transport import pack_snapshot, unpack_snapshot


def test_pack_is_lossless_standalone_and_preserves_field_order_and_types():
    text = "A sufficiently long repeated field"
    card = {"oracle_text": text, "counters": {"plus": 1}, "tapped": False, "types": ["Creature"] * 20}
    native = {
        "version": 1,
        "state": {
            "zone": [card, copy.deepcopy(card), dict(reversed(list(card.items())))],
            "mixed": [None, True, 1, 1.0, False, 0, "0", [], {}, {"=": 3, "0": "original field"}],
            "text": text,
            "other": text,
        },
    }
    packed = json.loads(json.dumps(pack_snapshot(native)))
    assert packed["table"]
    restored = unpack_snapshot(packed)
    assert json.dumps(restored) == json.dumps(native)
    assert game_state_from_snapshot(packed) == game_state_from_snapshot(native)
    restored["state"]["zone"][0]["types"].append("Different")
    assert restored["state"]["zone"][1]["types"] == ["Creature"] * 20


@pytest.mark.parametrize(
    "invalid",
    [
        {"extra": "unused data"},
        {"table": ["orphan"]},
        {"shapes": [[], ["unreferenced"]]},
        {"shapes": [["x", "x"]]},
        {"state": {"0": [5]}},
        {"state": {"-1": []}},
        {"state": {"00": []}},
        {"state": {"=": True}},
        {"state": {"=": 0}, "table": [{"=": 0}]},
        {"version": 2.0},
    ],
)
def test_invalid_transport_fails_closed(invalid):
    candidate = {"version": 2, "shapes": [[]], "state": {"0": []}, "table": [], **invalid}
    with pytest.raises(ValueError):
        unpack_snapshot(candidate)


def test_expansion_honours_the_remaining_bundle_budget():
    packed = pack_snapshot({"version": 1, "state": {"many": ["repeated common printed text"] * 50}})
    with pytest.raises(ValueError, match="budget"):
        unpack_snapshot(packed, remaining=[100])


def test_actual_bundle_writer_preserves_small_legacy_and_packs_large_redacted_boards():
    rng = random.Random(3)
    printed = [rng.randbytes(140).hex() for _ in range(80)]
    cards = [
        {"instance_id": n, "oracle_text": line, "other_text": line, "display_name": "Example Private Name"}
        for n, line in enumerate(printed)
    ]
    rows = [{"state_snapshot": {"version": 1, "state": {"turn": t, "battlefield": cards}}} for t in range(25)]
    raw = {"packet": {"decisions": rows}}
    redacted = bundles.redact(raw, {"Example Private Name": "opponent"})
    original = copy.deepcopy(redacted)
    outgoing = bundles.encode_bundle(redacted)
    assert redacted == original
    decoded = json.loads(gzip.decompress(outgoing))
    assert "Example Private Name" not in json.dumps(decoded)
    assert all(r["state_snapshot"]["version"] == 2 for r in decoded["packet"]["decisions"])
    for new, old in zip(decoded["packet"]["decisions"], redacted["packet"]["decisions"], strict=True):
        assert json.dumps(unpack_snapshot(new["state_snapshot"])) == json.dumps(old["state_snapshot"])
    small = {"packet": {"decisions": [{"state_snapshot": {"version": 1, "state": {"turn": 1}}}]}}
    assert json.loads(gzip.decompress(bundles.encode_bundle(small))) == small


def test_benchmark_expands_then_applies_its_existing_whole_bundle_privacy_checks(tmp_path):
    original = sample()
    row = original["packet"]["decisions"][0]
    row["pending_decision"]["options"][0]["label"] = "Keep against Example Opponent"
    other = copy.deepcopy(row)
    other["state_snapshot"]["state"]["playerName"] = "Example Opponent"
    original["packet"]["decisions"].append(other)
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(original))
    before, _, counts = benchmark.build_cases([path])
    assert before and "Example Opponent" not in json.dumps(before)
    for d in original["packet"]["decisions"]:
        d["state_snapshot"] = pack_snapshot(d["state_snapshot"])
    path.write_text(json.dumps(original))
    after, _, other_counts = benchmark.build_cases([path])
    assert before == after and other_counts == counts
    other["state_snapshot"]["table"].append("unused source")
    path.write_text(json.dumps(original))
    cases, _, counts = benchmark.build_cases([path])
    assert not cases and counts["invalid_snapshot_encoding"] == 1 and counts["duplicates"] == 0


def test_redaction_recognizes_fields_in_previously_packed_board():
    native = {"version": 1, "state": {"players": [{"userId": "example-account-value"}]}}
    packet = {"packet": {"decisions": [{"state_snapshot": pack_snapshot(native)}]}}
    result = bundles.redact(packet)
    assert "example-account-value" not in json.dumps(result)
    assert result["packet"]["decisions"][0]["state_snapshot"]["version"] == 1
    assert packet["packet"]["decisions"][0]["state_snapshot"]["version"] == 2
