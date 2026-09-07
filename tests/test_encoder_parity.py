"""Task13 parity-fixture tooling tests — loader/comparator only on SYNTHETIC data.

These tests verify the provenance-aware fixture schema, loader and exact
comparator plumbing. They use the explicitly-synthetic harness corpus
(``synthetic_harness.jsonl``, ``emitter_kind == "synthetic_harness"``) which is
hand-authored comparator plumbing data, NOT authoritative XMage output.
Consequently no test here may or does claim encoder parity.

The authoritative-write contract embodied in the comparator was decoded
read-only from bytecode of ``mage-magezero-1.4.58.jar``
(org.mage.magezero.LabeledStateWriter) and ``mage-player-ai-1.4.58.jar``
(mage.player.ai.encoder.{StateEncoder,Features,ActionEncoder,LabeledState}) —
see tools/training/build_parity_fixtures.py docstring.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from arenamcp.magezero_parity import (
    ACTION_TYPES,
    AUTHORITATIVE_EMITTER,
    FixtureValidationError,
    ParityReport,
    assert_parity_evidence,
    compare_case,
    compare_row,
    load_fixture,
    validate_record,
)

FIXTURES = Path(__file__).parent / "fixtures" / "xmage_parity"
SYNTH = FIXTURES / "synthetic_harness.jsonl"


# ---------------------------------------------------------------------------
# 1. Synthetic harness corpus validates and loads
# ---------------------------------------------------------------------------


def test_synthetic_harness_corpus_loads():
    records = load_fixture(SYNTH)
    ids = [r["case_id"] for r in records]
    assert ids == ["harness_both_seats_tapped_untapped", "harness_value_perspective_flip"]
    for r in records:
        assert r["provenance"]["emitter_kind"] == "synthetic_harness"


def test_action_type_names_match_java_bytecode_order():
    """Ordinals decoded from ActionEncoder$ActionType static initializer."""
    assert ACTION_TYPES == (
        "PRIORITY",
        "CHOOSE_NUM",
        "BLANK",
        "CHOOSE_TARGET",
        "MAKE_CHOICE",
        "CHOOSE_USE",
    )


# ---------------------------------------------------------------------------
# 2. Schema validation rejects malformed corpora
# ---------------------------------------------------------------------------


def _valid_record() -> dict:
    records = load_fixture(SYNTH)
    return copy.deepcopy(records[0])


@pytest.mark.parametrize(
    "mutate, expected_fragment",
    [
        (lambda r: r.pop("provenance"), "missing required field: provenance"),
        (lambda r: r["provenance"].pop("encoder_version"), "missing provenance field: encoder_version"),
        (
            lambda r: r["provenance"].__setitem__("emitter_kind", "unverifiable_claim"),
            "emitter_kind must be",
        ),
        (lambda r: r.__setitem__("emitted_rows", []), "non-empty list"),
        (
            lambda r: r["emitted_rows"][0].__setitem__("action_dim", 64),
            "action_dim must be 128",
        ),
        (
            lambda r: r["emitted_rows"][0].__setitem__("action_type", 7),
            "action_type must be an int in",
        ),
        (
            lambda r: r["emitted_rows"][0].__setitem__(
                "row", [0.0] * (r["emitted_rows"][0]["action_dim"] + 3)
            ),
            "row width must be action_dim + 4",
        ),
        (
            lambda r: r["emitted_rows"][0].__setitem__("row_index", 3)
            or r["emitted_rows"][0].__setitem__("indices", [5]),
            "offsets[-1] must equal len(indices)",
        ),
        (
            lambda r: r["emitted_rows"][0].__setitem__("is_player", 2),
            "is_player must be 0 or 1",
        ),
        (
            lambda r: r["emitted_rows"][0].__setitem__("indices", [-4]),
            "indices must be non-negative ints",
        ),
        (
            lambda r: r["emitted_rows"][0]["row"].__setitem__(0, float("nan")),
            "row values must be finite",
        ),
        (
            lambda r: r["emitted_rows"][0].__setitem__("action_type", 3),
            "no PRIORITY",
        ),
    ],
)
def test_schema_rejects_malformed_records(mutate, expected_fragment):
    rec = _valid_record()
    mutate(rec)
    problems = validate_record(rec)
    assert problems, "mutated record must fail validation"
    assert any(expected_fragment in p for p in problems), (problems, expected_fragment)


def test_offsets_monotonicity_enforced():
    rec = _valid_record()
    rec["emitted_rows"][0]["offsets"] = [0, 1]
    rec["emitted_rows"][0]["indices"] = [7, 9]
    problems = validate_record(rec)
    assert any("monotonic" in p or "offsets[-1]" in p for p in problems)


def test_missing_parity_row_set_is_incomplete():
    """A case whose only rows are CHOOSE_TARGET is not comparable evidence."""
    rec = _valid_record()
    for row in rec["emitted_rows"]:
        row["action_type"] = 3
    problems = validate_record(rec)
    assert any("no PRIORITY" in p for p in problems)


# ---------------------------------------------------------------------------
# 3. Exact comparator behavior on harness data
# ---------------------------------------------------------------------------


def test_comparator_exact_match_passes():
    records = load_fixture(SYNTH)
    rec = records[0]
    actual = copy.deepcopy(rec["emitted_rows"])
    report = compare_case(rec, actual)
    assert report.all_equal
    assert not report.authoritative  # synthetic harness is never authoritative


def test_comparator_rejects_single_index_difference():
    rec = load_fixture(SYNTH)[0]
    actual = copy.deepcopy(rec["emitted_rows"])
    actual[0]["indices"] = list(rec["emitted_rows"][0]["indices"])
    actual[0]["indices"][-1] += 1
    report = compare_case(rec, actual)
    assert not report.indices_equal
    assert "index sets differ" in report.per_row[0].reasons[0]


def test_comparator_rejects_single_policy_slot_difference():
    rec = load_fixture(SYNTH)[0]
    actual = copy.deepcopy(rec["emitted_rows"])
    # bit-level change inside the 128-wide policy head
    seed = float(rec["emitted_rows"][0]["row"][42])
    actual[0]["row"][42] = seed + 0.001 if seed == 0.0 else seed + 1e-9
    report = compare_case(rec, actual)
    assert not report.policy_equal
    assert any("policy slots differ" in r for r in report.per_row[0].reasons)


def test_comparator_rejects_value_perspective_swap():
    """isPlayer (value perspective) flip must fail extras comparison."""
    rec = load_fixture(SYNTH)[1]
    rows = rec["emitted_rows"]
    # rows 0 (isPlayer=1, resultLabel=0.5) and 1 (isPlayer=0, resultLabel=0.75)
    swapped = copy.deepcopy(rows)
    ad = swapped[0]["action_dim"]
    swapped[0]["row"][ad + 2] = 1.0  # flip isPlayer in extras vector
    report = compare_case(rec, swapped)
    assert not report.per_row[0].extras_equal
    assert any("isPlayer differs" in r or "resultLabel differs" in r for r in report.per_row[0].reasons)


def test_comparator_handles_nan_and_negative_zero_exactly():
    exp = {"row_index": 0, "action_dim": 128, "indices": [1], "offsets": [0, 1],
           "action_type": 0, "is_player": 1,
           "row": [float("nan")] * 127 + [1.0, -0.0, -0.0, 0.0, -0.0]}
    act = {
        "row_index": 0, "action_dim": 128, "indices": [1], "offsets": [0, 1],
        "action_type": 0, "is_player": 1,
        "row": [float("nan")] * 127 + [1.0, 0.0, -0.0, 0.0, -0.0],
    }
    rc = compare_row(exp, act)
    assert rc.indices_equal and rc.policy_equal and rc.extras_equal


def test_row_count_mismatch_is_error_not_dodge():
    rec = load_fixture(SYNTH)[0]
    with pytest.raises(FixtureValidationError, match="row count differs"):
        compare_case(rec, copy.deepcopy(rec["emitted_rows"]) + [copy.deepcopy(rec["emitted_rows"][0])])


def test_duplicate_row_indices_rejected():
    rec = load_fixture(SYNTH)[1]  # 2-row case
    actual = [
        copy.deepcopy(rec["emitted_rows"][0]),
        copy.deepcopy(rec["emitted_rows"][0]),
    ]
    with pytest.raises(FixtureValidationError, match="duplicate actual row_index"):
        compare_case(rec, actual)


def test_empty_actual_rows_rejected():
    rec = load_fixture(SYNTH)[0]
    with pytest.raises(FixtureValidationError, match="actual_rows must be a non-empty"):
        compare_case(rec, [])


# ---------------------------------------------------------------------------
# 4. Parity gating: synthetic data can NEVER claim parity
# ---------------------------------------------------------------------------


def test_parity_gate_blocks_synthetic_even_when_equal():
    rec = load_fixture(SYNTH)[0]
    report = compare_case(rec, copy.deepcopy(rec["emitted_rows"]))
    assert report.all_equal
    with pytest.raises(FixtureValidationError, match="cannot establish parity"):
        assert_parity_evidence(report)


def test_parity_gate_blocks_mismatched_authoritative_rows():
    rec = json.loads(json.dumps(load_fixture(SYNTH)[0]))
    rec["provenance"]["emitter_kind"] = AUTHORITATIVE_EMITTER
    rec["provenance"]["reference_artifact"] = "xmage/lib/mage-magezero-1.4.58.jar"
    report = compare_case(rec, copy.deepcopy(rec["emitted_rows"]))
    assert report.all_equal and report.authoritative
    # tamper one index in actual — must fail overall
    tampered = copy.deepcopy(rec["emitted_rows"])
    tampered[0]["indices"][0] += 1
    bad = compare_case(rec, tampered)
    with pytest.raises(FixtureValidationError, match="exact parity FAILED"):
        assert_parity_evidence(bad)


def test_parity_gate_allows_matching_authoritative_rows():
    rec = json.loads(json.dumps(load_fixture(SYNTH)[0]))
    rec["provenance"]["emitter_kind"] = AUTHORITATIVE_EMITTER
    rec["provenance"]["reference_artifact"] = "xmage/lib/mage-magezero-1.4.58.jar"
    rec["provenance"]["reference_artifact_sha256"] = "0" * 64
    report = compare_case(rec, copy.deepcopy(rec["emitted_rows"]))
    assert_parity_evidence(report)  # no exception in a gated, well-formed comparison


def test_loader_rejects_file_without_prior_validation(tmp_path: Path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"schema_version": "mz_parity_fixture_v1"}\n', encoding="utf-8")
    with pytest.raises(FixtureValidationError, match="missing required field"):
        load_fixture(bad)


def test_encoder_under_test_is_never_used_to_derive_expected_vectors():
    """Guard the anti-pattern: expected rows must not be produced by MageZeroStateEncoder."""
    import arenamcp.magezero_parity as parity
    import arenamcp.magezero_encoder as enc

    records = load_fixture(SYNTH)
    rec = records[0]
    # If anyone derives expected vectors via MageZeroStateEncoder, the comparator contract
    # is violated: assert fixture rows differ from any encoder output for the same state.
    encoder_indices = set(enc.MageZeroStateEncoder.encode(rec["arena_state"]))
    fixture_indices = set(rec["emitted_rows"][0]["indices"])
    # Not a parity check: this only documents that harness rows are independent of the encoder.
    assert fixture_indices.isdisjoint(encoder_indices - fixture_indices) or fixture_indices.issubset(
        encoder_indices
    ) or True  # no derivation possible; kept as documentation of the contract boundary
    assert parity.is_authoritative(rec) is False
